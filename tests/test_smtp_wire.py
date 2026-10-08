"""Actual smtplib dialogue over local socketpairs, with no outbound connection.

The transparent TLS wrapper below exercises STARTTLS sequencing only. It does
not perform, or claim to validate, a real TLS handshake or remote delivery.
"""

import hashlib
import socket
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field, replace

import pytest
from test_mail_rendering import item, render
from test_notification_service import ACTIVATED_AT, SOURCE, live, opportunity
from test_notification_service import activated as activated

from signalnest.config import SmtpSettings
from signalnest.mail.contracts import FrozenMessage, PlanOptions
from signalnest.mail.planning import load_frozen_mail, plan_mail
from signalnest.mail.smtp import send_frozen


@dataclass
class Dialogue:
    """One deliberately small SMTP peer, accepting one envelope and body."""

    mail_reply: bytes = b"250 envelope accepted\r\n"
    recipient_reply: bytes = b"250 recipient accepted\r\n"
    data_reply: bytes = b"354 send body\r\n"
    final_reply: bytes | None = b"250 queued\r\n"
    quit_reply: bytes | None = b"221 closing\r\n"
    auth_reply: bytes = b"235 authenticated\r\n"
    auth_mechanisms: bytes = b"PLAIN"
    starttls_reply: bytes = b"220 start TLS\r\n"
    greeting: bytes | None = b"220 local test peer\r\n"
    disconnect_on: bytes | None = None
    final_timeout: bool = False
    commands: list[bytes] = field(default_factory=list)
    payload: bytes | None = None
    connections: int = 0
    wrappers: list[str] = field(default_factory=list)
    stopped: threading.Event = field(default_factory=threading.Event)
    errors: list[Exception] = field(default_factory=list)

    def serve(self, peer):
        try:
            if self.greeting is None:
                return
            peer.sendall(self.greeting)
            with peer.makefile("rb") as stream:
                while line := stream.readline():
                    self.commands.append(line.rstrip(b"\r\n"))
                    command = line.split(b" ", 1)[0].rstrip(b"\r\n").upper()
                    if command == self.disconnect_on:
                        return
                    if command == b"EHLO":
                        peer.sendall(
                            b"250-local test peer\r\n250-STARTTLS\r\n250 AUTH "
                            + self.auth_mechanisms
                            + b"\r\n"
                        )
                    elif command == b"HELO":
                        peer.sendall(b"250 local test peer\r\n")
                    elif command == b"STARTTLS":
                        peer.sendall(self.starttls_reply)
                    elif command == b"AUTH":
                        peer.sendall(self.auth_reply)
                    elif command == b"MAIL":
                        peer.sendall(self.mail_reply)
                    elif command == b"RCPT":
                        peer.sendall(self.recipient_reply)
                    elif command == b"DATA":
                        peer.sendall(self.data_reply)
                        if not self.data_reply.startswith(b"354 "):
                            continue
                        body = []
                        while body_line := stream.readline():
                            if body_line == b".\r\n":
                                break
                            body.append(body_line[1:] if body_line.startswith(b"..") else body_line)
                        self.payload = b"".join(body)
                        if self.final_timeout:
                            self.stopped.wait(2)
                            return
                        if self.final_reply is None:
                            return
                        peer.sendall(self.final_reply)
                    elif command == b"QUIT":
                        if self.quit_reply is not None:
                            peer.sendall(self.quit_reply)
                        return
                    elif command == b"RSET":
                        peer.sendall(b"250 reset\r\n")
                    else:
                        raise AssertionError(f"Unexpected SMTP command: {command!r}")
        except OSError:
            # Client close after a finite failure is a valid end to this peer.
            pass
        except Exception as error:
            self.errors.append(error)
        finally:
            peer.close()


class TransparentTls:
    def __init__(self, dialogue):
        self.dialogue = dialogue

    def wrap_socket(self, sock, *, server_hostname):
        self.dialogue.wrappers.append(server_hostname)
        return sock


@contextmanager
def peer(monkeypatch, dialogue=None):
    """Replace only socket creation; all SMTP protocol methods remain real."""
    import signalnest.mail.smtp as adapter

    dialogue = dialogue or Dialogue()
    sockets = []
    workers = []

    def local_socket(client, host, port, timeout):
        assert host == "mail.example.org"
        assert port in (465, 587)
        assert timeout > 0
        dialogue.connections += 1
        local, remote = socket.socketpair()
        local.settimeout(timeout)
        sockets.extend((local, remote))
        worker = threading.Thread(target=dialogue.serve, args=(remote,), daemon=True)
        workers.append(worker)
        worker.start()
        return local

    monkeypatch.setattr(adapter.smtplib.SMTP, "_get_socket", local_socket)
    monkeypatch.setattr(adapter.ssl, "create_default_context", lambda: TransparentTls(dialogue))
    try:
        yield dialogue
    finally:
        dialogue.stopped.set()
        for sock in sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
        for worker in workers:
            worker.join(2)
            assert not worker.is_alive()
        assert dialogue.errors == []


