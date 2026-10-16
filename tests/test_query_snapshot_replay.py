"""Daily-use replay budgets and actual read-only fixture services, without live HTTP."""

import json
import runpy
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from test_query_agent import AT, completion, digests
from test_query_agent import campus as campus
from test_query_snapshots import AT as SNAPSHOT_AT
from test_query_snapshots import snapshots as snapshots

from signalnest.querying.agent import ask
from signalnest.querying.config import load_query_config
from signalnest.querying.snapshots import validate_snapshot

ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "deploy/replay_query_snapshot.py"
MODULE = runpy.run_path(str(ENTRY))
replay_snapshot = MODULE["replay_snapshot"]
ReplayError = MODULE["ReplayError"]


def configure(campus, *, priced=False):
    model = (
        '\n[model]\nmodel="test-model"\napi_key_env="SIGNALNEST_REPLAY_TEST_KEY"\n'
        'input_usd_per_million="1"\noutput_usd_per_million="4"\nmax_cost_usd="0.1"\n'
        if priced
        else '\n[model]\nprovider="openai-compatible-chat-completions"\n'
        'base_url="http://127.0.0.1:8001/v1"\nmodel="test-model"\n'
        'api_key_env="SIGNALNEST_REPLAY_TEST_KEY"\npricing_mode="unpriced_proxy"\n'
    )
    with campus.query_file.open("a") as stream:
        stream.write("\n[limits]\nmax_model_calls=2\nmax_requests=2\nrun_seconds=10.0\n" + model)


def test_real_child_local_replay_queries_both_fixture_instances_without_changes(campus):
    before = digests(campus)
    replay = replay_snapshot(campus.query_file, AT, run_seconds=500.0)
    summary = replay["summary"]
    assert summary["completed_reports"] == summary["human_pending"] == 5
    assert summary["human_confirmed"] == summary["observed_http_requests"] == 0
    assert summary["degraded_cases"] == []
    assert summary["database_bytes_unchanged"] is True
    assert replay["query_version"] == "campus-query-v4"
    assert len(replay["source_sha256"]["src/signalnest/querying/agent.py"]) == 64
    assert digests(campus) == before
    ta = replay["results"][1]
    assert ta["report"]["status"] == "local"
    assert ta["report"]["search_results"][0]["instance_id"] == "cs"
    assert {s["instance_id"] for s in ta["report"]["source_status"]} == {"uc", "cs"}
    assert all(r["human_review"]["status"] == "pending" for r in replay["results"])


@pytest.mark.parametrize(
    "change",
    [
        {"max_total_requests": 9},
        {"run_seconds": 79.0},
        {"max_total_cost_usd": Decimal("0.49")},
    ],
)
def test_insufficient_full_allocations_refused_before_any_query(campus, change):
    configure(campus, priced=True)
    called = []
    options = {"max_total_requests": 10, "run_seconds": 80.0, "max_total_cost_usd": Decimal("0.5")}
    with pytest.raises(ReplayError, match="query_replay_full_allocations_required"):
        replay_snapshot(
            campus.query_file,
            AT,
            local=False,
            runner=lambda *args, **kwargs: called.append(True),
            **(options | change),
        )
    assert called == []


