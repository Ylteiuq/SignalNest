"""The evaluation replays immutable evidence through the actual production code."""

import hashlib
import json
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy/evaluate_notifications.py"
EVALUATION = runpy.run_path(str(SCRIPT))
evaluate_cases = EVALUATION["evaluate_cases"]
EvaluationError = EVALUATION["EvaluationError"]
MANIFEST = ROOT / "docs/validation/notification-production-cases.json"


def test_production_replay_is_deterministic_and_keeps_real_fixture_evidence():
    before = {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (ROOT / "research/fixtures").rglob("*")
        if path.is_file()
    }
    first, second = evaluate_cases(), evaluate_cases()
    assert first["results"] == second["results"]
    assert first["summary"]["cases"] == first["summary"]["parse_success"] == 13
    assert first["summary"]["unique_pages"] == 8
    assert first["summary"]["human_gold"] is False
    assert first["summary"]["engineering_mismatch_cases"] == []
    assert first["manifest_sha256"] == hashlib.sha256(MANIFEST.read_bytes()).hexdigest()
    rows = {row["case_id"]: row for row in first["results"]}
    assert rows["P02"]["decision"]["action"] == "STORE_ONLY"
    assert rows["P12"]["decision"]["action"] == "IGNORE"
    assert rows["P13"]["decision"]["action"] == "PUSH_NOW"
    assert rows["P04"]["decision"]["action"] == "PUSH_NOW"
    assert rows["P11"]["decision"]["action"] == "DIGEST"
    for row in first["results"]:
        assert "body_text" not in row["facts"]
        assert row["decision"]["content_sha256"] == row["facts"]["content_sha256"]
        assert row["fixture_sha256"] == before[ROOT / row["fixture"]]
    assert before == {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in before}


@pytest.mark.parametrize("path", ["../outside.html", "/tmp/outside.html", "README.md"])
def test_fixture_paths_are_confined_to_existing_research_evidence(tmp_path, path):
    manifest = json.loads(MANIFEST.read_text())
    manifest["cases"][0]["fixture"] = path
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps(manifest))
    with pytest.raises(EvaluationError, match="evaluation_fixture_path_invalid"):
        evaluate_cases(cases)


def test_fixture_hash_mismatch_is_rejected_without_changing_original(tmp_path):
    manifest = json.loads(MANIFEST.read_text())
    manifest["cases"][0]["sha256"] = "0" * 64
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps(manifest))
    with pytest.raises(EvaluationError, match="evaluation_fixture_digest_mismatch"):
        evaluate_cases(cases)


@pytest.mark.parametrize("clock", ["2026-09-28T09:00:00", "tomorrow", "2026-10-01T09:00:00+08:00"])
def test_clocks_must_be_explicit_and_precede_the_next_digest(tmp_path, clock):
    manifest = json.loads(MANIFEST.read_text())
    manifest["cases"][0]["evaluated_at"] = clock
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps(manifest))
    with pytest.raises(EvaluationError, match="evaluation_clock_invalid"):
        evaluate_cases(cases)


def test_source_change_during_replay_is_rejected(monkeypatch):
    original = evaluate_cases.__globals__["source_hashes"]
    calls = 0

    def changed():
        nonlocal calls
        calls += 1
        return original() if calls < 3 else {"changed": "concurrent-edit"}

    monkeypatch.setitem(evaluate_cases.__globals__, "source_hashes", changed)
    with pytest.raises(EvaluationError, match="evaluation_source_changed"):
        evaluate_cases()


def test_cli_runs_from_other_directory_and_refuses_to_replace_snapshots(tmp_path):
    output = tmp_path / "report.json"
    command = [sys.executable, str(SCRIPT), "--check", "--output", str(output)]
    child = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True)
    assert child.returncode == 0, child.stderr
    saved = output.read_bytes()
    assert not child.stdout
    assert json.loads(saved)["summary"]["engineering_mismatch_cases"] == []
    again = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True)
    assert again.returncode == 2
    assert "evaluation_file_unavailable" in again.stderr
    assert output.read_bytes() == saved
    assert sorted(path.name for path in tmp_path.iterdir()) == ["report.json"]


def test_cli_check_reports_regression_mismatch_without_relabeling_result(tmp_path):
    manifest = json.loads(MANIFEST.read_text())
    manifest["cases"][0]["engineering_expectation"]["action"] = "PUSH_NOW"
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps(manifest))
    child = subprocess.run(
        [sys.executable, str(SCRIPT), "--cases", str(cases), "--check"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert child.returncode == 1
    result = json.loads(child.stdout)
    assert result["summary"]["engineering_mismatch_cases"] == ["P01"]
    assert result["results"][0]["decision"]["action"] == "STORE_ONLY"


def test_cli_help_is_independent_of_fixture_or_storage_access(tmp_path):
    child = subprocess.run(
        [sys.executable, str(SCRIPT), "--cases", "missing.json", "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert child.returncode == 0
    assert "--check" in child.stdout
    assert list(tmp_path.iterdir()) == []
