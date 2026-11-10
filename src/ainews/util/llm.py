"""Claude 구조화 출력 호출(FR-SEL-07, FR-OPS-01·02).

- 출력은 tool use를 강제해 받고 pydantic 모델로 검증한다.
- 일시 오류(429·5xx·시간 초과·연결) 재시도는 SDK의 max_retries(지수 백오프)에 맡긴다.
- 과부하(529)·5xx·연결 오류·출력 검증 실패로 끝내 실패하면 대체 모델로 1회 더 시도한다.
  요청·인증·권한 오류(4xx)는 모델을 바꿔도 같으므로 바로 실패한다.
- 사용량은 CostTracker에 누적하고, 호출 전에 상한을 확인한다.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

import anthropic
import pydantic
from pydantic import BaseModel

from ainews.util.cost import CHARS_PER_TOKEN_ESTIMATE, CostTracker, Usage

log = logging.getLogger(__name__)

TOOL_NAME = "submit"


class LLM(Protocol):
    def structured[M: BaseModel](
        self, *, model: str, system: str, user: str, schema: type[M], max_tokens: int
    ) -> M: ...


class LLMError(Exception):
    """기본 모델과 대체 모델이 모두 실패했다."""


class LLMTruncatedError(LLMError):
    """출력이 max_tokens에서 잘렸다. 대체 모델도 같은 상한이라 다시 시도하지 않는다."""


def _should_fallback(exc: Exception) -> bool:
    if isinstance(exc, anthropic.APIStatusError):
        return exc.status_code >= 500
    if isinstance(exc, LLMTruncatedError):
        return False
    return isinstance(exc, anthropic.APIConnectionError | pydantic.ValidationError | LLMError)


class ClaudeLLM:
    def __init__(
        self,
        *,
        api_key: str | None,
        cost: CostTracker,
        fallback_model: str,
        client: Any = None,
        max_retries: int = 3,
    ) -> None:
        self._api_key = api_key
        self._client = client
        self._max_retries = max_retries
        self.cost = cost
        self.fallback_model = fallback_model

    def _get_client(self) -> Any:
        if self._client is None:  # 키가 필요한 시점까지 만들지 않는다(--offline·stub 실행)
            self._client = anthropic.Anthropic(api_key=self._api_key, max_retries=self._max_retries)
        return self._client

    def structured[M: BaseModel](
        self, *, model: str, system: str, user: str, schema: type[M], max_tokens: int
    ) -> M:
        last: Exception | None = None
        for m in dict.fromkeys([model, self.fallback_model]):  # 같은 모델이면 한 번만
            if last is not None:
                log.warning("%s 실패(%s), 대체 모델 %s로 1회 시도", model, type(last).__name__, m)
            try:
                return self._call(m, system, user, schema, max_tokens)
            except Exception as exc:
                if not _should_fallback(exc):
                    raise
                last = exc
        raise LLMError(f"{model}·{self.fallback_model} 실패: {type(last).__name__}") from last

    def _call[M: BaseModel](
        self, model: str, system: str, user: str, schema: type[M], max_tokens: int
    ) -> M:
        est_in = (len(system) + len(user)) // CHARS_PER_TOKEN_ESTIMATE
        self.cost.check(self.cost.estimate(model, est_in, max_tokens))
        resp = self._get_client().messages.create(
            model=model,
            max_tokens=max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            tools=[
                {
                    "name": TOOL_NAME,
                    "description": "결과를 이 스키마로 제출한다.",
                    "input_schema": schema.model_json_schema(),
                }
            ],
            tool_choice={"type": "tool", "name": TOOL_NAME},
        )
        u = resp.usage
        cost = self.cost.record(
            model,
            Usage(
                input_tokens=u.input_tokens,
                output_tokens=u.output_tokens,
                cache_creation_input_tokens=u.cache_creation_input_tokens or 0,
                cache_read_input_tokens=u.cache_read_input_tokens or 0,
            ),
        )
        log.info("%s: in=%d out=%d $%.4f", model, u.input_tokens, u.output_tokens, cost)
        if resp.stop_reason == "max_tokens":
            raise LLMTruncatedError(f"{model} 출력이 max_tokens={max_tokens}에서 잘렸다")
        block = next((b for b in resp.content if b.type == "tool_use"), None)
        if block is None:
            raise LLMError(f"{model} 응답에 tool_use가 없다(stop_reason={resp.stop_reason})")
        return schema.model_validate(block.input)
