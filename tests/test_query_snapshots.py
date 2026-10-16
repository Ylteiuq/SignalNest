"""Frozen production-style SQLite/raw backups feed real read-only query tools offline."""

import hashlib
import json
import runpy
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_notification_service import activated as activated
from test_query_agent import campus as campus

from signalnest.config import load_config
from signalnest.querying.agent import ask, render_report
from signalnest.querying.config import load_query_config, load_query_inputs
from signalnest.querying.contracts import QueryError, Question
from signalnest.querying.snapshots import database_sha256, validate_snapshot
from signalnest.sources import CS, UC

ROOT = Path(__file__).resolve().parents[1]
PREPARER = runpy.run_path(str(ROOT / "deploy/prepare_query_snapshot.py"))
BackupInstance = PREPARER["BackupInstance"]
SnapshotSpec = PREPARER["SnapshotSpec"]
prepare_query_snapshot = PREPARER["prepare_query_snapshot"]
BackupVerificationError = PREPARER["BackupVerificationError"]
CAPTURED_AT = 1791963225
PREPARED_AT = CAPTURED_AT + 20
AT = datetime.fromtimestamp(PREPARED_AT + 3600, UTC)


def backup(database, data_dir, destination):
    destination.mkdir(mode=0o700)
    copied = destination / "data.sqlite3"
    with (
        closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as source,
        closing(sqlite3.connect(copied)) as target,
    ):
        source.backup(target)
    shutil.copytree(data_dir / "raw", destination / "raw")
    (destination / "backup.complete").write_text("verified\n", encoding="ascii")
    # An unrelated deployment secret must neither be read nor copied by the preparer.
    (destination / "smtp.env").write_text("DO_NOT_COPY_THIS_SENTINEL", encoding="ascii")
    return copied


@pytest.fixture
def snapshots(campus, tmp_path):
    instances = []
    parents = {}
    for identifier, binding in (("uc", UC), ("cs", CS)):
        settings = campus.instances[identifier]
        directory = tmp_path / f"backup-{identifier}"
        copied = backup(settings.storage.database, settings.storage.data_dir, directory)
        parents[identifier] = (copied, database_sha256(copied))
        instances.append(
            BackupInstance(
                id=identifier,
                source_id=binding.source_id,
                parser=binding.name,
                backup_dir=directory,
                database="data.sqlite3",
                snapshot_at=CAPTURED_AT,
            )
        )
    spec = SnapshotSpec(instances=tuple(instances))
    output = tmp_path / "query-copy"
    result = prepare_query_snapshot(
        spec, output, profile=campus.settings.profile, prepared_at=PREPARED_AT
    )
    return output, spec, result, parents


def query(output, text="助教招聘"):
    return ask(load_query_config(output / "ask.local.toml"), Question(text=text, at=AT), local=True)


def test_two_sources_real_parser_index_and_profiles_query_a_private_frozen_copy(snapshots):
    output, _, result, parents = snapshots
    before = {key: database_sha256(output / key / "signalnest.sqlite3") for key in parents}
    report = query(output)
    assert report.status == "local" and report.error_code is None
    assert any(hit["source_document_id"] == "1074:64481" for hit in report.search_results)
    assert {s["instance_id"] for s in report.source_status} == {"uc", "cs"}
    for source in report.source_status:
        origin = source["data_origin"]
        assert origin["kind"] == "frozen_snapshot" and origin["live_database"] is False
        assert origin["snapshot_at"] == CAPTURED_AT
        assert origin["facts"]["latest_successful_body_fetched_at"] == 100
        assert origin["facts"]["latest_successful_content_parsed_at"] == 102
        assert origin["facts"]["last_complete_scan_at"] is None
        identifier = source["instance_id"]
        assert origin["parent_database_sha256"] == parents[identifier][1]
        assert f"{identifier}:frozen_snapshot_not_live" in report.warnings
        assert (output / identifier / "signalnest.sqlite3").stat().st_mode & 0o777 == 0o400
        assert not (output / identifier / "raw").exists()
        assert not (output / identifier / "smtp.env").exists()
        config = load_config(output / identifier / "instance.toml")
        assert config.smtp is None and config.mail_runtime.enabled is False
    assert before == {key: database_sha256(output / key / "signalnest.sqlite3") for key in parents}
    assert all(database_sha256(path) == digest for path, digest in parents.values())
    assert output.stat().st_mode & 0o777 == 0o700
    assert result["production_pause_state_changed"] is False
    text = render_report(report)
    assert "静态快照" in text and "不是实时库" in text and "1970-01-01T00:01:40" in text
    assert "DO_NOT_COPY_THIS_SENTINEL" not in report.model_dump_json()


