"""Recorded snapshots remain read-only evidence, including missing intervals."""

import copy
import json
import runpy
from pathlib import Path

import pytest
from test_observe_deployment import observation_config as _observation_config
from test_status import seed_document

from signalnest.config import load_config
from signalnest.instance_lock import writer_lock
from signalnest.observation_summary import (
    ObservationError,
    read_observations,
    summarize_observations,
)
from signalnest.rollout import observe_instance

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "whu-undergrad-student"
ENTRY = ROOT / "deploy/summarize_observations.py"

observation_config = _observation_config


def observation(config, at):
    settings = load_config(config[0])
    return observe_instance(
        settings, at=at, release_root=ROOT, profile_path=config[1], profile_confirmed=True
    )


def summarize(records, start=0, end=12000, sources=(SOURCE,)):
    return summarize_observations(records, start=start, end=end, expected_sources=sources)


def test_actual_snapshots_net_backlog_and_duplicate_exports_are_read_only(
    state_env, observation_config, tmp_path
):
    with state_env.engine.begin() as connection:
        seed_document(connection, "1517:123", failed=True, due=100)
    first = observation(observation_config, 100)
    with state_env.engine.begin() as connection:
        seed_document(connection, "1517:124", due=100)
    second = observation(observation_config, 3700)
    log = tmp_path / "observations.jsonl"
    log.write_text("\n".join(json.dumps(r) for r in (second, first, first)) + "\n")
    before = state_env.settings.database.read_bytes(), log.read_bytes()
    with writer_lock(state_env.settings.database):
        records, diagnostics = read_observations([log])
        result = summarize(records, end=4000)
    source = result["sources"][SOURCE]
    assert source["sample_count"] == 2
    assert source["net_changes"] == {
        "documents": 1,
        "processed": 0,
        "failed": 0,
        "first_backlog": 1,
    }
    assert source["gaps"] == []
    assert result["duplicate_samples"] == 1
    assert diagnostics["unreadable_journal_messages"] == 0
    assert before == (state_env.settings.database.read_bytes(), log.read_bytes())


def test_missing_middle_tail_and_entire_source_are_not_assumed_online(observation_config):
    points = [observation(observation_config, at) for at in (100, 3700, 10900)]
    result = summarize(points, sources=(SOURCE, "whu-cs-undergrad-teaching"))
    assert result["sources"][SOURCE]["gaps"] == [{"from_at": 3700, "to_at": 10900, "seconds": 7200}]
    missing = result["sources"]["whu-cs-undergrad-teaching"]
    assert missing["sample_count"] == 0 and missing["net_changes"] is None
    assert missing["gaps"] == [{"from_at": 0, "to_at": 12000, "seconds": 12000}]
    assert summarize(points, end=16000)["sources"][SOURCE]["gaps"][-1]["to_at"] == 16000


def test_coverage_is_computed_from_persisted_timestamp_not_claimed_age(observation_config):
    point = observation(observation_config, 100000)
    point["collection"]["source"].update(last_complete_scan_at=1, complete_scan_age_seconds=0)
    result = summarize([point], end=100000)
    source = result["sources"][SOURCE]
    assert source["coverage_stale_at"] == [100000]
    assert source["points"][0]["collection"]["complete_age_seconds"] == 99999
    assert source["points"][0]["collection"]["coverage"] == "stale"


def test_seven_days_elapsed_never_claims_acceptance_and_missing_database_stays_unknown(
    observation_config,
):
    point = observation(observation_config, 100)
    point["collection"] = {"error_code": "storage_unavailable_or_uninitialized"}
    result = summarize([point], end=7 * 86400)
    assert result["seven_days_elapsed"] is True
    assert result["rollout_acceptance"] == "requires_human_evidence_review"
    assert result["sources"][SOURCE]["collection_unavailable_at"] == [100]
    assert result["sources"][SOURCE]["net_changes"] is None


