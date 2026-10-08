"""One synchronous SMTP transaction using immutable N2 bytes, without retries or DB I/O.

Only an explicit send_frozen call opens a connection or reads credential values.
The caller must persist a sending attempt before calling (N4); this module does
not make a pending row safe to send, schedule retries, or prove inbox delivery.
"""

import binascii
import hashlib
import os
import smtplib
import ssl
from collections.abc import Mapping
from dataclasses import replace
from email import policy
from email.parser import BytesParser

from signalnest.config import SmtpSettings
from signalnest.mail.contracts import (
    FrozenMessage,
    MailError,
    RenderedMail,
    SendResult,
)
from signalnest.mail.contracts import (
    SendErrorCode as Error,
)
from signalnest.mail.contracts import (
    SendOutcome as Outcome,
)
from signalnest.mail.contracts import (
    SendStage as Stage,
)
from signalnest.mail.rendering import _address


def _valid_message(message: FrozenMessage) -> bool:
    if (
        not isinstance(message, FrozenMessage)
        or type(message.mail_id) is not int
        or not 1 <= message.mail_id <= 2**63 - 1
        or message.kind not in {"immediate", "digest"}
        or not isinstance(message.rendered, RenderedMail)
    ):
        return False
    rendered = message.rendered
    if (
        not isinstance(rendered.payload, bytes)
        or not 0 < len(rendered.payload) <= 1024 * 1024
        or hashlib.sha256(rendered.payload).hexdigest() != rendered.payload_sha256
        or not rendered.payload.endswith(b"\r\n")
        or b"\n" in rendered.payload.replace(b"\r\n", b"")
        or b"\r" in rendered.payload.replace(b"\r\n", b"")
        or any(b > 127 for b in rendered.payload)
    ):
        return False
    try:
        _address(rendered.sender)
        _address(rendered.recipient)
        parsed = BytesParser(policy=policy.SMTP).parsebytes(rendered.payload)
        return (
            not parsed.defects
            and not parsed.is_multipart()
            and parsed.get_content_type() == "text/plain"
            and parsed.get_content_charset() == "utf-8"
            and all(
                len(parsed.get_all(header, [])) == 1 and str(parsed[header]) == value
                for header, value in (
                    ("From", rendered.sender),
                    ("To", rendered.recipient),
                    ("Subject", rendered.subject),
                    ("Message-ID", rendered.message_id),
                )
            )
            and not any(parsed.get_all(header, []) for header in ("Cc", "Bcc", "Resent-To"))
        )
    except (MailError, ValueError, TypeError, UnicodeError):
        return False


def _reply(code: int, stage: Stage, *, error: Error = Error.SERVER_REJECTED) -> SendResult:
    """Only method return values or specific refusal exceptions supply this code."""
    scope = "message" if stage in {Stage.MAIL, Stage.RCPT, Stage.BODY_OR_FINAL} else "channel"
    if type(code) is int and 400 <= code <= 599:
        return SendResult(
            Outcome.RETRYABLE if code < 500 else Outcome.PERMANENT,
            stage,
            error,
            code,
            scope,
        )
    return SendResult(
        Outcome.UNCERTAIN if stage == Stage.BODY_OR_FINAL else Outcome.RETRYABLE,
        stage,
        Error.PROTOCOL_ERROR,
        code if type(code) is int and 100 <= code <= 599 else None,
        scope,
    )


def _make_client(settings: SmtpSettings, context: ssl.SSLContext, local_hostname: str):
    kwargs = dict(timeout=settings.timeout_seconds, local_hostname=local_hostname)
    client = (
        smtplib.SMTP_SSL(context=context, **kwargs)
        if settings.security == "tls"
        else smtplib.SMTP(**kwargs)
    )
    # connect() does not retain the target in CPython 3.12; both TLS modes use
    # _host for SNI and certificate hostname checks. Retain ownership before I/O.
    client._host = settings.host
    client.set_debuglevel(0)
    return client


