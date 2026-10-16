"""Real fixture replay, bounded model protocol and independently bound human review."""

import copy
import json
import runpy
import socket
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa

from signalnest.config import StorageSettings, load_config
from signalnest.human_review import ReviewError, make_label, prepare_review, review_report
from signalnest.model_client import ModelConfig
from signalnest.schema import documents
from signalnest.storage import open_initialized_engine

ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "deploy/evaluate_queries.py"
MANIFEST = ROOT / "docs/validation/query-cases.json"
EVALUATION = runpy.run_path(str(ENTRY))
evaluate_cases = EVALUATION["evaluate_cases"]
EvaluationError = EVALUATION["EvaluationError"]
AT = "2026-10-12T12:00:00+08:00"


@pytest.fixture(scope="module")
def replay():
    return evaluate_cases()


@pytest.fixture(scope="module")
def bundle(replay):
    return prepare_review({}, query_snapshot=replay)


def case_file(tmp_path, change):
    cases = json.loads(MANIFEST.read_text())
    change(cases)
    path = tmp_path / "cases.json"
    path.write_text(json.dumps(cases))
    return path


def human_label(bundle, identifier, **kwargs):
    return make_label(
        bundle,
        identifier,
        reviewer="engineering test, not a real human review",
        reviewed_at=AT,
        **kwargs,
    )


def model_config():
    return ModelConfig(
        model="test-tool-model",
        api_key_env="SIGNALNEST_TEST_QUERY_KEY",
        input_usd_per_million="1",
        output_usd_per_million="4",
        max_cost_usd="0.10",
    )


def completion(name, args, identifier):
    return {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": identifier,
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(args, ensure_ascii=False),
                            },
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 100},
    }


def test_actual_fixture_replay_is_offline_and_query_databases_remain_unchanged(replay):
    summary = replay["summary"]
    assert summary["corpus_count"] == summary["completed_cases"] == summary["total_cases"] == 9
    assert summary["engineering_mismatch_cases"] == summary["not_run_case_ids"] == []
    assert summary["model_http_requests"] == 0
    assert summary["charged_estimate_usd"] == "0"
    assert summary["database_bytes_unchanged"] is True
    assert {c["instance_id"] for c in replay["corpus"]} == {"uc", "cs"}
    assert all(c["parser_version"] and c["content_sha256"] for c in replay["corpus"])
    rows = {c["case_id"]: c for c in replay["results"]}
    assert "uc-128291" in rows["Q01"]["retrieved_corpus_ids"]
    assert rows["Q02"]["retrieved_corpus_ids"] == ["cs-64481"]
    assert rows["Q03"]["retrieved_corpus_ids"] == ["uc-117011"]
    assert rows["Q07"]["report"]["publication_window"]["warning"] == "deadline_window_not_supported"
    assert rows["Q08"]["report"]["publication_window"]["warning"] == "date_expression_not_resolved"
    assert rows["Q09"]["retrieved_corpus_ids"] == []
    assert all(
        r["report"]["status"] == "local" and not r["answer_corpus_ids"] for r in rows.values()
    )


def test_bundle_never_turns_engineering_expectations_into_human_labels(bundle):
    encoded = json.dumps(bundle)
    assert "engineering_expectation" not in encoded
    assert "candidate_contains" not in encoded
    report = review_report(bundle, [])
    assert report["confirmed"] == 0 and report["pending"] == 9
    assert report["query"]["macro_retrieval_recall"] is None
    assert report["query"]["macro_answer_recall"] is None
    assert not report["engineering_expectations_used_as_labels"]
    item = bundle["items"][0]
    assert len(item["input"]["corpus"]) == 9
    assert item["input"]["profile"]["study_level"] == "undergraduate"
    assert item["input"]["query_limits"]["max_model_calls"] == 6


@pytest.mark.parametrize("old_version", ["campus-query-v1", "campus-query-v2", "campus-query-v3"])
def test_old_query_snapshot_and_review_remain_frozen(replay, old_version):
    snapshot = copy.deepcopy(replay)
    for row in snapshot["results"]:
        row["report"]["query_version"] = old_version
    before = copy.deepcopy(snapshot)
    bundle = prepare_review({}, query_snapshot=snapshot)
    pending = review_report(bundle, [])
    assert pending["confirmed"] == 0 and pending["pending"] == 9
    assert pending["query"]["macro_retrieval_recall"] is None
    assert pending["query"]["macro_answer_recall"] is None
    assert snapshot == before


