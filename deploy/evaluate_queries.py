"""Replay campus questions over temporary real-fixture instances, never production storage.

Default is local retrieval. Model calls require an explicit --model-config, and
retain the normal per-query limits plus one batch time/estimated-cost budget.
Engineering expectations and later human judgments are separate.
"""

import argparse
import hashlib
import json
import platform
import sys
import tempfile
import time
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, ValidationError, model_validator

from signalnest.config import ConfigurationError, load_config
from signalnest.contracts import Contract, Digest, WebUrl
from signalnest.ingestion import ResponseInput, import_page
from signalnest.instance_lock import writer_lock
from signalnest.model_client import ModelConfig
from signalnest.notifications.contracts import Profile
from signalnest.querying.agent import ask
from signalnest.querying.config import InstanceConfig, QueryLimits, QuerySettings, load_query_config
from signalnest.querying.contracts import Question
from signalnest.rawstore import RawStore
from signalnest.schema import documents
from signalnest.search import get_notice
from signalnest.sources import CS, UC
from signalnest.storage import initialize_storage, open_initialized_engine

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = ROOT / "docs/validation/query-cases.json"
ID = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")]
SOURCE_FILES = (
    "deploy/evaluate_queries.py",
    "src/signalnest/model_client.py",
    "src/signalnest/querying/agent.py",
    "src/signalnest/querying/tools.py",
    "src/signalnest/querying/contracts.py",
    "src/signalnest/querying/config.py",
    "src/signalnest/querying/dates.py",
    "src/signalnest/querying/evidence.py",
    "src/signalnest/search.py",
    "src/signalnest/status.py",
    "src/signalnest/parsing.py",
    "src/signalnest/cs_parsing.py",
    "src/signalnest/notifications/facts.py",
    "src/signalnest/notifications/decision.py",
    "src/signalnest/notifications/contracts.py",
)


class EvaluationError(ValueError):
    """Finite validation failures, without arbitrary values, HTTP text or credentials."""


class CorpusItem(Contract):
    corpus_id: ID
    instance_id: Literal["uc", "cs"]
    source_document_id: str = Field(min_length=1, max_length=100)
    fixture: str = Field(min_length=1, max_length=300)
    sha256: Digest
    source_url: WebUrl
    fetched_at: int = Field(strict=True, ge=0)
    provenance: str = Field(min_length=1, max_length=1000)


class EngineeringExpectation(Contract):
    candidate_contains: tuple[ID, ...] = ()
    candidate_absent: tuple[ID, ...] = ()
    date_from: str | None = None
    date_to: str | None = None
    date_warning: str | None = None


class Case(Contract):
    case_id: ID
    question: Question
    label_origin: Literal["engineering_expectation_unconfirmed"]
    engineering_expectation: EngineeringExpectation
    note: str = Field(min_length=1, max_length=1000)


class Manifest(Contract):
    schema_version: Literal[1]
    human_gold: bool = Field(strict=True)
    note: str = Field(min_length=1, max_length=2000)
    profile: Profile
    corpus: tuple[CorpusItem, ...] = Field(min_length=1, max_length=30)
    cases: tuple[Case, ...] = Field(min_length=1, max_length=12)

    @model_validator(mode="after")
    def known_inputs(self):
        corpus = {c.corpus_id for c in self.corpus}
        if (
            self.human_gold
            or len(corpus) != len(self.corpus)
            or len({c.case_id for c in self.cases}) != len(self.cases)
            or len({(c.instance_id, c.source_document_id) for c in self.corpus}) != len(self.corpus)
        ):
            raise ValueError("duplicate identities or invented human gold")
        for case in self.cases:
            expected = case.engineering_expectation
            positive, negative = set(expected.candidate_contains), set(expected.candidate_absent)
            if (
                not (positive | negative).issubset(corpus)
                or positive & negative
                or len(positive) != len(expected.candidate_contains)
                or len(negative) != len(expected.candidate_absent)
            ):
                raise ValueError("invalid engineering expectations")
            if (expected.date_from is None) != (expected.date_to is None):
                raise ValueError("both publication boundaries are required")
            if expected.date_from and date.fromisoformat(expected.date_from) > date.fromisoformat(
                expected.date_to
            ):
                raise ValueError("ordered publication boundaries are required")
        return self


