"""sitemap.xml 수집기. URL·lastmod·slug만 읽는다(D-12: Anthropic 글 본문은 받지 않는다)."""

from __future__ import annotations

import datetime as dt
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit

from ainews.collect.base import Entry, make_item, parse_iso
from ainews.context import RunContext
from ainews.models import SourceConfig

NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"


def slug_title(url: str) -> str:
    """`/news/claude-code-on-the-web` → `Claude code on the web`."""
    slug = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
    return slug.replace("-", " ").strip().capitalize()


def sitemap_lastmods(url: str, ctx: RunContext) -> dict[str, dt.datetime | None]:
    """sitemap.xml의 {loc: lastmod}."""
    root = ET.fromstring(ctx.http.get(url).content)
    if root.tag == f"{NS}sitemapindex":  # 색인이면 url이 없어 조용히 0건이 된다
        raise ValueError(f"sitemap 색인은 지원하지 않는다: {url}")
    out: dict[str, dt.datetime | None] = {}
    for node in root.iter(f"{NS}url"):
        loc = (node.findtext(f"{NS}loc") or "").strip()
        if loc:
            out[loc] = parse_iso(node.findtext(f"{NS}lastmod"))
    return out


def collect_sitemap(src: SourceConfig, since: dt.datetime, ctx: RunContext) -> list[Entry]:
    """options:
    - path_prefixes: 이 경로로 시작하는 URL만(예: ["/news/", "/engineering/"])

    lastmod는 글을 고쳐도 바뀐다(한 날에 100건 넘게 바뀐 적이 있다). 새 글과 고친 글을 구분할
    수 없으므로 max_items와 발송 이력으로 양을 막는다.
    """
    prefixes: list[str] = src.options.get("path_prefixes") or ["/"]
    entries: list[Entry] = []
    for loc, lastmod in sitemap_lastmods(src.url, ctx).items():
        path = urlsplit(loc).path
        if any(path.startswith(p) and path != p for p in prefixes):
            item = make_item(src, url=loc, title=slug_title(loc), published_at=lastmod)
            entries.append((item, ""))
    return entries