def test_mock_model_failure_uses_own_budget_and_does_not_starve_later_cases(campus, monkeypatch):
    configure(campus)
    monkeypatch.setenv("SIGNALNEST_REPLAY_TEST_KEY", "offline-placeholder")
    calls, used = [], []

    def runner(config, question, *, local, timeout):
        settings = load_query_config(config)
        calls.append((settings.limits.max_requests, settings.limits.max_model_calls, timeout))
        current = len(calls)

        def transport(request):
            used.append(current)
            # First question exhausts its complete two-request allocation.
            if current == 1:
                return httpx.Response(503)
            payload = json.loads(request.content)
            assert payload["tool_choice"] == "required"
            offered = [tool["function"]["name"] for tool in payload["tools"]]
            if offered == ["read_notice"]:
                initial = json.loads(payload["messages"][1]["content"])
                hit = next(
                    item
                    for result in initial["initial_retrieval"]
                    for source in result["data"]["sources"]
                    for item in source.get("items", [])
                )
                return httpx.Response(
                    200,
                    json=completion(
                        "read_notice",
                        {"instance_id": hit["instance_id"], "document_id": hit["document_id"]},
                        "read",
                    ),
                )
            assert offered == ["answer"]
            return httpx.Response(200, json=completion("answer", {"notices": []}))

        with httpx.Client(transport=httpx.MockTransport(transport)) as client:
            return ask(settings, question, local=local, client=client), None

    before = digests(campus)
    replay = replay_snapshot(
        campus.query_file,
        AT,
        local=False,
        max_total_requests=10,
        run_seconds=80.0,
        runner=runner,
    )
    assert calls == [(2, 2, 15.0)] * 5
    assert used == [1, 1, 2, 2, 3, 3, 4, 4, 5]
    assert replay["results"][0]["report"]["error_code"] == "model_http_failed"
    assert replay["summary"]["answered_cases"] == list(MODULE["QUESTIONS"])[1:]
    assert replay["summary"]["observed_http_requests"] == 9
    assert replay["summary"]["reserved_http_requests"] == 10
    assert replay["summary"]["human_confirmed"] == 0
    assert replay["summary"]["human_pending"] == 5
    assert digests(campus) == before


def test_worker_timeout_usage_is_unknown_and_later_case_keeps_full_budget(campus):
    configure(campus)
    seen = []

    def runner(config, question, *, local, timeout):
        seen.append(timeout)
        if len(seen) == 1:
            return None, "query_worker_wall_timeout"
        return ask(load_query_config(config), question, local=True), None

    replay = replay_snapshot(
        campus.query_file,
        AT,
        case_ids=["research", "cs-ta"],
        local=False,
        max_total_requests=4,
        run_seconds=35.0,
        runner=runner,
    )
    assert seen == [15.0, 15.0]
    assert replay["summary"]["http_requests_unknown_workers"] == 1
    assert replay["summary"]["http_requests_upper_bound"] == 2
    assert replay["summary"]["degraded_cases"] == ["research"]


def test_real_subprocess_wall_watchdog_terminates_a_stuck_worker(tmp_path, monkeypatch):
    slow = tmp_path / "slow.py"
    slow.write_text("import time\ntime.sleep(5)\n")
    worker = MODULE["_run_worker"]
    monkeypatch.setitem(worker.__globals__, "__file__", str(slow))
    question = MODULE["Question"](text="助教招聘", at=AT)
    report, error = worker(tmp_path / "unused.toml", question, local=True, timeout=0.15)
    assert report is None and error == "query_worker_wall_timeout"


def test_child_failure_is_finite_without_echoing_raw_stderr(tmp_path, monkeypatch):
    faulty = tmp_path / "faulty.py"
    faulty.write_text(
        "import sys\nprint('private-child-output', file=sys.stderr)\nraise SystemExit(2)\n"
    )
    worker = MODULE["_run_worker"]
    monkeypatch.setitem(worker.__globals__, "__file__", str(faulty))
    report, error = worker(
        tmp_path / "unused.toml", MODULE["Question"](text="科研", at=AT), local=True, timeout=1.0
    )
    assert report is None and error == "query_worker_failed"


def test_database_mutation_during_query_is_rejected(campus):
    def runner(config, question, **kwargs):
        report = ask(load_query_config(config), question, local=True)
        with campus.instances["uc"].storage.database.open("ab") as stream:
            stream.write(b"changed")
        return report, None

    with pytest.raises(ReplayError, match="query_snapshot_input_changed"):
        replay_snapshot(campus.query_file, AT, runner=runner)


def test_per_instance_snapshot_manifests_are_bound_in_replay_report(snapshots):
    output, *_ = snapshots
    replay = replay_snapshot(
        output / "ask.local.toml", SNAPSHOT_AT, case_ids=["cs-ta"], run_seconds=100.0
    )
    inputs = replay["input_sha256"]
    for identifier in ("uc", "cs"):
        assert inputs[f"snapshot_manifest:{identifier}"] == MODULE["_digest"](
            output / identifier / "snapshot.json"
        )


