"""Real N1 evidence, SQLite transactions and frozen N2 messages, entirely offline.

The backlog helper duplicates already sealed, valid N1 event/decision records.
It avoids hundreds of redundant Parser calls; baseline, content and decision
provenance still originate in the real archive and N1 success transaction.
"""

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from email import policy
from email.parser import BytesParser

import pytest
from sqlalchemy.exc import IntegrityError
from test_notification_service import (
    ACTIVATED_AT,
    SOURCE,
    document,
    live,
    opportunity,
    rows,
)
from test_notification_service import (
    activated as activated,
)

from signalnest.mail.contracts import MailError, PlanOptions
from signalnest.mail.planning import (
    commit_plan_in_transaction,
    load_frozen_mail,
    plan_mail,
    prepare_plan,
    preview_mail,
    preview_plan,
)
from signalnest.notifications.contracts import Profile
from signalnest.schema import (
    documents,
    email_outbox,
    mail_message_members,
    mail_messages,
    mail_plan_errors,
    notice_versions,
    notification_channel_state,
    notification_decisions,
    notification_events,
    notification_policy_revisions,
)
from signalnest.storage import open_initialized_engine


def digest_due(env):
    return int(
        min(
            datetime.fromisoformat(row["context"]["next_digest_at"]).timestamp()
            for row in rows(env, notification_decisions)
        )
    )


def seal_copies(env, count, *, event_id=None):
    """Produce count equivalent eligible events with genuine sealed N1 fields."""
    originals = rows(env, notification_events)
    original = next(row for row in originals if event_id is None or row["id"] == event_id)
    selected = next(
        row
        for row in rows(env, notification_decisions)
        if row["id"] == original["selected_decision_id"]
    )
    intent = next(
        (row for row in rows(env, email_outbox) if row["event_id"] == original["id"]), None
    )
    identifiers = [original["id"]]
    start_seq = max(row["event_seq"] for row in originals)
    with env.engine.begin() as connection:
        for offset in range(1, count):
            values = original | dict(
                event_seq=start_seq + offset, selected_decision_id=None, outbox_id=None
            )
            del values["id"]
            copied_event = connection.execute(notification_events.insert().values(**values))
            identity = copied_event.inserted_primary_key[0]
            values = selected | dict(event_id=identity)
            del values["id"]
            copied_decision = connection.execute(notification_decisions.insert().values(**values))
            decision_id = copied_decision.inserted_primary_key[0]
            outbox_id = None
            if intent:
                values = intent | dict(
                    event_id=identity,
                    decision_id=decision_id,
                    delivery_key=f"sealed-copy-{identity}",
                )
                del values["id"]
                copied_intent = connection.execute(email_outbox.insert().values(**values))
                outbox_id = copied_intent.inserted_primary_key[0]
            connection.execute(
                notification_events.update()
                .where(notification_events.c.id == identity)
                .values(selected_decision_id=decision_id, outbox_id=outbox_id)
            )
            identifiers.append(identity)
    return identifiers


def message(env, index=0):
    return sorted(rows(env, mail_messages), key=lambda row: row["id"])[index]


def payload_text(row):
    return BytesParser(policy=policy.default).parsebytes(row["payload_bytes"]).get_content()


def plan(env, *, at=None, **limits):
    return plan_mail(
        env.engine,
        SOURCE,
        PlanOptions(**limits),
        at=digest_due(env) if at is None else at,
    )


def test_immediate_freezes_once_and_never_enters_digest(activated):
    env = activated
    live(env, opportunity())
    original_events = rows(env, notification_events)
    original_intents = rows(env, email_outbox)
    first = plan(env, at=ACTIVATED_AT + 10)
    frozen = message(env)
    members = rows(env, mail_message_members)
    assert first["planned"] == 1 and first["mail_ids"] == [frozen["id"]]
    assert frozen["kind"] == "immediate" and frozen["digest_slot"] is None
    assert frozen["immediate_intent_id"] == original_intents[0]["id"]
    assert [member["event_id"] for member in members] == [original_events[0]["id"]]
    assert frozen["payload_sha256"] == hashlib.sha256(frozen["payload_bytes"]).hexdigest()
    assert "国际交流项目报名通知" in payload_text(frozen)
    assert plan(env)["planned"] == 0
    assert rows(env, mail_messages) == [frozen]
    assert rows(env, mail_message_members) == members
    assert rows(env, email_outbox) == original_intents
    assert rows(env, notification_events) == original_events


