"""Real TLS handshakes and certificate checks over local socketpairs only.

The checked-in certificate and key are public test fixtures, not credentials.
They identify only smtp.example.test and must never be used in deployment.
No DNS, public SMTP connection or external delivery is involved in these tests.
"""

import socket
import ssl
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from test_mail_rendering import item, render

from signalnest.config import SmtpSettings
from signalnest.mail.contracts import FrozenMessage, SendErrorCode, SendOutcome, SendStage
from signalnest.mail.smtp import send_frozen

CERTIFICATES = Path(__file__).parent / "fixtures" / "smtp"
CERTIFICATE = CERTIFICATES / "test-only-cert.pem"
KEY = CERTIFICATES / "test-only-key.pem"


@dataclass
class TlsPeer:
    """One minimal local peer, upgrading the actual socket for STARTTLS."""

    security: str
    context: ssl.SSLContext
    commands: list[bytes] = field(default_factory=list)
    encrypted_commands: list[bytes] = field(default_factory=list)
    server_names: list[str | None] = field(default_factory=list)
    payload: bytes | None = None
    connections: int = 0
    handshakes: int = 0
    tls_failures: list[ssl.SSLError] = field(default_factory=list)
    unexpected_errors: list[Exception] = field(default_factory=list)
    sockets: list[socket.socket] = field(default_factory=list)

    def encrypted(self, raw):
        wrapped = self.context.wrap_socket(raw, server_side=True)
        self.sockets.append(wrapped)
        self.handshakes += 1
        assert wrapped.cipher() is not None
        return wrapped

    def serve(self, peer):
        stream = None
        try:
            if self.security == "tls":
                peer = self.encrypted(peer)
            peer.sendall(b"220 local TLS peer\r\n")
            stream = peer.makefile("rb")
            while line := stream.readline():
                command_line = line.rstrip(b"\r\n")
                self.commands.append(command_line)
                if isinstance(peer, ssl.SSLSocket):
                    self.encrypted_commands.append(command_line)
                command = line.split(b" ", 1)[0].rstrip(b"\r\n").upper()
                if command == b"EHLO":
                    peer.sendall(b"250-local TLS peer\r\n250-STARTTLS\r\n250 AUTH PLAIN\r\n")
                elif command == b"STARTTLS":
                    assert not isinstance(peer, ssl.SSLSocket)
                    peer.sendall(b"220 upgrade now\r\n")
                    stream.close()
                    stream = None
                    peer = self.encrypted(peer)
                    stream = peer.makefile("rb")
                elif command == b"AUTH":
                    assert isinstance(peer, ssl.SSLSocket)
                    peer.sendall(b"235 authenticated\r\n")
                elif command in {b"MAIL", b"RCPT"}:
                    assert isinstance(peer, ssl.SSLSocket)
                    peer.sendall(b"250 accepted\r\n")
                elif command == b"DATA":
                    assert isinstance(peer, ssl.SSLSocket)
                    peer.sendall(b"354 send body\r\n")
                    body = []
                    while body_line := stream.readline():
                        if body_line == b".\r\n":
                            break
                        body.append(body_line[1:] if body_line.startswith(b"..") else body_line)
                    self.payload = b"".join(body)
                    peer.sendall(b"250 queued\r\n")
                elif command == b"QUIT":
                    peer.sendall(b"221 goodbye\r\n")
                    return
                else:
                    raise AssertionError(f"unexpected local test command: {command!r}")
        except ssl.SSLError as error:
            # Peer sees the client's fatal alert on a genuine certificate failure.
            self.tls_failures.append(error)
        except OSError:
            # Closing a rejected TLS session is an expected end to this peer.
            pass
        except Exception as error:
            self.unexpected_errors.append(error)
        finally:
            if stream is not None:
                stream.close()
            peer.close()


