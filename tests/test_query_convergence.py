"""Finite v3 exploration keeps real read/citation validation and a terminal budget."""

import json

import httpx
import pytest
from test_query_agent import AT, completion, digests, import_text_notice, model_settings, selection
from test_query_agent import campus as campus

from signalnest.querying.agent import SYSTEM, ask, render_report
from signalnest.querying.config import QueryLimits
from signalnest.querying.contracts import Question


def settings(campus, **limits):
    return campus.settings.model_copy(
        update={"model": model_settings(), "limits": QueryLimits(**limits)}
    )


def run(campus, monkeypatch, handler, *, text="助教招聘", config=None):
    monkeypatch.setenv("OPENAI_API_KEY", "offline-test-placeholder")
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        return ask(config or settings(campus), Question(text=text, at=AT), client=client)


def required(payload):
    choice = payload["tool_choice"]
    return choice["function"]["name"] if isinstance(choice, dict) else None


def response(name, args, number):
    return httpx.Response(200, json=completion(name, args, f"step_{number}"))


def last_data(payload):
    return json.loads(
        next(m["content"] for m in reversed(payload["messages"]) if m["role"] == "tool")
    )["data"]


def test_two_exploration_rounds_then_one_read_and_answer_are_enforced(campus, monkeypatch):
    seen = []

    def handler(request):
        payload = json.loads(request.content)
        seen.append(payload)
        phase = required(payload)
        if phase is None:
            return response("search", {"query": "助教招聘"}, len(seen))
        assert [tool["function"]["name"] for tool in payload["tools"]] == [phase]
        if phase == "read_notice":
            return response(
                phase, {"instance_id": "cs", "document_id": campus.cs.document_id}, len(seen)
            )
        return response("answer", {"notices": [selection(last_data(payload))]}, len(seen))

    before = digests(campus)
    report = run(campus, monkeypatch, handler)
    assert report.status == "answered" and report.error_code is None
    assert len(seen) == 4
    assert [required(p) for p in seen] == [None, None, "read_notice", "answer"]
    assert report.model_usage["exploration_rounds"] == 2
    assert report.model_usage["forced_tools"] == ["read_notice", "answer"]
    assert report.notices[0]["source_document_id"] == "1074:64481"
    assert report.query_version == "campus-query-v4" and digests(campus) == before


def test_a_delivered_read_goes_directly_to_answer_after_two_rounds(campus, monkeypatch):
    phases = []

    def handler(request):
        payload = json.loads(request.content)
        phases.append(required(payload))
        if len(phases) == 1:
            return response(
                "read_notice", {"instance_id": "cs", "document_id": campus.cs.document_id}, 1
            )
        if len(phases) == 2:
            return response("read_profile", {}, 2)
        notice = next(
            json.loads(m["content"])["data"]
            for m in payload["messages"]
            if m["role"] == "tool" and "reference" in json.loads(m["content"]).get("data", {})
        )
        return response("answer", {"notices": [selection(notice)]}, 3)

    report = run(campus, monkeypatch, handler)
    assert report.status == "answered" and phases == [None, None, "answer"]
    assert report.model_usage["forced_tools"] == ["answer"]


