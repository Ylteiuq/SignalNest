"""Background execution in a real child; the only socket is inherited from its parent.

Credentials are inherited environment values and read by the production adapter.
Transparent TLS tests process/protocol recovery, not certificate handshakes.
"""

import argparse
import json
import signal
import socket
from pathlib import Path

from signalnest.config import load_config
from signalnest.instance_lock import WriterLockError
from signalnest.mail import smtp
from signalnest.mail.background import run_mail_pass
from signalnest.mail.contracts import MailError


class TransparentTls:
    def wrap_socket(self, transport, *, server_hostname):
        assert server_hostname == "mail.example.org"
        return transport


def forbidden(*args, **kwargs):
    raise AssertionError("Background mail process attempted external network access")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("config", type=Path)
    parser.add_argument("epoch", type=int)
    parser.add_argument("socket_fd", type=int)
    parser.add_argument("--ignore-term", action="store_true")
    args = parser.parse_args()
    if args.ignore_term:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    socket.socket.connect = forbidden
    socket.socket.connect_ex = forbidden
    socket.create_connection = forbidden
    transport = socket.socket(fileno=args.socket_fd)
    transport.settimeout(60)
    connections = []

    def inherited_socket(client, host, port, timeout):
        assert host == "mail.example.org" and port == 587
        connections.append(True)
        assert len(connections) == 1
        return transport

    smtp.smtplib.SMTP._get_socket = inherited_socket
    smtp.ssl.create_default_context = TransparentTls

    def sender(message, settings):
        return smtp.send_frozen(message, settings)

    try:
        summary = run_mail_pass(
            load_config(args.config),
            run_id="background-process-test",
            now=lambda: args.epoch,
            sender=sender,
        )
        print(json.dumps({"summary": summary, "connections": len(connections)}), flush=True)
        return 0
    except (MailError, WriterLockError) as exc:
        print(json.dumps({"error_code": exc.code, "connections": len(connections)}), flush=True)
        return 1
    finally:
        transport.close()


if __name__ == "__main__":
    raise SystemExit(main())
