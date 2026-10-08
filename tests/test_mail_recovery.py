"""Actual SIGKILL/restart through frozen mail, SMTP coordination and SQLite.

The parent peer records a simulated SMTP acceptance outside the killed child.
Its socketpair protocol uses transparent TLS: no public network, inbox delivery,
TLS handshake or power-loss durability is claimed by these experiments.
"""

import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from email import policy
from email.parser import BytesParser
from pathlib import Path

import pytest
from test_notification_service import ACTIVATED_AT, SOURCE, document, live, opportunity, rows
from test_notification_service import activated as activated

from signalnest.instance_lock import writer_lock
from signalnest.mail.contracts import PlanOptions, SendErrorCode, SendOutcome, SendResult, SendStage
from signalnest.mail.planning import load_frozen_mail, plan_mail
from signalnest.mail.sending import DrainOptions, finish_attempt, register_attempt, retry_mail
from signalnest.schema import mail_attempts, mail_delivery, mail_message_members, mail_messages

WORKER = Path(__file__).parent / "helpers/mail_recovery_worker.py"


def publish_marker(path, payload):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


class DurablePeer:
    """One small parent-owned SMTP session and independent acceptance journal."""

    def __init__(self, transport, acceptance_log, marker, boundary):
        self.transport = transport
        self.acceptance_log = acceptance_log
        self.marker = marker
        self.boundary = boundary
        self.stop = threading.Event()
        self.errors = []
        self.thread = threading.Thread(target=self.serve, daemon=True)

    def serve(self):
        try:
            self.transport.sendall(b"220 parent-owned recovery peer\r\n")
            with self.transport.makefile("rb") as stream:
                while line := stream.readline():
                    verb = line.split(b" ", 1)[0].rstrip(b"\r\n").upper()
                    if verb == b"EHLO":
                        self.transport.sendall(b"250-parent peer\r\n250 STARTTLS\r\n")
                    elif verb == b"STARTTLS":
                        self.transport.sendall(b"220 test transparent TLS\r\n")
                    elif verb in {b"MAIL", b"RCPT"}:
                        self.transport.sendall(b"250 accepted envelope\r\n")
                    elif verb == b"DATA":
                        self.transport.sendall(b"354 send body\r\n")
                        body = []
                        while body_line := stream.readline():
                            if body_line == b".\r\n":
                                break
                            body.append(body_line[1:] if body_line.startswith(b"..") else body_line)
                        else:
                            raise AssertionError("Worker closed before the SMTP terminator")
                        payload = b"".join(body)
                        parsed = BytesParser(policy=policy.SMTP).parsebytes(payload)
                        record = {
                            "payload_sha256": hashlib.sha256(payload).hexdigest(),
                            "message_id": str(parsed["Message-ID"]),
                            "sender": str(parsed["From"]),
                            "recipient": str(parsed["To"]),
                        }
                        with self.acceptance_log.open("a", encoding="utf-8") as journal:
                            journal.write(json.dumps(record) + "\n")
                            journal.flush()
                            os.fsync(journal.fileno())
                        if self.boundary == "peer_accepted_no_final":
                            publish_marker(
                                self.marker,
                                {"boundary": self.boundary, "external_acceptance": record},
                            )
                            self.stop.wait(15)
                            return
                        self.transport.sendall(b"250 queued\r\n")
                    elif verb == b"QUIT":
                        self.transport.sendall(b"221 closing\r\n")
                        return
                    else:
                        raise AssertionError(f"Unexpected SMTP verb: {verb!r}")
        except OSError:
            # A killed child or a run with no eligible mail may close at any time.
            pass
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


def launch(env, marker, boundary, epoch, acceptance_log):
    local, remote = socket.socketpair()
    peer = DurablePeer(remote, acceptance_log, marker, boundary)
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                str(WORKER),
                str(env.settings.database),
                str(marker),
                boundary,
                str(epoch),
                str(local.fileno()),
            ],
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
    return process, peer


def wait_for_boundary(process, marker):
    deadline = time.monotonic() + 10
    while not marker.exists():
        if process.poll() is not None:
            stdout, stderr = process.communicate(timeout=2)
            pytest.fail(f"Mail worker exited before its boundary: {stdout}\n{stderr}")
        if time.monotonic() >= deadline:
            pytest.fail("Mail worker did not reach the measured boundary within 10 seconds")
        time.sleep(0.01)
    return json.loads(marker.read_text(encoding="utf-8"))


def resume(env, marker, epoch, acceptance_log):
    process, peer = launch(env, marker, "none", epoch, acceptance_log)
    try:
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, stderr
        return json.loads(stdout)
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)
        peer.close()


