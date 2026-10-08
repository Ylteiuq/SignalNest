"""Bounded policy maintenance over genuine live events and SQLite transactions."""

import json
from dataclasses import replace
from datetime import datetime

import pytest
from sqlalchemy.exc import IntegrityError
from test_notification_service import (
    ACTIVATED_AT,
    SOURCE,
    USER_PROFILE,
    document,
    live,
    live_discover,
    new_listing,
    opportunity,
    rows,
)
from test_notification_service import activated as activated

from signalnest.errors import IngestError
from signalnest.notifications.contracts import Profile, canonical_json, canonical_sha256
from signalnest.notifications.maintenance import (
    prepare_reevaluation,
    preview_policy_update,
    reevaluate_events,
    register_reevaluation_in_transaction,
    update_policy,
)
from signalnest.schema import (
    email_outbox,
    notification_channel_state,
    notification_decisions,
    notification_events,
    notification_operations,
    notification_policy_revisions,
)
from signalnest.storage import open_initialized_engine

SAVE_ONLY = Profile(
    institution="whu",
    study_level="undergraduate",
    interest_topics=("exchange",),
    store_only_topics=("exchange",),
)
AT = ACTIVATED_AT + 10


def none_event(env):
    update_policy(env.engine, SOURCE, SAVE_ONLY, "save-only", at=ACTIVATED_AT + 1)
    live(env, opportunity())
    event = rows(env, notification_events)[0]
    assert event["effective_route"] == "none"
    update_policy(env.engine, SOURCE, USER_PROFILE, "interested", at=AT)
    return event["id"]


def register(env, event_id, operation_id="resume-fixed"):
    prepared = prepare_reevaluation(env.engine, SOURCE, operation_id, (event_id,), at=AT)
    with env.engine.begin() as connection:
        register_reevaluation_in_transaction(connection, prepared)
    return prepared


def test_policy_preview_changes_nothing_and_update_does_not_make_mail(activated):
    env = activated
    before = rows(env, notification_channel_state)
    preview = preview_policy_update(env.engine, SOURCE, SAVE_ONLY, at=AT)
    assert preview["changed"] and not preview["creates_historical_mail"]
    assert rows(env, notification_channel_state) == before
    assert rows(env, notification_operations) == []
    result = update_policy(env.engine, SOURCE, SAVE_ONLY, "change", at=AT)
    assert result["complete"] and result["results"][0]["status"] == "applied"
    assert len(rows(env, notification_policy_revisions)) == 2
    assert rows(env, notification_events) == rows(env, email_outbox) == []


def test_policy_operation_replay_cannot_revert_later_policy(activated):
    env = activated
    first = update_policy(env.engine, SOURCE, SAVE_ONLY, "change", at=AT)
    update_policy(env.engine, SOURCE, USER_PROFILE, "change-back", at=AT + 1)
    current = rows(env, notification_channel_state)[0]["policy_revision_id"]
    repeated = update_policy(env.engine, SOURCE, SAVE_ONLY, "change", at=AT)
    assert repeated == first | dict(reused=True)
    assert rows(env, notification_channel_state)[0]["policy_revision_id"] == current


@pytest.mark.parametrize("change", ["profile", "at", "source"])
def test_policy_operation_parameter_mismatch_has_no_side_effect(activated, change):
    env = activated
    update_policy(env.engine, SOURCE, SAVE_ONLY, "change", at=AT)
    before = rows(env, notification_operations)
    with pytest.raises(IngestError, match="operation_parameters_mismatch"):
        update_policy(
            env.engine,
            "different" if change == "source" else SOURCE,
            USER_PROFILE if change == "profile" else SAVE_ONLY,
            "change",
            at=AT + 1 if change == "at" else AT,
        )
    assert rows(env, notification_operations) == before


def test_policy_write_failure_rolls_back_manifest_channel_and_operation(activated):
    env = activated
    before = rows(env, notification_channel_state)
    with env.engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TRIGGER reject_operation BEFORE INSERT ON notification_operations "
            "BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
    with pytest.raises(IngestError, match="notification_database_write_failed"):
        update_policy(env.engine, SOURCE, SAVE_ONLY, "change", at=AT)
    assert rows(env, notification_channel_state) == before
    assert len(rows(env, notification_policy_revisions)) == 1
    assert rows(env, notification_operations) == []