def source_hashes():
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCE_FILES}


def _load_manifest(path):
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise EvaluationError("query_manifest_too_large")
        manifest = Manifest.model_validate_json(raw)
    except (OSError, ValidationError) as exc:
        raise EvaluationError("query_manifest_invalid_or_unreadable") from exc
    bodies = {}
    for item in manifest.corpus:
        candidate = Path(item.fixture)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise EvaluationError("query_fixture_path_invalid")
        resolved = (ROOT / candidate).resolve()
        if not resolved.is_relative_to(ROOT / "research/fixtures"):
            raise EvaluationError("query_fixture_path_invalid")
        try:
            bodies[item.corpus_id] = resolved.read_bytes()
        except OSError as exc:
            raise EvaluationError("query_fixture_unreadable") from exc
        if hashlib.sha256(bodies[item.corpus_id]).hexdigest() != item.sha256:
            raise EvaluationError("query_fixture_digest_mismatch")
    return manifest, hashlib.sha256(raw).hexdigest(), bodies


def _import_corpus(manifest, bodies, directory):
    instances, corpus, revisions = [], [], {}
    for binding, example, identifier in (
        (UC, "config.example.toml", "uc"),
        (CS, "config.cs.example.toml", "cs"),
    ):
        location = directory / identifier
        location.mkdir()
        config_file = location / "instance.toml"
        # Never inherit a template's storage paths, SMTP or production settings.
        template = load_config(ROOT / example)
        config_file.write_text(
            '[storage]\ndata_dir = "data"\ndatabase = "data/evaluation.sqlite3"\n'
            + "\n".join(
                "["
                + section
                + "]\n"
                + "\n".join(
                    f"{key} = {json.dumps(value, ensure_ascii=False)}"
                    for key, value in values.model_dump(mode="json").items()
                )
                for section, values in (("source", template.source), ("http", template.http))
            ),
            encoding="utf-8",
        )
        settings = load_config(config_file)
        revisions[identifier] = initialize_storage(settings.storage)
        instances.append(InstanceConfig(id=identifier, config=config_file))
        engine = open_initialized_engine(settings.storage.database)
        _, parser, _ = binding.parsers()
        try:
            with writer_lock(settings.storage.database):
                store = RawStore(settings.storage.data_dir)
                for item in manifest.corpus:
                    if item.instance_id != identifier:
                        continue
                    # Explicit offline targets, not invented list/discovery/HTTP evidence.
                    processed_at = int(datetime.now(UTC).timestamp())
                    with engine.begin() as connection:
                        document_id = connection.execute(
                            documents.insert().values(
                                source_id=settings.source.id,
                                source_document_id=item.source_document_id,
                                detail_url=str(item.source_url),
                                discovered_title="offline evaluation target",
                                discovered_at=processed_at,
                                discovery_origin="unknown",
                            )
                        ).inserted_primary_key[0]
                    outcome = import_page(
                        engine,
                        store,
                        ResponseInput(
                            page_type="notice",
                            source_id=settings.source.id,
                            source_document_id=item.source_document_id,
                            requested_url=item.source_url,
                            final_url=item.source_url,
                            fetched_at=item.fetched_at,
                            status_code=200,
                            body_state="complete",
                            content_type="text/html",
                        ),
                        bodies[item.corpus_id],
                        processed_at,
                        notice_parser=parser,
                        processing_origin="offline",
                    )
                    if outcome.outcome != "processed":
                        raise EvaluationError("query_fixture_import_failed")
                    detail = get_notice(engine, document_id)
                    corpus.append(
                        {
                            **item.model_dump(mode="json"),
                            "source_id": detail.source_id,
                            "document_id": document_id,
                            "version_id": detail.version_id,
                            "content_sha256": detail.content_sha256,
                            "parser_version": detail.parser_version,
                            "title": detail.content.title,
                            "published_date": detail.content.published_date.isoformat(),
                            "body_text": detail.content.body_text,
                            "images": [r.model_dump(mode="json") for r in detail.content.images],
                            "attachments": [
                                r.model_dump(mode="json") for r in detail.content.attachments
                            ],
                        }
                    )
        finally:
            engine.dispose()
    return instances, corpus, revisions