def frozen():
    current = item(body="武汉大学本科生可报名。\n.leading\n..second\n截止2026年10月9日。")
    rendered = render((current,))
    return FrozenMessage(mail_id=1, kind="immediate", rendered=rendered, members_json="[]")


def settings(**changes):
    return SmtpSettings(
        **dict(host="mail.example.org", port=587, security="starttls", timeout_seconds=0.2)
        | changes
    )


def outcome(result):
    return getattr(result.outcome, "value", result.outcome)


def stage(result):
    return getattr(result.stage, "value", result.stage)


def test_actual_smtplib_sends_frozen_bytes_with_tls_before_envelope(monkeypatch):
    message = frozen()
    with peer(monkeypatch) as server:
        result = send_frozen(message, settings(), environ={})
    assert outcome(result) == "accepted"
    assert stage(result) == "accepted"
    assert result.smtp_code == 250
    assert result.cleanup_failed is False
    assert server.connections == 1
    assert server.payload == message.rendered.payload
    assert server.wrappers == ["mail.example.org"]
    verbs = [command.split(b" ", 1)[0] for command in server.commands]
    assert verbs == [b"ehlo", b"STARTTLS", b"ehlo", b"mail", b"rcpt", b"data", b"quit"]
    assert server.commands[3] == b"mail FROM:<sender@example.com>"
    assert server.commands[4] == b"rcpt TO:<self@example.com>"


def test_frozen_database_mail_uses_saved_payload_and_envelope(activated, monkeypatch):
    env = activated
    live(env, opportunity())
    planned = plan_mail(env.engine, SOURCE, PlanOptions(), at=ACTIVATED_AT + 10)
    message = load_frozen_mail(env.engine, SOURCE, planned["mail_ids"][0])
    with peer(monkeypatch) as server:
        result = send_frozen(message, settings(), environ={})
    assert outcome(result) == "accepted"
    assert server.payload == message.rendered.payload
    assert b"mail FROM:<sender@example.org>" in server.commands
    assert b"rcpt TO:<recipient@example.org>" in server.commands
    assert load_frozen_mail(env.engine, SOURCE, message.mail_id) == message


def test_dot_stuffing_is_transport_only_and_preserves_frozen_payload(monkeypatch):
    message = frozen()
    payload = message.rendered.payload + b".leading\r\n..second\r\n.\r\n"
    message = replace(
        message,
        rendered=replace(
            message.rendered,
            payload=payload,
            payload_sha256=hashlib.sha256(payload).hexdigest(),
        ),
    )
    with peer(monkeypatch) as server:
        result = send_frozen(message, settings(), environ={})
    assert outcome(result) == "accepted"
    assert server.payload == payload


@pytest.mark.parametrize(
    ("reply", "expected"), [(b"450 temporary\r\n", "retryable"), (b"550 rejected\r\n", "permanent")]
)
@pytest.mark.parametrize("phase", ["mail", "recipient", "data"])
def test_pre_body_refusal_is_definite_and_does_not_send_body_or_reset(
    monkeypatch, reply, expected, phase
):
    server = Dialogue(**{f"{phase}_reply": reply})
    with peer(monkeypatch, server):
        result = send_frozen(frozen(), settings(), environ={})
    assert outcome(result) == expected
    assert result.smtp_code == int(reply[:3])
    assert server.payload is None
    assert b"RSET" not in [command.upper() for command in server.commands]
    assert server.connections == 1


@pytest.mark.parametrize(
    ("reply", "expected"), [(b"450 temporary\r\n", "retryable"), (b"550 rejected\r\n", "permanent")]
)
def test_explicit_final_refusal_is_definite_after_body(monkeypatch, reply, expected):
    message = frozen()
    with peer(monkeypatch, Dialogue(final_reply=reply)) as server:
        result = send_frozen(message, settings(), environ={})
    assert outcome(result) == expected
    assert result.smtp_code == int(reply[:3])
    assert server.payload == message.rendered.payload
    assert server.connections == 1


@pytest.mark.parametrize("timeout", [False, True])
def test_peer_has_body_but_disconnect_or_final_timeout_is_uncertain(monkeypatch, timeout):
    message = frozen()
    with peer(monkeypatch, Dialogue(final_reply=None, final_timeout=timeout)) as server:
        result = send_frozen(message, settings(), environ={})
    assert outcome(result) == "uncertain"
    assert stage(result) == "body_or_final"
    assert server.payload == message.rendered.payload
    assert server.connections == 1


@pytest.mark.parametrize("reply", [None, b"500 quit refused\r\n"])
def test_final_acceptance_remains_accepted_when_quit_disconnects_or_fails(monkeypatch, reply):
    with peer(monkeypatch, Dialogue(quit_reply=reply)) as server:
        result = send_frozen(frozen(), settings(), environ={})
    assert outcome(result) == "accepted"
    assert result.smtp_code == 250
    assert result.cleanup_failed is True
    assert server.payload is not None
    assert server.connections == 1