def test_human_retrieval_labels_include_missed_members_and_do_not_score_local_as_answer(bundle):
    labels = [
        human_label(
            bundle,
            "query:Q01",
            relevant_corpus_ids=["uc-128291", "cs-64481"],
            answer_assessment="not_answered",
        ),
        human_label(bundle, "query:Q09", relevant_corpus_ids=[], answer_assessment="not_answered"),
    ]
    result = review_report(bundle, labels)
    assert result["confirmed"] == 2 and result["pending"] == 7
    assert result["search"]["confirmed"] == 0
    query = result["query"]
    assert query["macro_retrieval_recall"] == 0.5
    assert query["macro_answer_recall"] == 0
    assert query["empty_answer_correct"] == 1
    assert query["assessment_counts"] == {"not_answered": 2}


@pytest.mark.parametrize(
    "change,error",
    [
        ({"answer_assessment": "supported"}, "review_query_assessment_invalid"),
        ({"answer_assessment": None}, "review_query_assessment_invalid"),
        ({"answer_assessment": []}, "review_query_assessment_invalid"),
        ({"relevant_corpus_ids": ["imagined"]}, "review_relevance_invalid"),
        ({"relevant_corpus_ids": ["uc-128291", "uc-128291"]}, "review_relevance_invalid"),
        ({"action": "PUSH_NOW"}, "review_relevance_invalid"),
        ({"status": "pending"}, "review_pending_has_label"),
    ],
)
def test_query_labels_require_explicit_relevance_and_appropriate_assessment(bundle, change, error):
    args = {"relevant_corpus_ids": ["uc-128291"], "answer_assessment": "not_answered"} | change
    with pytest.raises(ReviewError, match=error):
        human_label(bundle, "query:Q01", **args)


def test_query_labels_remain_stale_on_changed_snapshot_and_conflicts_are_explicit(bundle):
    label = human_label(
        bundle, "query:Q01", relevant_corpus_ids=[], answer_assessment="not_answered"
    )
    stale = dict(label, input_sha256="0" * 64)
    result = review_report(bundle, [stale])
    assert result["pending"] == 9 and result["stale_records"]
    other = human_label(
        bundle, "query:Q01", relevant_corpus_ids=["uc-128291"], answer_assessment="not_answered"
    )
    with pytest.raises(ReviewError, match="review_conflicting_labels"):
        review_report(bundle, [label, other])


@pytest.mark.parametrize(
    "change,error",
    [
        (lambda m: m.update(human_gold=True), "query_manifest_invalid"),
        (lambda m: m["corpus"].append(m["corpus"][0]), "query_manifest_invalid"),
        (
            lambda m: m["cases"][0]["question"].update(at="2026-10-12T12:00:00"),
            "query_manifest_invalid",
        ),
        (
            lambda m: m["cases"][0]["engineering_expectation"].update(
                candidate_contains=["invented"]
            ),
            "query_manifest_invalid",
        ),
        (lambda m: m["corpus"][0].update(sha256="0" * 64), "query_fixture_digest_mismatch"),
        (lambda m: m["corpus"][0].update(fixture="../outside.html"), "query_fixture_path_invalid"),
        (lambda m: m["corpus"][0].update(fixture="README.md"), "query_fixture_path_invalid"),
    ],
)
def test_manifest_does_not_guess_unknown_inputs_or_modify_fixtures(tmp_path, change, error):
    with pytest.raises(EvaluationError, match=error):
        evaluate_cases(case_file(tmp_path, change))


