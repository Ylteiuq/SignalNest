"""Local evidence over actual SQLite; never infer live deployment or mail receipt."""

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.script import ScriptDirectory
from test_mail_planning import plan
from test_mail_sending import ACCEPTED, UNCERTAIN, make_backlog
from test_notification_service import ACTIVATED_AT, SOURCE, USER_PROFILE, live, opportunity
from test_notification_service import activated as activated
from test_status import seed_document, settings_for

from signalnest.config import MailRuntimeSettings, SmtpSettings, StorageSettings
from signalnest.ingestion_state import set_cooldown_in_transaction
from signalnest.instance_lock import writer_lock
from signalnest.mail.contracts import MailError
from signalnest.mail.sending import DrainOptions, drain_mail, register_attempt, set_sending_paused
from signalnest.rollout import (
    RolloutError,
    build_release_manifest,
    create_release_snapshot,
    inspect_rollout,
    observe_instance,
)
from signalnest.schema import mail_delivery, notification_channel_state
from signalnest.storage import migration_config

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def personal_profile(tmp_path):
    target = tmp_path / "personal.toml"
    target.write_text(
        'schema_version = 1\nprofile_id = "self"\ninstitution = "whu"\n'
        'study_level = "undergraduate"\ninterest_topics = ["exchange", "course_enrollment"]\n'
        'high_value_topics = ["exchange"]\n',
        encoding="utf-8",
    )
    return target


def inspect(settings, *, at=1_000_000, **options):
    return inspect_rollout(settings, at=at, release_root=ROOT, **options)


def check_states(report):
    return {check["code"]: check["state"] for check in report["checks"]}


def write_minimal_release(root):
    (root / "src/signalnest").mkdir(parents=True)
    (root / "pyproject.toml").write_text('[project]\nname="signalnest"\n')
    (root / "uv.lock").write_text("version = 1\n")
    (root / "README.md").write_text("# SignalNest\n")
    (root / "src/signalnest/__init__.py").write_text('"""Test release."""\n')


def test_release_manifest_includes_code_lock_templates_and_actual_runtime():
    report = build_release_manifest(ROOT)
    assert report["runtime_code_matches_release"] is True
    assert report["runtime"]["python"].startswith("3.12.")
    assert report["runtime"]["sqlite"]
    assert (
        report["schema_head"] == ScriptDirectory.from_config(migration_config()).get_current_head()
    )
    assert len(report["git_head"]) == 40
    assert (
        report["file_sha256"]["uv.lock"]
        == hashlib.sha256((ROOT / "uv.lock").read_bytes()).hexdigest()
    )
    assert "src/signalnest/rollout.py" in report["file_sha256"]
    assert "deploy/systemd/signalnest-mail.service" in report["file_sha256"]
    assert "profile.example.toml" not in report["file_sha256"]
    assert all(not key.startswith("research/") for key in report["file_sha256"])


def test_release_fingerprint_is_deterministic_and_content_sensitive(tmp_path):
    write_minimal_release(tmp_path)
    first = build_release_manifest(tmp_path)
    assert first == build_release_manifest(tmp_path)
    assert first["git_head"] is None and first["allowlisted_worktree_dirty"] is None
    assert first["runtime_code_matches_release"] is False
    (tmp_path / "uv.lock").write_text("version = 2\n")
    assert build_release_manifest(tmp_path)["content_sha256"] != first["content_sha256"]


def test_release_never_hashes_untracked_personal_or_environment_files(tmp_path):
    write_minimal_release(tmp_path)
    first = build_release_manifest(tmp_path)
    (tmp_path / "signalnest.toml").write_text("very-private-config-value")
    (tmp_path / "smtp.env").write_text("PASSWORD=very-private-password")
    (tmp_path / "profile.personal.toml").write_text("very-private-profile")
    (tmp_path / "data").mkdir()
    (tmp_path / "data/notice.html").write_text("private notice body")
    assert build_release_manifest(tmp_path) == first


def test_release_rejects_symlink_instead_of_reading_target(tmp_path):
    write_minimal_release(tmp_path)
    secret = tmp_path / "secret.env"
    secret.write_text("SECRET=do-not-read")
    (tmp_path / "src/signalnest/linked.py").symlink_to(secret)
    with pytest.raises(RolloutError, match="release_path_unsafe"):
        build_release_manifest(tmp_path)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFO behavior")
