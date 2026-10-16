"""One evidence-only answer correction uses the original query's remaining budget."""

import json

import httpx
import pytest
from test_query_agent import AT, completion, digests, model_settings, selection
from test_query_agent import campus as campus

from signalnest.querying.agent import ask
from signalnest.querying.config import QueryLimits
from signalnest.querying.contracts import Question


def latest_notice(payload):
    return next(
        json.loads(message["content"])["data"]
        for message in reversed(payload["messages"])
        if message["role"] == "tool"
        and "reference" in json.loads(message["content"]).get("data", {})
    )


def bad_selection(notice, code):
    if code == "citation_not_read":
        return selection(notice, reference="uc:999999:v1")
    return selection(notice, conditions=[{"field": "body_text", "text": "未经读取的虚构条件"}])


def run(campus, monkeypatch, handler, **limits):
    monkeypatch.setenv("OPENAI_API_KEY", "offline-placeholder")
    configured = campus.settings.model_copy(
        update={"model": model_settings(), "limits": QueryLimits(**limits)}
    )
    before = digests(campus)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = ask(configured, Question(text="助教招聘", at=AT), client=client)
    assert digests(campus) == before
    return result


def reply(name, args, index):
    return httpx.Response(200, json=completion(name, args, f"turn_{index}"))


@pytest.mark.parametrize("error_code", ["citation_not_read", "citation_quote_invalid"])
def test_one_correction_copies_only_already_read_reference_and_quotes(
    campus, monkeypatch, error_code
):
    payloads = []
    ledgers = []
    from signalnest.querying import agent

    original_budget = agent.ModelBudget

    def capture_budget(**kwargs):
        ledger = original_budget(**kwargs)
        ledgers.append((ledger, ledger.deadline))
        return ledger

    monkeypatch.setattr(agent, "ModelBudget", capture_budget)

    def handler(request):
        payload = json.loads(request.content)
        payloads.append(payload)
        index = len(payloads)
        if index == 1:
            return reply(
                "read_notice", {"instance_id": "cs", "document_id": campus.cs.document_id}, index
            )
        if index == 2:
            return reply("read_profile", {}, index)
        notice = latest_notice(payload)
        if index == 3:
            return reply("answer", {"notices": [bad_selection(notice, error_code)]}, index)
        assert index == 4
        assert payload["tool_choice"]["function"]["name"] == "answer"
        assert [tool["function"]["name"] for tool in payload["tools"]] == ["answer"]
        assert payload["messages"][-1]["role"] == "user"
        instruction = json.loads(payload["messages"][-1]["content"])
        assert instruction["error_code"] == error_code
        assert instruction["allowed_references"] == [notice["reference"]]
        assert "原样复制已读原句" in instruction["instruction"]
        assert "不再搜索或读取新通知" in instruction["instruction"]
        assert "唯一一次纠正机会" in instruction["instruction"]
        assert json.loads(payload["messages"][1]["content"])["question"] == "助教招聘"
        return reply("answer", {"notices": [selection(notice)]}, index)

    result = run(campus, monkeypatch, handler)
    assert result.status == "answered" and result.error_code is None
    assert result.model_usage["answer_corrections"] == 1
    assert result.model_usage["http_requests"] == 4
    assert result.model_usage["exploration_rounds"] == 2
    assert "answer_correction_attempted" in result.warnings
    assert "tool_results_incomplete" in result.warnings
    assert [trace.error_code for trace in result.tool_trace if not trace.ok] == [error_code]
    assert [trace.tool for trace in result.tool_trace[-2:]] == ["answer", "answer"]
    ledger, initial_deadline = ledgers[0]
    assert ledger.max_calls == 6 and ledger.max_requests == 8
    assert ledger.deadline == initial_deadline


def test_autonomous_invalid_answer_uses_same_single_correction(campus, monkeypatch):
    phases = []

    def handler(request):
        payload = json.loads(request.content)
        phases.append(payload["tool_choice"])
        if len(phases) == 1:
            return reply(
                "read_notice", {"instance_id": "cs", "document_id": campus.cs.document_id}, 1
            )
        notice = latest_notice(payload)
        if len(phases) == 2:
            assert payload["tool_choice"] == "required"
            return reply("answer", {"notices": [bad_selection(notice, "citation_not_read")]}, 2)
        assert len(phases) == 3
        assert payload["tool_choice"]["function"]["name"] == "answer"
        return reply("answer", {"notices": [selection(notice)]}, 3)

    result = run(campus, monkeypatch, handler)
    assert result.status == "answered" and result.model_usage["answer_corrections"] == 1
    assert result.model_usage["http_requests"] == 3
    assert result.model_usage["exploration_rounds"] == 2


