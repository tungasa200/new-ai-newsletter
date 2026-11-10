from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import anthropic
import httpx
import pytest
from pydantic import BaseModel

from ainews.util.cost import BudgetExceeded, CostTracker
from ainews.util.llm import ClaudeLLM, LLMError, LLMTruncatedError


class Out(BaseModel):
    answer: str


def _resp(payload: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(type="tool_use", input=payload)],
        usage=SimpleNamespace(
            input_tokens=1000,
            output_tokens=100,
            cache_creation_input_tokens=None,
            cache_read_input_tokens=0,
        ),
        stop_reason="tool_use",
    )


class FakeClient:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = outcomes
        self.calls: list[dict[str, Any]] = []
        self.messages = self

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        out = self.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


def _api_error() -> anthropic.APIStatusError:
    req = httpx.Request("POST", "https://api.example.invalid")
    return anthropic.InternalServerError(
        "boom", response=httpx.Response(500, request=req), body=None
    )


def _auth_error() -> anthropic.APIStatusError:
    req = httpx.Request("POST", "https://api.example.invalid")
    return anthropic.AuthenticationError(
        "bad key", response=httpx.Response(401, request=req), body=None
    )


def _llm(client: FakeClient, budget: float = 1.0) -> ClaudeLLM:
    return ClaudeLLM(api_key=None, cost=CostTracker(budget), fallback_model="cheap", client=client)


def test_structured_forces_tool_and_records_cost() -> None:
    client = FakeClient([_resp({"answer": "hi"})])
    llm = _llm(client)
    out = llm.structured(model="claude-haiku-5-5", system="s", user="u", schema=Out, max_tokens=50)
    assert out.answer == "hi"
    call = client.calls[0]
    assert call["tool_choice"] == {"type": "tool", "name": "submit"}
    assert call["tools"][0]["input_schema"]["properties"]["answer"]["type"] == "string"
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert llm.cost.run_cost > 0


def test_falls_back_once_on_failure() -> None:
    client = FakeClient([_api_error(), _resp({"answer": "fallback"})])
    out = _llm(client).structured(model="main", system="s", user="u", schema=Out, max_tokens=50)
    assert out.answer == "fallback"
    assert [c["model"] for c in client.calls] == ["main", "cheap"]


def test_invalid_output_triggers_fallback_then_error() -> None:
    client = FakeClient([_resp({"wrong": 1}), _resp({"wrong": 2})])
    with pytest.raises(LLMError):
        _llm(client).structured(model="main", system="s", user="u", schema=Out, max_tokens=50)


def test_budget_checked_before_call() -> None:
    client = FakeClient([])
    with pytest.raises(BudgetExceeded):
        _llm(client, budget=0.0).structured(
            model="claude-sonnet-5-5", system="s", user="u", schema=Out, max_tokens=1000
        )
    assert client.calls == []


def test_client_error_does_not_fall_back() -> None:
    client = FakeClient([_auth_error(), _resp({"answer": "unused"})])
    with pytest.raises(anthropic.AuthenticationError):
        _llm(client).structured(model="main", system="s", user="u", schema=Out, max_tokens=50)
    assert [c["model"] for c in client.calls] == ["main"]


def test_truncated_output_raises_without_fallback() -> None:
    cut = _resp({"answer": "partial"})
    cut.stop_reason = "max_tokens"
    client = FakeClient([cut, _resp({"answer": "unused"})])
    llm = _llm(client)
    with pytest.raises(LLMTruncatedError):
        llm.structured(model="main", system="s", user="u", schema=Out, max_tokens=50)
    assert [c["model"] for c in client.calls] == ["main"]
    assert llm.cost.run_cost > 0  # 잘려도 쓴 비용은 기록한다


def test_same_fallback_model_retries_once_on_invalid_output() -> None:
    client = FakeClient([_resp({"wrong": 1}), _resp({"answer": "again"})])
    llm = ClaudeLLM(api_key=None, cost=CostTracker(1.0), fallback_model="main", client=client)
    out = llm.structured(model="main", system="s", user="u", schema=Out, max_tokens=50)
    assert out.answer == "again"
    assert [c["model"] for c in client.calls] == ["main", "main"]


def test_same_fallback_model_does_not_repeat_server_error() -> None:
    client = FakeClient([_api_error(), _resp({"answer": "unused"})])
    llm = ClaudeLLM(api_key=None, cost=CostTracker(1.0), fallback_model="main", client=client)
    with pytest.raises(anthropic.InternalServerError):
        llm.structured(model="main", system="s", user="u", schema=Out, max_tokens=50)
    assert len(client.calls) == 1  # 일시 오류는 SDK가 이미 재시도했다


def _text_resp(text: str, stop: str = "end_turn") -> SimpleNamespace:
    resp = _resp({})
    resp.content = [SimpleNamespace(type="text", text=text)]
    resp.stop_reason = stop
    return resp


def _forced_tool_rejected() -> anthropic.BadRequestError:
    req = httpx.Request("POST", "https://api.example.invalid")
    msg = 'tool_choice: type "tool" and "any" are not supported for this model.'
    return anthropic.BadRequestError(msg, response=httpx.Response(400, request=req), body=None)


def test_model_rejecting_forced_tool_switches_to_json_output() -> None:
    """Actions 두 번째 실행: Sonnet 5.5가 강제 tool_choice를 400으로 거절했다."""
    client = FakeClient(
        [_forced_tool_rejected(), _text_resp('{"answer": "a"}'), _text_resp('{"answer": "b"}')]
    )
    llm = _llm(client)
    first = llm.structured(model="main", system="s", user="u", schema=Out, max_tokens=50)
    second = llm.structured(model="main", system="s", user="u", schema=Out, max_tokens=50)
    assert (first.answer, second.answer) == ("a", "b")
    assert "tool_choice" in client.calls[0]
    for call in client.calls[1:]:  # 그 뒤로는 그 모델에 구조화 출력만 쓴다
        assert "tool_choice" not in call and "tools" not in call
        fmt = call["output_config"]["format"]
        assert fmt["type"] == "json_schema"
        assert fmt["schema"]["additionalProperties"] is False


def test_json_output_refusal_falls_back() -> None:
    client = FakeClient(
        [_forced_tool_rejected(), _text_resp("no", stop="refusal"), _resp({"answer": "cheap"})]
    )
    out = _llm(client).structured(model="main", system="s", user="u", schema=Out, max_tokens=50)
    assert out.answer == "cheap"
    assert [c["model"] for c in client.calls] == ["main", "main", "cheap"]


def test_other_bad_request_is_not_retried() -> None:
    req = httpx.Request("POST", "https://api.example.invalid")
    bad = anthropic.BadRequestError("bad", response=httpx.Response(400, request=req), body=None)
    client = FakeClient([bad, _resp({"answer": "unused"})])
    with pytest.raises(anthropic.BadRequestError):
        _llm(client).structured(model="main", system="s", user="u", schema=Out, max_tokens=50)
    assert len(client.calls) == 1
