"""Actual process execution/termination of the automatic mail pass, fully offline.

The parent SMTP peer owns an independent acceptance journal. TLS is transparent,
credentials are dummy inherited environment values, and supervisor termination is
simulated here; no real systemd instance, public SMTP or inbox is exercised.
"""

import base64
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from test_mail_recovery import publish_marker, wait_for_boundary
from test_notification_service import ACTIVATED_AT, SOURCE, document, live, opportunity, rows
from test_notification_service import activated as activated

from signalnest.instance_lock import writer_lock
from signalnest.mail.contracts import PlanOptions
from signalnest.mail.planning import load_frozen_mail, plan_mail
from signalnest.mail.sending import mail_status, set_sending_paused
from signalnest.schema import mail_attempts, mail_delivery, mail_message_members, mail_messages

WORKER = Path(__file__).parent / "helpers/background_mail_worker.py"
USERNAME = "background-dummy-user"
PASSWORD = "background-dummy-password"
EPOCH = ACTIVATED_AT + 20
pytestmark = pytest.mark.skipif(os.name != "posix", reason="Requires inherited fds/POSIX signals")


def configuration(env, tmp_path):
    path = tmp_path / "background.toml"
    path.write_text(
        f"""
[storage]
data_dir = {json.dumps(str(env.settings.data_dir))}
database = {json.dumps(str(env.settings.database))}
[source]
id = "{SOURCE}"
list_url = "https://uc.whu.edu.cn/tzgg/xstz.htm"
[http]
connect_timeout_seconds = 5.0
read_timeout_seconds = 5.0
request_interval_seconds = 2.0
user_agent = "SignalNest/background-process-test"
[smtp]
host = "mail.example.org"
port = 587
security = "starttls"
timeout_seconds = 60.0
username_env = "SIGNALNEST_SMTP_USERNAME"
password_env = "SIGNALNEST_SMTP_PASSWORD"
[mail_runtime]
enabled = true
max_plan_messages = 1
max_events = 50
max_bytes = 131072
[mail_sending]
max_messages = 1
run_seconds = 60.0
""",
        encoding="utf-8",
    )
    assert USERNAME not in path.read_text() and PASSWORD not in path.read_text()
    return path


class BackgroundPeer:
    def __init__(self, transport, journal, marker, *, block_final=False):
        self.transport = transport
        self.journal = journal
        self.marker = marker
        self.block_final = block_final
        self.stop = threading.Event()
        self.errors = []
        self.authenticated = False
        self.thread = threading.Thread(target=self.serve, daemon=True)

    def serve(self):
        try:
            self.transport.sendall(b"220 background peer\r\n")
            with self.transport.makefile("rb") as stream:
                while line := stream.readline():
                    verb = line.split(b" ", 1)[0].rstrip(b"\r\n").upper()
                    if verb == b"EHLO":
                        self.transport.sendall(
                            b"250-parent peer\r\n250-STARTTLS\r\n250 AUTH PLAIN\r\n"
                        )
                    elif verb == b"STARTTLS":
                        self.transport.sendall(b"220 transparent test TLS\r\n")
                    elif verb == b"AUTH":
                        _, mechanism, encoded = line.split()
                        assert mechanism == b"PLAIN"
                        assert base64.b64decode(encoded) == f"\0{USERNAME}\0{PASSWORD}".encode()
                        self.authenticated = True
                        self.transport.sendall(b"235 authenticated\r\n")
                    elif verb in {b"MAIL", b"RCPT"}:
                        assert self.authenticated
                        self.transport.sendall(b"250 accepted\r\n")
                    elif verb == b"DATA":
                        self.transport.sendall(b"354 send body\r\n")
                        body = []
                        while body_line := stream.readline():
                            if body_line == b".\r\n":
                                break
                            body.append(body_line[1:] if body_line.startswith(b"..") else body_line)
                        else:
                            raise AssertionError("Child closed before its full SMTP body")
                        digest = hashlib.sha256(b"".join(body)).hexdigest()
                        with self.journal.open("a", encoding="utf-8") as stream_out:
                            stream_out.write(json.dumps({"payload_sha256": digest}) + "\n")
                            stream_out.flush()
                            os.fsync(stream_out.fileno())
                        if self.block_final:
                            publish_marker(self.marker, {"boundary": "body_accepted"})
                            self.stop.wait(15)
                            return
                        self.transport.sendall(b"250 queued\r\n")
                    elif verb == b"QUIT":
                        self.transport.sendall(b"221 closing\r\n")
                        return
                    else:
                        raise AssertionError("Unexpected SMTP command")
        except OSError:
            pass  # A rejected locked process or killed child can close before connecting.
        except Exception as exc:
            self.errors.append(exc)
        finally:
            self.transport.close()

    def close(self):
        self.stop.set()
        try:
            self.transport.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.transport.close()
        self.thread.join(2)
        assert not self.thread.is_alive()
        assert not self.errors