def test_model_protocol_connects_actual_reads_to_frozen_answers_and_human_review(
    tmp_path, monkeypatch
):
    path = case_file(tmp_path, lambda m: m.update(cases=m["cases"][:1]))
    monkeypatch.setenv("SIGNALNEST_TEST_QUERY_KEY", "fake-offline-key")

    def handler(request):
        messages = json.loads(request.content)["messages"]
        results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
        if not results:
            return httpx.Response(
                200,
                json=completion("read_notice", {"instance_id": "uc", "document_id": 3}, "read_1"),
            )
        notice = results[-1]["data"]
        return httpx.Response(
            200,
            json=completion(
                "answer",
                {
                    "notices": [
                        {
                            "reference": notice["reference"],
                            "relevance": [{"field": "title", "text": notice["title"]}],
                            "conditions": [],
                            "deadline": [],
                        }
                    ]
                },
                "answer_1",
            ),
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        report = evaluate_cases(path, model=model_config(), client=client)
    assert report["summary"]["model_http_requests"] == 2
    assert report["results"][0]["answer_corpus_ids"] == ["uc-128291"]
    query_bundle = prepare_review({}, query_snapshot=report)
    label = human_label(
        query_bundle, "query:Q01", relevant_corpus_ids=["uc-128291"], answer_assessment="incomplete"
    )
    result = review_report(query_bundle, [label])["query"]
    assert result["macro_answer_recall"] == result["macro_retrieval_recall"] == 1
    assert result["assessment_counts"] == {"incomplete": 1}
    assert "fake-offline-key" not in json.dumps(report)


def test_batch_limits_prevent_extra_model_requests_and_keep_partial_records(monkeypatch):
    monkeypatch.setenv("SIGNALNEST_TEST_QUERY_KEY", "fake-offline-key")
    requests = []

    def overrun(request):
        requests.append(request)
        response = completion("answer", {"notices": []}, "answer_1")
        response["usage"] = {"prompt_tokens": 1_000_000, "completion_tokens": 100}
        return httpx.Response(200, json=response)

    with httpx.Client(transport=httpx.MockTransport(overrun)) as client:
        result = evaluate_cases(
            model=model_config(), client=client, max_total_cost_usd=Decimal("0.10")
        )
    assert len(requests) == 1
    assert result["summary"]["limit_reason"] == "query_batch_cost_overrun_reported"
    assert result["summary"]["completed_cases"] == 1
    assert len(result["summary"]["not_run_case_ids"]) == 8
    assert result["summary"]["degraded_case_ids"] == ["Q01"]
    # The larger supplier usage is recorded, not hidden to satisfy the application estimate.
    assert Decimal(result["summary"]["charged_estimate_usd"]) > Decimal("0.10")
    assert len(prepare_review({}, query_snapshot=result)["items"]) == 1


def test_elapsed_batch_budget_does_not_invoke_a_model():
    result = evaluate_cases(run_seconds=1e-12, model=model_config())
    assert result["summary"]["limit_reason"] == "query_batch_time_budget"
    assert result["summary"]["completed_cases"] == 0
    assert result["summary"]["model_http_requests"] == 0


def test_unpriced_proxy_batch_has_an_aggregate_request_cap_and_unknown_money(monkeypatch):
    monkeypatch.setenv("SIGNALNEST_TEST_QUERY_KEY", "fake-offline-key")
    settings = ModelConfig(
        provider="openai-compatible-chat-completions",
        base_url="http://127.0.0.1:8001/v1",
        model="auto",
        api_key_env="SIGNALNEST_TEST_QUERY_KEY",
        pricing_mode="unpriced_proxy",
    )
    requests = []

    def handler(request):
        requests.append(request)
        payload = json.loads(request.content)
        assert payload["tool_choice"] == "required" and len(payload["tools"]) == 1
        if payload["tools"][0]["function"]["name"] == "read_notice":
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
                    "one_read",
                ),
            )
        return httpx.Response(200, json=completion("answer", {"notices": []}, "one_answer"))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = evaluate_cases(model=settings, client=client, max_total_requests=2)
    summary = result["summary"]
    assert summary["model_http_requests"] == len(requests) == 2
    assert summary["completed_cases"] == 1 and len(summary["not_run_case_ids"]) == 8
    assert summary["limit_reason"] == "query_batch_request_budget"
    assert summary["charged_estimate_usd"] is summary["max_total_cost_usd"] is None
    assert summary["database_bytes_unchanged"] is True
    assert all(r["report"]["model_usage"]["price_source"] == "unknown" for r in result["results"])
    assert len(prepare_review({}, query_snapshot=result)["items"]) == 1
    with pytest.raises(EvaluationError, match="query_evaluation_usd_budget_unavailable"):
        evaluate_cases(model=settings, max_total_cost_usd=Decimal("0.05"))


@pytest.mark.parametrize("limit", [0, -1, True, 97, 1.5])
def test_batch_request_budget_validation(limit):
    with pytest.raises(EvaluationError, match="query_evaluation_budget_invalid"):
        evaluate_cases(max_total_requests=limit)


def test_database_mutation_is_detected_instead_of_exported_as_read_only(monkeypatch):
    actual = EVALUATION["ask"]

    def change_database(settings, question, **kwargs):
        report = actual(settings, question, **kwargs)
        config = load_config(settings.instances[0].config)
        engine = open_initialized_engine(config.storage.database)
        try:
            with engine.begin() as connection:
                connection.execute(
                    sa.update(documents).values(discovered_title="injected mutation")
                )
        finally:
            engine.dispose()
        return report

    monkeypatch.setitem(evaluate_cases.__globals__, "ask", change_database)
    with pytest.raises(EvaluationError, match="query_evaluation_database_changed"):
        evaluate_cases()