def test_release_rejects_fifo_without_waiting_for_writer(tmp_path):
    write_minimal_release(tmp_path)
    readme = tmp_path / "README.md"
    readme.unlink()
    os.mkfifo(readme)
    script = (
        "from pathlib import Path; from signalnest.rollout import "
        "build_release_manifest, RolloutError\n"
        "try:\n    build_release_manifest(Path(" + repr(str(tmp_path)) + "))\n"
        "except RolloutError as exc:\n    print(exc.code)\n"
    )
    child = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=False, timeout=5
    )
    assert child.returncode == 0
    assert child.stdout.strip() == "release_path_unsafe"
    assert not child.stderr


def test_unsupported_writing_platform_is_blocked_but_observable(
    state_env, personal_profile, monkeypatch
):
    monkeypatch.setattr("signalnest.rollout.writing_platform_supported", lambda: False)
    settings = settings_for(state_env.settings)
    options = dict(profile_path=personal_profile, profile_confirmed=True)
    report = inspect(settings, **options)
    assert report["prepared"] is False
    assert check_states(report)["writing_platform_supported"] == "blocked"
    observation = observe_instance(settings, at=1_000_000, release_root=ROOT, **options)
    assert observation["format"] == "signalnest-observation-v1"
    assert observation["prepared"] is False
    assert observation["collection"]["documents"]["total"] == 0


def test_unsupported_writing_platform_cannot_claim_mail_readiness(
    activated, personal_profile, monkeypatch
):
    settings = settings_for(activated.settings)
    settings.smtp = SmtpSettings(host="smtp.example.org", port=465, security="tls")
    settings.mail_runtime = MailRuntimeSettings(enabled=True)
    options = dict(at=ACTIVATED_AT + 100, profile_path=personal_profile, profile_confirmed=True)
    assert inspect(settings, **options)["mail_locally_ready"] is True
    monkeypatch.setattr("signalnest.rollout.writing_platform_supported", lambda: False)
    report = inspect(settings, **options)
    assert report["prepared"] is False
    assert report["mail_locally_ready"] is False


@pytest.mark.parametrize("missing_flag", ["O_NOFOLLOW", "O_NONBLOCK"])
def test_missing_posix_file_flags_produce_finite_blocked_observation(
    state_env, personal_profile, monkeypatch, missing_flag
):
    monkeypatch.delattr(os, missing_flag)
    monkeypatch.setattr("signalnest.rollout.writing_platform_supported", lambda: False)
    settings = settings_for(state_env.settings)
    report = observe_instance(
        settings,
        at=1_000_000,
        release_root=ROOT,
        profile_path=personal_profile,
        profile_confirmed=True,
    )
    assert report["prepared"] is False
    assert report["release"]["error_code"] == "release_platform_unsupported"
    assert report["collection"]["documents"]["total"] == 0


def test_missing_instance_is_reported_without_creating_directories_or_lock(tmp_path):
    storage = StorageSettings(
        data_dir=str(tmp_path / "absent/data"), database=str(tmp_path / "absent/db.sqlite")
    )
    report = inspect(settings_for(storage))
    assert report["prepared"] is False and report["externally_verified"] is False
    assert check_states(report)["database_initialized"] == "blocked"
    assert report["mail"]["activated"] is False
    assert not (tmp_path / "absent").exists()


def test_valid_example_is_never_automatically_declared_personal(state_env):
    report = inspect(settings_for(state_env.settings), profile_path=ROOT / "profile.example.toml")
    assert report["profile"]["valid"] is True
    assert report["profile"]["operator_declared_personal"] is False
    assert report["prepared"] is False
    assert check_states(report)["personal_profile_operator_declaration"] == "blocked"


def test_local_preparation_does_not_claim_external_verification(state_env, personal_profile):
    report = inspect(
        settings_for(state_env.settings), profile_path=personal_profile, profile_confirmed=True
    )
    assert report["prepared"] is True
    assert report["mail_locally_ready"] is False
    assert report["externally_verified"] is False
    assert "seven_days_continuous_observation" in report["external_verifications_required"]
    assert check_states(report)["notifications_not_activated"] == "attention"
    assert check_states(report)["smtp_configured"] == "attention"


