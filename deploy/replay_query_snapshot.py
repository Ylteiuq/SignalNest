"""Replay five daily-use questions on existing read-only snapshots; local unless --model.

Full reports contain notice text and profile-derived evidence. Keep them outside
the repository. No human relevance or answer-quality labels are inferred here.
"""

import argparse
import hashlib
import json
import math
import os
import runpy
import subprocess
import sys
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from pydantic import ValidationError

from signalnest.config import ConfigurationError
from signalnest.querying.agent import ask
from signalnest.querying.config import load_query_config, load_query_inputs
from signalnest.querying.contracts import QueryReport, Question

ROOT = Path(__file__).resolve().parents[1]
WORKER_OVERHEAD_SECONDS = 5.0
QUESTIONS = {
    "research": "最近一个月有没有本科生可以报名的科研训练机会？列出报名条件和截止时间。",
    "cs-ta": "计算机学院有没有本科生可以申请的助教招聘？列出对象、报名方式、截止和待核对条件。",
    "applicant": "科研训练通知中，哪些供学生报名，哪些只是向教师征集课题？不要混用两类截止。",
    "ambiguous-deadline": (
        "科研训练或助教通知若只写近期或另行通知，能确认报名截止吗？给出依据，未知保留未知。"
    ),
    "no-match": "有没有量子海豚星际考古招聘报名机会？没有匹配时请明确检索范围。",
}


class ReplayError(ValueError):
    """Finite local replay failure; never echo a credential or raw worker output."""


def _source_hashes():
    # Load without running evaluate_queries.main, including when this script is
    # invoked from outside the repository and deploy is absent from sys.path.
    return runpy.run_path(str(ROOT / "deploy/evaluate_queries.py"))["source_hashes"]()


def _digest(path):
    try:
        with Path(path).open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError as exc:
        raise ReplayError("snapshot_input_unreadable") from exc


def _inputs(config, settings, manifest):
    instances, _ = load_query_inputs(settings)
    paths = {"query_config": config}
    for binding in settings.instances:
        paths[f"instance_config:{binding.id}"] = binding.config
        if binding.snapshot_manifest is not None:
            paths[f"snapshot_manifest:{binding.id}"] = binding.snapshot_manifest
        database = instances[binding.id].storage.database
        # An uncheckpointed/live WAL is not the immutable snapshot this replay needs.
        wal = database.with_name(database.name + "-wal")
        if wal.exists() and wal.stat().st_size:
            raise ReplayError("snapshot_has_live_wal")
        paths[f"database:{binding.id}"] = database
    if settings.profile:
        paths["profile"] = settings.profile
    if manifest:
        paths["snapshot_manifest"] = manifest
    return paths, {key: _digest(path) for key, path in paths.items()}


