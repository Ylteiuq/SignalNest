"""Finite observations and preflight isolation; fault injection is not network validation."""

import hashlib
import smtplib
import ssl
from dataclasses import FrozenInstanceError, replace

import pytest
from test_mail_rendering import render
from test_smtp_wire import Dialogue, frozen, peer, settings

from signalnest.mail.contracts import (
    FrozenMessage,
    SendErrorCode,
    SendOutcome,
    SendResult,
    SendStage,
)
from signalnest.mail.smtp import send_frozen


@pytest.mark.parametrize(
    "failure",
    [
        ConnectionError("private disconnect"),
        smtplib.SMTPNotSupportedError("private DATA extension error"),
        TimeoutError("private timeout"),
        ssl.SSLError("private TLS detail"),
        ssl.SSLCertVerificationError("private certificate detail"),
    ],
)
def test_data_transport_failure_is_uncertain_even_for_tls_errors(monkeypatch, failure):
    def broken_data(client, payload):
        raise failure

    monkeypatch.setattr(smtplib.SMTP, "data", broken_data)
    with peer(monkeypatch) as server:
        result = send_frozen(frozen(), settings(), environ={})
    assert result.outcome == "uncertain"
    assert result.stage == "body_or_final"
    assert result.smtp_code is None
    assert "private" not in repr(result)
    assert server.connections == 1
    assert not any(command.upper() == b"QUIT" for command in server.commands)


@pytest.mark.parametrize(
    "environment",
    [
        {},
        {"TEST_SMTP_USER": "user"},
        {"TEST_SMTP_PASSWORD": "password"},
        {"TEST_SMTP_USER": "", "TEST_SMTP_PASSWORD": "password"},
    ],
)
def test_missing_credentials_stop_before_network(monkeypatch, environment):
    import signalnest.mail.smtp as adapter

    monkeypatch.setattr(adapter, "_make_client", lambda *args: pytest.fail("must not open SMTP"))
    result = send_frozen(
        frozen(),
        settings(username_env="TEST_SMTP_USER", password_env="TEST_SMTP_PASSWORD"),
        environ=environment,
    )
    assert result.outcome == "permanent"
    assert result.stage == "config" and result.scope == "channel"
    assert result.error_code == "credentials_missing"
    assert "password" not in repr(result)


@pytest.mark.parametrize("value", ["nonascii-秘密", "private\r\ncommand", "\x00", "x" * 4097, 17])
def test_invalid_credentials_have_no_network_or_secret_echo(monkeypatch, value):
    import signalnest.mail.smtp as adapter

    monkeypatch.setattr(adapter, "_make_client", lambda *args: pytest.fail("must not open SMTP"))
    result = send_frozen(
        frozen(),
        settings(username_env="TEST_SMTP_USER", password_env="TEST_SMTP_PASSWORD"),
        environ={"TEST_SMTP_USER": "user", "TEST_SMTP_PASSWORD": value},
    )
    assert result.outcome == "permanent" and result.error_code == "credentials_invalid"
    assert result.scope == "channel"
    assert "秘密" not in repr(result) and "private" not in repr(result)


def test_actual_send_reads_configured_environment_once_and_clears_client_credentials(monkeypatch):
    import signalnest.mail.smtp as adapter

    monkeypatch.setenv("TEST_SMTP_USER", "sender-user")
    monkeypatch.setenv("TEST_SMTP_PASSWORD", "private-password")
    clients = []
    create = adapter._make_client

    def capture(*args):
        client = create(*args)
        clients.append(client)
        return client

    monkeypatch.setattr(adapter, "_make_client", capture)
    with peer(monkeypatch):
        result = send_frozen(
            frozen(), settings(username_env="TEST_SMTP_USER", password_env="TEST_SMTP_PASSWORD")
        )
    assert result.outcome == "accepted"
    assert clients[0].user is clients[0].password is None
    assert clients[0].sock is None
    assert "sender-user" not in repr(result) and "private-password" not in repr(result)