def acceptances(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


@pytest.mark.skipif(os.name != "posix", reason="Requires real POSIX SIGKILL and inherited fds")
def test_sigkill_before_attempt_commit_cannot_publish_attempt_or_send(activated, tmp_path):
    env = activated
    live(env, opportunity())
    planned = plan_mail(env.engine, SOURCE, PlanOptions(), at=ACTIVATED_AT + 10)
    mail_id = planned["mail_ids"][0]
    frozen = load_frozen_mail(env.engine, SOURCE, mail_id)
    original_document = document(env)
    pending = rows(env, mail_delivery)[0]
    epoch = ACTIVATED_AT + 20
    marker = tmp_path / "attempt-precommit.json"
    journal = tmp_path / "parent-accepted.jsonl"
    process, peer = launch(env, marker, "attempt_precommit", epoch, journal)
    try:
        reached = wait_for_boundary(process, marker)
        assert reached["state_inside_transaction"] == "sending"
        os.kill(process.pid, signal.SIGKILL)
        process.communicate(timeout=10)
        assert process.returncode == -signal.SIGKILL
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)
        peer.close()
    assert acceptances(journal) == []
    assert rows(env, mail_delivery) == [pending]
    assert rows(env, mail_attempts) == []
    assert document(env) == original_document
    resumed = resume(env, tmp_path / "resumed.json", epoch + 1, journal)
    assert resumed["connections"] == 1
    assert rows(env, mail_delivery)[0]["state"] == "accepted"
    assert rows(env, mail_delivery)[0]["attempt_count"] == 1
    assert len(rows(env, mail_attempts)) == 1
    assert acceptances(journal)[0]["payload_sha256"] == frozen.rendered.payload_sha256
    assert load_frozen_mail(env.engine, SOURCE, mail_id) == frozen
    assert document(env) == original_document


@pytest.mark.skipif(os.name != "posix", reason="Requires real POSIX SIGKILL and inherited fds")
def test_sigkill_consumes_only_one_manual_retry_and_preserves_uncertain_floor(activated, tmp_path):
    env = activated
    live(env, opportunity())
    planned = plan_mail(env.engine, SOURCE, PlanOptions(), at=ACTIVATED_AT + 10)
    mail_id = planned["mail_ids"][0]
    frozen = load_frozen_mail(env.engine, SOURCE, mail_id)
    original_document = document(env)
    epoch = ACTIVATED_AT + 20
    options = DrainOptions(max_attempts=1)
    # A definite synthetic connection refusal exhausts the explicitly smaller
    # budget. The following manual send/crash/restart uses actual child processes.
    with writer_lock(env.settings.database):
        attempt = register_attempt(env.engine, SOURCE, mail_id, at=epoch, options=options)
        finish_attempt(
            env.engine,
            SOURCE,
            mail_id,
            attempt["attempt_no"],
            SendResult(SendOutcome.RETRYABLE, SendStage.CONNECT, SendErrorCode.CONNECTION_FAILED),
            at=epoch,
            options=options,
        )
        granted = retry_mail(env.engine, SOURCE, mail_id, at=epoch + 1)
    assert granted["manual_retry_pending"] is True
    marker = tmp_path / "manual-registered.json"
    journal = tmp_path / "parent-accepted.jsonl"
    process, peer = launch(env, marker, "attempt_registered", epoch + 1, journal)
    try:
        reached = wait_for_boundary(process, marker)
        assert reached["attempt_no"] == 2
        os.kill(process.pid, signal.SIGKILL)
        process.communicate(timeout=10)
        assert process.returncode == -signal.SIGKILL
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)
        peer.close()
    assert rows(env, mail_delivery)[0]["manual_retry_pending"] is False
    assert rows(env, mail_attempts)[1]["manual"] is True
    restored = resume(env, tmp_path / "manual-restored.json", epoch + 2, journal)
    assert restored["connections"] == 0
    blocked = rows(env, mail_delivery)[0]
    recovered = rows(env, mail_attempts)[1]
    assert blocked["state"] == "blocked" and blocked["blocked_reason"] == "manual_attempt_failed"
    assert blocked["attempt_count"] == 2
    assert recovered["outcome"] == "uncertain" and recovered["recovered"] is True
    assert recovered["uncertain_until"] == epoch + 2 + 1800
    assert acceptances(journal) == []
    # Even though the new process has a larger automatic budget, it cannot turn
    # an already consumed explicit manual permission into more automatic sends.
    assert (
        resume(env, tmp_path / "manual-no-new-grant.json", epoch + 10000, journal)["connections"]
        == 0
    )
    with writer_lock(env.settings.database):
        granted = retry_mail(env.engine, SOURCE, mail_id, at=epoch + 3)
    assert granted["next_attempt_at"] == recovered["uncertain_until"]
    assert resume(env, tmp_path / "manual-not-due.json", epoch + 4, journal)["connections"] == 0
    due = granted["next_attempt_at"]
    assert resume(env, tmp_path / "manual-due.json", due, journal)["connections"] == 1
    assert rows(env, mail_delivery)[0]["state"] == "accepted"
    assert rows(env, mail_delivery)[0]["attempt_count"] == 3
    assert len(acceptances(journal)) == 1
    assert acceptances(journal)[0]["payload_sha256"] == frozen.rendered.payload_sha256
    assert load_frozen_mail(env.engine, SOURCE, mail_id) == frozen
    assert document(env) == original_document


