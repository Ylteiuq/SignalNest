"""Finite coordinator policy against real N1/N2 records and actual SQLite transactions."""

import hashlib
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import OperationalError
from test_mail_planning import message, plan, seal_copies
from test_notification_service import ACTIVATED_AT, SOURCE, document, live, opportunity, rows
from test_notification_service import activated as activated

from signalnest.config import SmtpSettings
from signalnest.mail.contracts import MailError, SendErrorCode, SendOutcome, SendResult, SendStage
from signalnest.mail.sending import (
    DrainOptions,
    drain_mail,
    finish_attempt,
    mail_status,
    recover_sending,
    register_attempt,
    retry_mail,
    set_sending_paused,
)
from signalnest.schema import (
    mail_attempts,
    mail_delivery,
    mail_message_members,
    mail_messages,
)
from signalnest.storage import open_initialized_engine

SMTP = SmtpSettings(host="smtp.example.org", port=465, security="tls")
OPTIONS = DrainOptions()
ACCEPTED = SendResult(SendOutcome.ACCEPTED, SendStage.ACCEPTED, smtp_code=250)
RETRYABLE = SendResult(SendOutcome.RETRYABLE, SendStage.RCPT, SendErrorCode.SERVER_REJECTED, 450)
UNCERTAIN = SendResult(SendOutcome.UNCERTAIN, SendStage.BODY_OR_FINAL, SendErrorCode.DISCONNECTED)
PERMANENT = SendResult(SendOutcome.PERMANENT, SendStage.RCPT, SendErrorCode.SERVER_REJECTED, 550)
AUTH = SendResult(
    SendOutcome.PERMANENT,
    SendStage.AUTH,
    SendErrorCode.AUTHENTICATION_REJECTED,
    535,
    scope="channel",
)
CONNECT = SendResult(
    SendOutcome.RETRYABLE, SendStage.CONNECT, SendErrorCode.CONNECTION_FAILED, scope="channel"
)


@pytest.fixture
def pending(activated):
    env = activated
    live(env, opportunity())
    plan(env, at=ACTIVATED_AT + 10)
    return env


def delivery(env, index=0):
    return sorted(rows(env, mail_delivery), key=lambda row: row["mail_id"])[index]


def drain(env, result=ACCEPTED, *, at=ACTIVATED_AT + 20, options=OPTIONS, sender=None, **kwargs):
    return drain_mail(
        env.engine,
        SOURCE,
        SMTP,
        options,
        now=lambda: at,
        sender=sender or (lambda mail, settings: result),
        **kwargs,
    )


def make_backlog(env, count=3):
    seal_copies(env, count)
    plan(env, at=ACTIVATED_AT + 12)
    assert len(rows(env, mail_messages)) == min(count, 6)


def test_acceptance_is_durable_does_not_change_content_or_mail(pending):
    env = pending
    original = document(env)
    frozen, members = rows(env, mail_messages), rows(env, mail_message_members)
    sent = []

    def sender(mail, settings):
        # A second connection can read the committed attempt during SMTP.
        status = mail_status(env.engine, SOURCE, at=ACTIVATED_AT + 20)
        assert status["counts"]["sending"] == 1
        assert status["messages"][0]["attempt_count"] == 1
        sent.append(mail)
        return ACCEPTED

    result = drain(env, sender=sender)
    assert result["accepted"] == result["attempted"] == 1
    assert result["needs_attention"] is False
    assert delivery(env)["state"] == "accepted"
    assert delivery(env)["accepted_at"] == ACTIVATED_AT + 20
    assert rows(env, mail_attempts)[0]["smtp_code"] == 250
    assert document(env) == original
    assert rows(env, mail_messages) == frozen
    assert rows(env, mail_message_members) == members
    assert sent[0].rendered.payload == frozen[0]["payload_bytes"]
    assert drain(env, at=ACTIVATED_AT + 100000)["attempted"] == 0
    with pytest.raises(MailError, match="mail_already_accepted"):
        retry_mail(env.engine, SOURCE, sent[0].mail_id, at=ACTIVATED_AT + 100001)