@pytest.mark.parametrize("compatible", [False, True])
def test_latest_program_instruction_preserves_question_and_read_evidence(
    campus, monkeypatch, compatible
):
    question = "计算机学院的助教招聘，哪些资格仍需核对？"
    configured = settings(campus, max_model_calls=2, max_requests=2)
    if compatible:
        configured = configured.model_copy(
            update={
                "model": model_settings(
                    provider="openai-compatible-chat-completions",
                    base_url="http://127.0.0.1:8001/v1",
                )
            }
        )
    phases = []

    def handler(request):
        payload = json.loads(request.content)
        phase = payload["tools"][0]["function"]["name"]
        phases.append(phase)
        if compatible:
            assert payload["tool_choice"] == "required"
        else:
            assert required(payload) == phase
        assert len(payload["tools"]) == 1
        assert payload["parallel_tool_calls"] is False
        original = json.loads(payload["messages"][1]["content"])
        assert original["question"] == question
        assert original["initial_retrieval"] and original["source_status"]["ok"] is True
        latest = payload["messages"][-1]
        assert latest["role"] == "user"
        assert f"恰好一次 {phase}" in latest["content"]
        assert "禁止并行调用" in latest["content"]
        if phase == "read_notice":
            assert "一个候选" in latest["content"]
            return response(phase, {"instance_id": "cs", "document_id": campus.cs.document_id}, 1)
        assert phase == "answer" and "不再搜索" in latest["content"]
        assert "notices=[]" in latest["content"]
        notice = last_data(payload)
        assert notice["body_text"] and notice["reference"]
        return response("answer", {"notices": [selection(notice)]}, 2)

    before = digests(campus)
    report = run(campus, monkeypatch, handler, text=question, config=configured)
    assert report.status == "answered" and report.error_code is None
    assert phases == ["read_notice", "answer"]
    assert report.model_usage["http_requests"] == 2
    assert digests(campus) == before


def test_relevance_prompt_distinguishes_actions_without_claiming_semantic_accuracy():
    assert "共享关键词不足以证明匹配" in SYSTEM
    assert "学生报名、教师征题和成果推荐/选拔" in SYSTEM
    assert "仅有弱相关或无法确认主题与行动关系时拒选或保持未知" in SYSTEM
    assert "不能把历史通知说成仍可报名" in SYSTEM


def test_no_match_answer_is_limited_to_actual_keywords_sources_and_dates(campus, monkeypatch):
    seen = []
    keyword = "不存在的火星地质实习"

    def handler(request):
        payload = json.loads(request.content)
        seen.append(required(payload))
        if required(payload) is None:
            return response("search", {"query": keyword}, len(seen))
        assert required(payload) == "answer"
        return response("answer", {"notices": []}, len(seen))

    report = run(campus, monkeypatch, handler, text=keyword)
    assert report.status == "answered" and report.notices == ()
    assert seen == [None, None, "answer"]
    assert report.retrieval_scope and all(s["total"] == 0 for s in report.retrieval_scope)
    assert {s["instance_id"] for s in report.retrieval_scope} == {"uc", "cs"}
    assert all(s["query"] == keyword for s in report.retrieval_scope)
    text = render_report(report)
    assert keyword in text and "检索范围 uc" in text and "检索范围 cs" in text
    assert "不证明全校没有机会" in text
    assert "核心主题" in SYSTEM and "泛词" in SYSTEM


@pytest.mark.parametrize("limit", ["max_model_calls", "max_requests"])
def test_two_remaining_calls_or_requests_skip_exploration(campus, monkeypatch, limit):
    phases = []

    def handler(request):
        payload = json.loads(request.content)
        phases.append(required(payload))
        if required(payload) == "read_notice":
            return response(
                "read_notice", {"instance_id": "cs", "document_id": campus.cs.document_id}, 1
            )
        return response("answer", {"notices": [selection(last_data(payload))]}, 2)

    report = run(campus, monkeypatch, handler, config=settings(campus, **{limit: 2}))
    assert report.status == "answered" and phases == ["read_notice", "answer"]
    assert report.model_usage["exploration_rounds"] == 0


def test_one_call_cannot_turn_unread_search_hits_into_success(campus, monkeypatch):
    report = run(
        campus,
        monkeypatch,
        lambda request: pytest.fail("budget-insufficient model request"),
        config=settings(campus, max_model_calls=1),
    )
    assert report.status == "fallback" and report.error_code == "model_call_budget"
    assert report.search_results and not report.notices
    assert report.model_usage["http_requests"] == 0


def test_tool_budget_reserves_read_and_answer_before_exploration(campus, monkeypatch):
    phases = []

    def handler(request):
        payload = json.loads(request.content)
        phase = required(payload)
        phases.append(phase)
        if phase == "read_notice":
            return response(phase, {"instance_id": "cs", "document_id": campus.cs.document_id}, 1)
        return response("answer", {"notices": [selection(last_data(payload))]}, 2)

    report = run(campus, monkeypatch, handler, config=settings(campus, max_tool_calls=4))
    assert report.status == "answered" and phases == ["read_notice", "answer"]
    assert len(report.tool_trace) == 4