def test_preparer_pauses_only_the_derivative_and_preserves_parent_channels(activated, tmp_path):
    env = activated
    directory = tmp_path / "activated-backup"
    original = backup(env.settings.database, env.settings.data_dir, directory)
    parent_sha = database_sha256(original)
    spec = SnapshotSpec(
        instances=(
            BackupInstance(
                id="uc",
                source_id=UC.source_id,
                parser=UC.name,
                backup_dir=directory,
                database="data.sqlite3",
                snapshot_at=CAPTURED_AT,
            ),
        )
    )
    output = tmp_path / "paused-query"
    prepare_query_snapshot(spec, output, prepared_at=PREPARED_AT)
    with closing(sqlite3.connect(original.as_uri() + "?mode=ro", uri=True)) as connection:
        assert connection.execute("SELECT paused FROM notification_channel_state").fetchone() == (
            0,
        )
    derived = output / "uc/signalnest.sqlite3"
    with closing(sqlite3.connect(derived.as_uri() + "?mode=ro", uri=True)) as connection:
        assert connection.execute(
            "SELECT paused,pause_reason,paused_at FROM notification_channel_state"
        ).fetchone() == (1, "manual", PREPARED_AT)
    assert database_sha256(original) == parent_sha
    report = query(output)
    source = report.source_status[0]
    complete_at = source["data_origin"]["facts"]["last_complete_scan_at"]
    assert complete_at is not None
    assert source["source"]["complete_scan_age_seconds"] == int(AT.timestamp()) - complete_at
    assert "uc:complete_scan_stale" in report.warnings
    # Rebinding a new digest after unpausing does not satisfy the copy contract.
    derived.chmod(0o600)
    with sqlite3.connect(derived) as connection:
        connection.execute(
            "UPDATE notification_channel_state SET paused=0,pause_reason=NULL,paused_at=NULL"
        )
    manifest_path = output / "uc/snapshot.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["database_sha256"] = database_sha256(derived)
    manifest_path.write_text(json.dumps(manifest))
    assert query(output).source_status[0]["error_code"] == "snapshot_sending_not_paused"


def test_manifest_config_validation_does_not_open_database(tmp_path, monkeypatch):
    config = tmp_path / "ask.toml"
    config.write_text(
        '[[instances]]\nid="uc"\nconfig="missing.toml"\nsnapshot_manifest="missing.json"\n'
    )
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: pytest.fail("configuration opened DB"))
    settings = load_query_config(config)
    assert settings.instances[0].snapshot_manifest == tmp_path / "missing.json"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["ask.toml"]


@pytest.mark.parametrize(
    "damage,code",
    [
        ("database_bytes", "snapshot_database_changed"),
        ("manifest_source", "snapshot_binding_mismatch"),
        ("manifest_instance", "snapshot_binding_mismatch"),
        ("manifest_facts", "snapshot_binding_mismatch"),
        ("manifest_missing", "snapshot_unavailable"),
        ("manifest_invalid", "snapshot_manifest_invalid"),
        ("database_symlink", "snapshot_path_unsafe"),
        ("manifest_symlink", "snapshot_path_unsafe"),
        ("journal", "snapshot_database_not_frozen"),
    ],
)
def test_snapshot_mismatch_is_explicit_and_does_not_hide_other_source(snapshots, damage, code):
    output, _, _, _ = snapshots
    database = output / "uc/signalnest.sqlite3"
    manifest_path = output / "uc/snapshot.json"
    if damage == "database_bytes":
        database.chmod(0o600)
        with database.open("ab") as stream:
            stream.write(b"changed after freezing")
    elif damage.startswith("manifest_") and damage not in {"manifest_symlink", "manifest_missing"}:
        data = json.loads(manifest_path.read_text())
        if damage == "manifest_source":
            data["facts"]["source_id"] = CS.source_id
        elif damage == "manifest_instance":
            data["instance_id"] = "other"
        elif damage == "manifest_facts":
            data["facts"]["documents"] += 1
        else:
            data["format_version"] = "unexpected"
        manifest_path.write_text(json.dumps(data))
    elif damage == "manifest_missing":
        manifest_path.unlink()
    elif damage == "database_symlink":
        original = database.with_suffix(".original")
        database.rename(original)
        database.symlink_to(original)
    elif damage == "manifest_symlink":
        original = manifest_path.with_suffix(".original")
        manifest_path.rename(original)
        manifest_path.symlink_to(original)
    else:
        Path(str(database) + "-wal").write_bytes(b"not a frozen main DB")
    report = query(output)
    assert report.source_status[0]["error_code"] == code
    assert report.source_status[1]["data_origin"]["kind"] == "frozen_snapshot"
    assert any(hit["instance_id"] == "cs" for hit in report.search_results)
    assert f"uc:{code}" in report.warnings