@pytest.mark.skipif(os.name != "posix", reason="Requires real POSIX SIGKILL and inherited fds")
@pytest.mark.parametrize(
    "boundary",
    [
        "attempt_registered",
        "peer_accepted_no_final",
        "adapter_accepted",
        "result_precommit",
        "accepted_committed",
    ],
)
def test_sigkill_recovery_retains_frozen_identity_and_exposes_acceptance_gap(
    activated, tmp_path, boundary
):
    env = activated
    live(env, opportunity())
    planned = plan_mail(env.engine, SOURCE, PlanOptions(), at=ACTIVATED_AT + 10)
    mail_id = planned["mail_ids"][0]
    frozen = load_frozen_mail(env.engine, SOURCE, mail_id)
    original_message = rows(env, mail_messages)
    original_members = rows(env, mail_message_members)
    original_document = document(env)
    epoch = ACTIVATED_AT + 20
    marker = tmp_path / "boundary.json"
    journal = tmp_path / "parent-accepted.jsonl"
    process, peer = launch(env, marker, boundary, epoch, journal)
    try:
        reached = wait_for_boundary(process, marker)
        assert reached["boundary"] == boundary
        if boundary == "result_precommit":
            assert reached["state_inside_transaction"] == "accepted"
        os.kill(process.pid, signal.SIGKILL)
        process.communicate(timeout=10)
        assert process.returncode == -signal.SIGKILL
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)
        peer.close()

    with writer_lock(env.settings.database):
        pass  # Actual kernel lock release, not deleting a stale marker file.
    expected_external = 0 if boundary == "attempt_registered" else 1
    assert len(acceptances(journal)) == expected_external
    delivery = rows(env, mail_delivery)[0]
    attempt = rows(env, mail_attempts)[0]
    committed = boundary == "accepted_committed"
    assert delivery["state"] == ("accepted" if committed else "sending")
    assert delivery["attempt_count"] == attempt["attempt_no"] == 1
    assert attempt["outcome"] == ("accepted" if committed else None)
    assert attempt["finished_at"] == (epoch if committed else None)
    assert document(env) == original_document

    # A fresh process performs actual stale-attempt recovery, with no SMTP yet.
    early = resume(env, tmp_path / "restart.json", epoch + 1, journal)
    assert early["connections"] == 0
    recovered = rows(env, mail_delivery)[0]
    old_attempt = rows(env, mail_attempts)[0]
    if committed:
        assert recovered == delivery
        assert old_attempt == attempt
    else:
        assert recovered["state"] == "uncertain"
        assert recovered["attempt_count"] == 1
        assert recovered["accepted_at"] is None
        assert recovered["next_attempt_at"] >= epoch + 1 + 1800
        assert old_attempt["outcome"] == "uncertain" and old_attempt["recovered"] is True
        assert old_attempt["error_code"] == "process_interrupted"
        assert old_attempt["finished_at"] == epoch + 1

    # Repeated startup must not push a recovered due farther into the future.
    again = resume(env, tmp_path / "restart-again.json", epoch + 2, journal)
    assert again["connections"] == 0
    assert rows(env, mail_delivery) == [recovered]
    assert rows(env, mail_attempts) == [old_attempt]

    due = epoch + 10000 if committed else recovered["next_attempt_at"]
    resumed = resume(env, tmp_path / "due.json", due, journal)
    assert resumed["connections"] == (0 if committed else 1)
    final = rows(env, mail_delivery)[0]
    assert final["state"] == "accepted"
    assert final["attempt_count"] == (1 if committed else 2)
    assert final["accepted_at"] == (epoch if committed else due)
    observations = acceptances(journal)
    assert len(observations) == expected_external + (0 if committed else 1)
    for observation in observations:
        assert observation == {
            "payload_sha256": frozen.rendered.payload_sha256,
            "message_id": frozen.rendered.message_id,
            "sender": frozen.rendered.sender,
            "recipient": frozen.rendered.recipient,
        }
    # After service acceptance but before local commit, the peer really saw two
    # copies. Reusing Message-ID and payload does not create SMTP idempotence.
    if boundary in {"peer_accepted_no_final", "adapter_accepted", "result_precommit"}:
        assert len(observations) == 2
    later = resume(env, tmp_path / "accepted-later.json", due + 10000, journal)
    assert later["connections"] == 0
    assert acceptances(journal) == observations
    assert rows(env, mail_messages) == original_message
    assert rows(env, mail_message_members) == original_members
    assert load_frozen_mail(env.engine, SOURCE, mail_id) == frozen
    assert document(env) == original_document