@pytest.mark.parametrize(
    "result,state,delay",
    [(RETRYABLE, "retry", 300), (UNCERTAIN, "uncertain", 1800), (PERMANENT, "blocked", None)],
)
def test_finite_results_keep_success_and_persist_due(pending, result, state, delay):
    original = document(pending)
    summary = drain(pending, result)
    row = delivery(pending)
    assert summary["needs_attention"]
    assert row["state"] == state
    assert row["accepted_at"] is None
    assert row["next_attempt_at"] == (ACTIVATED_AT + 20 + delay if delay else None)
    assert document(pending) == original and original["status"] == "processed"
    attempt = rows(pending, mail_attempts)[0]
    assert attempt["outcome"] == result.outcome.value
    assert attempt["stage"] == result.stage.value
    assert attempt["error_code"] == result.error_code.value
    pending.engine.dispose()
    engine = open_initialized_engine(pending.settings.database)
    try:
        assert mail_status(engine, SOURCE, at=ACTIVATED_AT + 21)["messages"][0]["state"] == state
    finally:
        engine.dispose()


def test_due_delays_do_not_block_other_pending_mail(pending):
    make_backlog(pending)
    counts = drain(pending, RETRYABLE, options=DrainOptions(max_messages=1))
    assert counts["attempted"] == 1
    second = drain(pending, at=ACTIVATED_AT + 21)
    assert second["attempted"] == second["accepted"] == 2
    assert delivery(pending)["state"] == "retry"
    assert drain(pending, at=ACTIVATED_AT + 319)["attempted"] == 0
    assert drain(pending, at=ACTIVATED_AT + 320)["accepted"] == 1


def test_ordinary_failure_continues_other_mail(pending):
    make_backlog(pending)
    results = iter([PERMANENT, ACCEPTED, UNCERTAIN])
    assert drain(pending, sender=lambda *args: next(results))["attempted"] == 3
    assert [row["state"] for row in rows(pending, mail_delivery)] == [
        "blocked",
        "accepted",
        "uncertain",
    ]


def test_permanent_channel_error_pauses_all_and_resume_keeps_frozen_mail(pending):
    make_backlog(pending)
    snapshot = rows(pending, mail_messages)
    first = drain(pending, AUTH)
    assert first["attempted"] == 1 and first["paused"]
    status = mail_status(pending.engine, SOURCE, at=ACTIVATED_AT + 21)
    assert status["pause_reason"] == "authentication_rejected"
    assert status["counts"]["pending"] == 2
    assert drain(pending, at=ACTIVATED_AT + 22)["attempted"] == 0
    set_sending_paused(pending.engine, SOURCE, False, at=ACTIVATED_AT + 23)
    assert drain(pending, at=ACTIVATED_AT + 24)["accepted"] == 2
    assert rows(pending, mail_messages) == snapshot


def test_temporary_channel_error_stops_this_pass_without_persistent_pause(pending):
    make_backlog(pending)
    result = drain(pending, CONNECT)
    assert result["attempted"] == 1 and not result["paused"]
    assert delivery(pending)["state"] == "retry"
    assert drain(pending, at=ACTIVATED_AT + 21)["accepted"] == 2


def test_manual_pause_does_not_prevent_new_mail_plan(pending):
    set_sending_paused(pending.engine, SOURCE, True, at=ACTIVATED_AT + 11)
    make_backlog(pending)
    assert len(rows(pending, mail_messages)) == 3
    assert drain(pending)["attempted"] == 0
    assert not rows(pending, mail_attempts)


def test_automatic_six_attempts_then_one_manual_permission_without_count_reset(pending):
    env, at = pending, ACTIVATED_AT + 20
    identity = message(env)["id"]
    for attempt_no in range(1, 7):
        assert drain(env, RETRYABLE, at=at)["attempted"] == 1
        row = delivery(env)
        assert row["attempt_count"] == attempt_no
        if attempt_no < 6:
            assert row["state"] == "retry"
            assert row["next_attempt_at"] == at + OPTIONS.retry_delays_seconds[attempt_no - 1]
            at = row["next_attempt_at"]
    assert row["state"] == "blocked" and row["blocked_reason"] == "retry_exhausted"
    assert drain(env, at=at + 1)["attempted"] == 0
    retry_mail(env.engine, SOURCE, identity, at=at + 2)
    assert retry_mail(env.engine, SOURCE, identity, at=at + 2)["already_granted"]
    assert drain(env, RETRYABLE, at=at + 3)["attempted"] == 1
    assert delivery(env)["attempt_count"] == 7
    assert delivery(env)["state"] == "blocked"
    assert delivery(env)["blocked_reason"] == "manual_attempt_failed"
    assert drain(env, at=at + 100000)["attempted"] == 0
    retry_mail(env.engine, SOURCE, identity, at=at + 100001)
    assert drain(env, at=at + 100002)["accepted"] == 1
    assert delivery(env)["attempt_count"] == 8
    assert [r["attempt_no"] for r in rows(env, mail_attempts)] == list(range(1, 9))
    assert [r["manual"] for r in rows(env, mail_attempts)] == [False] * 6 + [True, True]
    assert len(rows(env, mail_messages)) == 1