@contextmanager
def tls_peer(monkeypatch, *, security, host="smtp.example.test", trusted=True):
    """Use production smtplib and verified client SSL, replacing socket creation only."""
    import signalnest.mail.smtp as adapter

    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(CERTIFICATE, KEY)
    peer = TlsPeer(security, server_context)
    server_context.set_servername_callback(
        lambda ssl_socket, server_name, context: peer.server_names.append(server_name)
    )
    workers = []
    original_context_factory = ssl.create_default_context
    client_context = original_context_factory()
    if trusted:
        client_context.load_verify_locations(cafile=CERTIFICATE)
    assert client_context.verify_mode == ssl.CERT_REQUIRED
    assert client_context.check_hostname is True
    monkeypatch.setattr(adapter.ssl, "create_default_context", lambda: client_context)

    def local_connection(address, timeout, source_address=None):
        assert address == (host, 465 if security == "tls" else 587)
        assert timeout > 0
        assert source_address is None
        peer.connections += 1
        local, remote = socket.socketpair()
        local.settimeout(timeout)
        remote.settimeout(timeout)
        peer.sockets.extend((local, remote))
        worker = threading.Thread(target=peer.serve, args=(remote,), daemon=True)
        workers.append(worker)
        worker.start()
        return local

    def external_connection_forbidden(*args, **kwargs):
        raise AssertionError("socketpair tests must never open an external connection")

    monkeypatch.setattr(socket, "create_connection", local_connection)
    monkeypatch.setattr(socket.socket, "connect", external_connection_forbidden)
    try:
        yield peer
    finally:
        for worker in workers:
            worker.join(2)
        for opened in peer.sockets:
            try:
                opened.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            opened.close()
        for worker in workers:
            worker.join(2)
            assert not worker.is_alive()
        assert peer.unexpected_errors == []
        assert client_context.verify_mode == ssl.CERT_REQUIRED
        assert client_context.check_hostname is True


def frozen():
    rendered = render((item(body="本地 TLS 测试通知；不发送真实邮件。"),))
    return FrozenMessage(mail_id=1, kind="immediate", rendered=rendered, members_json="[]")


def settings(security, *, host="smtp.example.test"):
    return SmtpSettings(
        host=host,
        port=465 if security == "tls" else 587,
        security=security,
        timeout_seconds=2.0,
        username_env="TEST_ONLY_SMTP_USERNAME",
        password_env="TEST_ONLY_SMTP_PASSWORD",
    )


ENVIRONMENT = {
    "TEST_ONLY_SMTP_USERNAME": "test-user",
    "TEST_ONLY_SMTP_PASSWORD": "test-password",
}


@pytest.mark.parametrize("security", ["starttls", "tls"])
def test_real_verified_tls_accepts_frozen_mail_and_protects_auth(monkeypatch, security):
    message = frozen()
    with tls_peer(monkeypatch, security=security) as peer:
        result = send_frozen(message, settings(security), environ=ENVIRONMENT)
    assert result.outcome == SendOutcome.ACCEPTED
    assert result.stage == SendStage.ACCEPTED
    assert result.smtp_code == 250
    assert result.cleanup_failed is False
    assert peer.connections == 1
    assert peer.handshakes == 1
    assert peer.server_names == ["smtp.example.test"]
    assert peer.tls_failures == []
    assert peer.payload == message.rendered.payload
    encrypted_verbs = [command.split(b" ", 1)[0].upper() for command in peer.encrypted_commands]
    assert encrypted_verbs == [b"EHLO", b"AUTH", b"MAIL", b"RCPT", b"DATA", b"QUIT"]
    if security == "starttls":
        plain_verbs = [command.split(b" ", 1)[0].upper() for command in peer.commands[:2]]
        assert plain_verbs == [b"EHLO", b"STARTTLS"]
    else:
        assert peer.commands == peer.encrypted_commands


@pytest.mark.parametrize("security", ["starttls", "tls"])
@pytest.mark.parametrize("failure", ["untrusted", "hostname_mismatch"])
def test_real_tls_rejects_bad_certificates_before_auth_or_mail(monkeypatch, security, failure):
    host = "wrong.example.test" if failure == "hostname_mismatch" else "smtp.example.test"
    with tls_peer(
        monkeypatch, security=security, host=host, trusted=failure != "untrusted"
    ) as peer:
        result = send_frozen(frozen(), settings(security, host=host), environ=ENVIRONMENT)
    assert result.outcome == SendOutcome.PERMANENT
    assert result.stage == SendStage.TLS
    assert result.error_code == SendErrorCode.TLS_VERIFICATION_FAILED
    assert result.scope == "channel"
    assert peer.connections == 1
    assert peer.server_names == [host]
    assert peer.payload is None
    assert not any(
        command.upper().startswith((b"AUTH ", b"MAIL ", b"RCPT ", b"DATA"))
        for command in peer.commands
    )
