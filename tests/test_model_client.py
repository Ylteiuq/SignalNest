"""Bounded real HTTPX requests against offline transports; not live model or billing evidence."""

import json
from decimal import Decimal

import httpx
import pytest
from test_query_agent import completion, model_settings

from signalnest.model_client import (
    ENDPOINT,
    MAX_RESPONSE_BYTES,
    ModelBudget,
    ModelError,
    OpenAIChatClient,
)


@pytest.fixture(autouse=True)
def environment(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "offline-private-test-key")
    # Do not actually sleep in retry tests, but let budget assertions use real time.
    monkeypatch.setattr("signalnest.model_client.time.sleep", lambda seconds: None)


def budget(**overrides):
    return ModelBudget(**dict(max_calls=6, max_requests=8, run_seconds=90) | overrides)


def execute(handler, *, settings=None, ledger=None):
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAIChatClient(settings or model_settings(), client=client)
        return provider.complete([{"role": "user", "content": "科研机会"}], [], ledger or budget())


def test_actual_headers_payload_usage_and_resource_closure():
    seen = []
    ledger = budget()

    def handler(request):
        seen.append(request)
        assert str(request.url) == ENDPOINT
        assert request.headers["Authorization"] == "Bearer offline-private-test-key"
        assert request.headers["Accept-Encoding"] == "identity"
        body = json.loads(request.content)
        assert body["max_completion_tokens"] == 1800
        assert body["store"] is False and body["n"] == 1
        assert body["tool_choice"] == "required" and body["stream"] is False
        response = httpx.Response(200, json=completion("read_profile", {}))
        seen.append(response)
        return response

    result = execute(handler, ledger=ledger)
    assert result.calls[0].name == "read_profile"
    assert seen[1].is_closed
    assert ledger.snapshot() == {
        "model_calls": 1,
        "http_requests": 1,
        "charged_estimate_usd": "0.0005",
        "uncertain_requests": 0,
        "reported_prompt_tokens": 100,
        "reported_completion_tokens": 100,
    }


@pytest.mark.parametrize(
    "status,code",
    [
        (400, "model_http_failed"),
        (401, "model_authentication_failed"),
        (403, "model_access_denied"),
        (302, "model_http_failed"),
        (429, "model_rate_limited"),
        (503, "model_http_failed"),
    ],
)
def test_finite_status_codes_no_redirect_or_unbounded_retry(status, code):
    seen = []
    ledger = budget()

    def handler(request):
        response = httpx.Response(
            status, text="private failure text", headers={"Location": "https://evil.test"}
        )
        seen.append(response)
        return response

    with pytest.raises(ModelError) as caught:
        execute(handler, ledger=ledger)
    assert caught.value.code == code and "private" not in str(caught.value)
    assert len(seen) == (2 if status in {429, 503} else 1)
    assert all(r.is_closed for r in seen)
    assert ledger.uncertain_requests == len(seen)
    assert ledger.charged_usd > 0


def test_only_one_retry_layer_and_success_settles_only_successful_attempt():
    seen = []
    ledger = budget()

    def handler(request):
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(429, headers={"Retry-After": "1"})
        return httpx.Response(200, json=completion("answer", {"notices": []}))

    result = execute(handler, ledger=ledger)
    assert result.calls[0].name == "answer"
    assert len(seen) == ledger.requests == 2 and ledger.calls == 1
    assert ledger.uncertain_requests == 1
    assert ledger.charged_usd > Decimal("0.0005")


@pytest.mark.parametrize(
    "retry_after", ["100", "nan", "-1", "not-a-date", "Wed, 21 Oct 2026 07:28:00 GMT"]
)
def test_retry_after_cannot_force_long_wait(retry_after):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(429, headers={"Retry-After": retry_after})

    with pytest.raises(ModelError, match="model_rate_limited"):
        execute(handler)
    assert len(seen) == 1


@pytest.mark.parametrize(
    "error,retries,code",
    [
        (httpx.ConnectError, 2, "model_connection_failed"),
        (httpx.ConnectTimeout, 2, "model_connection_failed"),
        (httpx.ReadTimeout, 1, "model_timeout"),
        (httpx.WriteTimeout, 1, "model_timeout"),
        (httpx.ReadError, 1, "model_transport_failed"),
        (httpx.RemoteProtocolError, 1, "model_transport_failed"),
    ],
)
def test_transport_retry_semantics(error, retries, code):
    seen = []

    def handler(request):
        seen.append(request)
        raise error("credential-containing exception", request=request)

    with pytest.raises(ModelError) as caught:
        execute(handler)
    assert caught.value.code == code and len(seen) == retries
    assert "credential" not in str(caught.value)


@pytest.mark.parametrize("kind", ["calls", "requests", "time", "cost"])
def test_budgets_prevent_the_next_actual_request(kind):
    ledger = budget(max_requests=1)
    settings = model_settings()
    if kind == "calls":
        ledger.calls = ledger.max_calls
    elif kind == "requests":
        ledger.requests = ledger.max_requests
    elif kind == "time":
        ledger.deadline = 0
    else:
        settings = settings.model_copy(update={"max_cost_usd": Decimal("0.000001")})

    def forbidden(request):
        raise AssertionError("budget should block all transport calls")

    with pytest.raises(
        ModelError,
        match={
            "calls": "model_call_budget",
            "requests": "model_request_budget",
            "time": "model_time_budget",
            "cost": "model_cost_budget",
        }[kind],
    ):
        execute(forbidden, ledger=ledger, settings=settings)