def evaluate_cases(
    case_file=DEFAULT_CASES,
    *,
    model: ModelConfig | None = None,
    max_total_cost_usd=Decimal("0.50"),
    run_seconds=300.0,
    client=None,
):
    """Use real services with fixture instances; injected client is only for offline tests."""
    if (
        not isinstance(max_total_cost_usd, Decimal)
        or not max_total_cost_usd.is_finite()
        or not 0 < max_total_cost_usd <= 10
        or type(run_seconds) not in {int, float}
        or not 0 < run_seconds <= 600
    ):
        raise EvaluationError("query_evaluation_budget_invalid")
    before = source_hashes()
    manifest, digest, bodies = _load_manifest(case_file)
    deadline = time.monotonic() + run_seconds
    rows, cost, limited = [], Decimal(0), None
    with tempfile.TemporaryDirectory(prefix="signalnest-query-evaluation-") as temporary:
        directory = Path(temporary)
        instances, corpus, revisions = _import_corpus(manifest, bodies, directory)
        # JSON is not an accepted Profile file; emit a minimal explicit TOML snapshot.
        profile_file = directory / "profile.toml"
        profile_file.write_text(
            "\n".join(
                f"{key} = {json.dumps(value, ensure_ascii=False)}"
                for key, value in manifest.profile.model_dump(mode="json").items()
                if value is not None
            ),
            encoding="utf-8",
        )
        database_files = [load_config(i.config).storage.database for i in instances]
        initial_db = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in database_files}
        locators = {(c["instance_id"], c["document_id"]): c["corpus_id"] for c in corpus}
        for case in manifest.cases:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                limited = "query_batch_time_budget"
                break
            if model is not None and cost >= max_total_cost_usd:
                limited = "query_batch_cost_budget"
                break
            query_model = (
                model.model_copy(
                    update={"max_cost_usd": min(model.max_cost_usd, max_total_cost_usd - cost)}
                )
                if model
                else None
            )
            settings = QuerySettings(
                instances=tuple(instances),
                profile=profile_file,
                model=query_model,
                limits=QueryLimits(run_seconds=min(90.0, remaining)),
            )
            report = ask(
                settings,
                case.question,
                local=model is None,
                client=client,
            )
            if source_hashes() != before:
                raise EvaluationError("query_evaluation_source_changed")
            cost += Decimal(report.model_usage["charged_estimate_usd"])
            if cost > max_total_cost_usd:
                limited = "query_batch_cost_overrun_reported"
            candidates = list(
                dict.fromkeys(
                    locators[(r["instance_id"], r["document_id"])] for r in report.search_results
                )
            )
            selected = [locators[(r["instance_id"], r["document_id"])] for r in report.notices]
            expected = case.engineering_expectation
            mismatch = []
            if not set(expected.candidate_contains).issubset(candidates):
                mismatch.append("expected_candidate_missing")
            if set(expected.candidate_absent) & set(candidates):
                mismatch.append("unexpected_candidate_present")
            if any(
                report.publication_window[name] != getattr(expected, name)
                for name in ("date_from", "date_to")
            ):
                mismatch.append("publication_window_mismatch")
            if report.publication_window["warning"] != expected.date_warning:
                mismatch.append("date_warning_mismatch")
            rows.append(
                {
                    "case_id": case.case_id,
                    "question": case.question.model_dump(mode="json"),
                    "query_limits": settings.limits.model_dump(mode="json"),
                    "effective_model_config": query_model.model_dump(
                        mode="json", exclude={"api_key_env"}
                    )
                    if query_model
                    else None,
                    "retrieved_corpus_ids": candidates,
                    "answer_corpus_ids": selected,
                    "report": report.model_dump(mode="json"),
                    "engineering_mismatches": mismatch,
                }
            )
            if limited:
                break
        if initial_db != {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in database_files}:
            raise EvaluationError("query_evaluation_database_changed")
    return {
        "kind": "offline_campus_query_evaluation",
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "manifest_sha256": digest,
        "source_sha256": before,
        "storage_revisions": revisions,
        "profile": manifest.profile.model_dump(mode="json"),
        "timezone": "Asia/Shanghai",
        "model_config": model.model_dump(mode="json", exclude={"api_key_env"}) if model else None,
        "corpus": corpus,
        "cases": [c.model_dump(mode="json") for c in manifest.cases],
        "results": rows,
        "summary": {
            "mode": "model" if model else "local",
            "corpus_count": len(corpus),
            "completed_cases": len(rows),
            "total_cases": len(manifest.cases),
            "not_run_case_ids": [c.case_id for c in manifest.cases[len(rows) :]],
            "limit_reason": limited,
            "model_http_requests": sum(r["report"]["model_usage"]["http_requests"] for r in rows),
            "charged_estimate_usd": str(cost),
            "max_total_cost_usd": str(max_total_cost_usd),
            "cost_is_invoice": False,
            "engineering_mismatch_cases": [
                r["case_id"] for r in rows if r["engineering_mismatches"]
            ],
            "degraded_case_ids": [
                r["case_id"]
                for r in rows
                if r["report"]["error_code"] or "tool_results_incomplete" in r["report"]["warnings"]
            ],
            "human_confirmed": 0,
            "human_pending": len(rows),
            "database_bytes_unchanged": True,
        },
        "environment": {"python": platform.python_version(), "platform": platform.platform()},
        "limitations": [
            "Engineering candidate checks are not human answer-quality labels.",
            "Local retrieval does not exercise a model or judge semantic answers.",
            "Historical clocks do not prove vacancies were open or the live site was covered.",
            "Fixture targets are offline registrations, not real list discovery.",
            "Injected HTTP transports do not establish live model or billing evidence.",
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument(
        "--output", type=Path, required=True, help="new full snapshot; never overwrite"
    )
    parser.add_argument("--check", action="store_true", help="local engineering regressions only")
    parser.add_argument(
        "--model-config", type=Path, help="explicitly enable configured OpenAI calls"
    )
    parser.add_argument("--max-total-cost-usd", type=Decimal, default=Decimal("0.50"))
    parser.add_argument("--run-seconds", type=float, default=300.0)
    args = parser.parse_args(argv)
    if args.check and args.model_config:
        parser.error("--check is local-only; model quality needs explicit human labels")
    try:
        if args.output.exists():
            raise EvaluationError("query_evaluation_output_exists")
        model = None
        if args.model_config:
            model = load_query_config(args.model_config).model
            if model is None:
                raise EvaluationError("query_evaluation_model_not_configured")
        report = evaluate_cases(
            args.cases,
            model=model,
            max_total_cost_usd=args.max_total_cost_usd,
            run_seconds=args.run_seconds,
        )
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
        print(json.dumps(report["summary"], ensure_ascii=False))
        return (
            1
            if report["summary"]["limit_reason"]
            or report["summary"]["degraded_case_ids"]
            or (args.check and report["summary"]["engineering_mismatch_cases"])
            else 0
        )
    except (EvaluationError, ConfigurationError) as exc:
        message = (
            str(exc) if isinstance(exc, EvaluationError) else "query_model_configuration_invalid"
        )
        print(f"query evaluation failed: {message}", file=sys.stderr)
        return 2
    except OSError:
        print("query evaluation failed: query_file_unavailable_or_exists", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
