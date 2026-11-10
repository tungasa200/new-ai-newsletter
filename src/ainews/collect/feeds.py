"""RSS·Atom 피드 수집기."""

from __future__ import annotations

import datetime as dt
import time
from typing import Any
from urllib.parse import urljoin

import feedparser

from ainews.collect.base import Entry, keyword_filter, make_item
from ainews.collect.sitemap import sitemap_lastmods
from ainews.collect.text import html_to_text
from ainews.context import RunContext
from ainews.models import SourceConfig
from ainews.util.urlnorm import normalize_url

ONE_LINE_MAX = 300


def _entry_time(entry: Any) -> dt.datetime | None:
    parsed: time.struct_time | None = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed is None:
        return None
    return dt.datetime(*parsed[:6], tzinfo=dt.UTC)


def _first_line(raw: str) -> str:
    """피드 설명의 첫 줄(평문·HTML 모두). 태그만 있는 줄은 건너뛴다."""
    for raw_line in raw.strip().splitlines():
        lines = html_to_text(raw_line).splitlines()
        if lines:
            return lines[0][:ONE_LINE_MAX]
    return ""


def _title(entry: Any) -> str:
    """제목. feedparser가 평문(text/plain)으로 준 제목은 `Vec<T>` 같은 꺾쇠를 살린다."""
    title: str = entry.get("title", "")
    if entry.get("title_detail", {}).get("type") == "text/plain":
        return title
    return html_to_text(title)


def collect_rss(src: SourceConfig, since: dt.datetime, ctx: RunContext) -> list[Entry]:
    """options:
    - keywords: 이 단어가 제목·요약에 있는 글만(예: GitHub Changelog의 AI 항목)
    - one_line: true면 요약을 피드 설명의 첫 줄(원문 그대로)만 쓰고 본문은 버린다(D-12 Cursor)
    - dates_from_sitemap: 피드에 날짜가 없으면 이 sitemap.xml의 lastmod를 쓴다
      (Google Developers Blog 피드는 항목에 날짜가 없다)
    """
    resp = ctx.http.get(src.url)
    feed = feedparser.parse(resp.content)
    if not feed.get("version"):  # 차단 페이지·캡차 HTML 등 피드가 아닌 응답
        raise ValueError(f"피드가 아니다({resp.headers.get('content-type', '?')})")
    wanted = keyword_filter(src)
    one_line = bool(src.options.get("one_line"))
    sitemap_url = src.options.get("dates_from_sitemap")
    lastmods = (
        {normalize_url(k): v for k, v in sitemap_lastmods(sitemap_url, ctx).items()}
        if sitemap_url
        else {}
    )
    entries: list[Entry] = []
    for e in feed.entries:
        link = e.get("link")
        if not link:
            continue
        title = _title(e)
        raw_summary = e.get("summary", "")
        summary = html_to_text(raw_summary)
        if not wanted(f"{title}\n{summary}"):
            continue
        if one_line:
            summary, full = _first_line(raw_summary), ""
        else:
            content = e.get("content") or []
            full = (html_to_text(content[0].get("value", "")) if content else "") or summary
        url = urljoin(src.url, link)
        item = make_item(
            src,
            url=url,
            title=title,
            published_at=_entry_time(e) or lastmods.get(normalize_url(url)),
            summary=summary,
        )
        entries.append((item, full))
    return entries