def test_apply_registers_new_decision_without_new_content_event(activated):
    env = activated
    event_id = none_event(env)
    doc_before = document(env)
    original = rows(env, notification_events)[0]
    result = reevaluate_events(env.engine, SOURCE, "promote", event_ids=(event_id,), at=AT)
    event = rows(env, notification_events)[0]
    assert result["complete"] and result["results"][0]["status"] == "applied"
    assert event["id"] == original["id"] and event["event_seq"] == original["event_seq"]
    assert event["effective_route"] == "immediate"
    assert event["delivery_intent_registered_at"] == AT
    assert len(rows(env, notification_decisions)) == 2
    assert len(rows(env, email_outbox)) == 1
    assert document(env) == doc_before
    selected = rows(env, notification_decisions)[1]
    assert selected["evaluation_key"] == "operation:promote"
    assert selected["evaluated_at"] == AT
    assert "body_text" not in selected["facts"]


def test_preview_produces_reason_and_never_registers_mail(activated):
    env = activated
    event_id = none_event(env)
    before = rows(env, notification_events)
    result = reevaluate_events(
        env.engine, SOURCE, "preview", event_ids=(event_id,), at=AT, preview=True
    )
    assert result["results"][0]["decision"]["effective_route"] == "immediate"
    assert result["results"][0]["decision"]["reason_codes"]
    assert rows(env, notification_events) == before
    assert rows(env, email_outbox) == []
    assert not any(row["id"] == "preview" for row in rows(env, notification_operations))


@pytest.mark.parametrize("content", [None, "urgent"])
def test_eligible_digest_or_immediate_is_locked_even_without_frozen_mail(activated, content):
    env = activated
    live(env, opportunity() if content else None)
    event = rows(env, notification_events)[0]
    assert event["effective_route"] == ("immediate" if content else "digest")
    if content is None:
        assert event["outbox_id"] is None
    with pytest.raises(IngestError, match="event_route_locked"):
        reevaluate_events(env.engine, SOURCE, "blocked", event_ids=(event["id"],), at=AT)
    preview = reevaluate_events(
        env.engine, SOURCE, "view", event_ids=(event["id"],), at=AT, preview=True
    )
    assert preview["preview"]
    assert rows(env, notification_events) == [event]


def test_completed_resume_precedes_new_eligibility_gate(activated):
    env = activated
    event_id = none_event(env)
    first = reevaluate_events(env.engine, SOURCE, "promote", event_ids=(event_id,), at=AT)
    count = len(rows(env, notification_decisions))
    assert reevaluate_events(env.engine, SOURCE, "promote") == first | dict(reused=True)
    assert reevaluate_events(
        env.engine, SOURCE, "promote", event_ids=(event_id, event_id), at=AT
    ) == first | dict(reused=True)
    assert len(rows(env, notification_decisions)) == count
    assert len(rows(env, email_outbox)) == 1


def test_resume_reuses_frozen_clock_decision_and_digest_context(activated, monkeypatch):
    env = activated
    event_id = none_event(env)
    register(env, event_id)
    snapshot = rows(env, notification_operations)[-1]["snapshot"]["payload"]["items"][0]

    def forbidden(*args, **kwargs):
        raise AssertionError("resume must not re-evaluate rules or sample a new clock")

    monkeypatch.setattr("signalnest.notifications.maintenance.decide", forbidden)
    monkeypatch.setattr("signalnest.notifications.maintenance.extract_facts", forbidden)
    result = reevaluate_events(env.engine, SOURCE, "resume-fixed")
    assert result["results"][0]["effective_route"] == "immediate"
    saved = rows(env, notification_decisions)[-1]
    assert saved["decision"] == snapshot["decision"]
    assert saved["context"] == snapshot["context"]
    assert datetime.fromisoformat(saved["decision"]["evaluated_at"]).timestamp() == AT


@pytest.mark.parametrize("change", ["ids", "time", "policy", "calendar"])
def test_explicit_operation_replay_rejects_changed_parameters(activated, change):
    env = activated
    event_id = none_event(env)
    register(env, event_id)
    if change == "policy":
        update_policy(env.engine, SOURCE, SAVE_ONLY, "again", at=AT + 1)
    if change == "calendar":
        with env.engine.begin() as connection:
            connection.execute(notification_channel_state.update().values(digest_hour=8))
    decisions_before = rows(env, notification_decisions)
    with pytest.raises(IngestError, match="operation_parameters_mismatch"):
        reevaluate_events(
            env.engine,
            SOURCE,
            "resume-fixed",
            event_ids=(event_id + 1,) if change == "ids" else (event_id,),
            at=AT + 1 if change == "time" else AT,
        )
    assert rows(env, notification_decisions) == decisions_before
    assert rows(env, email_outbox) == []