def test_correction_without_read_evidence_can_only_end_without_notices(campus, monkeypatch):
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        payload = json.loads(request.content)
        if count == 1:
            return reply(
                "answer",
                {
                    "notices": [
                        {
                            "reference": "cs:999:v1",
                            "relevance": [{"field": "title", "text": "未读取的招聘通知"}],
                        }
                    ]
                },
                count,
            )
        assert count == 2
        assert json.loads(payload["messages"][-1]["content"])["allowed_references"] == []
        assert payload["tool_choice"]["function"]["name"] == "answer"
        return reply("answer", {"notices": []}, count)

    result = run(campus, monkeypatch, handler)
    assert result.status == "answered" and result.notices == ()
    assert result.model_usage["answer_corrections"] == 1
    assert count == 2
    assert not any(trace.tool == "read_notice" for trace in result.tool_trace)
    assert result.tool_trace[-2].error_code == "citation_not_read"


@pytest.mark.parametrize("error_code", ["invalid_tool_arguments", "duplicate_answer_reference"])
def test_other_answer_errors_do_not_trigger_citation_correction(campus, monkeypatch, error_code):
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        if count == 1:
            return reply(
                "read_notice", {"instance_id": "cs", "document_id": campus.cs.document_id}, count
            )
        assert count == 2
        payload = json.loads(request.content)
        notice = latest_notice(payload)
        args = (
            {"notices": [selection(notice), selection(notice)]}
            if error_code == "duplicate_answer_reference"
            else {"notices": [], "unknown": "invalid"}
        )
        return reply("answer", args, count)

    result = run(campus, monkeypatch, handler)
    assert result.status == "fallback" and result.error_code == error_code
    assert count == 2 and result.model_usage["answer_corrections"] == 0


@pytest.mark.parametrize("second_result", ["invalid_quote", "search", "read_notice"])
def test_correction_cannot_repeat_or_resume_exploration(campus, monkeypatch, second_result):
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        payload = json.loads(request.content)
        if count == 1:
            return reply(
                "read_notice", {"instance_id": "cs", "document_id": campus.cs.document_id}, count
            )
        if count == 2:
            return reply("read_profile", {}, count)
        notice = latest_notice(payload)
        if count == 3 or second_result == "invalid_quote":
            return reply(
                "answer", {"notices": [bad_selection(notice, "citation_quote_invalid")]}, count
            )
        return reply(second_result, {"query": "禁止继续搜索"}, count)

    result = run(campus, monkeypatch, handler)
    assert result.status == "fallback" and result.notices == ()
    assert result.error_code == (
        "citation_quote_invalid"
        if second_result == "invalid_quote"
        else "model_required_tool_ignored"
    )
    assert count == 4 and result.model_usage["answer_corrections"] == 1
    assert [trace.tool for trace in result.tool_trace].count("read_notice") == 1
    assert [trace.tool for trace in result.tool_trace].count("search") == 1
    failed = [trace.error_code for trace in result.tool_trace if not trace.ok]
    assert failed == ["citation_quote_invalid"] * (2 if second_result == "invalid_quote" else 1)


@pytest.mark.parametrize(
    "limits", [{"max_model_calls": 3}, {"max_requests": 3}, {"max_tool_calls": 5}]
)
def test_no_correction_request_when_original_allocation_is_exhausted(campus, monkeypatch, limits):
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        payload = json.loads(request.content)
        if count == 1:
            return reply(
                "read_notice", {"instance_id": "cs", "document_id": campus.cs.document_id}, count
            )
        if count == 2:
            return reply("read_profile", {}, count)
        assert count == 3
        return reply(
            "answer",
            {"notices": [bad_selection(latest_notice(payload), "citation_not_read")]},
            count,
        )

    result = run(campus, monkeypatch, handler, **limits)
    assert result.status == "fallback" and result.error_code == "citation_not_read"
    assert result.model_usage["answer_corrections"] == 0
    assert result.model_usage["http_requests"] == count == 3
    assert "answer_correction_attempted" not in result.warnings


def test_no_correction_request_when_time_expires_after_answer_validation(campus, monkeypatch):
    from signalnest.querying.tools import QueryTools

    clock = [1000.0]
    monkeypatch.setattr("signalnest.model_client.time.monotonic", lambda: clock[0])
    original_call = QueryTools.call

    def call(self, name, arguments):
        result = original_call(self, name, arguments)
        if name == "answer" and not result["ok"]:
            clock[0] = self.budget.deadline + 1
        return result

    monkeypatch.setattr(QueryTools, "call", call)
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        payload = json.loads(request.content)
        if count == 1:
            return reply(
                "read_notice", {"instance_id": "cs", "document_id": campus.cs.document_id}, count
            )
        assert count == 2
        return reply(
            "answer",
            {"notices": [bad_selection(latest_notice(payload), "citation_not_read")]},
            count,
        )

    result = run(campus, monkeypatch, handler)
    assert result.status == "fallback" and result.error_code == "citation_not_read"
    assert count == 2 and result.model_usage["answer_corrections"] == 0
    assert result.tool_trace[-1].error_code == "citation_not_read"
