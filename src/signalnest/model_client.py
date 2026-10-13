"""One bounded synchronous Chat Completions adapter, independent of campus tools/storage."""

import json
import math
import os
import re
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Annotated, Literal

import httpx
from pydantic import Field, StringConstraints

from signalnest.contracts import Contract

ENDPOINT = "https://api.openai.com/v1/chat/completions"
MAX_RESPONSE_BYTES = 256 * 1024


class ModelError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class ModelConfig(Contract):
    provider: Literal["openai-chat-completions"] = "openai-chat-completions"
    model: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}$")]
    api_key_env: Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{0,99}$")] = (
        "OPENAI_API_KEY"
    )
    timeout_seconds: float = Field(default=20.0, strict=True, gt=0, le=60, allow_inf_nan=False)
    max_attempts: int = Field(default=2, strict=True, ge=1, le=2)
    max_completion_tokens: int = Field(default=1800, strict=True, ge=128, le=8192)
    # Operator-provided USD rates, never a claim about current provider prices.
    input_usd_per_million: Decimal = Field(gt=0, le=1000, max_digits=12, decimal_places=6)
    output_usd_per_million: Decimal = Field(gt=0, le=1000, max_digits=12, decimal_places=6)
    max_cost_usd: Decimal = Field(default=Decimal("0.10"), gt=0, le=10)


class ModelBudget:
    """In-memory query ledger. Failed/unknown HTTP attempts consume their full reservation."""

    def __init__(self, *, max_calls: int, max_requests: int, run_seconds: float):
        if (
            type(max_calls) is not int
            or not 1 <= max_calls <= 20
            or type(max_requests) is not int
            or not 1 <= max_requests <= 30
            or not isinstance(run_seconds, (int, float))
            or isinstance(run_seconds, bool)
            or not math.isfinite(run_seconds)
            or not 0 < run_seconds <= 600
        ):
            raise ValueError("invalid finite model budget")
        self.max_calls = max_calls
        self.max_requests = max_requests
        self.deadline = time.monotonic() + run_seconds
        self.calls = 0
        self.requests = 0
        self.charged_usd = Decimal(0)
        self.uncertain_requests = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0

    def remaining(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ModelError("model_time_budget")
        return remaining

    def snapshot(self) -> dict:
        return {
            "model_calls": self.calls,
            "http_requests": self.requests,
            "charged_estimate_usd": str(self.charged_usd),
            "uncertain_requests": self.uncertain_requests,
            "reported_prompt_tokens": self.prompt_tokens,
            "reported_completion_tokens": self.completion_tokens,
        }


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str

    def message(self) -> dict:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.arguments},
        }


@dataclass(frozen=True)
class ModelTurn:
    calls: tuple[ToolCall, ...]

    def message(self) -> dict:
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [c.message() for c in self.calls],
        }


def _price(settings: ModelConfig, prompt: int, completion: int) -> Decimal:
    return (
        prompt * settings.input_usd_per_million + completion * settings.output_usd_per_million
    ) / Decimal(1_000_000)


def _parse_turn(data: object) -> ModelTurn:
    if not isinstance(data, dict) or not isinstance(data.get("choices"), list):
        raise ModelError("model_invalid_response")
    choices = data["choices"]
    if len(choices) != 1 or not isinstance(choices[0], dict):
        raise ModelError("model_invalid_response")
    choice = choices[0]
    if choice.get("finish_reason") == "length":
        raise ModelError("model_output_truncated")
    message = choice.get("message")
    if not isinstance(message, dict):
        raise ModelError("model_invalid_response")
    if message.get("refusal") or choice.get("finish_reason") == "content_filter":
        raise ModelError("model_refused")
    calls = message.get("tool_calls")
    if choice.get("finish_reason") != "tool_calls" or not isinstance(calls, list):
        raise ModelError("model_no_answer_tool")
    if not 1 <= len(calls) <= 4:
        raise ModelError("model_invalid_response")
    parsed = []
    for call in calls:
        if not isinstance(call, dict) or call.get("type") != "function":
            raise ModelError("model_invalid_response")
        fn = call.get("function")
        identifier = call.get("id")
        if not isinstance(fn, dict) or not isinstance(identifier, str):
            raise ModelError("model_invalid_response")
        name, arguments = fn.get("name"), fn.get("arguments")
        if (
            not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", identifier)
            or not isinstance(name, str)
            or not re.fullmatch(r"[A-Za-z_]{1,60}", name)
            or not isinstance(arguments, str)
            or len(arguments) > 16_000
        ):
            raise ModelError("model_invalid_response")
        parsed.append(ToolCall(identifier, name, arguments))
    if len({c.id for c in parsed}) != len(parsed):
        raise ModelError("model_invalid_response")
    return ModelTurn(tuple(parsed))


