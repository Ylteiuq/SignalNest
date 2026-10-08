"""Real subprocess SMTP recovery harness; all transport stays on an inherited socketpair.

Only test wrappers pause at observable boundaries. The archive, frozen mail,
attempt registration, send coordinator and SQLite transactions are production code.
The parent SMTP peer survives SIGKILL and retains its own acceptance evidence.
"""

import argparse
import json
import os
import signal
import socket
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from signalnest.config import SmtpSettings
from signalnest.instance_lock import writer_lock
from signalnest.mail import sending, smtp
from signalnest.storage import open_initialized_engine

SOURCE = "whu-undergrad-student"


class TransparentTls:
    """Protocol transport only; these process experiments do not test real TLS."""

    def wrap_socket(self, sock, *, server_hostname):
        assert server_hostname == "mail.example.org"
        return sock


def forbidden(*args, **kwargs):
    raise AssertionError("Mail recovery subprocess must not open a network connection")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    parser.add_argument("signal_file", type=Path)
    parser.add_argument("boundary")
    parser.add_argument("epoch", type=int)
    parser.add_argument("socket_fd", type=int)
    args = parser.parse_args()
    socket.socket.connect = forbidden
    socket.socket.connect_ex = forbidden
    socket.create_connection = forbidden
    local = socket.socket(fileno=args.socket_fd)
    local.settimeout(60)
    calls = []

    def pause(payload):
        temporary = args.signal_file.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(args.signal_file)
        while True:
            signal.pause()

    def local_socket(client, host, port, timeout):
        assert host == "mail.example.org" and port == 587
        calls.append({"host": host, "port": port})
        assert len(calls) == 1
        return local

    smtp.smtplib.SMTP._get_socket = local_socket
    smtp.ssl.create_default_context = TransparentTls
    original_register = sending.register_attempt

    def register(*values, **options):
        result = original_register(*values, **options)
        if args.boundary == "attempt_registered":
            pause({"boundary": args.boundary, "attempt_no": result["attempt_no"]})
        return result

    sending.register_attempt = register
    original_finish = sending.finish_attempt

    def finish(*values, **options):
        result = original_finish(*values, **options)
        if args.boundary == "accepted_committed":
            assert result == "accepted"
            pause({"boundary": args.boundary, "state": result})
        return result

    sending.finish_attempt = finish

    if args.boundary in {"attempt_precommit", "result_precommit"}:

        @sa.event.listens_for(Engine, "after_execute")
        def after_execute(connection, clause, multiparams, params, execution_options, result):
            if isinstance(clause, sa.sql.dml.Update) and clause.table.name == "mail_delivery":
                state = connection.scalar(sa.select(clause.table.c.state))
                expected = "sending" if args.boundary == "attempt_precommit" else "accepted"
                if state == expected:
                    connection.info["mail_recovery_boundary"] = {
                        "boundary": args.boundary,
                        "state_inside_transaction": state,
                    }

        @sa.event.listens_for(Engine, "commit")
        def before_commit(connection):
            if payload := connection.info.pop("mail_recovery_boundary", None):
                pause(payload)

    def send(message, settings):
        result = smtp.send_frozen(message, settings, environ={})
        if args.boundary == "adapter_accepted":
            assert result.outcome.value == "accepted"
            pause({"boundary": args.boundary, "observed": result.outcome.value})
        return result

    engine = open_initialized_engine(args.database)
    try:
        with writer_lock(args.database):
            result = sending.drain_mail(
                engine,
                SOURCE,
                SmtpSettings(host="mail.example.org", port=587, timeout_seconds=60.0),
                sending.DrainOptions(max_messages=1),
                now=lambda: args.epoch,
                sender=send,
            )
        if hasattr(result, "model_dump"):
            result = result.model_dump(mode="json")
        print(json.dumps({"summary": result, "connections": len(calls)}), flush=True)
    finally:
        engine.dispose()
        local.close()


if __name__ == "__main__":
    main()