def test_exhausted_uncertain_manual_retry_preserves_original_cooldown(pending):
    opts = DrainOptions(max_attempts=1, uncertain_delay_seconds=7200)
    at = ACTIVATED_AT + 20
    drain(pending, UNCERTAIN, options=opts, at=at)
    identity = message(pending)["id"]
    retried = retry_mail(pending.engine, SOURCE, identity, at=at + 1)
    assert retried["next_attempt_at"] == at + 7200
    assert drain(pending, options=opts, at=at + 7199)["attempted"] == 0
    assert drain(pending, options=opts, at=at + 7200)["accepted"] == 1


def test_manual_retry_cannot_bypass_automatic_due(pending):
    drain(pending, UNCERTAIN)
    with pytest.raises(MailError, match="mail_manual_retry_requires_blocked"):
        retry_mail(pending.engine, SOURCE, message(pending)["id"], at=ACTIVATED_AT + 21)


def test_lowered_budget_blocks_exhausted_work_without_stranding_others(pending):
    make_backlog(pending)
    drain(pending, RETRYABLE, options=DrainOptions(max_messages=1))
    result = drain(pending, at=ACTIVATED_AT + 320, options=DrainOptions(max_attempts=1))
    assert result["accepted"] == 2
    assert delivery(pending)["blocked_reason"] == "retry_exhausted"


@pytest.mark.parametrize("manual", [False, True])
def test_recovery_preserves_attempt_and_persistent_uncertain_due(pending, manual):
    env, identity = pending, message(pending)["id"]
    if manual:
        drain(env, PERMANENT)
        retry_mail(env.engine, SOURCE, identity, at=ACTIVATED_AT + 21)
    attempt = register_attempt(env.engine, SOURCE, identity, at=ACTIVATED_AT + 22, options=OPTIONS)
    at = ACTIVATED_AT + 23
    assert recover_sending(env.engine, SOURCE, at=at, options=OPTIONS) == 1
    original = delivery(env)
    recovered = rows(env, mail_attempts)[-1]
    assert recovered["recovered"] and recovered["outcome"] == "uncertain"
    assert recovered["stage"] == "unknown" and recovered["error_code"] == "process_interrupted"
    assert original["attempt_count"] == attempt["attempt_no"]
    if manual:
        assert (
            original["state"] == "blocked" and original["blocked_reason"] == "manual_attempt_failed"
        )
    else:
        assert original["next_attempt_at"] == at + 1800
    assert recover_sending(env.engine, SOURCE, at=at + 30, options=OPTIONS) == 0
    assert delivery(env) == original


def inject_sql_error(engine, needle, *, event="before_cursor_execute"):
    def fail(connection, cursor, statement, parameters, context, executemany):
        if needle in statement:
            raise OperationalError("fault injection", {}, RuntimeError("sensitive detail"))

    sa.event.listen(engine, event, fail)
    return fail


@pytest.mark.parametrize("needle", ["INSERT INTO mail_attempts", "UPDATE mail_delivery"])
def test_network_never_starts_if_registration_fails(pending, needle):
    env = pending
    called = []
    fail = inject_sql_error(env.engine, needle)
    try:
        with pytest.raises(MailError, match="mail_(attempt|recovery)_write_failed"):
            drain(env, sender=lambda *args: called.append(1))
    finally:
        sa.event.remove(env.engine, "before_cursor_execute", fail)
    assert called == []
    assert delivery(env)["state"] == "pending" and not rows(env, mail_attempts)


def test_result_write_failure_stops_before_next_mail_and_rolls_back_entire_result(pending):
    env = pending
    make_backlog(env)
    sent = []
    fault = None

    def sender(mail, settings):
        nonlocal fault
        sent.append(mail.mail_id)
        fault = inject_sql_error(env.engine, "UPDATE mail_delivery")
        return ACCEPTED

    try:
        with pytest.raises(MailError, match="mail_result_write_failed"):
            drain(env, sender=sender)
    finally:
        sa.event.remove(env.engine, "before_cursor_execute", fault)
    assert len(sent) == 1
    assert delivery(env)["state"] == "sending"
    assert rows(env, mail_attempts)[0]["finished_at"] is None
    assert mail_status(env.engine, SOURCE, at=ACTIVATED_AT + 21)["counts"]["pending"] == 2
    assert recover_sending(env.engine, SOURCE, at=ACTIVATED_AT + 22, options=OPTIONS) == 1


