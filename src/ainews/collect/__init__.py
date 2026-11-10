"""수집(SRC). 담당: AN-backend2. 지금은 stub이다. 시그니처는 계약(B-1)대로 유지한다."""

from __future__ import annotations

import datetime as dt

from ainews.context import RunContext
from ainews.models import CollectResult, Item, SourceConfig


def collect(
    sources: list[SourceConfig], since: dict[str, dt.datetime], ctx: RunContext
) -> CollectResult:
    """소스별로 수집해 정규화한 항목과 소스별 결과를 돌려준다.

    한 소스가 실패해도 나머지를 계속한다(FR-SRC-04). since[name] 이후 발행분만.
    """
    return CollectResult()


def enrich_bodies(items: list[Item], ctx: RunContext) -> list[Item]:
    """상위 후보의 원문 본문을 받아 body를 채운다(D-05 2단계). 실패한 항목은 그대로 둔다."""
    return items