@pytest.mark.parametrize("change", ["policy", "calendar", "body"])
def test_pending_resume_marks_stale_without_replacing_input(activated, change):
    env = activated
    event_id = none_event(env)
    register(env, event_id)
    if change == "policy":
        update_policy(env.engine, SOURCE, SAVE_ONLY, "again", at=AT + 1)
    elif change == "calendar":
        with env.engine.begin() as connection:
            connection.execute(notification_channel_state.update().values(digest_hour=8))
    else:
        # An actual successful new live response advances the observation evidence.
        live(env, opportunity(), at=AT + 2)
    before = rows(env, notification_decisions)
    result = reevaluate_events(env.engine, SOURCE, "resume-fixed")
    expected = "stale_input" if change == "body" else "stale_context"
    assert result["results"] == [dict(event_id=event_id, status=expected)]
    assert result["complete"]
    assert rows(env, notification_decisions) == before
    assert rows(env, email_outbox) == []
    assert reevaluate_events(env.engine, SOURCE, "resume-fixed")["results"] == result["results"]


def test_send_pause_does_not_invalidate_rules_or_resume(activated):
    env = activated
    event_id = none_event(env)
    register(env, event_id)
    with env.engine.begin() as connection:
        connection.execute(notification_channel_state.update().values(paused=True))
    result = reevaluate_events(env.engine, SOURCE, "resume-fixed")
    assert result["results"][0]["status"] == "applied"