def _run_worker(config, question, *, local, timeout):
    command = [sys.executable, str(Path(__file__).resolve()), "--worker", str(config)]
    if local:
        command.append("--local")
    try:
        completed = subprocess.run(
            command,
            input=question.model_dump_json().encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        # The child may already have submitted a request. Never report zero usage.
        return None, "query_worker_wall_timeout"
    except OSError:
        return None, "query_worker_unavailable"
    if completed.returncode:
        return None, "query_worker_failed"
    try:
        return QueryReport.model_validate_json(completed.stdout), None
    except ValidationError:
        return None, "query_worker_invalid_report"


def replay_snapshot(
    config,
    at,
    *,
    case_ids=None,
    local=True,
    max_total_requests=40,
    run_seconds=480.0,
    max_total_cost_usd=None,
    snapshot_manifest=None,
    runner=None,
    clock=time.monotonic,
):
    """Give every question its configured full allocation, never a shrinking remainder.

    The default runner bounds each ask in a separate process. The batch reserves
    every question's maximum, including unknown usage after a terminated worker;
    unused allocations are not lent to another question. No outer retries occur.
    ``runner`` and ``clock`` are only seams for offline tests.
    """
    config = Path(config).expanduser().resolve()
    settings = load_query_config(config)
    selected = tuple(QUESTIONS) if case_ids is None else tuple(case_ids)
    if (
        not selected
        or len(set(selected)) != len(selected)
        or any(key not in QUESTIONS for key in selected)
    ):
        raise ReplayError("query_replay_cases_invalid")
    questions = [Question(text=QUESTIONS[key], at=at) for key in selected]
    if not local and settings.model is None:
        raise ReplayError("query_replay_model_not_configured")
    count = len(selected)
    requests_needed = 0 if local else count * settings.limits.max_requests
    envelope = settings.limits.run_seconds + WORKER_OVERHEAD_SECONDS
    seconds_needed = count * envelope + WORKER_OVERHEAD_SECONDS
    priced = not local and settings.model.pricing_mode == "token_rates"
    cost_needed = count * settings.model.max_cost_usd if priced else None
    if (
        type(max_total_requests) is not int
        or not 0 <= max_total_requests <= 150
        or type(run_seconds) not in {int, float}
        or not math.isfinite(run_seconds)
        or not 0 < run_seconds <= 3600
        or (
            max_total_cost_usd is not None
            and (
                not isinstance(max_total_cost_usd, Decimal)
                or not max_total_cost_usd.is_finite()
                or not 0 < max_total_cost_usd <= 50
            )
        )
    ):
        raise ReplayError("query_replay_budget_invalid")
    if not priced and max_total_cost_usd is not None:
        raise ReplayError("query_replay_usd_budget_unavailable")
    if (
        max_total_requests < requests_needed
        or run_seconds < seconds_needed
        or (priced and (max_total_cost_usd is None or max_total_cost_usd < cost_needed))
    ):
        raise ReplayError("query_replay_full_allocations_required")
    paths, before = _inputs(config, settings, snapshot_manifest)
    source_before = _source_hashes()
    query_version = QueryReport.model_fields["query_version"].default
    started = datetime.now(UTC).isoformat()
    deadline = clock() + run_seconds
    rows = []
    runner = runner or _run_worker
    for index, (case_id, question) in enumerate(zip(selected, questions, strict=True)):
        # Preserve all later envelopes even if process launch/parent work took time.
        allowance = deadline - clock() - (count - index - 1) * envelope
        if allowance < envelope:
            rows.append(
                {
                    "case_id": case_id,
                    "question": question.model_dump(mode="json"),
                    "error_code": "query_batch_time_reservation",
                    "report": None,
                    "human_review": {"status": "pending", "label_origin": None},
                }
            )
            continue
        report, error = runner(config, question, local=local, timeout=envelope)
        if {key: _digest(path) for key, path in paths.items()} != before:
            raise ReplayError("query_snapshot_input_changed")
        if _source_hashes() != source_before:
            raise ReplayError("query_replay_source_changed")
        if report is not None and report.query_version != query_version:
            raise ReplayError("query_worker_version_mismatch")
        if report is not None and (
            report.model_usage["http_requests"] > settings.limits.max_requests
            or (
                priced
                and Decimal(report.model_usage["charged_estimate_usd"])
                > settings.model.max_cost_usd
            )
        ):
            raise ReplayError("query_worker_budget_overrun_reported")
        rows.append(
            {
                "case_id": case_id,
                "question": question.model_dump(mode="json"),
                "error_code": error or (report.error_code if report is not None else None),
                "report": report.model_dump(mode="json") if report is not None else None,
                "human_review": {"status": "pending", "label_origin": None},
            }
        )
    if _source_hashes() != source_before:
        raise ReplayError("query_replay_source_changed")
    observed_requests = sum(
        row["report"]["model_usage"]["http_requests"] for row in rows if row["report"]
    )
    unknown_workers = sum(
        row["report"] is None and row["error_code"] != "query_batch_time_reservation"
        for row in rows
    )
    observed_cost = (
        sum(
            (
                Decimal(row["report"]["model_usage"]["charged_estimate_usd"])
                for row in rows
                if row["report"]
            ),
            Decimal(0),
        )
        if priced
        else None
    )
    return {
        "format": "signalnest-query-snapshot-replay-v1",
        "started_at": started,
        "finished_at": datetime.now(UTC).isoformat(),
        "at": questions[0].at.isoformat(),
        "input_sha256": before,
        "source_sha256": source_before,
        "query_version": query_version,
        "query_limits_per_case": settings.limits.model_dump(mode="json"),
        "model_config": settings.model.model_dump(mode="json", exclude={"api_key_env"})
        if not local
        else None,
        "results": rows,
        "summary": {
            "mode": "local" if local else "model",
            "total_cases": count,
            "completed_reports": sum(row["report"] is not None for row in rows),
            "answered_cases": [
                row["case_id"]
                for row in rows
                if row["report"] and row["report"]["status"] == "answered"
            ],
            "degraded_cases": [
                row["case_id"]
                for row in rows
                if row["error_code"]
                or (row["report"] and "tool_results_incomplete" in row["report"]["warnings"])
            ],
            "observed_http_requests": observed_requests,
            "http_requests_unknown_workers": unknown_workers if not local else 0,
            "http_requests_upper_bound": observed_requests
            + (unknown_workers * settings.limits.max_requests if not local else 0),
            "reserved_http_requests": requests_needed,
            "max_total_requests": max_total_requests,
            "reserved_wall_seconds": seconds_needed,
            "max_total_seconds": run_seconds,
            "reserved_cost_usd": str(cost_needed) if priced else None,
            "observed_charged_estimate_usd": str(observed_cost) if priced else None,
            "cost_unknown_workers": unknown_workers if priced else None,
            "max_total_cost_usd": str(max_total_cost_usd) if priced else None,
            "cost_is_invoice": False,
            "database_bytes_unchanged": True,
            "human_confirmed": 0,
            "human_pending": count,
        },
        "limitations": [
            "Engineering questions are not human relevance or answer-quality labels.",
            "Answers cover saved, successfully parsed notices; campus coverage is not exhaustive.",
            "A killed worker can have unknown usage; its full allocation remains reserved.",
            "A manifest digest binds supplied evidence but does not certify its backup provenance.",
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--at", required=True, help="explicit query clock including UTC offset")
    parser.add_argument("--case", choices=QUESTIONS, action="append", dest="case_ids")
    parser.add_argument("--model", action="store_true", help="enable actual configured model calls")
    parser.add_argument("--snapshot-manifest", type=Path)
    parser.add_argument("--max-total-requests", type=int, default=40)
    parser.add_argument("--run-seconds", type=float, default=480.0)
    parser.add_argument("--max-total-cost-usd", type=Decimal)
    parser.add_argument(
        "--output", type=Path, required=True, help="new private report outside repo"
    )
    args = parser.parse_args(argv)
    descriptor = None
    try:
        output = args.output.expanduser().resolve()
        if output.is_relative_to(ROOT):
            raise ReplayError("query_replay_output_must_be_private")
        # Reserve an exclusive restricted output before any possible model request.
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        report = replay_snapshot(
            args.config,
            args.at,
            case_ids=args.case_ids,
            local=not args.model,
            max_total_requests=args.max_total_requests,
            run_seconds=args.run_seconds,
            max_total_cost_usd=args.max_total_cost_usd,
            snapshot_manifest=args.snapshot_manifest,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = None
            stream.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(report["summary"], ensure_ascii=False))
        return 1 if report["summary"]["degraded_cases"] else 0
    except (ReplayError, ConfigurationError, ValidationError, OSError) as exc:
        code = str(exc) if isinstance(exc, ReplayError) else "query_replay_input_or_file_invalid"
        print(f"snapshot replay failed: {code}", file=sys.stderr)
        return 2
    finally:
        if descriptor is not None:
            os.close(descriptor)
            output.unlink(missing_ok=True)


def _worker(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("config", type=Path)
    parser.add_argument("--local", action="store_true")
    args = parser.parse_args(argv)
    question = Question.model_validate_json(sys.stdin.buffer.read(8192))
    report = ask(load_query_config(args.config), question, local=args.local)
    print(report.model_dump_json())


if __name__ == "__main__":
    if sys.argv[1:2] == ["--worker"]:
        _worker(sys.argv[2:])
    else:
        raise SystemExit(main())