def test_recovery_failure_stops_before_smtp(pending):
    env = pending
    register_attempt(env.engine, SOURCE, message(env)["id"], at=ACTIVATED_AT + 19, options=OPTIONS)
    fault = inject_sql_error(env.engine, "UPDATE mail_attempts")
    try:
        with pytest.raises(MailError, match="mail_recovery_write_failed"):
            drain(env)
    finally:
        sa.event.remove(env.engine, "before_cursor_execute", fault)
    assert delivery(env)["state"] == "sending"
    assert rows(env, mail_attempts)[0]["finished_at"] is None


def test_unexpected_adapter_bug_is_not_converted_to_success(pending):
    def broken(*args):
        raise RuntimeError("adapter programming error")

    with pytest.raises(RuntimeError, match="adapter programming error"):
        drain(pending, sender=broken)
    assert delivery(pending)["state"] == "sending"
    assert rows(pending, mail_attempts)[0]["finished_at"] is None


def test_corrupt_frozen_payload_is_blocked_without_attempt_or_network(pending):
    env = pending
    with env.engine.begin() as connection:
        connection.execute(mail_messages.update().values(payload_bytes=b"corrupted"))
    result = drain(env, sender=lambda *args: pytest.fail("must not send"))
    assert result["corrupt"] == 1 and result["attempted"] == 0
    assert delivery(env)["blocked_reason"] == "frozen_corrupt"
    assert not rows(env, mail_attempts)


def test_checksum_valid_but_invalid_mime_is_adapter_permanent(pending):
    env = pending
    with env.engine.begin() as connection:
        connection.execute(
            mail_messages.update().values(
                payload_bytes=b"corrupt", payload_sha256=hashlib.sha256(b"corrupt").hexdigest()
            )
        )
    assert drain(env)["corrupt"] == 1


def test_budget_stops_before_registering_next_message(pending):
    make_backlog(pending)
    ticks = iter([0.0, 0.0, 0.0, 301.0])
    result = drain(pending, monotonic=lambda: next(ticks))
    assert result["attempted"] == result["accepted"] == 1
    assert result["budget_exhausted"]
    assert mail_status(pending.engine, SOURCE, at=ACTIVATED_AT + 21)["counts"]["pending"] == 2


def test_budget_can_expire_during_payload_validation(pending):
    ticks = iter([0.0, 0.0, 300.0])
    result = drain(pending, monotonic=lambda: next(ticks))
    assert result["attempted"] == 0 and result["budget_exhausted"]
    assert delivery(pending)["state"] == "pending"


def test_acceptance_after_cooperative_budget_is_still_saved(pending):
    tick = SimpleNamespace(at=0.0)

    def sender(*args):
        tick.at = 301.0
        return ACCEPTED

    result = drain(pending, monotonic=lambda: tick.at, sender=sender)
    assert result["accepted"] == 1 and delivery(pending)["state"] == "accepted"


def test_status_is_read_only_and_excludes_mail_contents_and_addresses(pending):
    env = pending
    before = rows(env, mail_delivery)
    first = mail_status(env.engine, SOURCE, at=ACTIVATED_AT + 20)
    assert first["counts"]["pending"] == first["due_count"] == 1
    assert first["oldest_pending_at"] == message(env)["frozen_at"]
    text = str(first)
    for prohibited in (
        message(env)["subject"],
        message(env)["sender"],
        message(env)["recipient"],
        "payload_bytes",
        "body_text",
    ):
        assert prohibited not in text
    assert rows(env, mail_delivery) == before


@pytest.mark.parametrize("at", [-1, True, 2**63, "0"])
def test_invalid_clock_fails_before_smtp(pending, at):
    with pytest.raises(MailError, match="mail_time_invalid"):
        drain(pending, at=at)
    assert not rows(pending, mail_attempts)


def test_regressed_result_clock_leaves_unfinished_attempt(pending):
    env, identity = pending, message(pending)["id"]
    attempt = register_attempt(env.engine, SOURCE, identity, at=ACTIVATED_AT + 20, options=OPTIONS)
    with pytest.raises(MailError, match="mail_clock_regressed"):
        finish_attempt(
            env.engine,
            SOURCE,
            identity,
            attempt["attempt_no"],
            ACCEPTED,
            at=ACTIVATED_AT + 19,
            options=OPTIONS,
        )
    assert delivery(env)["state"] == "sending"
    assert rows(env, mail_attempts)[0]["finished_at"] is None


