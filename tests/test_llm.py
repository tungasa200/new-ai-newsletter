from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import anthropic
import httpx
import pytest
from pydantic import BaseModel

from ainews.util.cost import BudgetExceeded, CostTracker
from ainews.util.llm import ClaudeLLM, LLMError


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