def test_invalid_profile_gives_finite_error_without_private_input(state_env, tmp_path):
    profile = tmp_path / "profile.toml"
    profile.write_text('institution = "PRIVATE_PERSONAL_FIELD"\n')
    report = inspect(settings_for(state_env.settings), profile_path=profile, profile_confirmed=True)
    assert check_states(report)["profile_invalid"] == "blocked"
    assert "PRIVATE_PERSONAL_FIELD" not in json.dumps(report)


@pytest.mark.parametrize(
    "username,password,ready", [("u", "p", True), ("", "p", False), ("u", "", False)]
)
def test_credential_presence_never_outputs_values(state_env, username, password, ready):
    settings = settings_for(state_env.settings)
    settings.smtp = SmtpSettings(
        host="smtp.example.org",
        port=465,
        security="tls",
        username_env="SIGNALNEST_USER",
        password_env="SIGNALNEST_PASSWORD",
    )
    environment = {
        "SIGNALNEST_USER": username,
        "SIGNALNEST_PASSWORD": password,
        "UNRELATED_SECRET": "never-inspected",
    }
    report = inspect(settings, environment=environment)
    assert check_states(report)["smtp_credentials_present"] == ("pass" if ready else "attention")
    assert report["smtp_credentials"] == {
        "configured": True,
        "username_present": bool(username),
        "password_present": bool(password),
    }
    assert "never-inspected" not in json.dumps(report)
    assert "SIGNALNEST_PASSWORD" not in json.dumps(report)


def test_mail_states_and_waits_use_persisted_facts_without_recovering_sending(
    activated, personal_profile
):
    env = activated
    live(env, opportunity())
    plan(env, at=ACTIVATED_AT + 10)
    make_backlog(env, count=5)
    results = iter([ACCEPTED, UNCERTAIN])
    drain_mail(
        env.engine,
        SOURCE,
        SmtpSettings(host="smtp.example.org", port=465, security="tls"),
        DrainOptions(max_messages=2),
        now=lambda: ACTIVATED_AT + 20,
        sender=lambda *args: next(results),
    )
    with env.engine.connect() as connection:
        pending_id = (
            connection.execute(
                sa.select(mail_delivery.c.mail_id).where(mail_delivery.c.state == "pending")
            )
            .scalars()
            .first()
        )
    register_attempt(env.engine, SOURCE, pending_id, at=ACTIVATED_AT + 21, options=DrainOptions())
    set_sending_paused(env.engine, SOURCE, True, at=ACTIVATED_AT + 22)
    settings = settings_for(env.settings)
    settings.smtp = SmtpSettings(host="smtp.example.org", port=465, security="tls")
    settings.mail_runtime = MailRuntimeSettings(enabled=True)
    report = inspect(
        settings, at=ACTIVATED_AT + 40, profile_path=personal_profile, profile_confirmed=True
    )
    mail = report["mail"]
    assert mail["counts"] == {
        "pending": 2,
        "sending": 1,
        "retry": 0,
        "uncertain": 1,
        "accepted": 1,
        "blocked": 0,
    }
    assert mail["paused"] is True and mail["pause_reason"] == "manual"
    assert mail["uncertain_count"] == 2 and mail["uncertain_attempt_count"] == 1
    assert mail["accepted_count"] == 1
    assert mail["max_frozen_to_local_acceptance_seconds"] == 10
    assert mail["oldest_nonaccepted_wait_seconds"] == 28
    assert mail["profile_policy_matches"] is True
    assert report["mail_locally_ready"] is False
    with env.engine.connect() as connection:
        assert (
            connection.execute(
                sa.select(mail_delivery.c.state).where(mail_delivery.c.mail_id == pending_id)
            ).scalar_one()
            == "sending"
        )


def test_profile_policy_difference_requires_explicit_maintenance(activated):
    report = inspect(settings_for(activated.settings), profile_path=ROOT / "profile.example.toml")
    assert report["mail"]["profile_policy_matches"] is False
    assert check_states(report)["active_policy_differs"] == "attention"


