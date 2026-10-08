"""Real SQLite backup/raw copies preserve frozen mail and its delivery evidence.

SMTP outcomes are injected finite observations, never actual server acceptance.
Backup and recovery use production archive, Parser, N1/N2 and N4 services.
"""

import hashlib
import json
import shutil
import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from test_backup_verification import BackupVerificationError, verify_backup
from test_mail_planning import plan, seal_copies
from test_mail_sending import ACCEPTED, OPTIONS, PERMANENT, RETRYABLE, SMTP, UNCERTAIN
from test_notification_service import (
    ACTIVATED_AT,
    NOTICE,
    REQUEST_PROFILE,
    SOURCE,
    evidence,
    live,
    opportunity,
    rows,
)
from test_notification_service import activated as activated

from signalnest.cache import select_cache_candidate
from signalnest.ingestion import process_cached_response, record_response
from signalnest.instance_lock import writer_lock
from signalnest.mail.planning import load_frozen_mail
from signalnest.mail.sending import (
    drain_mail,
    finish_attempt,
    mail_status,
    recover_sending,
    register_attempt,
    retry_mail,
    set_sending_paused,
)
from signalnest.notifications.contracts import canonical_sha256
from signalnest.schema import (
    mail_attempts,
    mail_delivery,
    mail_message_members,
    mail_messages,
    notification_channel_state,
)
from signalnest.storage import open_initialized_engine

TABLES = (
    mail_messages,
    mail_message_members,
    mail_attempts,
    mail_delivery,
    notification_channel_state,
)