def test_digest_uses_saved_due_and_waits_before_due(activated):
    env = activated
    live(env)
    due = digest_due(env)
    early = plan(env, at=due - 1)
    assert early["planned"] == 0
    assert early["deferred_digest"] == 1
    assert rows(env, mail_messages) == rows(env, mail_message_members) == []
    ready = plan(env, at=due)
    assert ready["planned"] == 1
    assert message(env)["kind"] == "digest"
    assert message(env)["digest_slot"] == due
    assert message(env)["date_at"] == due
    assert plan(env, at=due + 1)["planned"] == 0


def test_shutdown_backlog_keeps_event_time_and_current_elapsed_digest_slot(activated):
    env = activated
    live(env)
    event = rows(env, notification_events)[0]
    overdue_at = digest_due(env) + 6 * 86400 + 3600
    result = plan(env, at=overdue_at)
    frozen = message(env)
    assert result["planned"] == 1
    assert frozen["digest_slot"] == digest_due(env) + 6 * 86400
    assert frozen["date_at"] == frozen["digest_slot"]
    assert frozen["frozen_at"] == overdue_at
    text = payload_text(frozen)
    assert datetime.fromtimestamp(event["occurred_at"], UTC).isoformat(timespec="seconds") in text
    assert "2026-09-04" in text
    assert "积压补计划" in text


def test_preview_renders_without_allocating_or_recording_errors(activated):
    env = activated
    live(env, opportunity())
    before = {
        table.name: rows(env, table)
        for table in (notification_events, email_outbox, mail_messages, mail_message_members)
    }
    preview = preview_plan(env.engine, SOURCE, PlanOptions(), at=ACTIVATED_AT + 10)
    assert preview["preview_only"] is True
    assert len(preview["messages"]) == 1
    assert "国际交流项目报名通知" in str(preview["messages"])
    for table in (notification_events, email_outbox, mail_messages, mail_message_members):
        assert rows(env, table) == before[table.name]
    assert rows(env, mail_plan_errors) == []


def test_event_limit_splits_digest_and_resumes_exact_members(activated):
    env = activated
    live(env)
    expected = seal_copies(env, 8)
    first = plan(env, max_messages=2, max_events=3)
    assert first["planned"] == 2 and first["remaining_digest"] == 2
    assert [
        len([m for m in rows(env, mail_message_members) if m["mail_id"] == mail["id"]])
        for mail in rows(env, mail_messages)
    ] == [3, 3]
    second = plan(env, max_messages=2, max_events=3)
    assert second["planned"] == 1 and second["remaining_digest"] == 0
    assert [mail["part"] for mail in rows(env, mail_messages)] == [1, 2, 3]
    assert sorted(row["event_id"] for row in rows(env, mail_message_members)) == expected
    assert plan(env)["planned"] == 0


def test_large_backlog_exceeds_one_run_capacity_without_loss(activated):
    env = activated
    live(env)
    expected = seal_copies(env, 260)
    first = plan(env, max_bytes=1024 * 1024)
    assert first["planned"] == 5 and first["remaining_digest"] == 10
    assert len(rows(env, mail_message_members)) == 250
    assert plan(env, max_bytes=1024 * 1024)["planned"] == 1
    members = rows(env, mail_message_members)
    assert sorted(row["event_id"] for row in members) == expected
    assert [mail["part"] for mail in rows(env, mail_messages)] == [1, 2, 3, 4, 5, 6]
    assert all(mail["state"] == "pending" for mail in rows(env, mail_messages))
    assert plan(env)["planned"] == 0


def test_maximum_event_limit_splits_before_rendering_a_101_member_message(activated):
    env = activated
    live(env)
    expected = seal_copies(env, 101)
    result = plan(env, max_events=100, max_bytes=1024 * 1024)
    assert result["planned"] == 2
    assert sorted(row["event_id"] for row in rows(env, mail_message_members)) == expected
    assert [
        sum(member["mail_id"] == row["id"] for member in rows(env, mail_message_members))
        for row in rows(env, mail_messages)
    ] == [100, 1]


