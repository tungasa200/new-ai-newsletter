"""수집기 공통 도구."""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Callable
from typing import Any

from ainews.context import RunContext
from ainews.models import Item, SourceConfig

# (항목, 본문 보강에 쓸 전체 텍스트. 없으면 "")
Entry = tuple[Item, str]
# 수집기: 소스 하나를 받아 항목을 돌려준다. since 이후만 고르는 일은 collect()가 한다.
Collector = Callable[[SourceConfig, dt.datetime, RunContext], list[Entry]]


def make_item(src: SourceConfig, *, url: str, title: str, **fields: Any) -> Item:
    return Item.new(
        url=url,
        source=src.name,
        category=src.category,
        kind=src.kind,
        title=title.strip() or url,
        **fields,
    )


def parse_iso(value: str | None) -> dt.datetime | None:
    """ISO 8601 문자열 → UTC aware. 시간대가 없으면 UTC로 본다."""
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


def keyword_filter(src: SourceConfig) -> Callable[[str], bool]:
    """options.keywords 중 한 단어라도 들어간 글만 통과(대소문자 무시, 단어 경계)."""
    keywords: list[str] = src.options.get("keywords") or []
    if not keywords:
        return lambda _text: True
    pattern = re.compile(
        r"\b(?:" + "|".join(re.escape(k) for k in keywords) + r")\b", re.IGNORECASE
    )
    return lambda text: bool(pattern.search(text))