def test_accepted_quit_cleanup_failure_is_not_retried(pending):
    result = SendResult(
        SendOutcome.ACCEPTED, SendStage.ACCEPTED, smtp_code=250, cleanup_failed=True
    )
    assert drain(pending, result)["accepted"] == 1
    assert rows(pending, mail_attempts)[0]["cleanup_failed"]
    assert drain(pending, at=ACTIVATED_AT + 5000)["attempted"] == 0


def test_backlog_more_than_one_drain_preserves_exact_members_and_payloads(pending):
    make_backlog(pending, count=12)
    frozen, members = rows(pending, mail_messages), rows(pending, mail_message_members)
    # Planning itself is bounded to5; add the remaining7 explicitly.
    while plan(pending, at=ACTIVATED_AT + 12)["planned"]:
        pass
    frozen, members = rows(pending, mail_messages), rows(pending, mail_message_members)
    assert len(frozen) == 12
    assert [drain(pending, at=ACTIVATED_AT + 20 + offset)["accepted"] for offset in range(3)] == [
        5,
        5,
        2,
    ]
    assert rows(pending, mail_messages) == frozen and rows(pending, mail_message_members) == members


def test_lowered_budget_rejects_regressed_clock_and_rolls_back(pending):
    drain(pending, RETRYABLE)
    before = rows(pending, mail_delivery)
    with pytest.raises(MailError, match="mail_clock_regressed"):
        recover_sending(
            pending.engine, SOURCE, at=ACTIVATED_AT + 19, options=DrainOptions(max_attempts=1)
        )
    assert rows(pending, mail_delivery) == before


def test_acceptance_keeps_historical_uncertain_attempt_diagnostics(pending):
    drain(pending, UNCERTAIN)
    drain(pending, at=ACTIVATED_AT + 1820)
    status = mail_status(pending.engine, SOURCE, at=ACTIVATED_AT + 1821)
    assert status["counts"]["accepted"] == 1
    assert status["uncertain_count"] == 0
    assert status["uncertain_attempt_count"] == 1
    assert len(rows(pending, mail_attempts)) == 2


@pytest.mark.parametrize(
    "values",
    [
        dict(finished_at=ACTIVATED_AT + 20),
        dict(
            finished_at=ACTIVATED_AT + 20,
            outcome="accepted",
            stage="accepted",
            scope="message",
            smtp_code=None,
        ),
        dict(
            finished_at=ACTIVATED_AT + 20,
            outcome="accepted",
            stage="accepted",
            scope="message",
            smtp_code=251,
        ),
        dict(
            finished_at=ACTIVATED_AT + 20,
            outcome="retryable",
            stage="connect",
            scope="message",
            error_code="arbitrary_reply",
        ),
        dict(
            finished_at=ACTIVATED_AT + 20,
            outcome="uncertain",
            stage="unknown",
            scope="message",
            error_code="process_interrupted",
        ),
        dict(
            finished_at=ACTIVATED_AT + 20,
            outcome="uncertain",
            stage="unknown",
            scope="message",
            error_code="process_interrupted",
            uncertain_until=ACTIVATED_AT + 1819,
        ),
        dict(
            finished_at=ACTIVATED_AT + 19,
            outcome="accepted",
            stage="accepted",
            scope="message",
            smtp_code=250,
        ),
    ],
)
def test_attempt_constraints_reject_incomplete_or_false_evidence(pending, values):
    identity = message(pending)["id"]
    register_attempt(pending.engine, SOURCE, identity, at=ACTIVATED_AT + 20, options=OPTIONS)
    with pytest.raises(sa.exc.IntegrityError), pending.engine.begin() as connection:
        connection.execute(
            mail_attempts.update().where(mail_attempts.c.mail_id == identity).values(**values)
        )
    assert rows(pending, mail_attempts)[0]["finished_at"] is None


def test_status_bounded_display_keeps_full_backlog_counts(pending):
    seal_copies(pending, 101)
    while plan(pending, at=ACTIVATED_AT + 12, max_messages=20)["planned"]:
        pass
    status = mail_status(pending.engine, SOURCE, at=ACTIVATED_AT + 20)
    assert status["counts"]["pending"] == status["due_count"] == 101
    assert len(status["messages"]) == 100 and status["messages_truncated"]
    assert status["unplanned_immediate"] == 0