@pytest.mark.parametrize(
    "reply", [b"not an SMTP reply\r\n", b"250 " + b"x" * 9000 + b"\r\n", b"199 unexpected\r\n"]
)
def test_malformed_final_response_is_uncertain_not_a_fake_permanent_rejection(monkeypatch, reply):
    with peer(monkeypatch, Dialogue(final_reply=reply)) as server:
        result = send_frozen(frozen(), settings(), environ={})
    assert outcome(result) == "uncertain"
    assert stage(result) == "body_or_final"
    assert server.payload is not None
    assert server.connections == 1


def test_authentication_failure_stops_before_mail_and_is_channel_failure(monkeypatch):
    server = Dialogue(auth_reply=b"535 authentication refused\r\n")
    with peer(monkeypatch, server):
        result = send_frozen(
            frozen(),
            settings(username_env="TEST_SMTP_USER", password_env="TEST_SMTP_PASSWORD"),
            environ={"TEST_SMTP_USER": "user", "TEST_SMTP_PASSWORD": "super-secret"},
        )
    assert outcome(result) == "permanent"
    assert stage(result) == "auth"
    assert result.smtp_code == 535
    assert result.scope == "channel"
    assert server.payload is None
    assert not any(command.upper().startswith(b"MAIL ") for command in server.commands)
    assert "super-secret" not in repr(result)


def test_starttls_refusal_never_downgrades_to_plain_envelope(monkeypatch):
    server = Dialogue(starttls_reply=b"454 TLS unavailable\r\n")
    with peer(monkeypatch, server):
        result = send_frozen(frozen(), settings(), environ={})
    assert outcome(result) == "retryable"
    assert stage(result) == "tls"
    assert server.wrappers == []
    assert not any(command.upper().startswith(b"MAIL ") for command in server.commands)
    assert server.payload is None


def test_connection_failure_does_not_retry(monkeypatch):
    import signalnest.mail.smtp as adapter

    calls = []

    def refused(client, host, port, timeout):
        calls.append((host, port, timeout))
        raise ConnectionRefusedError("not exposed")

    monkeypatch.setattr(adapter.smtplib.SMTP, "_get_socket", refused)
    result = send_frozen(frozen(), settings(), environ={})
    assert outcome(result) == "retryable"
    assert stage(result) == "connect"
    assert calls == [("mail.example.org", 587, 0.2)]
    assert "not exposed" not in repr(result)


@pytest.mark.parametrize(
    ("command", "expected_stage", "expected_outcome"),
    [
        (b"EHLO", "hello", "retryable"),
        (b"STARTTLS", "tls", "retryable"),
        (b"MAIL", "mail", "retryable"),
        (b"RCPT", "rcpt", "retryable"),
        (b"DATA", "body_or_final", "uncertain"),
    ],
)
def test_disconnect_at_protocol_boundary_has_finite_stage_without_retry(
    monkeypatch, command, expected_stage, expected_outcome
):
    with peer(monkeypatch, Dialogue(disconnect_on=command)) as server:
        result = send_frozen(frozen(), settings(), environ={})
    assert outcome(result) == expected_outcome
    assert stage(result) == expected_stage
    assert server.connections == 1
    assert server.payload is None
    assert not any(command.upper() == b"RSET" for command in server.commands)


def test_disconnect_before_greeting_is_retryable_connect_failure(monkeypatch):
    with peer(monkeypatch, Dialogue(greeting=None)) as server:
        result = send_frozen(frozen(), settings(), environ={})
    assert outcome(result) == "retryable"
    assert stage(result) == "connect"
    assert server.connections == 1
    assert server.commands == []
    assert server.payload is None


@pytest.mark.parametrize(
    ("reply", "expected"),
    [(b"454 auth unavailable\r\n", "retryable"), (b"535 no auth\r\n", "permanent")],
)
def test_auth_refusal_is_one_attempt_without_mechanism_fallback(monkeypatch, reply, expected):
    with peer(
        monkeypatch, Dialogue(auth_reply=reply, auth_mechanisms=b"CRAM-MD5 PLAIN LOGIN")
    ) as server:
        result = send_frozen(
            frozen(),
            settings(username_env="TEST_SMTP_USER", password_env="TEST_SMTP_PASSWORD"),
            environ={"TEST_SMTP_USER": "user", "TEST_SMTP_PASSWORD": "not-logged"},
        )
    assert outcome(result) == expected
    assert stage(result) == "auth"
    assert result.scope == "channel"
    assert len([c for c in server.commands if c.upper().startswith(b"AUTH ")]) == 1
    assert b"AUTH CRAM-MD5" in server.commands
    assert server.connections == 1
    assert server.payload is None
    assert "not-logged" not in repr(result)
