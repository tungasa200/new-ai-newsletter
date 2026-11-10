"""--offline 실행과 테스트용 가짜 수집기·가짜 LLM. 네트워크와 키가 필요 없다.

각 단계 담당 세션이 자기 단계의 가짜 데이터를 고쳐도 된다.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from ainews import edit, judge
from ainews.context import RunContext
from ainews.models import CollectResult, Item, SourceConfig, SourceStat


def fake_items(now: dt.datetime) -> list[Item]:
    h = dt.timedelta(hours=1)
    return [
        Item.new(
            source="Fake Platform Notes",
            category="official",
            kind="changelog",
            title="API에 새 파라미터 추가",
            url="https://example.com/platform/notes#2026-11-10",
            published_at=now - 5 * h,
            summary="가짜 changelog 항목.",
        ),
        Item.new(
            source="Fake CLI Releases",
            category="official",
            kind="release",
            title="fake-cli v1.2.0",
            url="https://example.com/cli/releases/v1.2.0",
            published_at=now - 8 * h,
            summary="가짜 릴리스 노트.",
        ),
        Item.new(
            source="Fake Vendor News",
            category="official",
            kind="news",
            title="새 모델 발표",
            url="https://example.com/news/new-model?utm_source=rss",
            published_at=now - 10 * h,
            summary="가짜 공식 발표.",
        ),
        Item.new(
            source="Fake Practitioner Blog",
            category="community",
            kind="blog",
            title="에이전트 설정 파일로 반복 작업 줄이기",
            url="https://example.org/blog/agent-config/",
            published_at=now - 12 * h,
            summary="가짜 실무 글.",
        ),
        Item.new(
            source="Fake Forum",
            category="community",
            kind="forum",
            title="새 모델 발표 토론",
            url="https://example.net/item?id=1",
            published_at=now - 9 * h,
            summary="가짜 토론 글.",
            score=120,
            comments=45,
        ),
    ]


def fake_collect(
    sources: list[SourceConfig], since: dict[str, dt.datetime], ctx: RunContext
) -> CollectResult:
    items = fake_items(ctx.now)
    stats = [SourceStat(source=i.source, ok=True, count=1, newest_at=i.published_at) for i in items]
    return CollectResult(items=items, stats=stats)


def fake_enrich_bodies(items: list[Item], ctx: RunContext) -> list[Item]:
    return [i.model_copy(update={"body": f"(가짜 본문) {i.title}"}) for i in items]


def default_handlers() -> dict[type[BaseModel], Callable[[str], Any]]:
    """단계별 가짜 응답(--offline 기본값). 각 단계 담당 세션이 자기 모듈에 정의한다."""
    return {
        judge.ClassifyOutput: judge.fake_classify,
        edit.EditOutput: edit.fake_edit,
        edit.KnowledgeOutput: edit.fake_knowledge,
    }


class FakeLLM:
    """스키마별로 등록한 함수가 응답을 만든다. 등록하지 않은 스키마를 부르면 실패한다.

    handlers를 주지 않으면 default_handlers()를 쓴다.
    """

    def __init__(self, handlers: dict[type[BaseModel], Callable[[str], Any]] | None = None) -> None:
        self.handlers = default_handlers() if handlers is None else dict(handlers)
        self.calls: list[tuple[str, type[BaseModel]]] = []

    def structured[M: BaseModel](
        self, *, model: str, system: str, user: str, schema: type[M], max_tokens: int
    ) -> M:
        self.calls.append((model, schema))
        handler = self.handlers.get(schema)
        if handler is None:
            raise LookupError(f"FakeLLM에 {schema.__name__} 응답이 등록되지 않았다")
        return schema.model_validate(handler(user))