def launch(config, tmp_path, *, at=EPOCH, credentials=True, block_final=False, ignore_term=False):
    local, remote = socket.socketpair()
    marker = tmp_path / "body-accepted.json"
    journal = tmp_path / "background-accepted.jsonl"
    peer = BackgroundPeer(remote, journal, marker, block_final=block_final)
    inherited = dict(os.environ)
    for name in ("SIGNALNEST_SMTP_USERNAME", "SIGNALNEST_SMTP_PASSWORD"):
        inherited.pop(name, None)
    if credentials:
        inherited.update(SIGNALNEST_SMTP_USERNAME=USERNAME, SIGNALNEST_SMTP_PASSWORD=PASSWORD)
    args = [sys.executable, str(WORKER), str(config), str(at), str(local.fileno())]
    if ignore_term:
        args.append("--ignore-term")
    try:
        process = subprocess.Popen(
            args,
            env=inherited,
            pass_fds=(local.fileno(),),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except BaseException:
        local.close()
        remote.close()
        raise
    local.close()
    peer.thread.start()
    return process, peer, marker, journal


def stop(process, peer):
    if process.poll() is None:
        process.kill()
    process.communicate(timeout=10)
    peer.close()


def result(process):
    stdout, stderr = process.communicate(timeout=10)
    assert USERNAME not in stdout + stderr and PASSWORD not in stdout + stderr
    return process.returncode, json.loads(stdout), stderr


def seed(env):
    live(env, opportunity())
    assert rows(env, mail_messages) == []
    return document(env)


def test_real_background_child_plans_sends_with_inherited_credentials_and_is_idempotent(
    activated, tmp_path
):
    env = activated
    original_document = seed(env)
    config = configuration(env, tmp_path)
    process, peer, _, journal = launch(config, tmp_path)
    try:
        code, summary, error = result(process)
        assert code == 0, error
        assert summary["connections"] == 1 and peer.authenticated
    finally:
        stop(process, peer)
    mail_id = rows(env, mail_messages)[0]["id"]
    frozen = load_frozen_mail(env.engine, SOURCE, mail_id)
    original_members = rows(env, mail_message_members)
    assert json.loads(journal.read_text())["payload_sha256"] == frozen.rendered.payload_sha256
    assert rows(env, mail_delivery)[0]["state"] == "accepted"
    assert len(rows(env, mail_attempts)) == 1
    process, peer, _, _ = launch(config, tmp_path, at=EPOCH + 1)
    try:
        code, summary, error = result(process)
        assert code == 0, error
        assert summary["connections"] == 0
    finally:
        stop(process, peer)
    assert len(rows(env, mail_messages)) == 1
    assert len(rows(env, mail_attempts)) == 1
    assert load_frozen_mail(env.engine, SOURCE, mail_id) == frozen
    assert rows(env, mail_message_members) == original_members
    assert document(env) == original_document


def test_real_background_child_missing_credentials_pauses_and_keeps_frozen_mail(
    activated, tmp_path
):
    env = activated
    original_document = seed(env)
    config = configuration(env, tmp_path)
    process, peer, _, journal = launch(config, tmp_path, credentials=False)
    try:
        _, summary, _ = result(process)
        assert summary["connections"] == 0
        assert not peer.authenticated
    finally:
        stop(process, peer)
    assert not journal.exists()
    attempts = rows(env, mail_attempts)
    assert len(attempts) == 1
    assert attempts[0]["outcome"] == "permanent"
    assert attempts[0]["error_code"] == "credentials_missing"
    status = mail_status(env.engine, SOURCE, at=EPOCH)
    assert status["paused"]
    assert rows(env, mail_delivery)[0]["state"] == "blocked"
    frozen = rows(env, mail_messages)
    process, peer, _, _ = launch(config, tmp_path, at=EPOCH + 1)
    try:
        _, summary, _ = result(process)
        assert summary["connections"] == 0
    finally:
        stop(process, peer)
    assert rows(env, mail_attempts) == attempts
    assert rows(env, mail_messages) == frozen
    assert document(env) == original_document


def test_supervisor_term_kill_after_body_then_paused_restart_recovers_uncertain(
    activated, tmp_path
):
    env = activated
    original_document = seed(env)
    config = configuration(env, tmp_path)
    process, peer, marker, journal = launch(config, tmp_path, block_final=True, ignore_term=True)
    try:
        assert wait_for_boundary(process, marker)["boundary"] == "body_accepted"
        mail_id = rows(env, mail_messages)[0]["id"]
        frozen = load_frozen_mail(env.engine, SOURCE, mail_id)
        assert rows(env, mail_delivery)[0]["state"] == "sending"
        # A supervisor hard timeout: actual SIGTERM, measured grace expiry, SIGKILL.
        os.kill(process.pid, signal.SIGTERM)
        with pytest.raises(subprocess.TimeoutExpired):
            process.wait(timeout=0.1)
        os.kill(process.pid, signal.SIGKILL)
        process.communicate(timeout=10)
        assert process.returncode == -signal.SIGKILL
    finally:
        stop(process, peer)
    assert json.loads(journal.read_text())["payload_sha256"] == frozen.rendered.payload_sha256
    assert rows(env, mail_delivery)[0]["state"] == "sending"
    with writer_lock(env.settings.database):
        set_sending_paused(env.engine, SOURCE, True, at=EPOCH + 1)
    process, peer, _, _ = launch(config, tmp_path, at=EPOCH + 2)
    try:
        _, summary, _ = result(process)
        assert summary["connections"] == 0
    finally:
        stop(process, peer)
    restored = rows(env, mail_delivery)[0]
    attempt = rows(env, mail_attempts)[0]
    assert restored["state"] == "uncertain" and restored["attempt_count"] == 1
    assert restored["next_attempt_at"] >= EPOCH + 2 + 1800
    assert attempt["outcome"] == "uncertain" and attempt["recovered"]
    assert mail_status(env.engine, SOURCE, at=EPOCH + 2)["paused"]
    process, peer, _, _ = launch(config, tmp_path, at=EPOCH + 3)
    try:
        _, summary, _ = result(process)
        assert summary["connections"] == 0
    finally:
        stop(process, peer)
    assert rows(env, mail_delivery)[0] == restored
    assert rows(env, mail_attempts) == [attempt]
    assert load_frozen_mail(env.engine, SOURCE, mail_id) == frozen
    assert document(env) == original_document


def test_background_second_process_lock_rejected_before_plan_or_network(activated, tmp_path):
    env = activated
    original_document = seed(env)
    config = configuration(env, tmp_path)
    with writer_lock(env.settings.database):
        process, peer, _, journal = launch(config, tmp_path)
        try:
            code, summary, _ = result(process)
            assert code == 1
            assert summary["error_code"] == "writer_lock_busy"
            assert summary["connections"] == 0
        finally:
            stop(process, peer)
        assert rows(env, mail_messages) == []
        assert rows(env, mail_attempts) == []
        assert not journal.exists()
    process, peer, _, journal = launch(config, tmp_path, at=EPOCH + 1)
    try:
        code, summary, error = result(process)
        assert code == 0, error
        assert summary["connections"] == 1
    finally:
        stop(process, peer)
    assert rows(env, mail_delivery)[0]["state"] == "accepted"
    assert document(env) == original_document


def test_background_paused_process_keeps_already_frozen_bytes_and_members(activated, tmp_path):
    env = activated
    seed(env)
    plan_mail(env.engine, SOURCE, PlanOptions(), at=ACTIVATED_AT + 10)
    frozen, members = rows(env, mail_messages), rows(env, mail_message_members)
    with writer_lock(env.settings.database):
        set_sending_paused(env.engine, SOURCE, True, at=EPOCH)
    process, peer, _, journal = launch(configuration(env, tmp_path), tmp_path, at=EPOCH + 1)
    try:
        _, summary, _ = result(process)
        assert summary["connections"] == 0
    finally:
        stop(process, peer)
    assert not journal.exists()
    assert rows(env, mail_attempts) == []
    assert rows(env, mail_messages) == frozen
    assert rows(env, mail_message_members) == members
    assert mail_status(env.engine, SOURCE, at=EPOCH + 1)["paused"]