def test_source_mismatch_is_not_silently_treated_as_inactive(activated):
    report = inspect(settings_for(activated.settings, source_id="different-source"))
    assert report["mail"]["source_matches"] is False
    assert check_states(report)["notification_source_mismatch"] == "blocked"


def test_observation_retains_cooldown_backlog_and_exact_time(state_env, personal_profile):
    with state_env.engine.begin() as connection:
        set_cooldown_in_transaction(connection, SOURCE, 2000)
        seed_document(connection, "1517:1", discovered_at=500, failed=True, due=1500)
        seed_document(connection, "1517:2", success=600, due=900)
    report = observe_instance(
        settings_for(state_env.settings),
        at=1000,
        release_root=ROOT,
        profile_path=personal_profile,
        profile_confirmed=True,
    )
    assert report["format"] == "signalnest-observation-v1" and report["at"] == 1000
    assert report["collection"]["source"]["cooldown_remaining_seconds"] == 1000
    assert report["collection"]["first_processing"]["total"] == 1
    assert report["collection"]["first_processing"]["deferred"] == 1
    assert report["collection"]["rechecks"]["due"] == 1
    assert report["collection"]["source"]["last_complete_scan_at"] is None
    assert report["read_consistency"] == "separate_read_only_snapshots"


def test_observation_is_read_only_even_while_writer_lock_held(activated, personal_profile):
    settings = settings_for(activated.settings)
    with writer_lock(settings.storage.database):
        before = settings.storage.database.read_bytes()
        names = set(settings.storage.database.parent.rglob("*"))
        inspect(settings, at=ACTIVATED_AT + 100, profile_path=personal_profile)
        assert settings.storage.database.read_bytes() == before
        assert set(settings.storage.database.parent.rglob("*")) == names


def test_mail_read_failure_blocks_preparation_instead_of_becoming_empty_success(
    activated, personal_profile, monkeypatch
):
    def fail(*args, **kwargs):
        raise MailError("mail_database_read_failed")

    monkeypatch.setattr("signalnest.rollout.mail_status", fail)
    report = inspect(
        settings_for(activated.settings),
        at=ACTIVATED_AT + 10,
        profile_path=personal_profile,
        profile_confirmed=True,
    )
    assert report["prepared"] is False
    assert report["mail"]["error_code"] == "rollout_mail_read_failed"


@pytest.mark.parametrize("at", [-1, True, 1.0, "1000", 2**63])
def test_invalid_observation_time_fails_before_file_or_database_access(tmp_path, at):
    settings = settings_for(
        StorageSettings(data_dir=str(tmp_path / "data"), database=str(tmp_path / "db.sqlite"))
    )
    with pytest.raises(RolloutError, match="rollout_time_invalid"):
        inspect_rollout(settings, at=at, release_root=tmp_path / "absent")
    assert not list(tmp_path.iterdir())


def test_non_boolean_profile_confirmation_fails(state_env):
    with pytest.raises(RolloutError, match="profile_confirmation_invalid"):
        inspect(settings_for(state_env.settings), profile_confirmed="yes")


def test_untrusted_pause_reason_is_filtered(activated):
    with activated.engine.begin() as connection:
        connection.execute(
            notification_channel_state.update().values(
                pause_reason="SECRET password <html>", paused=True
            )
        )
    report = inspect(settings_for(activated.settings), at=ACTIVATED_AT + 10)
    assert report["mail"]["pause_reason"] == "unrecognized_error_code"
    assert "SECRET" not in json.dumps(report)


def test_test_profile_matches_production_fixture(personal_profile):
    from signalnest.notifications.profile import load_profile

    assert load_profile(personal_profile) == USER_PROFILE


