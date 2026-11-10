"""Hacker News(Algolia 검색 API) 수집기. 요청 1회로 기간·점수를 거르고 키워드는 여기서 거른다."""

from __future__ import annotations

import datetime as dt

from ainews.collect.base import Entry, keyword_filter, make_item, parse_iso
from ainews.collect.text import html_to_text
from ainews.context import RunContext
from ainews.models import SourceConfig

HN_ITEM = "https://news.ycombinator.com/item?id="
HITS = 200


def collect_hn(src: SourceConfig, since: dt.datetime, ctx: RunContext) -> list[Entry]:
    """options:
    - min_points: 이 점수 이상만(기본 50, 레거시와 같다)
    - keywords: 이 단어가 제목에 있는 글만
    """
    min_points = int(src.options.get("min_points", 50))
    params: dict[str, str | int] = {
        "tags": "story",
        "numericFilters": f"created_at_i>{int(since.timestamp())},points>={min_points}",
        "hitsPerPage": HITS,
    }
    hits = ctx.http.get(src.url, params=params).json()["hits"]
    wanted = keyword_filter(src)
    entries: list[Entry] = []
    for h in hits:
        title = h.get("title") or ""
        if not wanted(title):
            continue
        discussion = HN_ITEM + str(h["objectID"])
        text = html_to_text(h.get("story_text") or "")
        item = make_item(
            src,
            url=h.get("url") or discussion,
            title=title,
            published_at=parse_iso(h.get("created_at")),
            summary=f"HN 토론: {discussion}\n{text}".strip(),
            score=h.get("points"),
            comments=h.get("num_comments"),
        )
        entries.append((item, text))
    return entries