def snapshot_hashes(data_dir):
    return {
        str(path.relative_to(data_dir)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in data_dir.rglob("*")
        if path.is_file()
    }


def sqlite_snapshot(env, clone):
    clone.mkdir()
    database = clone / "signalnest.sqlite3"
    with writer_lock(env.settings.database):
        with (
            closing(
                sqlite3.connect(env.settings.database.as_uri() + "?mode=ro", uri=True)
            ) as source,
            closing(sqlite3.connect(database)) as destination,
        ):
            source.backup(destination)
        shutil.copytree(env.settings.data_dir / "raw", clone / "raw", symlinks=True)
    return database


@pytest.fixture
def mail_backup(activated, tmp_path):
    env = activated
    live(env, opportunity())
    seal_copies(env, 4)
    assert plan(env, at=ACTIVATED_AT + 10, max_messages=4)["planned"] == 4
    ids = [row["id"] for row in sorted(rows(env, mail_messages), key=lambda row: row["id"])]
    for mail_id, result in ((ids[1], ACCEPTED), (ids[2], None), (ids[3], UNCERTAIN)):
        attempt = register_attempt(
            env.engine, SOURCE, mail_id, at=ACTIVATED_AT + 20, options=OPTIONS
        )
        if result is not None:
            finish_attempt(
                env.engine,
                SOURCE,
                mail_id,
                attempt["attempt_no"],
                result,
                at=ACTIVATED_AT + 21,
                options=OPTIONS,
            )
    set_sending_paused(env.engine, SOURCE, True, at=ACTIVATED_AT + 22)
    expected = {table.name: rows(env, table) for table in TABLES}
    frozen = {mail_id: load_frozen_mail(env.engine, SOURCE, mail_id) for mail_id in ids}
    clone = tmp_path / "mail-snapshot"
    # The real backup API, not a main-file copy, and one lock spanning DB/raw.
    database = sqlite_snapshot(env, clone)
    return SimpleNamespace(
        original=env,
        database=database,
        data_dir=clone,
        expected=expected,
        frozen=frozen,
        ids=ids,
    )


def test_mail_backup_retains_exact_bytes_members_attempts_and_all_requested_states(mail_backup):
    backup = mail_backup
    before = snapshot_hashes(backup.data_dir)
    report = verify_backup(backup.database, backup.data_dir)
    assert report["mail_messages"] == report["mail_message_members"] == 4
    assert report["mail_attempts"] == 3
    assert report["mail_paused_channels"] == 1
    assert {
        state: report[f"mail_{state}"] for state in ("pending", "accepted", "sending", "uncertain")
    } == dict.fromkeys(("pending", "accepted", "sending", "uncertain"), 1)
    assert report["mail_retry"] == report["mail_blocked"] == 0
    engine = open_initialized_engine(backup.database)
    try:
        with engine.connect() as connection:
            for table in TABLES:
                actual = [dict(row) for row in connection.execute(sa.select(table)).mappings()]
                assert actual == backup.expected[table.name]
        for mail_id in backup.ids:
            assert load_frozen_mail(engine, SOURCE, mail_id) == backup.frozen[mail_id]
    finally:
        engine.dispose()
    assert snapshot_hashes(backup.data_dir) == before


def test_restore_pause_recovery_is_once_due_survives_and_accepted_is_never_resent(mail_backup):
    backup = mail_backup
    original_hashes = snapshot_hashes(backup.original.settings.data_dir)
    engine = open_initialized_engine(backup.database)
    at = ACTIVATED_AT + 30
    try:
        with writer_lock(backup.database):
            # Explicit isolation step, even if the saved channel was already paused.
            set_sending_paused(engine, SOURCE, True, at=at)
            status = mail_status(engine, SOURCE, at=at)
            assert status["paused"] and status["counts"]["sending"] == 1

            def must_not_send(*args):
                pytest.fail("restored copy must not contact SMTP while paused")

            result = drain_mail(engine, SOURCE, SMTP, OPTIONS, now=lambda: at, sender=must_not_send)
            assert result["attempted"] == 0 and result["recovered"] == 1
            assert result["status"]["counts"]["uncertain"] == 2
            assert result["status"]["counts"]["accepted"] == 1
            assert recover_sending(engine, SOURCE, at=at + 1, options=OPTIONS) == 0
        with engine.connect() as connection:
            recovered = dict(
                connection.execute(
                    sa.select(mail_delivery).where(mail_delivery.c.mail_id == backup.ids[2])
                )
                .mappings()
                .one()
            )
        assert recovered["next_attempt_at"] == at + 1800
    finally:
        engine.dispose()
    engine = open_initialized_engine(backup.database)
    try:
        with engine.connect() as connection:
            assert (
                dict(
                    connection.execute(
                        sa.select(mail_delivery).where(mail_delivery.c.mail_id == backup.ids[2])
                    )
                    .mappings()
                    .one()
                )
                == recovered
            )
        report = verify_backup(backup.database, backup.data_dir)
        assert report["mail_sending"] == 0 and report["mail_uncertain"] == 2
        assert report["mail_attempts"] == 3
        sent = []
        with writer_lock(backup.database):
            # This test's explicit review/resume is not an automatic restore action.
            set_sending_paused(engine, SOURCE, False, at=at + 2000)
            result = drain_mail(
                engine,
                SOURCE,
                SMTP,
                OPTIONS,
                now=lambda: at + 2000,
                sender=lambda mail, settings: sent.append(mail.mail_id) or ACCEPTED,
            )
        assert result["accepted"] == 3
        assert set(sent) == {backup.ids[0], backup.ids[2], backup.ids[3]}
        assert backup.ids[1] not in sent
        assert verify_backup(backup.database, backup.data_dir)["mail_accepted"] == 4
    finally:
        engine.dispose()
    assert snapshot_hashes(backup.original.settings.data_dir) == original_hashes


@pytest.mark.parametrize(
    "damage,code",
    [
        ("payload", "backup_mail_payload"),
        ("payload_header_rehashed", "backup_mail_payload"),
        ("missing_member", "backup_mail_members"),
        ("snapshot", "backup_mail_members"),
        ("snapshot_rehashed", "backup_mail_evidence"),
        ("decision", "backup_mail_evidence"),
        ("missing_attempt", "backup_mail_attempts"),
        ("attempt_gap", "backup_mail_attempts"),
        ("accepted_without_evidence", "backup_mail_delivery"),
        ("accepted_with_unfinished_attempt", "backup_mail_attempts"),
        ("uncertain_due_early", "backup_mail_delivery"),
        ("unpaused_with_reason", "backup_mail_channel_pause"),
    ],
)
def test_relationally_valid_mail_corruption_is_rejected_without_repair(mail_backup, damage, code):
    backup = mail_backup
    with sqlite3.connect(backup.database) as connection:
        if damage in {"payload", "payload_header_rehashed"}:
            payload, digest = connection.execute(
                "SELECT payload_bytes, payload_sha256 FROM mail_messages WHERE id = ?",
                (backup.ids[0],),
            ).fetchone()
            payload = payload.replace(b"sender@example.org", b"other@example.org", 1)
            if damage.endswith("rehashed"):
                digest = hashlib.sha256(payload).hexdigest()
            connection.execute(
                "UPDATE mail_messages SET payload_bytes=?, payload_sha256=? WHERE id=?",
                (payload, digest, backup.ids[0]),
            )
        elif damage == "missing_member":
            connection.execute("DELETE FROM mail_message_members WHERE mail_id=?", (backup.ids[0],))
        elif damage in {"snapshot", "snapshot_rehashed"}:
            snapshot = json.loads(
                connection.execute(
                    "SELECT snapshot FROM mail_message_members WHERE mail_id=?", (backup.ids[0],)
                ).fetchone()[0]
            )
            snapshot["source_document_id"] = "1517:unrelated"
            connection.execute(
                "UPDATE mail_message_members SET snapshot=? WHERE mail_id=?",
                (json.dumps(snapshot), backup.ids[0]),
            )
            if damage.endswith("rehashed"):
                connection.execute(
                    "UPDATE mail_messages SET members_sha256=? WHERE id=?",
                    (canonical_sha256([snapshot]), backup.ids[0]),
                )
        elif damage == "decision":
            selected_id, decision = connection.execute(
                "SELECT d.id,d.decision FROM notification_decisions d "
                "JOIN mail_message_members m ON m.decision_id=d.id WHERE m.mail_id=?",
                (backup.ids[0],),
            ).fetchone()
            decision = json.loads(decision) | {"reasons": ["changed after backup"]}
            connection.execute(
                "UPDATE notification_decisions SET decision=? WHERE id=?",
                (json.dumps(decision), selected_id),
            )
        elif damage == "missing_attempt":
            connection.execute("DELETE FROM mail_attempts WHERE mail_id=?", (backup.ids[1],))
        elif damage == "attempt_gap":
            connection.execute(
                "UPDATE mail_attempts SET attempt_no=2 WHERE mail_id=?", (backup.ids[1],)
            )
            connection.execute(
                "UPDATE mail_delivery SET attempt_count=2 WHERE mail_id=?", (backup.ids[1],)
            )
        elif damage == "accepted_without_evidence":
            connection.execute(
                "UPDATE mail_delivery SET state='accepted',accepted_at=updated_at,"
                "next_attempt_at=NULL WHERE mail_id=?",
                (backup.ids[3],),
            )
        elif damage == "accepted_with_unfinished_attempt":
            connection.execute(
                "UPDATE mail_delivery SET state='accepted',accepted_at=updated_at WHERE mail_id=?",
                (backup.ids[2],),
            )
        elif damage == "uncertain_due_early":
            connection.execute(
                "UPDATE mail_delivery SET next_attempt_at=updated_at+1 WHERE mail_id=?",
                (backup.ids[3],),
            )
        else:
            connection.execute("UPDATE notification_channel_state SET paused=0")
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    before = snapshot_hashes(backup.data_dir)
    with pytest.raises(BackupVerificationError, match=code):
        verify_backup(backup.database, backup.data_dir)
    assert snapshot_hashes(backup.data_dir) == before


def test_readonly_verification_never_initializes_missing_delivery_state(mail_backup):
    backup = mail_backup
    with sqlite3.connect(backup.database) as connection:
        connection.execute("DELETE FROM mail_delivery WHERE mail_id=?", (backup.ids[0],))
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    before = snapshot_hashes(backup.data_dir)
    with pytest.raises(BackupVerificationError, match="backup_mail_delivery"):
        verify_backup(backup.database, backup.data_dir)
    assert snapshot_hashes(backup.data_dir) == before


@pytest.mark.parametrize(
    "scenario,expected_state",
    [
        ("retry", "retry"),
        ("permanent", "blocked"),
        ("exhausted", "blocked"),
        ("manual_granted", "pending"),
        ("manual_accepted", "accepted"),
        ("manual_uncertain", "blocked"),
    ],
)
def test_valid_retry_blocked_and_manual_histories_remain_verifiable(
    mail_backup, scenario, expected_state
):
    backup = mail_backup
    engine = open_initialized_engine(backup.database)
    at = ACTIVATED_AT + 30
    mail_id = backup.ids[0]
    options = OPTIONS.model_copy(update={"max_attempts": 1}) if scenario == "exhausted" else OPTIONS
    try:
        with writer_lock(backup.database):
            set_sending_paused(engine, SOURCE, False, at=at)
            attempt = register_attempt(engine, SOURCE, mail_id, at=at, options=options)
            result = RETRYABLE if scenario in {"retry", "exhausted"} else PERMANENT
            finish_attempt(
                engine, SOURCE, mail_id, attempt["attempt_no"], result, at=at, options=options
            )
            if scenario.startswith("manual_"):
                retry_mail(engine, SOURCE, mail_id, at=at)
                if scenario != "manual_granted":
                    attempt = register_attempt(engine, SOURCE, mail_id, at=at, options=options)
                    finish_attempt(
                        engine,
                        SOURCE,
                        mail_id,
                        attempt["attempt_no"],
                        ACCEPTED if scenario == "manual_accepted" else UNCERTAIN,
                        at=at,
                        options=options,
                    )
        with engine.connect() as connection:
            assert (
                connection.execute(
                    sa.select(mail_delivery.c.state).where(mail_delivery.c.mail_id == mail_id)
                ).scalar_one()
                == expected_state
            )
    finally:
        engine.dispose()
    assert verify_backup(backup.database, backup.data_dir)[f"mail_{expected_state}"] >= 1


def test_legacy_unknown_pause_is_preserved_without_guessing(mail_backup):
    backup = mail_backup
    with sqlite3.connect(backup.database) as connection:
        connection.execute("UPDATE notification_channel_state SET pause_reason=NULL,paused_at=NULL")
    before = snapshot_hashes(backup.data_dir)
    assert verify_backup(backup.database, backup.data_dir)["mail_paused_channels"] == 1
    assert snapshot_hashes(backup.data_dir) == before


def test_restoring_an_unpaused_snapshot_explicitly_pauses_only_the_copy(mail_backup, tmp_path):
    backup = mail_backup
    original = backup.original
    at = ACTIVATED_AT + 30
    unpaused_copy = tmp_path / "unpaused-restore.sqlite3"
    with writer_lock(original.settings.database):
        set_sending_paused(original.engine, SOURCE, False, at=at)
        with (
            closing(
                sqlite3.connect(original.settings.database.as_uri() + "?mode=ro", uri=True)
            ) as source,
            closing(sqlite3.connect(unpaused_copy)) as destination,
        ):
            source.backup(destination)
    original_before = snapshot_hashes(original.settings.data_dir)
    engine = open_initialized_engine(unpaused_copy)
    try:
        assert mail_status(engine, SOURCE, at=at)["paused"] is False
        with writer_lock(unpaused_copy):
            set_sending_paused(engine, SOURCE, True, at=at)
            result = drain_mail(
                engine,
                SOURCE,
                SMTP,
                OPTIONS,
                now=lambda: at,
                sender=lambda *args: pytest.fail("restored copy contacted SMTP"),
            )
        assert result["paused"] and result["attempted"] == 0
        assert mail_status(original.engine, SOURCE, at=at)["paused"] is False
    finally:
        engine.dispose()
    assert snapshot_hashes(original.settings.data_dir) == original_before


@pytest.mark.parametrize(
    "table,field,mail_index,code",
    [
        ("mail_messages", "date_at", 0, "backup_mail_payload"),
        ("mail_messages", "frozen_at", 0, "backup_mail_payload"),
        ("mail_messages", "max_bytes", 0, "backup_mail_payload"),
        ("mail_messages", "max_events", 0, "backup_mail_payload"),
        ("mail_delivery", "attempt_count", 0, "backup_mail_delivery"),
        ("mail_delivery", "updated_at", 0, "backup_mail_delivery"),
        ("mail_delivery", "next_attempt_at", 0, "backup_mail_delivery"),
        ("mail_delivery", "accepted_at", 1, "backup_mail_delivery"),
        ("mail_attempts", "started_at", 2, "backup_mail_attempts"),
        ("mail_attempts", "finished_at", 1, "backup_mail_attempts"),
        ("mail_attempts", "uncertain_until", 3, "backup_mail_attempts"),
    ],
)
def test_sqlite_numeric_affinity_corruption_has_a_finite_error(
    mail_backup, table, field, mail_index, code
):
    backup = mail_backup
    key = "id" if table == "mail_messages" else "mail_id"
    with sqlite3.connect(backup.database) as connection:
        # Names are from the fixed test parameter table, never user-supplied SQL.
        connection.execute(
            f"UPDATE {table} SET {field}=? WHERE {key}=?",
            ("not-an-integer", backup.ids[mail_index]),
        )
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    before = snapshot_hashes(backup.data_dir)
    with pytest.raises(BackupVerificationError, match=code):
        verify_backup(backup.database, backup.data_dir)
    assert snapshot_hashes(backup.data_dir) == before


def test_uncertain_lookahead_validates_the_next_attempt_time_before_comparison(mail_backup):
    backup = mail_backup
    mail_id = backup.ids[3]
    engine = open_initialized_engine(backup.database)
    try:
        with writer_lock(backup.database):
            set_sending_paused(engine, SOURCE, False, at=ACTIVATED_AT + 2000)
            register_attempt(engine, SOURCE, mail_id, at=ACTIVATED_AT + 2000, options=OPTIONS)
    finally:
        engine.dispose()
    with sqlite3.connect(backup.database) as connection:
        connection.execute(
            "UPDATE mail_attempts SET started_at=? WHERE mail_id=? AND attempt_no=2",
            ("bad-next-start-time", mail_id),
        )
        # The unfinished sending row satisfies SQL CHECK/FK constraints; only
        # the verifier's explicit type checks protect the cooldown lookahead.
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    before = snapshot_hashes(backup.data_dir)
    with pytest.raises(BackupVerificationError, match="backup_mail_attempts"):
        verify_backup(backup.database, backup.data_dir)
    assert snapshot_hashes(backup.data_dir) == before


@pytest.mark.parametrize(
    "table,field,key,value,code",
    [
        ("notification_channel_state", "paused_at", None, "bad", "backup_mail_channel_pause"),
        ("notification_channel_state", "paused", None, 2, "backup_mail_channel_pause"),
        ("mail_delivery", "manual_retry_pending", "mail_id", 2, "backup_mail_delivery"),
        ("mail_attempts", "manual", "mail_id", 2, "backup_mail_attempts"),
    ],
)
def test_mail_flag_and_channel_time_types_are_checked_explicitly(
    mail_backup, table, field, key, value, code
):
    backup = mail_backup
    query = f"UPDATE {table} SET {field}=?" + (f" WHERE {key}=?" if key else "")
    values = (value, backup.ids[1]) if key else (value,)
    with sqlite3.connect(backup.database) as connection:
        connection.execute(query, values)
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    with pytest.raises(BackupVerificationError, match=code):
        verify_backup(backup.database, backup.data_dir)


@pytest.fixture
def not_modified_mail_backup(activated, tmp_path):
    env = activated
    body_id = record_response(env.engine, env.store, evidence(ACTIVATED_AT + 2), opportunity())
    candidate = select_cache_candidate(
        env.engine, env.store, SOURCE, NOTICE, REQUEST_PROFILE
    ).candidate
    observed_id = record_response(
        env.engine,
        env.store,
        evidence(ACTIVATED_AT + 5, status_code=304),
        None,
        candidate=candidate,
    )
    process_cached_response(
        env.engine,
        env.store,
        observed_id,
        ACTIVATED_AT + 6,
        processing_origin="live",
        ingestion_run_id=env.run_id,
    )
    assert plan(env, at=ACTIVATED_AT + 10)["planned"] == 1
    # Later transport metadata may advance independently; do not require the
    # historical 304 body to remain the resource's current latest response.
    latest_id = record_response(env.engine, env.store, evidence(ACTIVATED_AT + 11), opportunity())
    clone = tmp_path / "not-modified-snapshot"
    database = sqlite_snapshot(env, clone)
    with sqlite3.connect(database) as connection:
        resource_id, document_id = connection.execute(
            "SELECT resource_id,document_id FROM raw_responses WHERE id=?", (body_id,)
        ).fetchone()
        other_resource, other_body = connection.execute(
            "SELECT resource_id,id FROM raw_responses WHERE page_type='list' LIMIT 1"
        ).fetchone()
    return SimpleNamespace(
        database=database,
        data_dir=clone,
        body_id=body_id,
        observed_id=observed_id,
        latest_id=latest_id,
        resource_id=resource_id,
        document_id=document_id,
        other_resource=other_resource,
        other_body=other_body,
    )


def test_historical_304_binding_remains_valid_after_new_transport_baseline(
    not_modified_mail_backup,
):
    backup = not_modified_mail_backup
    assert verify_backup(backup.database, backup.data_dir)["mail_pending"] == 1
    with sqlite3.connect(backup.database) as connection:
        assert (
            connection.execute(
                "SELECT latest_response_id FROM http_resources WHERE id=?", (backup.resource_id,)
            ).fetchone()[0]
            == backup.latest_id
        )


@pytest.mark.parametrize(
    "damage",
    [
        "source",
        "document",
        "page_type",
        "status",
        "baseline",
        "resource",
        "uri",
        "profile",
        "fetched_at",
    ],
)
def test_304_evidence_binding_corruption_is_rejected(not_modified_mail_backup, damage):
    backup = not_modified_mail_backup
    with sqlite3.connect(backup.database) as connection:
        if damage == "source":
            values = {"source_id": "other-source"}
        elif damage == "document":
            other_id = connection.execute(
                "SELECT id FROM documents WHERE id!=? LIMIT 1", (backup.document_id,)
            ).fetchone()[0]
            values = {"document_id": other_id}
        elif damage == "page_type":
            values = {"page_type": "list", "document_id": None}
        elif damage == "status":
            values = {"status_code": 404, "validated_response_id": None}
        elif damage == "baseline":
            values = {"validated_response_id": backup.other_body}
        elif damage == "resource":
            values = {"resource_id": backup.other_resource}
        elif damage == "uri":
            values = {"requested_url": "https://uc.whu.edu.cn/info/1517/999999.htm"}
        elif damage == "fetched_at":
            values = {"fetched_at": "bad"}
        else:
            connection.execute(
                "UPDATE http_resources SET profile_sha256=? WHERE id=?",
                ("0" * 64, backup.resource_id),
            )
            values = {}
        if values:
            assignments = ",".join(f"{key}=?" for key in values)
            connection.execute(
                f"UPDATE raw_responses SET {assignments} WHERE id=?",
                (*values.values(), backup.observed_id),
            )
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    before = snapshot_hashes(backup.data_dir)
    with pytest.raises(BackupVerificationError, match="backup_mail_evidence"):
        verify_backup(backup.database, backup.data_dir)
    assert snapshot_hashes(backup.data_dir) == before


def test_another_complete_200_cannot_replace_event_observation_even_with_rehashed_member(
    not_modified_mail_backup,
):
    backup = not_modified_mail_backup
    with sqlite3.connect(backup.database) as connection:
        connection.execute(
            "UPDATE notification_events SET observed_response_id=?", (backup.latest_id,)
        )
        snapshot = json.loads(
            connection.execute("SELECT snapshot FROM mail_message_members").fetchone()[0]
        )
        snapshot["observed_response_id"] = backup.latest_id
        connection.execute("UPDATE mail_message_members SET snapshot=?", (json.dumps(snapshot),))
        connection.execute(
            "UPDATE mail_messages SET members_sha256=?", (canonical_sha256([snapshot]),)
        )
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    with pytest.raises(BackupVerificationError, match="backup_mail_evidence"):
        verify_backup(backup.database, backup.data_dir)


def test_v1_frozen_evidence_remains_backup_verifiable_after_explicit_v2_update(
    activated, tmp_path, monkeypatch
):
    from test_notification_policy_v1_compatibility import deny_current_rules, install_old_seals
    from test_notification_service import USER_PROFILE

    from signalnest.notifications.maintenance import update_policy

    env = activated
    live(env, opportunity())
    install_old_seals(env)
    mail_id = plan(env, at=ACTIVATED_AT + 10)["mail_ids"][0]
    expected = load_frozen_mail(env.engine, SOURCE, mail_id)
    update_policy(env.engine, SOURCE, USER_PROFILE, "backup-publish-v2", at=ACTIVATED_AT + 20)
    clone = tmp_path / "v1-mail-snapshot"
    database = sqlite_snapshot(env, clone)
    monkeypatch.setattr("signalnest.notifications.facts.extract_facts", deny_current_rules)
    monkeypatch.setattr("signalnest.notifications.decision.decide", deny_current_rules)
    before = snapshot_hashes(clone)
    assert verify_backup(database, clone)["mail_pending"] == 1
    engine = open_initialized_engine(database)
    try:
        assert load_frozen_mail(engine, SOURCE, mail_id) == expected
    finally:
        engine.dispose()
    assert snapshot_hashes(clone) == before
