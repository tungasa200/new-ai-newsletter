"""Claude 구조화 출력 호출(FR-SEL-07, FR-OPS-01·02).

- 출력은 tool use를 강제해 받고 pydantic 모델로 검증한다. 강제 도구 호출을 400으로 거절하는
  모델(Sonnet 5.5 등)은 그 실행 동안 구조화 출력(output_config.format, json_schema)으로 받는다.
- 일시 오류(429·5xx·시간 초과·연결) 재시도는 SDK의 max_retries(지수 백오프)에 맡긴다.
- 과부하(529)·5xx·연결 오류·출력 검증 실패로 끝내 실패하면 대체 모델로 1회 더 시도한다.
  대체 모델이 같은 모델이면 출력 검증 실패일 때만 1회 더 시도한다.
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


def _should_fallback(exc: Exception, *, same_model: bool) -> bool:
    if isinstance(exc, LLMTruncatedError):
        return False
    if isinstance(exc, pydantic.ValidationError | LLMError):
        return True  # 출력 문제는 다시 뽑으면 달라질 수 있어 같은 모델이어도 1회 더 시도한다
    if same_model:
        return False  # 일시 오류는 SDK가 이미 재시도했다
    if isinstance(exc, anthropic.APIStatusError):
        return exc.status_code >= 500
    return isinstance(exc, anthropic.APIConnectionError)


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
        self._json_models: set[str] = set()  # 강제 도구 호출 대신 구조화 출력을 쓰는 모델

    def _get_client(self) -> Any:
        if self._client is None:  # 키가 필요한 시점까지 만들지 않는다(--offline·stub 실행)
            self._client = anthropic.Anthropic(api_key=self._api_key, max_retries=self._max_retries)
        return self._client

    def structured[M: BaseModel](
        self, *, model: str, system: str, user: str, schema: type[M], max_tokens: int
    ) -> M:
        last: Exception | None = None
        same = model == self.fallback_model
        for m in (model, self.fallback_model):
            if last is not None:
                log.warning("%s 실패(%s), %s로 1회 더 시도", model, type(last).__name__, m)
            try:
                return self._call(m, system, user, schema, max_tokens)
            except Exception as exc:
                if not _should_fallback(exc, same_model=same):
                    raise
                last = exc
        raise LLMError(f"{model}·{self.fallback_model} 실패: {type(last).__name__}") from last

    def _call[M: BaseModel](
        self, model: str, system: str, user: str, schema: type[M], max_tokens: int
    ) -> M:
        est_in = (len(system) + len(user)) // CHARS_PER_TOKEN_ESTIMATE
        self.cost.check(self.cost.estimate(model, est_in, max_tokens))
        json_out = model in self._json_models
        if json_out:
            # 구조화 출력: 스키마에 맞는 JSON이 text 블록으로 온다. SDK가 스키마를 API 제약에
            # 맞게 바꾼다(범위 제약은 설명으로 옮기고 검증은 아래 pydantic이 한다).
            fmt = {"type": "json_schema", "schema": anthropic.transform_schema(schema)}
            shape: dict[str, Any] = {"output_config": {"format": fmt}}
        else:
            shape = {
                "tools": [
                    {
                        "name": TOOL_NAME,
                        "description": "결과를 이 스키마로 제출한다.",
                        "input_schema": schema.model_json_schema(),
                    }
                ],
                "tool_choice": {"type": "tool", "name": TOOL_NAME},
            }
        try:
            resp = self._get_client().messages.create(
                model=model,
                max_tokens=max_tokens,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user}],
                **shape,
            )
        except anthropic.BadRequestError as exc:
            # 강제 도구 호출을 받지 않는 모델(예: Sonnet 5.5)은 400을 낸다. 구조화 출력으로 바꾼다.
            if json_out or "tool_choice" not in str(exc):
                raise
            log.warning("%s는 강제 도구 호출을 받지 않아 구조화 출력으로 바꾼다", model)
            self._json_models.add(model)
            return self._call(model, system, user, schema, max_tokens)
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
        log.info(
            "%s: in=%d out=%d $%.4f stop=%s",
            model,
            u.input_tokens,
            u.output_tokens,
            cost,
            resp.stop_reason,
        )
        if resp.stop_reason == "max_tokens":
            raise LLMTruncatedError(f"{model} 출력이 max_tokens={max_tokens}에서 잘렸다")
        if json_out:
            text = next((b.text for b in resp.content if b.type == "text"), None)
            if text is None or resp.stop_reason == "refusal":
                raise LLMError(f"{model} 응답에 JSON이 없다(stop_reason={resp.stop_reason})")
            return schema.model_validate_json(text)
        block = next((b for b in resp.content if b.type == "tool_use"), None)
        if block is None:
            raise LLMError(f"{model} 응답에 tool_use가 없다(stop_reason={resp.stop_reason})")
        return schema.model_validate(block.input)