def test_full_mime_byte_limit_splits_digest(activated):
    env = activated
    live(env)
    seal_copies(env, 4)
    # MIME encoding and headers consume budget, not just UTF-8 body text.
    prepared = prepare_plan(
        env.engine, SOURCE, PlanOptions(max_messages=1, max_events=1), at=digest_due(env)
    )
    limit = len(prepared.messages[0].rendered.payload) + 100
    result = plan(env, max_messages=5, max_events=100, max_bytes=limit)
    assert result["planned"] == 4
    assert all(len(row["payload_bytes"]) <= limit for row in rows(env, mail_messages))
    assert all(row["max_bytes"] == limit for row in rows(env, mail_messages))


def test_oversized_item_blocks_finitely_and_can_retry_after_limit_change(activated):
    env = activated
    live(env, opportunity())
    original = rows(env, notification_events)[0]
    first = plan(env, at=ACTIVATED_AT + 10, max_bytes=1024)
    assert first["planned"] == 0 and len(first["blocked"]) == 1
    assert first["blocked"] == [{"event_id": original["id"], "error_code": "mail_item_too_large"}]
    assert rows(env, mail_messages) == rows(env, mail_message_members) == []
    errors = rows(env, mail_plan_errors)
    assert errors[0]["event_id"] == original["id"]
    assert errors[0]["error_code"] == "mail_item_too_large"
    assert plan(env, at=ACTIVATED_AT + 20, max_bytes=1024)["planned"] == 0
    assert rows(env, mail_plan_errors) == errors
    assert plan(env, at=ACTIVATED_AT + 30, max_bytes=128 * 1024)["planned"] == 1
    assert rows(env, mail_plan_errors) == []
    assert rows(env, notification_events)[0] == original


def test_frozen_bytes_members_addresses_and_reasons_survive_profile_and_content_change(activated):
    env = activated
    live(env, opportunity())
    plan(env, at=ACTIVATED_AT + 10)
    frozen = message(env)
    frozen_members = rows(env, mail_message_members)
    preview = preview_mail(env.engine, SOURCE, frozen["id"])
    loaded = load_frozen_mail(env.engine, SOURCE, frozen["id"])
    original_version = document(env)["current_version_id"]
    live(env, opportunity().replace(b"18:00", b"17:30"), at=ACTIVATED_AT + 15)
    assert document(env)["current_version_id"] != original_version
    with env.engine.begin() as connection:
        connection.execute(
            notification_channel_state.update().values(
                sender="new-sender@example.net", recipient="new-recipient@example.net"
            )
        )
        # Frozen-mail reads must not evaluate the now changed active policy.
        policy_row = rows(env, notification_policy_revisions)[0]
        changed = dict(policy_row["manifest"])
        changed["profile"] = Profile(interest_topics=("competition",)).model_dump(mode="json")
        connection.execute(notification_policy_revisions.update().values(manifest=changed))
        connection.execute(documents.update().values(discovered_title="后续标题变化"))
    assert load_frozen_mail(env.engine, SOURCE, frozen["id"]) == loaded
    assert preview_mail(env.engine, SOURCE, frozen["id"]) == preview
    assert rows(env, mail_messages) == [frozen]
    assert rows(env, mail_message_members) == frozen_members
    assert frozen["sender"] == "sender@example.org"
    assert frozen["recipient"] == "recipient@example.org"


def test_reopened_sqlite_retains_exact_frozen_bytes_and_allocations(activated):
    env = activated
    live(env, opportunity())
    plan(env, at=ACTIVATED_AT + 10)
    frozen = message(env)
    expected = load_frozen_mail(env.engine, SOURCE, frozen["id"])
    env.engine.dispose()
    reopened = open_initialized_engine(env.settings.database)
    try:
        assert load_frozen_mail(reopened, SOURCE, frozen["id"]) == expected
        assert plan_mail(reopened, SOURCE, PlanOptions(), at=digest_due(env))["planned"] == 0
    finally:
        reopened.dispose()