class OpenAIChatClient:
    """Only this adapter retries; injected HTTPX clients are for offline transport tests."""

    def __init__(self, settings: ModelConfig, *, client: httpx.Client | None = None):
        key = os.environ.get(settings.api_key_env)
        if not key:
            raise ModelError("model_credentials_missing")
        if any(ord(c) < 33 or ord(c) > 126 for c in key):
            raise ModelError("model_credentials_invalid")
        self.settings = settings
        self._headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "User-Agent": "SignalNest/0.1",
        }
        self._owned = client is None
        self._client = client or httpx.Client(
            verify=True,
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
        )

    def close(self) -> None:
        if self._owned:
            self._client.close()

    def complete(self, messages: list[dict], tools: list[dict], budget: ModelBudget) -> ModelTurn:
        budget.remaining()
        if budget.calls >= budget.max_calls:
            raise ModelError("model_call_budget")
        budget.calls += 1
        payload = json.dumps(
            {
                "model": self.settings.model,
                "messages": messages,
                "tools": tools,
                "tool_choice": "required",
                "parallel_tool_calls": False,
                "max_completion_tokens": self.settings.max_completion_tokens,
                "store": False,
                "stream": False,
                "n": 1,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        # Deliberately overestimate without another tokenizer dependency. This is
        # an application estimate, not a mathematical bound on provider billing.
        prompt_estimate = len(payload) + 4096
        reserve = _price(self.settings, prompt_estimate, self.settings.max_completion_tokens)
        for attempt in range(self.settings.max_attempts):
            remaining = budget.remaining()
            if budget.requests >= budget.max_requests:
                raise ModelError("model_request_budget")
            if budget.charged_usd + reserve > self.settings.max_cost_usd:
                raise ModelError("model_cost_budget")
            budget.requests += 1
            budget.charged_usd += reserve
            budget.uncertain_requests += 1
            wait = 0.5 * (attempt + 1)
            try:
                timeout = min(remaining, self.settings.timeout_seconds)
                with self._client.stream(
                    "POST",
                    ENDPOINT,
                    content=payload,
                    headers=self._headers,
                    timeout=httpx.Timeout(timeout),
                    follow_redirects=False,
                ) as response:
                    status = response.status_code
                    if status == 200:
                        if (
                            response.headers.get("content-type", "").split(";")[0].strip().lower()
                            != "application/json"
                        ):
                            raise ModelError("model_invalid_response")
                        if (
                            response.headers.get("content-encoding", "identity").strip().lower()
                            != "identity"
                        ):
                            raise ModelError("model_response_encoding")
                        chunks = bytearray()
                        for chunk in response.iter_bytes(chunk_size=16 * 1024):
                            budget.remaining()
                            chunks.extend(chunk)
                            if len(chunks) > MAX_RESPONSE_BYTES:
                                raise ModelError("model_response_too_large")
                        budget.remaining()
                        try:
                            data = json.loads(chunks)
                        except (ValueError, UnicodeDecodeError, RecursionError) as exc:
                            raise ModelError("model_invalid_response") from exc
                        self._settle(data, budget, reserve, prompt_estimate)
                        return _parse_turn(data)
                    code = {
                        401: "model_authentication_failed",
                        403: "model_access_denied",
                        429: "model_rate_limited",
                    }.get(status, "model_http_failed")
                    retryable = status == 429 or status in {500, 502, 503, 504}
                    retry_after = response.headers.get("retry-after")
                    if retry_after is not None:
                        try:
                            seconds = float(retry_after)
                        except ValueError:
                            retryable = False
                        else:
                            if not math.isfinite(seconds) or not 0 <= seconds <= 3:
                                retryable = False
                            else:
                                wait = max(wait, seconds)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                code, retryable = "model_connection_failed", True
                cause = exc
            except httpx.TimeoutException as exc:
                # A POST may already have been processed/billed; do not retry it.
                raise ModelError("model_timeout") from exc
            except httpx.TransportError as exc:
                raise ModelError("model_transport_failed") from exc
            else:
                cause = None
            if not retryable or attempt + 1 >= self.settings.max_attempts:
                raise ModelError(code) from cause
            if wait >= budget.remaining():
                raise ModelError("model_time_budget")
            time.sleep(wait)
        raise AssertionError("attempt bounds validated by ModelConfig")

    def _settle(self, data, budget, reserve, prompt_estimate):
        usage = data.get("usage") if isinstance(data, dict) else None
        if not isinstance(usage, dict):
            return
        prompt, output = usage.get("prompt_tokens"), usage.get("completion_tokens")
        if type(prompt) is not int or type(output) is not int or min(prompt, output) < 0:
            raise ModelError("model_usage_invalid")
        total = usage.get("total_tokens")
        if total is not None and (type(total) is not int or total != prompt + output):
            raise ModelError("model_usage_invalid")
        if prompt > prompt_estimate or output > self.settings.max_completion_tokens:
            # Unexpected usage invalidates the estimate. Stop immediately, preserving
            # the larger reported cost instead of hiding it behind the configured cap.
            budget.charged_usd += max(Decimal(0), _price(self.settings, prompt, output) - reserve)
            raise ModelError("model_usage_out_of_bounds")
        budget.charged_usd += _price(self.settings, prompt, output) - reserve
        budget.uncertain_requests -= 1
        budget.prompt_tokens += prompt
        budget.completion_tokens += output