def test_parent_raw_failure_does_not_publish_or_modify_backups(snapshots, tmp_path):
    _, spec, _, parents = snapshots
    source = spec.instances[1].backup_dir
    next((source / "raw").iterdir()).write_bytes(b"damaged raw bytes")
    output = tmp_path / "never-published"
    with pytest.raises(BackupVerificationError):
        prepare_query_snapshot(spec, output, prepared_at=PREPARED_AT)
    assert not output.exists()
    assert not list(tmp_path.glob(".query-snapshot-*"))
    assert all(database_sha256(path) == digest for path, digest in parents.values())


def test_existing_destination_and_unsafe_database_filename_are_refused(snapshots):
    output, spec, _, _ = snapshots
    with pytest.raises(QueryError, match="snapshot_destination_exists"):
        prepare_query_snapshot(spec, output, prepared_at=PREPARED_AT)
    with pytest.raises(ValidationError):
        BackupInstance.model_validate(spec.instances[0].model_dump() | {"database": "../outside"})


def test_explicit_snapshot_time_cannot_precede_the_database(snapshots, tmp_path):
    _, spec, _, _ = snapshots
    invalid = spec.model_copy(
        update={"instances": (spec.instances[0].model_copy(update={"snapshot_at": 99}),)}
    )
    with pytest.raises(ValidationError):
        prepare_query_snapshot(invalid, tmp_path / "bad-time", prepared_at=PREPARED_AT)
    assert not (tmp_path / "bad-time").exists()


def test_preparer_cli_and_actual_ask_do_not_write_original_or_derived_databases(
    snapshots, tmp_path
):
    _, spec, _, parents = snapshots
    input_file = tmp_path / "spec.json"
    input_file.write_text(spec.model_dump_json())
    output = tmp_path / "cli-copy"
    command = [sys.executable, str(ROOT / "deploy/prepare_query_snapshot.py")]
    child = subprocess.run(
        [*command, "--spec", str(input_file), "--output", str(output)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert child.returncode == 0, child.stderr
    assert json.loads(child.stdout)["all_derived_notification_channels_paused"] is True
    frozen_before = {
        name: database_sha256(output / name / "signalnest.sqlite3") for name in parents
    }
    queried = subprocess.run(
        [
            sys.executable,
            "-m",
            "signalnest",
            "ask",
            "--question",
            "助教招聘",
            "--config",
            str(output / "ask.local.toml"),
            "--local",
            "--json",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert queried.returncode == 0, queried.stderr
    assert (
        json.loads(queried.stdout)["source_status"][0]["data_origin"]["kind"] == "frozen_snapshot"
    )
    assert frozen_before == {
        name: database_sha256(output / name / "signalnest.sqlite3") for name in parents
    }
    assert all(database_sha256(path) == digest for path, digest in parents.values())
    failed = subprocess.run(
        [*command, "--spec", str(input_file), "--output", str(output)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert failed.returncode == 1 and "snapshot_destination_exists" in failed.stderr
    assert str(tmp_path) not in failed.stderr


def test_valid_snapshot_has_no_sending_configuration(snapshots):
    output, _, _, _ = snapshots
    settings = load_query_config(output / "ask.local.toml")
    instances, _ = load_query_inputs(settings)
    assert settings.model is None
    for item in settings.instances:
        instance = instances[item.id]
        manifest = validate_snapshot(
            item.snapshot_manifest, instance.storage.database, item.id, instance.source.id
        )
        assert (
            manifest.database_sha256
            == hashlib.sha256(instance.storage.database.read_bytes()).hexdigest()
        )


def test_preparer_help_is_side_effect_free(tmp_path):
    output = tmp_path / "should-not-exist"
    child = subprocess.run(
        [
            sys.executable,
            str(ROOT / "deploy/prepare_query_snapshot.py"),
            "--help",
            "--spec",
            str(tmp_path / "missing.json"),
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert child.returncode == 0 and "--output" in child.stdout
    assert not output.exists() and not list(tmp_path.iterdir())