def test_already_allocated_immediate_cannot_be_allocated_again_after_route_change(activated):
    env = activated
    live(env, opportunity())
    plan(env, at=ACTIVATED_AT + 10)
    frozen = message(env)
    old_preview = preview_mail(env.engine, SOURCE, frozen["id"])
    selected = rows(env, notification_decisions)[0]
    values = selected | dict(
        evaluation_key="explicit-later-route",
        decision=selected["decision"] | dict(effective_route="digest"),
    )
    del values["id"]
    with env.engine.begin() as connection:
        later = connection.execute(notification_decisions.insert().values(**values))
        connection.execute(
            notification_events.update().values(
                selected_decision_id=later.inserted_primary_key[0],
                effective_route="digest",
                outbox_id=None,
            )
        )
    assert plan(env)["planned"] == 0
    assert rows(env, mail_messages) == [frozen]
    assert len(rows(env, mail_message_members)) == 1
    assert preview_mail(env.engine, SOURCE, frozen["id"]) == old_preview


@pytest.mark.parametrize("mutation", ["route", "address", "decision", "version"])
def test_stale_plan_is_rejected_without_partial_freeze(activated, mutation):
    env = activated
    live(env, opportunity())
    prepared = prepare_plan(env.engine, SOURCE, PlanOptions(), at=ACTIVATED_AT + 10)
    event_row = rows(env, notification_events)[0]
    with env.engine.begin() as connection:
        if mutation == "route":
            connection.execute(
                notification_events.update().values(effective_route="digest", outbox_id=None)
            )
        elif mutation == "address":
            connection.execute(
                notification_channel_state.update().values(recipient="changed@example.net")
            )
        elif mutation == "decision":
            original = rows(env, notification_decisions)[0]
            values = original | dict(evaluation_key="changed-selected")
            del values["id"]
            new = connection.execute(notification_decisions.insert().values(**values))
            connection.execute(
                notification_events.update().values(
                    selected_decision_id=new.inserted_primary_key[0]
                )
            )
        else:
            connection.execute(
                notice_versions.update()
                .where(notice_versions.c.id == event_row["version_id"])
                .values(content_sha256="a" * 64)
            )
    with pytest.raises(MailError) as error:
        with env.engine.begin() as connection:
            commit_plan_in_transaction(connection, prepared)
    assert error.value.code == "mail_prepare_stale"
    assert rows(env, mail_messages) == rows(env, mail_message_members) == []


def test_two_prepared_plans_cannot_allocate_the_same_member(activated):
    env = activated
    live(env)
    first = prepare_plan(env.engine, SOURCE, PlanOptions(), at=digest_due(env))
    second = prepare_plan(env.engine, SOURCE, PlanOptions(), at=digest_due(env))
    with env.engine.begin() as connection:
        identifiers = commit_plan_in_transaction(connection, first)
    with pytest.raises(MailError) as error:
        with env.engine.begin() as connection:
            commit_plan_in_transaction(connection, second)
    assert error.value.code == "mail_prepare_stale"
    assert len(identifiers) == len(rows(env, mail_messages)) == 1
    assert len(rows(env, mail_message_members)) == 1


@pytest.mark.parametrize("failure_table", ["mail_messages", "mail_message_members"])
def test_database_failure_rolls_back_all_frozen_messages_and_members(activated, failure_table):
    env = activated
    live(env)
    seal_copies(env, 3)
    with env.engine.begin() as connection:
        connection.exec_driver_sql(
            f"CREATE TRIGGER mail_test_fault BEFORE INSERT ON {failure_table} "
            f"WHEN (SELECT COUNT(*) FROM {failure_table}) >= 2 "
            "BEGIN SELECT RAISE(ABORT, 'injected freeze failure'); END"
        )
    with pytest.raises(MailError) as error:
        plan(env, max_events=1)
    assert error.value.code == "mail_database_write_failed"
    assert rows(env, mail_messages) == rows(env, mail_message_members) == []
    assert rows(env, mail_plan_errors) == []
    assert len(rows(env, notification_events)) == 3
    with env.engine.begin() as connection:
        connection.exec_driver_sql("DROP TRIGGER mail_test_fault")
    assert plan(env, max_events=1)["planned"] == 3