@pytest.mark.parametrize(
    "mutation", ["sha", "recipient", "header", "crlf", "raw_utf8", "too_large", "bad_id"]
)
def test_invalid_frozen_input_does_not_read_credentials_or_open_connection(monkeypatch, mutation):
    import signalnest.mail.smtp as adapter

    original = frozen()
    rendered = original.rendered
    if mutation == "sha":
        rendered = replace(rendered, payload_sha256="0" * 64)
    elif mutation == "recipient":
        rendered = replace(rendered, recipient="other@example.org")
    elif mutation == "header":
        payload = rendered.payload.replace(b"From: ", b"From: evil@example.org\r\nFrom: ", 1)
        rendered = replace(
            rendered, payload=payload, payload_sha256=hashlib.sha256(payload).hexdigest()
        )
    elif mutation in {"crlf", "raw_utf8", "too_large"}:
        payload = {
            "crlf": rendered.payload.replace(b"\r\n", b"\n"),
            "raw_utf8": rendered.payload + "原始正文".encode() + b"\r\n",
            "too_large": b"x" * (1024 * 1024) + b"\r\n",
        }[mutation]
        rendered = replace(
            rendered, payload=payload, payload_sha256=hashlib.sha256(payload).hexdigest()
        )
    broken = replace(original, rendered=rendered, mail_id=0 if mutation == "bad_id" else 1)

    class UntouchedEnvironment(dict):
        def get(self, key, default=None):
            pytest.fail("invalid mail must not read credentials")

    monkeypatch.setattr(adapter, "_make_client", lambda *args: pytest.fail("must not open SMTP"))
    result = send_frozen(
        broken,
        settings(username_env="TEST_SMTP_USER", password_env="TEST_SMTP_PASSWORD"),
        environ=UntouchedEnvironment(),
    )
    assert result.outcome == "permanent" and result.error_code == "invalid_frozen_mail"
    assert result.scope == "message"


def test_default_ssl_context_construction_failure_is_channel_error(monkeypatch):
    import signalnest.mail.smtp as adapter

    def broken_context():
        raise OSError("private CA path")

    monkeypatch.setattr(adapter.ssl, "create_default_context", broken_context)
    result = send_frozen(frozen(), settings(), environ={})
    assert result.outcome == "permanent" and result.error_code == "tls_failed"
    assert result.scope == "channel" and "private" not in repr(result)


def test_unknown_auth_mechanism_does_not_send_credentials_or_body(monkeypatch):
    monkeypatch.setattr(smtplib.SMTP, "has_extn", lambda client, name: True)
    monkeypatch.setattr(
        smtplib.SMTP, "auth", lambda *args, **kwargs: pytest.fail("no supported auth")
    )
    with peer(monkeypatch) as server:
        # The original PLAIN advertisement is replaced immediately after hello.
        ehlo = smtplib.SMTP.ehlo

        def unsupported(client, *args):
            result = ehlo(client, *args)
            client.esmtp_features["auth"] = "XOAUTH2"
            return result

        monkeypatch.setattr(smtplib.SMTP, "ehlo", unsupported)
        result = send_frozen(
            frozen(),
            settings(username_env="TEST_SMTP_USER", password_env="TEST_SMTP_PASSWORD"),
            environ={"TEST_SMTP_USER": "user", "TEST_SMTP_PASSWORD": "not-exposed"},
        )
    assert result.outcome == "permanent" and result.error_code == "auth_mechanism_not_supported"
    assert result.scope == "channel" and server.payload is None


def test_missing_auth_is_permanent_channel_failure(monkeypatch):
    has_extn = smtplib.SMTP.has_extn
    monkeypatch.setattr(
        smtplib.SMTP,
        "has_extn",
        lambda client, name: False if name == "auth" else has_extn(client, name),
    )
    with peer(monkeypatch) as server:
        result = send_frozen(
            frozen(),
            settings(username_env="TEST_SMTP_USER", password_env="TEST_SMTP_PASSWORD"),
            environ={"TEST_SMTP_USER": "user", "TEST_SMTP_PASSWORD": "not-exposed"},
        )
    assert result.outcome == "permanent" and result.error_code == "auth_not_supported"
    assert server.payload is None