def test_template_storage_is_not_used_as_an_evaluation_target(tmp_path, monkeypatch):
    victim = tmp_path / "existing.sqlite3"
    victim.write_bytes(b"existing private instance must never be opened")
    real_load = EVALUATION["load_config"]

    def with_existing_storage(path):
        config = real_load(path)
        if path.parent == ROOT:
            return config.model_copy(
                update={
                    "storage": StorageSettings(
                        data_dir=str(tmp_path / "private"),
                        database=str(victim),
                    )
                }
            )
        return config

    monkeypatch.setitem(evaluate_cases.__globals__, "load_config", with_existing_storage)
    assert evaluate_cases()["summary"]["database_bytes_unchanged"]
    assert victim.read_bytes() == b"existing private instance must never be opened"
    assert not (tmp_path / "private").exists()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r["results"][0]["report"]["search_results"][0].update(version_id=999),
        lambda r: r["results"][0]["report"].update(question="different question"),
        lambda r: r["results"][0].update(retrieved_corpus_ids=[]),
        lambda r: r["results"][0].update(question="invalid"),
    ],
)
def test_query_review_rejects_unbound_results(replay, mutate):
    forged = copy.deepcopy(replay)
    mutate(forged)
    with pytest.raises(ReviewError):
        prepare_review({}, query_snapshot=forged)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda n: n.update(reference="uc:999:v999"),
        lambda n: n.update(original_url="https://example.org/wrong"),
        lambda n: n.update(parser_version="unrelated-parser"),
        lambda n: n.update(content_sha256="0" * 64),
        lambda n: n["relevance"][0].update(text="捏造的原文证据不存在"),
        lambda n: n["relevance"][0].update(field=["title"]),
    ],
)
def test_answer_evidence_must_match_frozen_identity_version_url_and_actual_text(replay, mutate):
    snapshot = copy.deepcopy(replay)
    original = next(c for c in snapshot["corpus"] if c["corpus_id"] == "uc-128291")
    row = snapshot["results"][0]
    notice = {
        key: original[key]
        for key in (
            "instance_id",
            "document_id",
            "version_id",
            "source_id",
            "source_document_id",
            "content_sha256",
            "parser_version",
            "title",
            "published_date",
        )
    }
    notice.update(
        reference=f"uc:{original['document_id']}:v{original['version_id']}",
        original_url=original["source_url"],
        relevance=[{"field": "title", "text": original["title"]}],
        conditions=[],
        deadline=[],
    )
    row["report"]["status"] = "answered"
    row["report"]["notices"] = [notice]
    row["answer_corpus_ids"] = [original["corpus_id"]]
    assert prepare_review({}, query_snapshot=snapshot)
    mutate(notice)
    with pytest.raises(ReviewError, match="review_query_evidence_mismatch"):
        prepare_review({}, query_snapshot=snapshot)


def test_real_entrypoints_export_pending_records_and_do_not_overwrite(
    tmp_path, monkeypatch, capsys
):
    def no_network(*args, **kwargs):
        raise AssertionError("offline evaluator must not access a network")

    monkeypatch.setattr(socket, "create_connection", no_network)
    snapshot, bundle, label_file = (
        tmp_path / n for n in ("snapshot.json", "bundle.json", "label.json")
    )
    assert EVALUATION["main"](["--check", "--output", str(snapshot)]) == 0
    review_main = runpy.run_path(str(ROOT / "deploy/human_review.py"))["main"]
    assert review_main(["prepare", "--queries", str(snapshot), "--output", str(bundle)]) == 0
    assert review_main(["report", "--bundle", str(bundle)]) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["pending"] == 9
    assert (
        review_main(
            [
                "label",
                "--bundle",
                str(bundle),
                "--item-id",
                "query:Q01",
                "--reviewer",
                "test only",
                "--at",
                AT,
                "--pending",
                "--output",
                str(label_file),
            ]
        )
        == 0
    )
    before = snapshot.read_bytes()
    assert EVALUATION["main"](["--check", "--output", str(snapshot)]) == 2
    assert snapshot.read_bytes() == before
    assert (
        review_main(
            [
                "label",
                "--bundle",
                str(bundle),
                "--item-id",
                "query:Q01",
                "--reviewer",
                "test only",
                "--at",
                AT,
                "--pending",
                "--output",
                str(label_file),
            ]
        )
        == 2
    )


def test_model_execution_is_explicit_and_engineering_check_cannot_claim_model_quality(tmp_path):
    output = tmp_path / "snapshot.json"
    assert (
        EVALUATION["main"](
            ["--model-config", str(ROOT / "ask.example.toml"), "--output", str(output)]
        )
        == 2
    )
    assert not output.exists()
    with pytest.raises(SystemExit, match="2"):
        EVALUATION["main"](
            ["--check", "--model-config", str(ROOT / "ask.example.toml"), "--output", str(output)]
        )