@pytest.mark.parametrize("mutation", ["payload", "snapshot", "missing_member"])
def test_frozen_loader_rejects_digest_or_membership_tampering(activated, mutation):
    env = activated
    live(env, opportunity())
    plan(env, at=ACTIVATED_AT + 10)
    frozen = message(env)
    with env.engine.begin() as connection:
        if mutation == "payload":
            connection.execute(mail_messages.update().values(payload_bytes=b"corrupted"))
        elif mutation == "snapshot":
            connection.execute(mail_message_members.update().values(snapshot={"corrupted": True}))
        else:
            connection.execute(mail_message_members.delete())
    expected_code = "mail_payload_corrupt" if mutation == "payload" else "mail_members_corrupt"
    with pytest.raises(MailError) as error:
        load_frozen_mail(env.engine, SOURCE, frozen["id"])
    assert error.value.code == expected_code
    with pytest.raises(MailError) as error:
        preview_mail(env.engine, SOURCE, frozen["id"])
    assert error.value.code == expected_code


def test_wrong_source_cannot_plan_or_read_another_installation(activated):
    env = activated
    live(env, opportunity())
    plan(env, at=ACTIVATED_AT + 10)
    frozen = message(env)
    with pytest.raises(MailError) as error:
        plan_mail(env.engine, "other-source", PlanOptions(), at=ACTIVATED_AT + 11)
    assert error.value.code == "mail_source_mismatch"
    with pytest.raises(MailError) as error:
        load_frozen_mail(env.engine, "other-source", frozen["id"])
    assert error.value.code == "mail_source_mismatch"


def mixed_queues(env):
    live(env, opportunity())
    first = rows(env, notification_events)[0]
    with env.engine.begin() as connection:
        connection.execute(
            notification_channel_state.update().values(notification_mode="digest_only")
        )
    live(
        env,
        opportunity().replace("交流项目".encode(), "海外交流项目".encode()),
        at=ACTIVATED_AT + 5,
    )
    second = rows(env, notification_events)[1]
    assert first["effective_route"] == "immediate"
    assert second["effective_route"] == "digest"
    return first, second


@pytest.mark.parametrize("capacity", [1, 2, 5])
def test_due_digest_has_reserved_capacity_beside_immediate_backlog(activated, capacity):
    env = activated
    immediate, digest = mixed_queues(env)
    seal_copies(env, 8, event_id=immediate["id"])
    seal_copies(env, 2, event_id=digest["id"])
    result = plan(env, max_messages=capacity)
    kinds = [row["kind"] for row in rows(env, mail_messages)]
    assert result["planned"] == capacity
    assert kinds.count("digest") == 1
    assert kinds.count("immediate") == capacity - 1
    assert len({row["event_id"] for row in rows(env, mail_message_members)}) == capacity + 1
    assert result["remaining_immediate"] == 8 - (capacity - 1)
    assert result["remaining_digest"] == 0


def test_pending_review_is_shown_in_a_separate_digest_section(activated):
    env = activated
    with env.engine.begin() as connection:
        connection.execute(
            notification_channel_state.update().values(notification_mode="digest_only")
        )
    content = opportunity().replace(
        "面向武汉大学本科生".encode(),
        "面向武汉大学本科生，要求雅思成绩6.5分".encode(),
    )
    live(env, content)
    decision = rows(env, notification_decisions)[0]["decision"]
    assert decision["needs_review"]
    assert decision["effective_route"] == "digest"
    assert plan(env)["planned"] == 1
    assert "=== 待核对信息 ===" in payload_text(message(env))
    assert "提醒不代表已经符合报名条件" in payload_text(message(env))