def test_valid_manifest_provenance_change_during_query_is_rejected(snapshots):
    output, *_ = snapshots

    def runner(config, question, **kwargs):
        settings = load_query_config(config)
        report = ask(settings, question, local=True)
        manifest = output / "uc/snapshot.json"
        value = json.loads(manifest.read_text())
        value["prepared_at"] += 1
        manifest.write_text(json.dumps(value))
        # The file remains a valid DB binding; only replay immutability detects
        # this change to the supplied preparation evidence.
        validate_snapshot(
            manifest,
            output / "uc/signalnest.sqlite3",
            "uc",
            value["facts"]["source_id"],
        )
        return report, None

    with pytest.raises(ReplayError, match="query_snapshot_input_changed"):
        replay_snapshot(
            output / "ask.local.toml",
            SNAPSHOT_AT,
            case_ids=["cs-ta"],
            run_seconds=100.0,
            runner=runner,
        )


def test_source_changes_during_query_are_rejected_without_rewriting_any_source(campus, monkeypatch):
    versions = iter([{"source.py": "before"}, {"source.py": "after"}])
    monkeypatch.setitem(replay_snapshot.__globals__, "_source_hashes", lambda: next(versions))

    def runner(config, question, **kwargs):
        return ask(load_query_config(config), question, local=True), None

    with pytest.raises(ReplayError, match="query_replay_source_changed"):
        replay_snapshot(campus.query_file, AT, runner=runner)


def test_worker_query_version_must_match_parent_code(campus):
    def runner(config, question, **kwargs):
        report = ask(load_query_config(config), question, local=True)
        return report.model_copy(update={"query_version": "obsolete-query-v0"}), None

    with pytest.raises(ReplayError, match="query_worker_version_mismatch"):
        replay_snapshot(campus.query_file, AT, runner=runner)


def test_uncheckpointed_wal_and_unpriced_dollar_cap_are_rejected(campus):
    configure(campus)
    with pytest.raises(ReplayError, match="query_replay_usd_budget_unavailable"):
        replay_snapshot(campus.query_file, AT, local=False, max_total_cost_usd=Decimal("1"))
    database = campus.instances["uc"].storage.database
    database.with_name(database.name + "-wal").write_bytes(b"uncheckpointed")
    with pytest.raises(ReplayError, match="snapshot_has_live_wal"):
        replay_snapshot(campus.query_file, AT)


def test_cli_new_private_report_actual_local_worker_and_no_overwrite(campus, tmp_path):
    output = tmp_path / "replay.json"
    command = [
        sys.executable,
        str(ENTRY),
        "--config",
        str(campus.query_file),
        "--at",
        AT.isoformat(),
        "--case",
        "cs-ta",
        "--output",
        str(output),
    ]
    before = digests(campus)
    completed = subprocess.run(command, capture_output=True, check=False)
    assert completed.returncode == 0, completed.stderr
    assert output.stat().st_mode & 0o777 == 0o600
    assert json.loads(output.read_text())["summary"]["completed_reports"] == 1
    assert b"search_results" not in completed.stdout
    old = output.read_bytes()
    duplicate = subprocess.run(command, capture_output=True, check=False)
    assert duplicate.returncode == 2
    assert output.read_bytes() == old
    assert digests(campus) == before


def test_cli_invalid_budget_cleans_reserved_output_without_model_requests(campus, tmp_path):
    configure(campus)
    output = tmp_path / "invalid.json"
    code = MODULE["main"](
        [
            "--config",
            str(campus.query_file),
            "--at",
            AT.isoformat(),
            "--model",
            "--max-total-requests",
            "1",
            "--output",
            str(output),
        ]
    )
    assert code == 2
    assert not output.exists()


def test_help_does_not_create_storage_or_require_a_key(tmp_path):
    result = subprocess.run(
        [sys.executable, str(ENTRY), "--help"], cwd=tmp_path, capture_output=True, check=False
    )
    assert result.returncode == 0
    assert "--model" in result.stdout.decode()
    assert list(tmp_path.iterdir()) == []
