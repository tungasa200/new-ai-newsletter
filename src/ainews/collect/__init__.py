"""수집(SRC). 담당: AN-backend2.

소스 하나가 실패해도 나머지를 계속한다(FR-SRC-04). 소스별 결과는 SourceStat에 남긴다.
"""

from __future__ import annotations

import datetime as dt

from ainews.collect.base import Collector, Entry
from ainews.collect.feeds import collect_rss
from ainews.collect.github import collect_github_releases
from ainews.collect.hn import collect_hn
from ainews.collect.pages import Robots, collect_dated_page, fetch_page_text
from ainews.collect.sitemap import collect_sitemap
from ainews.context import RunContext
from ainews.models import CollectResult, Item, SourceConfig, SourceStat
from ainews.settings import load_sources
from ainews.state import DEFAULT_LOOKBACK

ERROR_MAX = 300

# 본문 보강 상한(FR-SEL-08, D-13)
BODY_MAX_DOCS = 10
BODY_MAX_CHARS = 12_000

# method(+options.api / options.parser) → 수집기
_COLLECTORS: dict[str, Collector] = {
    "rss": collect_rss,
    "sitemap": collect_sitemap,
    "api:github_releases": collect_github_releases,
    "api:hn_algolia": collect_hn,
    "html:dated_sections": collect_dated_page,
}


def _collector_for(src: SourceConfig) -> Collector:
    sub = src.options.get("api") or src.options.get("parser")
    key = f"{src.method}:{sub}" if sub else src.method
    if key not in _COLLECTORS:
        raise ValueError(f"지원하지 않는 수집 방법: {key}")
    return _COLLECTORS[key]


def collect(
    sources: list[SourceConfig], since: dict[str, dt.datetime], ctx: RunContext
) -> CollectResult:
    """소스별로 수집해 정규화한 항목과 소스별 결과를 돌려준다.

    한 소스가 실패해도 나머지를 계속한다(FR-SRC-04). since[name] 이후 발행분만, 새 글부터
    max_items개까지. 발행 시각이 없는 항목은 매일 다시 잡히므로 뺀다.
    """
    result = CollectResult()
    for src in sources:
        start = since.get(src.name, ctx.now - DEFAULT_LOOKBACK)
        try:
            entries = _collector_for(src)(src, start, ctx)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"[:ERROR_MAX]
            ctx.log.warning("소스 수집 실패 %s: %s", src.name, error)
            result.stats.append(SourceStat(source=src.name, ok=False, error=error))
            continue
        dated = sorted(
            ((i.published_at, i) for i, _ in entries if i.published_at is not None),
            key=lambda pair: pair[0],
            reverse=True,
        )
        fresh = [i for at, i in dated if at >= start][: src.max_items]
        result.items.extend(fresh)
        result.stats.append(
            SourceStat(
                source=src.name,
                ok=True,
                count=len(fresh),
                newest_at=dated[0][0] if dated else None,
            )
        )
    return result


def _refetch(src: SourceConfig, ctx: RunContext) -> dict[str, str]:
    """소스를 다시 받아 {key: 전체 텍스트}. 실패하면 빈 dict(같은 소스를 또 받지 않게)."""
    # since로 거르는 수집기(HN)도 후보를 다시 잡도록 조회 기간을 준다
    lookback = dt.timedelta(hours=src.lookback_hours) if src.lookback_hours else DEFAULT_LOOKBACK
    try:
        entries: list[Entry] = _collector_for(src)(src, ctx.now - lookback, ctx)
    except Exception as exc:
        ctx.log.warning("본문용 다시 받기 실패 %s: %s", src.name, type(exc).__name__)
        return {}
    return {i.key: text for i, text in entries}


def enrich_bodies(items: list[Item], ctx: RunContext) -> list[Item]:
    """상위 후보의 원문 본문을 받아 body를 채운다(D-05 2단계, FR-SEL-08).

    items는 우선순위 순서다(선별이 상위 후보 최대 7건과 그 관련 자료를 넘긴다). 앞에서부터
    최대 BODY_MAX_DOCS개 문서에 본문을 채우고, 문서마다 BODY_MAX_CHARS자에서 자른다.
    body: none 소스(D-12)는 건너뛴다. 실패한 항목은 그대로 둔다.
    """
    sources = {s.name: s for s in load_sources(ctx.settings.sources_path)}
    refetched: dict[str, dict[str, str]] = {}  # 소스 이름 → {key: 전체 텍스트}
    robots = Robots(ctx)
    filled = 0
    out: list[Item] = []
    for item in items:
        src = sources.get(item.source)
        # options.body: source(수집 때 받은 본문, 기본) | page(원문 페이지) | none(D-12)
        mode = src.options.get("body", "source") if src else "none"
        if filled >= BODY_MAX_DOCS or src is None or mode == "none":
            out.append(item)
            continue
        try:
            if mode == "page":
                body = fetch_page_text(item.url, ctx, robots)
            else:
                if src.name not in refetched:
                    refetched[src.name] = _refetch(src, ctx)
                body = refetched[src.name].get(item.key, "")
        except Exception as exc:
            ctx.log.warning("본문 받기 실패 %s: %s", item.url, type(exc).__name__)
            body = ""
        if body.strip():
            out.append(item.model_copy(update={"body": body.strip()[:BODY_MAX_CHARS]}))
            filled += 1
        else:
            out.append(item)
    return out