def test_digest_part_changed_since_preparation_rejects_stale_allocation(activated):
    env = activated
    live(env)
    identifiers = seal_copies(env, 2)
    first = prepare_plan(
        env.engine, SOURCE, PlanOptions(max_messages=1, max_events=1), at=digest_due(env)
    )
    second = prepare_plan(
        env.engine, SOURCE, PlanOptions(max_messages=1, max_events=1), at=digest_due(env)
    )
    with env.engine.begin() as connection:
        commit_plan_in_transaction(connection, first)
    with pytest.raises(MailError) as error:
        with env.engine.begin() as connection:
            commit_plan_in_transaction(connection, second)
    assert error.value.code == "mail_prepare_stale"
    assert [member["event_id"] for member in rows(env, mail_message_members)] == identifiers[:1]
    assert plan(env, max_messages=1, max_events=1)["planned"] == 1
    assert [row["part"] for row in rows(env, mail_messages)] == [1, 2]


def test_oversized_first_item_does_not_block_later_events(activated):
    env = activated
    live(env)
    identifiers = seal_copies(env, 2)
    prepared = prepare_plan(
        env.engine, SOURCE, PlanOptions(max_messages=1, max_events=1), at=digest_due(env)
    )
    normal_size = len(prepared.messages[0].rendered.payload)
    selected = rows(env, notification_decisions)[0]
    changed = selected["decision"] | dict(reasons=["需核对条件" * 55 for _ in range(8)])
    with env.engine.begin() as connection:
        connection.execute(
            notification_decisions.update()
            .where(notification_decisions.c.id == selected["id"])
            .values(decision=changed)
        )
    result = plan(env, max_bytes=normal_size + 500)
    assert result["planned"] == 1
    assert result["blocked"] == [{"event_id": identifiers[0], "error_code": "mail_item_too_large"}]
    assert [row["event_id"] for row in rows(env, mail_message_members)] == identifiers[1:]
    assert result["remaining_digest"] == 1


def test_plan_errors_and_later_good_message_roll_back_together(activated):
    env = activated
    live(env)
    seal_copies(env, 2)
    prepared = prepare_plan(
        env.engine, SOURCE, PlanOptions(max_messages=1, max_events=1), at=digest_due(env)
    )
    normal_size = len(prepared.messages[0].rendered.payload)
    selected = rows(env, notification_decisions)[0]
    changed = selected["decision"] | dict(reasons=["需核对条件" * 55 for _ in range(8)])
    with env.engine.begin() as connection:
        connection.execute(
            notification_decisions.update()
            .where(notification_decisions.c.id == selected["id"])
            .values(decision=changed)
        )
        connection.exec_driver_sql(
            "CREATE TRIGGER mail_error_fault BEFORE INSERT ON mail_plan_errors "
            "BEGIN SELECT RAISE(ABORT, 'injected plan error save failure'); END"
        )
    with pytest.raises(MailError) as error:
        plan(env, max_bytes=normal_size + 500)
    assert error.value.code == "mail_database_write_failed"
    assert rows(env, mail_messages) == rows(env, mail_message_members) == []
    assert rows(env, mail_plan_errors) == []
    assert len(rows(env, notification_events)) == 2


@pytest.mark.parametrize(
    ("mutation", "constraint"),
    [
        ("same_event_another_mail", "SQLITE_CONSTRAINT_PRIMARYKEY"),
        ("same_mail_position", "SQLITE_CONSTRAINT_UNIQUE"),
        ("another_events_decision", "SQLITE_CONSTRAINT_FOREIGNKEY"),
    ],
)
def test_sqlite_rejects_duplicate_or_wrongly_owned_members(activated, mutation, constraint):
    env = activated
    live(env)
    identities = seal_copies(env, 3)
    assert plan(env, max_messages=2, max_events=1)["planned"] == 2
    original_members = rows(env, mail_message_members)
    original_messages = rows(env, mail_messages)
    values = original_members[0] | dict(position=100)
    if mutation == "same_event_another_mail":
        values["mail_id"] = original_messages[1]["id"]
    else:
        third = next(
            event for event in rows(env, notification_events) if event["id"] == identities[2]
        )
        values["event_id"] = third["id"]
        if mutation == "same_mail_position":
            values.update(position=0, decision_id=third["selected_decision_id"])
        # The ownership case leaves the first event's decision on the third event.
    with pytest.raises(IntegrityError) as error:
        with env.engine.begin() as connection:
            connection.execute(mail_message_members.insert().values(**values))
    assert error.value.orig.sqlite_errorname == constraint
    assert rows(env, mail_message_members) == original_members
    assert rows(env, mail_messages) == original_messages