def test_mail_pause_is_observed_without_revealing_addresses_or_secret_fields(observation_config):
    point = observation(observation_config, 100)
    point["mail"] = {
        "activated": True,
        "paused": True,
        "counts": dict.fromkeys(
            ("pending", "sending", "retry", "uncertain", "accepted", "blocked"), 0
        ),
        "unallocated_digest": 1,
        "unplanned_immediate": 0,
        "plan_error_records": 0,
        "secret_password": "never-output",
        "sender": "never-output",
    }
    result = summarize([point], end=100)
    assert result["sources"][SOURCE]["mail_paused_at"] == [100]
    assert "never-output" not in json.dumps(result)
    assert result["sources"][SOURCE]["points"][0]["mail"]["unallocated_digest"] == 1


def test_journal_exports_report_null_and_ordinary_messages_without_inventing_data(
    observation_config, tmp_path
):
    point = observation(observation_config, 100)
    path = tmp_path / "journal.jsonl"
    path.write_text(
        "\n".join(
            json.dumps(row)
            for row in (
                {"MESSAGE": "Started read-only observation"},
                {"MESSAGE": None},
                {"MESSAGE": json.dumps(point)},
                {"MESSAGE": '{"event":"collection_success"}'},
            )
        )
    )
    records, diagnostics = read_observations([path])
    assert records == [point]
    assert diagnostics == {"ignored_journal_messages": 2, "unreadable_journal_messages": 1}


@pytest.mark.parametrize(
    "change",
    [
        lambda p: p.update(at=True),
        lambda p: p.update(source_id="invalid source"),
        lambda p: p["collection"].update(source_id="wrong-source"),
        lambda p: p["collection"]["source"].update(last_complete_scan_at=999999),
        lambda p: p["collection"]["documents"].update(total=-1),
    ],
)
def test_invalid_or_misbound_records_fail_as_a_whole(observation_config, change):
    point = observation(observation_config, 100)
    change(point)
    with pytest.raises(ObservationError):
        summarize([point])


def test_same_time_conflicting_snapshot_and_unexpected_source_fail(observation_config):
    point = observation(observation_config, 100)
    other = copy.deepcopy(point)
    other["collection"]["documents"]["failed"] = 9
    with pytest.raises(ObservationError, match="duplicate_conflict"):
        summarize([point, other])
    with pytest.raises(ObservationError, match="source_unexpected"):
        summarize([point], sources=("other",))


@pytest.mark.parametrize(
    "arguments",
    [
        {"start": 100, "end": 99},
        {"start": True, "end": 100},
        {"interval_seconds": 0},
        {"tolerance_seconds": 3600},
        {"expected_sources": ()},
    ],
)
def test_invalid_window_schedule_is_rejected(arguments):
    values = {"start": 0, "end": 100, "expected_sources": (SOURCE,)} | arguments
    with pytest.raises(ObservationError):
        summarize_observations([], **values)


def test_cli_real_file_help_and_bad_input_have_no_instance_side_effects(
    observation_config, tmp_path, capsys
):
    main = runpy.run_path(str(ENTRY))["main"]
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    capsys.readouterr()
    path = tmp_path / "export.jsonl"
    path.write_text(json.dumps(observation(observation_config, 100)) + "\n")
    assert main([str(path), "--source", SOURCE, "--start", "0", "--end", "100"]) == 0
    assert json.loads(capsys.readouterr().out)["sources"][SOURCE]["sample_count"] == 1
    path.write_text('{"format":"signalnest-observation-v1",')
    assert main([str(path), "--source", SOURCE, "--start", "0"]) == 2
    assert "observation_json_invalid" in capsys.readouterr().err


def test_outside_window_is_counted_and_input_failure_is_not_silent(observation_config, tmp_path):
    result = summarize([observation(observation_config, 100)], start=101, end=400)
    assert result["outside_window_samples"] == 1
    with pytest.raises(ObservationError, match="file_unavailable"):
        read_observations([tmp_path / "absent"])
    path = tmp_path / "nulls.jsonl"
    path.write_text(json.dumps({"MESSAGE": '{"format":"signalnest-observation-v1",'}) + "\n")
    with pytest.raises(ObservationError, match="record_truncated"):
        read_observations([path])