def _authenticate(client, username, password):
    if not client.has_extn("auth"):
        return SendResult(Outcome.PERMANENT, Stage.AUTH, Error.AUTH_NOT_SUPPORTED, scope="channel")
    advertised = client.esmtp_features.get("auth", "").upper().split()
    mechanism = next((m for m in ("CRAM-MD5", "PLAIN", "LOGIN") if m in advertised), None)
    if mechanism is None:
        return SendResult(
            Outcome.PERMANENT, Stage.AUTH, Error.AUTH_MECHANISM_NOT_SUPPORTED, scope="channel"
        )
    client.user, client.password = username, password
    try:
        # One selected mechanism only; login() would try another after a 4xx/5xx.
        code, _ = client.auth(
            mechanism, getattr(client, "auth_" + mechanism.lower().replace("-", "_"))
        )
        if code not in {235, 503}:
            return _reply(code, Stage.AUTH, error=Error.AUTHENTICATION_REJECTED)
    finally:
        client.user = client.password = None
    return None


def _transaction(client, message, settings, context, credentials):
    """Return the observed result before connection cleanup can change it."""
    stage = Stage.CONNECT
    try:
        code, _ = client.connect(settings.host, settings.port)
        if code != 220:
            return _reply(code, stage)
        stage = Stage.HELLO
        code, _ = client.ehlo()
        if code != 250:
            return _reply(code, stage)
        if settings.security == "starttls":
            stage = Stage.TLS
            client.starttls(context=context)
            stage = Stage.HELLO
            code, _ = client.ehlo()
            if code != 250:
                return _reply(code, stage)
        if credentials is not None:
            stage = Stage.AUTH
            rejected = _authenticate(client, *credentials)
            if rejected is not None:
                return rejected
        stage = Stage.MAIL
        options = [f"size={len(message.rendered.payload)}"] if client.has_extn("size") else []
        code, _ = client.mail(message.rendered.sender, options)
        if code != 250:
            return _reply(code, stage)
        stage = Stage.RCPT
        code, _ = client.rcpt(message.rendered.recipient)
        if code not in {250, 251}:
            return _reply(code, stage)
        # SMTP.data contains both the 354 handshake and the body/final-reply
        # boundary. Without a specific observed refusal, failures here are uncertain.
        stage = Stage.BODY_OR_FINAL
        code, _ = client.data(message.rendered.payload)
        if code == 250:
            return SendResult(Outcome.ACCEPTED, Stage.ACCEPTED, smtp_code=250)
        return _reply(code, stage)
    except smtplib.SMTPDataError as exc:
        return _reply(exc.smtp_code, stage)
    except smtplib.SMTPAuthenticationError as exc:
        return _reply(exc.smtp_code, stage, error=Error.AUTHENTICATION_REJECTED)
    except ssl.SSLCertVerificationError:
        if stage == Stage.BODY_OR_FINAL:
            return SendResult(Outcome.UNCERTAIN, stage, Error.TLS_VERIFICATION_FAILED)
        return SendResult(
            Outcome.PERMANENT, Stage.TLS, Error.TLS_VERIFICATION_FAILED, scope="channel"
        )
    except smtplib.SMTPNotSupportedError:
        if stage not in {Stage.TLS, Stage.AUTH}:
            return SendResult(
                Outcome.UNCERTAIN if stage == Stage.BODY_OR_FINAL else Outcome.RETRYABLE,
                stage,
                Error.PROTOCOL_ERROR,
                scope="message" if stage == Stage.BODY_OR_FINAL else "channel",
            )
        return SendResult(
            Outcome.PERMANENT,
            stage,
            Error.TLS_NOT_SUPPORTED if stage == Stage.TLS else Error.AUTH_NOT_SUPPORTED,
            scope="channel",
        )
    except ssl.SSLError:
        if stage in {Stage.CONNECT, Stage.TLS}:
            return SendResult(Outcome.PERMANENT, Stage.TLS, Error.TLS_FAILED, scope="channel")
        return SendResult(
            Outcome.UNCERTAIN if stage == Stage.BODY_OR_FINAL else Outcome.RETRYABLE,
            stage,
            Error.TLS_FAILED,
            scope="message" if stage == Stage.BODY_OR_FINAL else "channel",
        )
    except smtplib.SMTPResponseException as exc:
        if stage == Stage.TLS and exc.smtp_code != 500:
            # A local overlong-line error may synthesize 500; that ambiguous
            # exception is not proof of a real server refusal, even before DATA.
            return _reply(exc.smtp_code, stage, error=Error.TLS_FAILED)
        # A synthetic local 500 must not masquerade as a post-DATA rejection.
        return SendResult(
            Outcome.UNCERTAIN if stage == Stage.BODY_OR_FINAL else Outcome.RETRYABLE,
            stage,
            Error.PROTOCOL_ERROR,
            scope="message" if stage == Stage.BODY_OR_FINAL else "channel",
        )
    except TimeoutError:
        return SendResult(
            Outcome.UNCERTAIN if stage == Stage.BODY_OR_FINAL else Outcome.RETRYABLE,
            stage,
            Error.TIMEOUT,
            scope="message" if stage == Stage.BODY_OR_FINAL else "channel",
        )
    except smtplib.SMTPServerDisconnected as exc:
        return SendResult(
            Outcome.UNCERTAIN if stage == Stage.BODY_OR_FINAL else Outcome.RETRYABLE,
            stage,
            Error.TIMEOUT if isinstance(exc.__context__, TimeoutError) else Error.DISCONNECTED,
            scope="message" if stage == Stage.BODY_OR_FINAL else "channel",
        )
    except smtplib.SMTPException:
        return SendResult(
            Outcome.UNCERTAIN if stage == Stage.BODY_OR_FINAL else Outcome.RETRYABLE,
            stage,
            Error.PROTOCOL_ERROR,
            scope="message" if stage == Stage.BODY_OR_FINAL else "channel",
        )
    except binascii.Error:
        return SendResult(Outcome.RETRYABLE, stage, Error.PROTOCOL_ERROR, scope="channel")
    except OSError:
        return SendResult(
            Outcome.UNCERTAIN if stage == Stage.BODY_OR_FINAL else Outcome.RETRYABLE,
            stage,
            Error.CONNECTION_FAILED,
            scope="message" if stage == Stage.BODY_OR_FINAL else "channel",
        )