def test_retry_also_consumes_request_and_cost_limits():
    ledger = budget(max_requests=1)
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(503)

    with pytest.raises(ModelError, match="model_request_budget"):
        execute(handler, ledger=ledger)
    assert len(seen) == 1 and ledger.uncertain_requests == 1
    reserve = ledger.charged_usd
    ledger = budget()
    settings = model_settings().model_copy(update={"max_cost_usd": reserve * Decimal("1.5")})
    seen.clear()
    with pytest.raises(ModelError, match="model_cost_budget"):
        execute(handler, ledger=ledger, settings=settings)
    assert len(seen) == 1


@pytest.mark.parametrize(
    "data,code",
    [
        ({"choices": []}, "model_invalid_response"),
        ({"choices": [{"message": {}, "finish_reason": "length"}]}, "model_output_truncated"),
        (
            {"choices": [{"message": {"refusal": "refused"}, "finish_reason": "stop"}]},
            "model_refused",
        ),
        (
            {"choices": [{"message": {"content": "I made up a link"}, "finish_reason": "stop"}]},
            "model_no_answer_tool",
        ),
        (
            completion("search", {}, usage={"prompt_tokens": True, "completion_tokens": 5}),
            "model_usage_invalid",
        ),
        (
            completion("search", {}, usage={"prompt_tokens": 0, "completion_tokens": -1}),
            "model_usage_invalid",
        ),
        (
            completion("search", {}, usage={"prompt_tokens": 1_000_000, "completion_tokens": 1}),
            "model_usage_out_of_bounds",
        ),
        (
            completion("search", {}, usage={"prompt_tokens": 10, "completion_tokens": 1801}),
            "model_usage_out_of_bounds",
        ),
    ],
)
def test_bad_response_and_usage_fail_without_free_unknown_charge(data, code):
    ledger = budget()
    with pytest.raises(ModelError, match=code):
        execute(lambda request: httpx.Response(200, json=data), ledger=ledger)
    assert ledger.requests == 1 and ledger.charged_usd > 0


def test_missing_usage_retains_conservative_charge():
    ledger = budget()
    execute(
        lambda request: httpx.Response(200, json=completion("read_profile", {}, usage=None)),
        ledger=ledger,
    )
    assert ledger.uncertain_requests == 1 and ledger.charged_usd > Decimal("0.0005")


@pytest.mark.parametrize(
    "body,headers,code",
    [
        (
            b"not JSON: private message",
            {"content-type": "application/json"},
            "model_invalid_response",
        ),
        (b"<html>error</html>", {"content-type": "text/html"}, "model_invalid_response"),
        (
            b"x" * (MAX_RESPONSE_BYTES + 1),
            {"content-type": "application/json"},
            "model_response_too_large",
        ),
    ],
)
def test_response_body_limits_and_close_on_failure(body, headers, code):
    response = httpx.Response(200, content=body, headers=headers)
    with pytest.raises(ModelError, match=code):
        execute(lambda request: response)
    assert response.is_closed


def test_expired_budget_during_stream_closes_response():
    ledger = budget()

    class SlowStream(httpx.SyncByteStream):
        closed = False

        def __iter__(self):
            ledger.deadline = 0
            yield b"{}"

        def close(self):
            self.closed = True

    stream = SlowStream()
    with pytest.raises(ModelError, match="model_time_budget"):
        execute(
            lambda request: httpx.Response(
                200, stream=stream, headers={"content-type": "application/json"}
            ),
            ledger=ledger,
        )
    assert stream.closed


def test_credentials_only_from_selected_env_and_never_provider_payload(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY")
    with pytest.raises(ModelError, match="model_credentials_missing"):
        OpenAIChatClient(model_settings())
    monkeypatch.setenv("CUSTOM_MODEL_KEY", "custom-test-key")
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=completion("read_profile", {}))
        )
    ) as client:
        provider = OpenAIChatClient(model_settings(api_key_env="CUSTOM_MODEL_KEY"), client=client)
        assert "custom-test-key" not in repr(provider)
        provider.complete([], [], budget())
        provider.close()
        assert not client.is_closed  # Borrowed resources belong to the caller.
    monkeypatch.setenv("OPENAI_API_KEY", "key\r\ninjection")
    with pytest.raises(ModelError, match="model_credentials_invalid"):
        OpenAIChatClient(model_settings())


def test_compressed_response_is_rejected_before_decoding_and_is_closed():
    class Stream(httpx.SyncByteStream):
        closed = False

        def __iter__(self):
            raise AssertionError("unexpected encoding must not be decoded")
            yield b"unused"

        def close(self):
            self.closed = True

    stream = Stream()
    with pytest.raises(ModelError, match="model_response_encoding"):
        execute(
            lambda request: httpx.Response(
                200,
                stream=stream,
                headers={
                    "content-type": "Application/JSON; charset=utf-8",
                    "content-encoding": "gzip",
                },
            )
        )
    assert stream.closed


def test_inconsistent_total_usage_does_not_refund_reservation():
    ledger = budget()
    data = completion(
        "read_profile",
        {},
        usage={"prompt_tokens": 100, "completion_tokens": 100, "total_tokens": 99},
    )
    with pytest.raises(ModelError, match="model_usage_invalid"):
        execute(lambda request: httpx.Response(200, json=data), ledger=ledger)
    assert ledger.uncertain_requests == 1 and ledger.charged_usd > Decimal("0.0005")


@pytest.mark.parametrize(
    "values",
    [
        {"run_seconds": float("nan")},
        {"run_seconds": True},
        {"run_seconds": "90"},
        {"max_calls": True},
        {"max_requests": 0},
    ],
)
def test_reusable_budget_rejects_unbounded_or_wrong_types(values):
    with pytest.raises(ValueError, match="invalid finite model budget"):
        budget(**values)