def test_release_snapshot_preserves_actual_bytes_and_safe_tar_metadata(tmp_path):
    source = tmp_path / "source"
    write_minimal_release(source)
    (source / "config.personal.toml").write_text("do-not-archive-profile")
    (source / "smtp.env").write_text("PASSWORD=do-not-archive-secret")
    output = tmp_path / "release.tar.gz"
    result = create_release_snapshot(source, output)
    manifest = json.loads(Path(result["manifest"]).read_text())
    assert result["archive_sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert manifest["archive_sha256"] == result["archive_sha256"]
    assert manifest["archive_bytes"] == output.stat().st_size
    with tarfile.open(output, "r:gz") as archive:
        names = archive.getnames()
        assert names == sorted(names)
        assert "signalnest/README.md" in names
        assert "signalnest/smtp.env" not in names
        content_manifest = json.load(archive.extractfile("signalnest/RELEASE.json"))
        assert "git_head" not in content_manifest and "runtime" not in content_manifest
        for entry in archive:
            assert entry.isfile()
            assert entry.uid == entry.gid == entry.mtime == 0
            assert entry.mode == 0o644
            assert entry.uname == entry.gname == ""
            if entry.name != "signalnest/RELEASE.json":
                relative = entry.name.removeprefix("signalnest/")
                data = archive.extractfile(entry).read()
                assert data == (source / relative).read_bytes()
                assert hashlib.sha256(data).hexdigest() == content_manifest["file_sha256"][relative]
    assert b"do-not-archive-secret" not in output.read_bytes()
    assert not list(tmp_path.glob(".signalnest-release-*"))


def test_release_archive_determinism_excludes_git_metadata(tmp_path, monkeypatch):
    source = tmp_path / "source"
    write_minimal_release(source)
    current_head = ["a" * 40]
    monkeypatch.setattr(
        "signalnest.rollout._git",
        lambda root, *args: (
            current_head[0] if args[0] == "rev-parse" else " M src/signalnest/__init__.py"
        ),
    )
    first = create_release_snapshot(source, tmp_path / "first.tar.gz")
    current_head[0] = "b" * 40
    second = create_release_snapshot(source, tmp_path / "second.tar.gz")
    assert Path(first["archive"]).read_bytes() == Path(second["archive"]).read_bytes()
    assert first["archive_sha256"] == second["archive_sha256"]
    assert json.loads(Path(first["manifest"]).read_text())["release"]["git_head"] == "a" * 40
    assert json.loads(Path(second["manifest"]).read_text())["release"]["git_head"] == "b" * 40


@pytest.mark.parametrize("existing", ["archive", "manifest"])
def test_release_never_overwrites_existing_target_or_sidecar(tmp_path, existing):
    source = tmp_path / "source"
    write_minimal_release(source)
    output = tmp_path / "release.tar.gz"
    target = output if existing == "archive" else output.with_name(output.name + ".manifest.json")
    target.write_bytes(b"existing-evidence")
    with pytest.raises(RolloutError, match="release_output_exists"):
        create_release_snapshot(source, output)
    assert target.read_bytes() == b"existing-evidence"
    assert set(tmp_path.iterdir()) == {source, target}


def test_release_refuses_output_inside_source_or_symlinked_parent(tmp_path):
    source = tmp_path / "source"
    write_minimal_release(source)
    with pytest.raises(RolloutError, match="release_output_inside_source"):
        create_release_snapshot(source, source / "release.tar.gz")
    (tmp_path / "alias").symlink_to(source, target_is_directory=True)
    with pytest.raises(RolloutError, match="release_output_unsafe"):
        create_release_snapshot(source, tmp_path / "alias/release.tar.gz")
    assert not (source / "release.tar.gz").exists()


def test_release_refuses_output_symlink_without_reading_or_modifying_target(tmp_path):
    source = tmp_path / "source"
    write_minimal_release(source)
    target = tmp_path / "private-file"
    target.write_bytes(b"private-existing-data")
    output = tmp_path / "release.tar.gz"
    output.symlink_to(target)
    with pytest.raises(RolloutError, match="release_output_unsafe"):
        create_release_snapshot(source, output)
    assert target.read_bytes() == b"private-existing-data" and output.is_symlink()


def test_release_detects_source_changes_before_publish(tmp_path, monkeypatch):
    from signalnest import rollout

    source = tmp_path / "source"
    write_minimal_release(source)
    target = source / "src/signalnest/__init__.py"
    reads = 0
    original = rollout._file_bytes

    def changed(path, root):
        nonlocal reads
        if path == target:
            reads += 1
            if reads == 2:
                target.write_text('"""Changed after manifest."""\n')
        return original(path, root)

    monkeypatch.setattr(rollout, "_file_bytes", changed)
    with pytest.raises(RolloutError, match="release_changed_during_snapshot"):
        create_release_snapshot(source, tmp_path / "release.tar.gz")
    assert set(tmp_path.iterdir()) == {source}


def test_release_publish_failure_removes_only_own_partial_pair(tmp_path, monkeypatch):
    import os

    source = tmp_path / "source"
    write_minimal_release(source)
    original_link = os.link
    links = 0

    def fail_second_link(*args, **kwargs):
        nonlocal links
        links += 1
        if links == 2:
            raise OSError("synthetic publication failure")
        return original_link(*args, **kwargs)

    monkeypatch.setattr("signalnest.rollout.os.link", fail_second_link)
    with pytest.raises(RolloutError, match="release_snapshot_write_failed"):
        create_release_snapshot(source, tmp_path / "release.tar.gz")
    assert set(tmp_path.iterdir()) == {source}


@pytest.mark.parametrize(
    "kind,code",
    [
        ("unknown-root", "release_root_invalid"),
        ("suffix", "release_output_suffix_invalid"),
        ("missing-parent", "release_output_parent_missing"),
    ],
)
def test_release_invalid_locations_fail_clearly(tmp_path, kind, code):
    source = tmp_path / "source"
    write_minimal_release(source)
    output = tmp_path / "release.tar.gz"
    if kind == "unknown-root":
        source = tmp_path / "unrelated"
    elif kind == "suffix":
        output = tmp_path / "release.zip"
    else:
        output = tmp_path / "absent/release.tar.gz"
    with pytest.raises(RolloutError, match=code):
        create_release_snapshot(source, output)
    assert not output.exists()


@pytest.mark.parametrize(
    "project", ['project = "other"\n', '[project]\nname="other"\n', "bad TOML"]
)
def test_release_refuses_unrecognized_project_metadata(tmp_path, project):
    write_minimal_release(tmp_path)
    (tmp_path / "pyproject.toml").write_text(project)
    with pytest.raises(RolloutError, match="release_root_invalid"):
        build_release_manifest(tmp_path)


def test_release_tool_real_behavior_exit_codes_and_sidecar(tmp_path, capsys):
    spec = importlib.util.spec_from_file_location(
        "release_snapshot", ROOT / "deploy/release_snapshot.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    source = tmp_path / "source"
    write_minimal_release(source)
    arguments = ["--release-root", str(source), "--output", str(tmp_path / "release.tar.gz")]
    assert module.main(arguments) == 0
    result = json.loads(capsys.readouterr().out)
    assert Path(result["archive"]).is_file() and Path(result["manifest"]).is_file()
    assert module.main(arguments) == 1
    captured = capsys.readouterr()
    assert not captured.out
    assert json.loads(captured.err) == {"error_code": "release_output_exists"}


def test_release_tool_help_never_reads_source_or_writes_output(tmp_path, capsys, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "release_snapshot", ROOT / "deploy/release_snapshot.py"
    )
    release_snapshot = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(release_snapshot)

    def forbidden(*args, **kwargs):
        raise AssertionError("Help must not build a release")

    monkeypatch.setattr(release_snapshot, "create_release_snapshot", forbidden)
    with pytest.raises(SystemExit) as exc:
        release_snapshot.main(["--help"])
    assert exc.value.code == 0
    assert "--release-root" in capsys.readouterr().out
    assert not list(tmp_path.iterdir())


def test_credential_values_are_never_serialized_or_fingerprinted(state_env):
    settings = settings_for(state_env.settings)
    settings.smtp = SmtpSettings(
        host="smtp.example.org",
        port=465,
        security="tls",
        username_env="USER_VAR",
        password_env="PASSWORD_VAR",
    )
    first = inspect(
        settings,
        environment={
            "USER_VAR": "secret-username-unique-43",
            "PASSWORD_VAR": "secret-password-unique-72",
        },
    )
    second = inspect(
        settings,
        environment={
            "USER_VAR": "different-username-unique-83",
            "PASSWORD_VAR": "different-password-unique-91",
        },
    )
    assert first == second
    assert "secret-username-unique-43" not in json.dumps(first)
    assert "secret-password-unique-72" not in json.dumps(first)