@pytest.mark.parametrize(
    "table", ["notification_decisions", "email_outbox", "notification_operations"]
)
def test_member_write_failure_rolls_back_entire_decision_route_intent_progress(activated, table):
    env = activated
    event_id = none_event(env)
    register(env, event_id)
    before = rows(env, notification_events)
    selected = rows(env, notification_decisions)
    verb = "UPDATE" if table == "notification_operations" else "INSERT"
    with env.engine.begin() as connection:
        connection.exec_driver_sql(
            f"CREATE TRIGGER reject_member BEFORE {verb} ON {table} "
            "BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
    with pytest.raises(IngestError, match="notification_database_write_failed"):
        reevaluate_events(env.engine, SOURCE, "resume-fixed")
    assert rows(env, notification_events) == before
    assert rows(env, notification_decisions) == selected
    assert rows(env, email_outbox) == []
    operation = next(
        row for row in rows(env, notification_operations) if row["id"] == "resume-fixed"
    )
    assert operation["results"] == [] and operation["finished_at"] is None
    with env.engine.begin() as connection:
        connection.exec_driver_sql("DROP TRIGGER reject_member")
    assert reevaluate_events(env.engine, SOURCE, "resume-fixed")["complete"]
    assert len(rows(env, email_outbox)) == 1


def test_reopened_database_can_resume_exact_operation(activated):
    env = activated
    event_id = none_event(env)
    register(env, event_id)
    env.engine.dispose()
    env.engine = open_initialized_engine(env.settings.database)
    result = reevaluate_events(env.engine, SOURCE, "resume-fixed")
    assert result["complete"] and result["evaluated_at"] == AT


def test_prepared_registration_rejects_stale_event_before_operation_exists(activated):
    env = activated
    event_id = none_event(env)
    prepared = prepare_reevaluation(env.engine, SOURCE, "stale", (event_id,), at=AT)
    live(env, opportunity(), at=AT + 2)
    with (
        pytest.raises(IngestError, match="reevaluation_stale_input"),
        env.engine.begin() as connection,
    ):
        register_reevaluation_in_transaction(connection, prepared)
    assert not any(row["id"] == "stale" for row in rows(env, notification_operations))


def test_corrupted_content_digest_fails_without_operation(activated):
    env = activated
    event_id = none_event(env)
    from signalnest.schema import notice_versions

    with env.engine.begin() as connection:
        connection.execute(notice_versions.update().values(content_sha256="0" * 64))
    with pytest.raises(IngestError, match="version_digest"):
        reevaluate_events(env.engine, SOURCE, "bad", event_ids=(event_id,), at=AT)
    assert not any(row["id"] == "bad" for row in rows(env, notification_operations))


@pytest.mark.parametrize("ids", [(), (True,), (-1,), (2**63,), (1,) * 101, "1"])
def test_invalid_bounded_selection_is_rejected(activated, ids):
    with pytest.raises(IngestError, match="event_selection_invalid"):
        reevaluate_events(activated.engine, SOURCE, "bad", event_ids=ids, at=AT)


@pytest.mark.parametrize("operation_id", ["", "a" * 65, "bad/id", "bad\nID"])
def test_invalid_operation_id_is_rejected(activated, operation_id):
    with pytest.raises(IngestError, match="operation_id_invalid"):
        reevaluate_events(activated.engine, SOURCE, operation_id)


@pytest.mark.parametrize("kwargs", [dict(at=AT), dict(event_ids=(1,)), dict(preview=True)])
def test_half_create_parameters_and_id_only_preview_are_rejected(activated, kwargs):
    with pytest.raises(IngestError, match="operation_parameters_invalid"):
        reevaluate_events(activated.engine, SOURCE, "bad", **kwargs)


def test_unknown_operation_resume_is_clear(activated):
    with pytest.raises(IngestError, match="operation_missing"):
        reevaluate_events(activated.engine, SOURCE, "missing")


def test_operation_id_namespace_is_shared_across_kinds(activated):
    update_policy(activated.engine, SOURCE, SAVE_ONLY, "same", at=AT)
    with pytest.raises(IngestError, match="operation_parameters_mismatch"):
        reevaluate_events(activated.engine, SOURCE, "same")


def test_operation_snapshot_corruption_is_explicit(activated):
    env = activated
    event_id = none_event(env)
    register(env, event_id)
    with env.engine.begin() as connection:
        connection.execute(
            notification_operations.update()
            .where(notification_operations.c.id == "resume-fixed")
            .values(snapshot=dict(payload=dict(items=[]), sha256="0" * 64))
        )
    with pytest.raises(IngestError, match="operation_invalid"):
        reevaluate_events(env.engine, SOURCE, "resume-fixed")


def test_operation_completion_and_results_must_commit_together(activated):
    env = activated
    event_id = none_event(env)
    prepared = prepare_reevaluation(env.engine, SOURCE, "bad-time", (event_id,), at=AT)
    with pytest.raises(IntegrityError), env.engine.begin() as connection:
        register_reevaluation_in_transaction(connection, prepared)
        connection.execute(
            notification_operations.update()
            .where(notification_operations.c.id == "bad-time")
            .values(finished_at=AT - 1)
        )
    assert not any(row["id"] == "bad-time" for row in rows(env, notification_operations))


def test_event_set_is_canonical_in_preparation(activated):
    env = activated
    event_id = none_event(env)
    first = prepare_reevaluation(env.engine, SOURCE, "first", (event_id,), at=AT)
    repeated = prepare_reevaluation(env.engine, SOURCE, "second", (event_id, event_id), at=AT)
    assert replace(first, operation_id="second") == repeated


def test_same_policy_operation_called_initial_does_not_collide_with_n1(activated):
    env = activated
    event_id = none_event(env)
    # A fresh evaluation can also stay on none; it is still auditable evidence.
    update_policy(env.engine, SOURCE, SAVE_ONLY, "keep-saving", at=AT)
    result = reevaluate_events(env.engine, SOURCE, "initial", event_ids=(event_id,), at=AT)
    assert result["complete"]
    assert rows(env, notification_decisions)[-1]["evaluation_key"] == "operation:initial"


def test_old_expired_event_not_apply_candidate_but_can_be_previewed(activated):
    env = activated
    event_id = none_event(env)
    expired = AT + 8 * 86400
    with pytest.raises(IngestError, match="event_not_candidate"):
        reevaluate_events(env.engine, SOURCE, "expired", event_ids=(event_id,), at=expired)
    result = reevaluate_events(
        env.engine, SOURCE, "view-old", event_ids=(event_id,), at=expired, preview=True
    )
    assert result["results"][0]["decision"]["effective_route"] == "none"


def test_same_policy_fresh_clock_can_record_recent_now_expired_decision(activated):
    env = activated
    event_id = none_event(env)
    result = reevaluate_events(
        env.engine, SOURCE, "recent-but-closed", event_ids=(event_id,), at=AT + 86400
    )
    assert result["complete"]
    decision = rows(env, notification_decisions)[-1]["decision"]
    assert decision["time_status"] == "closed" and decision["effective_route"] == "none"


def test_multiple_members_resume_keeps_committed_member_and_frozen_selection(activated):
    env = activated
    first = none_event(env)
    update_policy(env.engine, SOURCE, SAVE_ONLY, "saving-again", at=AT + 1)
    live_discover(env, new_listing(), at=AT + 2)
    live(
        env,
        opportunity(),
        at=AT + 4,
        url="https://uc.whu.edu.cn/info/1517/999999.htm",
        identity="1517:999999",
    )
    second = rows(env, notification_events)[-1]["id"]
    update_policy(env.engine, SOURCE, USER_PROFILE, "interest-again", at=AT + 6)
    prepared = prepare_reevaluation(env.engine, SOURCE, "batch", (second, first), at=AT + 7)
    with env.engine.begin() as connection:
        register_reevaluation_in_transaction(connection, prepared)
        connection.exec_driver_sql(
            "CREATE TRIGGER reject_second_member BEFORE INSERT ON notification_decisions "
            f"WHEN NEW.event_id = {second} AND NEW.evaluation_key = 'operation:batch' "
            "BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
    with pytest.raises(IngestError, match="notification_database_write_failed"):
        reevaluate_events(env.engine, SOURCE, "batch")
    operation = next(row for row in rows(env, notification_operations) if row["id"] == "batch")
    assert [result["event_id"] for result in operation["results"]] == [first]
    assert operation["finished_at"] is None
    assert len(rows(env, email_outbox)) == 1
    with env.engine.begin() as connection:
        connection.exec_driver_sql("DROP TRIGGER reject_second_member")
    env.engine.dispose()
    env.engine = open_initialized_engine(env.settings.database)
    result = reevaluate_events(env.engine, SOURCE, "batch")
    assert result["complete"]
    assert [item["event_id"] for item in result["results"]] == [first, second]
    assert len(rows(env, email_outbox)) == 2
    assert len(rows(env, notification_events)) == 2


def test_actual_completion_clock_does_not_change_frozen_decision_clock(activated):
    env = activated
    event_id = none_event(env)
    register(env, event_id)
    result = reevaluate_events(env.engine, SOURCE, "resume-fixed", clock=lambda: AT + 86400)
    assert result["completed_at"] == AT + 86400
    assert result["evaluated_at"] == AT
    assert rows(env, notification_decisions)[-1]["evaluated_at"] == AT


def test_regressing_completion_clock_rolls_back_member_result(activated):
    env = activated
    event_id = none_event(env)
    register(env, event_id)
    before = rows(env, notification_events)
    with pytest.raises(IngestError, match="completion_clock_invalid"):
        reevaluate_events(env.engine, SOURCE, "resume-fixed", clock=lambda: AT - 1)
    assert rows(env, notification_events) == before
    assert rows(env, email_outbox) == []
    assert (
        next(row for row in rows(env, notification_operations) if row["id"] == "resume-fixed")[
            "results"
        ]
        == []
    )


def test_preview_never_reads_completion_clock(activated):
    env = activated
    event_id = none_event(env)

    def forbidden():
        raise AssertionError("preview must not sample a clock")

    result = reevaluate_events(
        env.engine,
        SOURCE,
        "view",
        event_ids=(event_id,),
        at=AT,
        preview=True,
        clock=forbidden,
    )
    assert result["preview"]


@pytest.mark.parametrize(
    "change",
    [
        "missing_policy",
        "wrong_source",
        "empty_items",
        "wrong_event",
        "wrong_time",
        "wrong_facts",
        "wrong_facts_digest",
        "wrong_profile_digest",
        "wrong_context_digest",
        "invalid_context",
        "false_complete",
        "unmarked_complete",
        "duplicate_results",
    ],
)
def test_valid_checksum_cannot_disguise_malformed_operation_bindings(activated, change):
    env = activated
    event_id = none_event(env)
    register(env, event_id)
    row = next(row for row in rows(env, notification_operations) if row["id"] == "resume-fixed")
    payload = row["snapshot"]["payload"]
    item = payload["items"][0]
    if change == "missing_policy":
        del payload["policy_revision_id"]
    elif change == "wrong_source":
        row["parameters"]["source_id"] = "other-source"
    elif change == "empty_items":
        payload["items"] = []
    elif change == "wrong_event":
        item["event_id"] = event_id + 100
    elif change == "wrong_time":
        item["decision"]["evaluated_at"] = "2026-09-05T00:00:00+00:00"
    elif change == "wrong_facts":
        item["facts"]["content_sha256"] = "0" * 64
    elif change == "wrong_facts_digest":
        item["decision"]["facts_sha256"] = "0" * 64
    elif change == "wrong_profile_digest":
        item["decision"]["profile_sha256"] = "0" * 64
    elif change == "wrong_context_digest":
        item["context"]["next_digest_at"] = "2026-09-07T09:00:00+08:00"
    elif change == "invalid_context":
        item["context"]["kind"] = "not-a-supported-kind"
    elif change == "false_complete":
        row["finished_at"] = AT
    else:
        result = dict(event_id=event_id, status="applied", decision_id=999)
        row["results"] = [result] if change == "unmarked_complete" else [result, result]
    row["snapshot"]["sha256"] = canonical_sha256(payload)
    with env.engine.begin() as connection:
        connection.execute(
            notification_operations.update()
            .where(notification_operations.c.id == "resume-fixed")
            .values(
                parameters=row["parameters"],
                parameters_sha256=canonical_sha256(row["parameters"]),
                snapshot=row["snapshot"],
                results=row["results"],
                finished_at=row["finished_at"],
            )
        )
    before = rows(env, notification_events)
    with pytest.raises(IngestError, match="notification_operation_invalid"):
        reevaluate_events(env.engine, SOURCE, "resume-fixed")
    assert rows(env, notification_events) == before
    assert rows(env, email_outbox) == []


@pytest.mark.parametrize("change", ["malformed_json", "missing_policy", "wrong_facts_digest"])
def test_public_prepared_operation_is_checked_before_registration(activated, change):
    env = activated
    event_id = none_event(env)
    prepared = prepare_reevaluation(env.engine, SOURCE, "bad-prepared", (event_id,), at=AT)
    snapshot = json.loads(prepared.snapshot_json)
    if change == "malformed_json":
        prepared = replace(prepared, snapshot_json="{")
    else:
        if change == "missing_policy":
            del snapshot["payload"]["policy_revision_id"]
        else:
            snapshot["payload"]["items"][0]["decision"]["facts_sha256"] = "0" * 64
        snapshot["sha256"] = canonical_sha256(snapshot["payload"])
        prepared = replace(prepared, snapshot_json=canonical_json(snapshot))
    with pytest.raises(IngestError, match="notification_operation_invalid"):
        with env.engine.begin() as connection:
            register_reevaluation_in_transaction(connection, prepared)
    assert not any(row["id"] == "bad-prepared" for row in rows(env, notification_operations))


def test_pending_frozen_operation_survives_current_rule_version_change(activated, monkeypatch):
    env = activated
    event_id = none_event(env)
    register(env, event_id)
    monkeypatch.setattr(
        "signalnest.notifications.maintenance.policy_manifest",
        lambda profile: dict(versions="stand-in for newer fixed code rules"),
    )
    with pytest.raises(IngestError, match="notification_policy_outdated"):
        prepare_reevaluation(env.engine, SOURCE, "new-code", (event_id,), at=AT)
    assert reevaluate_events(env.engine, SOURCE, "resume-fixed")["complete"]


def test_two_prepared_operations_cannot_override_first_registered_mail_route(activated):
    env = activated
    event_id = none_event(env)
    register(env, event_id, "first")
    register(env, event_id, "second")
    first = reevaluate_events(env.engine, SOURCE, "first")
    event = rows(env, notification_events)[0]
    result = reevaluate_events(env.engine, SOURCE, "second")
    assert first["results"][0]["status"] == "applied"
    assert result["results"] == [dict(event_id=event_id, status="stale_input")]
    assert rows(env, notification_events) == [event]
    assert len(rows(env, email_outbox)) == 1


@pytest.mark.parametrize("field", ["decision_id", "outbox_id", "effective_route"])
def test_completed_result_requires_real_registered_decision_and_intent(activated, field):
    env = activated
    event_id = none_event(env)
    reevaluate_events(env.engine, SOURCE, "done", event_ids=(event_id,), at=AT)
    operation = next(row for row in rows(env, notification_operations) if row["id"] == "done")
    operation["results"][0][field] = "digest" if field == "effective_route" else 999999
    with env.engine.begin() as connection:
        connection.execute(
            notification_operations.update()
            .where(notification_operations.c.id == "done")
            .values(results=operation["results"])
        )
    with pytest.raises(IngestError, match="notification_operation_invalid"):
        reevaluate_events(env.engine, SOURCE, "done")