def test_insufficient_tool_budget_falls_back_without_unread_answer(campus, monkeypatch):
    import_text_notice(campus, "本科生科研训练报名，请于今日申请。")
    report = run(
        campus,
        monkeypatch,
        lambda request: pytest.fail("no room for read and answer"),
        text="科研训练",
        config=settings(campus, max_tool_calls=4),
    )
    assert report.status == "fallback" and report.error_code == "tool_call_budget"
    assert report.search_results and not report.notices


def test_read_retry_cannot_consume_the_final_answer_request(campus, monkeypatch):
    used = []

    def handler(request):
        used.append(required(json.loads(request.content)))
        return httpx.Response(503)

    report = run(campus, monkeypatch, handler, config=settings(campus, max_requests=2))
    assert report.status == "fallback" and report.error_code == "model_request_budget"
    assert used == ["read_notice"] and report.model_usage["http_requests"] == 1
    assert not report.notices


@pytest.mark.parametrize("wrong_tool", ["search", "read_profile", "mail_drain", "answer"])
def test_forced_read_never_executes_a_provider_tool_that_ignored_the_constraint(
    campus, monkeypatch, wrong_tool
):
    def handler(request):
        return response(wrong_tool, {"notices": []} if wrong_tool == "answer" else {}, 1)

    report = run(campus, monkeypatch, handler, config=settings(campus, max_model_calls=2))
    assert report.status == "fallback" and report.error_code == "model_required_tool_ignored"
    assert [t.tool for t in report.tool_trace] == ["source_status", "search"]


def test_forced_read_failure_is_not_an_empty_success(campus, monkeypatch):
    def handler(request):
        return response("read_notice", {"instance_id": "cs", "document_id": 999999}, 1)

    report = run(campus, monkeypatch, handler, config=settings(campus, max_model_calls=2))
    assert report.status == "fallback" and report.error_code == "document_not_found"
    assert not report.notices and report.model_usage["http_requests"] == 1


def test_forced_answer_cannot_resume_searching(campus, monkeypatch):
    seen = []

    def handler(request):
        payload = json.loads(request.content)
        seen.append(required(payload))
        if len(seen) == 1:
            return response(
                "read_notice", {"instance_id": "cs", "document_id": campus.cs.document_id}, 1
            )
        if len(seen) == 2:
            return response("search", {"query": "助教招聘"}, 2)
        return response("search", {"query": "招聘"}, 3)

    report = run(campus, monkeypatch, handler)
    assert report.status == "fallback" and report.error_code == "model_required_tool_ignored"
    assert [t.tool for t in report.tool_trace] == [
        "source_status",
        "search",
        "read_notice",
        "search",
    ]
    assert seen == [None, None, "answer"]


def test_wall_budget_is_reserved_and_can_trigger_early_convergence(campus, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("signalnest.model_client.time.monotonic", lambda: clock[0])
    phases, timeouts = [], []

    def handler(request):
        payload = json.loads(request.content)
        phases.append(required(payload))
        timeouts.append(request.extensions["timeout"]["read"])
        if len(phases) == 1:
            clock[0] += 30
            return response("search", {"query": "助教招聘"}, 1)
        if required(payload) == "read_notice":
            return response(
                "read_notice", {"instance_id": "cs", "document_id": campus.cs.document_id}, 2
            )
        return response("answer", {"notices": [selection(last_data(payload))]}, 3)

    configured = settings(campus)
    configured = configured.model_copy(update={"model": model_settings(timeout_seconds=60.0)})
    report = run(campus, monkeypatch, handler, config=configured)
    assert report.status == "answered" and phases == [None, "read_notice", "answer"]
    assert timeouts == [45.0, 37.5, 60.0]
    assert report.model_usage["exploration_rounds"] == 1