def send_frozen(
    message: FrozenMessage, settings: SmtpSettings, *, environ: Mapping[str, str] | None = None
) -> SendResult:
    """Explicit network boundary, one frozen message/recipient/session, no retries.

    This does not modify mail_messages.state. N4 must record a sending attempt
    before calling and persist this observation before sending any further mail.
    """
    if not isinstance(settings, SmtpSettings):
        raise TypeError("send_frozen requires validated SmtpSettings")
    if not _valid_message(message):
        return SendResult(Outcome.PERMANENT, Stage.CONFIG, Error.INVALID_FROZEN_MAIL)
    credentials = None
    if settings.username_env is not None:
        env = os.environ if environ is None else environ
        values = (env.get(settings.username_env), env.get(settings.password_env))
        if any(value is None or value == "" for value in values):
            return SendResult(
                Outcome.PERMANENT, Stage.CONFIG, Error.CREDENTIALS_MISSING, scope="channel"
            )
        if any(
            not isinstance(value, str)
            or len(value) > 4096
            or any(ord(c) < 32 or ord(c) > 126 for c in value)
            for value in values
        ):
            return SendResult(
                Outcome.PERMANENT, Stage.CONFIG, Error.CREDENTIALS_INVALID, scope="channel"
            )
        credentials = values
    # Constructing a verified context and unconnected SMTP object does not fetch
    # DNS or credentials; all expected network failures are handled per stage.
    try:
        context = ssl.create_default_context()
    except OSError:
        return SendResult(Outcome.PERMANENT, Stage.TLS, Error.TLS_FAILED, scope="channel")
    client = _make_client(settings, context, message.rendered.sender.rsplit("@", 1)[-1])
    result = None
    cleanup_failed = False
    try:
        result = _transaction(client, message, settings, context, credentials)
        if result.outcome == Outcome.ACCEPTED:
            try:
                code, _ = client.quit()
                cleanup_failed = code != 221
            except OSError:
                # Includes smtplib exceptions. Acceptance cannot be undone by QUIT.
                cleanup_failed = True
    finally:
        try:
            client.close()
        except OSError:
            cleanup_failed = True
    return replace(result, cleanup_failed=cleanup_failed)