@pytest.mark.parametrize("kind", ["immediate", "digest"])
def test_sqlite_rejects_a_second_allocation_of_intent_or_digest_part(activated, kind):
    env = activated
    live(env, opportunity() if kind == "immediate" else None)
    assert plan(env)["planned"] == 1
    original = message(env)
    values = original | dict(
        delivery_key=hashlib.sha256(b"distinct-test-delivery-key").hexdigest(),
        message_id="<signalnest.distinct-test-message@example.org>",
    )
    del values["id"]
    # Distinct mail identities isolate the actual intent or digest-part constraint.
    with pytest.raises(IntegrityError) as error:
        with env.engine.begin() as connection:
            connection.execute(mail_messages.insert().values(**values))
    assert error.value.orig.sqlite_errorname == "SQLITE_CONSTRAINT_UNIQUE"
    assert rows(env, mail_messages) == [original]
    assert len(rows(env, mail_message_members)) == 1


@pytest.mark.parametrize(
    "mutation", ["other_member", "bad_json", "sender", "recipient", "intent", "date", "message_id"]
)
def test_commit_rejects_modified_prepared_allocation_and_envelope(activated, mutation):
    env = activated
    live(env, opportunity())
    seal_copies(env, 2)
    prepared = prepare_plan(env.engine, SOURCE, PlanOptions(), at=ACTIVATED_AT + 10)
    first, second = prepared.messages
    if mutation == "other_member":
        # This other event and decision are valid FK targets, but were not the
        # evidence selected for this message. SQL constraints alone cannot tell.
        first = replace(first, members_json=second.members_json)
    elif mutation == "bad_json":
        first = replace(first, members_json="invalid private prepared JSON")
    elif mutation == "intent":
        first = replace(first, immediate_intent_id=second.immediate_intent_id)
    else:
        updates = {
            "sender": {"sender": "other@example.org"},
            "recipient": {"recipient": "other@example.org"},
            "date": {"date_at": first.rendered.date_at + 1},
            "message_id": {"message_id": "<other@example.org>"},
        }
        first = replace(first, rendered=replace(first.rendered, **updates[mutation]))
    modified = replace(prepared, messages=(first,))
    with pytest.raises(MailError) as error:
        with env.engine.begin() as connection:
            commit_plan_in_transaction(connection, modified)
    assert error.value.code == "mail_prepared_payload_invalid"
    assert "private" not in str(error.value)
    assert rows(env, mail_messages) == rows(env, mail_message_members) == []
    assert plan(env, at=ACTIVATED_AT + 10)["planned"] == 2


def test_commit_rejects_changed_manifest_reason_without_freezing(activated):
    env = activated
    live(env, opportunity())
    prepared = prepare_plan(env.engine, SOURCE, PlanOptions(), at=ACTIVATED_AT + 10)
    first = prepared.messages[0]
    manifest = json.loads(first.members_json)
    manifest[0]["decision"]["reasons"] = ["substituted reason"]
    modified = replace(prepared, messages=(replace(first, members_json=json.dumps(manifest)),))
    with pytest.raises(MailError) as error:
        with env.engine.begin() as connection:
            commit_plan_in_transaction(connection, modified)
    assert error.value.code == "mail_prepared_payload_invalid"
    assert rows(env, mail_messages) == rows(env, mail_message_members) == []


def test_preview_and_frozen_readback_have_identical_logical_text_and_bytes(activated):
    env = activated
    live(env, opportunity())
    at = ACTIVATED_AT + 10
    preview = preview_plan(env.engine, SOURCE, PlanOptions(), at=at)["messages"][0]
    result = plan(env, at=at)
    saved = preview_mail(env.engine, SOURCE, result["mail_ids"][0])
    assert saved["body_text"] == preview["body_text"]
    assert "\r" not in saved["body_text"]
    assert saved["payload_sha256"] == preview["payload_sha256"]
    assert saved["byte_count"] == preview["byte_count"]
    assert b"\r\n" in load_frozen_mail(env.engine, SOURCE, result["mail_ids"][0]).rendered.payload