def test_missing_starttls_never_sends_plaintext_auth_or_mail(monkeypatch):
    monkeypatch.setattr(smtplib.SMTP, "has_extn", lambda client, name: False)
    with peer(monkeypatch) as server:
        result = send_frozen(frozen(), settings(), environ={})
    assert result.outcome == "permanent" and result.error_code == "tls_not_supported"
    assert result.stage == "tls" and result.scope == "channel"
    assert server.payload is None


def test_overlong_tls_reply_does_not_save_synthetic_500_as_observed_code(monkeypatch):
    with peer(monkeypatch, Dialogue(starttls_reply=b"220 " + b"x" * 9000 + b"\r\n")) as server:
        result = send_frozen(frozen(), settings(), environ={})
    assert result.outcome == "retryable" and result.error_code == "protocol_error"
    assert result.smtp_code is None and result.stage == "tls"
    assert server.payload is None


def test_cleanup_close_failure_does_not_undo_acceptance(monkeypatch):
    close = smtplib.SMTP.close

    def broken_close(client):
        close(client)
        raise OSError("private cleanup exception")

    monkeypatch.setattr(smtplib.SMTP, "close", broken_close)
    with peer(monkeypatch):
        result = send_frozen(frozen(), settings(), environ={})
    assert result.outcome == "accepted" and result.smtp_code == 250
    assert result.cleanup_failed
    assert "private" not in repr(result)


def test_programming_defect_propagates_after_closing_connection(monkeypatch):
    def defect(client, sender, options):
        raise RuntimeError("programming defect")

    monkeypatch.setattr(smtplib.SMTP, "mail", defect)
    with peer(monkeypatch):
        with pytest.raises(RuntimeError, match="programming defect"):
            send_frozen(frozen(), settings(), environ={})


@pytest.mark.parametrize(
    "changes",
    [
        {"smtp_code": 251},
        {"stage": SendStage.BODY_OR_FINAL},
        {"error_code": SendErrorCode.PROTOCOL_ERROR},
        {"cleanup_failed": 1},
    ],
)
def test_accepted_contract_rejects_missing_or_conflicting_acceptance_evidence(changes):
    values = dict(outcome=SendOutcome.ACCEPTED, stage=SendStage.ACCEPTED, smtp_code=250)
    with pytest.raises(ValueError):
        SendResult(**(values | changes))


def test_send_result_is_immutable_and_does_not_carry_arbitrary_server_text():
    result = SendResult(SendOutcome.ACCEPTED, SendStage.ACCEPTED, smtp_code=250)
    with pytest.raises(FrozenInstanceError):
        result.outcome = SendOutcome.UNCERTAIN
    with pytest.raises(ValueError):
        SendResult(SendOutcome.UNCERTAIN, SendStage.CONNECT, SendErrorCode.DISCONNECTED)
    with pytest.raises(ValueError):
        SendResult(SendOutcome.RETRYABLE, SendStage.CONNECT, "private arbitrary error")


def test_validated_settings_are_required_and_rendering_is_not_repeated(monkeypatch):
    with pytest.raises(TypeError):
        send_frozen(FrozenMessage(1, "immediate", render(), "[]"), None)
    monkeypatch.setattr(
        "signalnest.mail.rendering.render_mail",
        lambda *args, **kwargs: pytest.fail("do not render"),
    )
    with peer(monkeypatch):
        assert send_frozen(frozen(), settings(), environ={}).outcome == "accepted"


def test_malformed_auth_challenge_returns_finite_protocol_error(monkeypatch):
    with peer(monkeypatch, Dialogue(auth_reply=b"334 a\r\n")) as server:
        result = send_frozen(
            frozen(),
            settings(username_env="TEST_SMTP_USER", password_env="TEST_SMTP_PASSWORD"),
            environ={"TEST_SMTP_USER": "user", "TEST_SMTP_PASSWORD": "private-password"},
        )
    assert result.outcome == "retryable" and result.error_code == "protocol_error"
    assert result.stage == "auth" and result.scope == "channel"
    assert server.payload is None
    assert "private" not in repr(result)
