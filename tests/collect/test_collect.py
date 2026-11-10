"""수집 단위 테스트. 네트워크 없이 fixtures/와 httpx.MockTransport로 돈다."""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from ainews.collect import BODY_MAX_CHARS, BODY_MAX_DOCS, collect, enrich_bodies
from ainews.collect.text import extract_main_text, html_to_text
from ainews.context import RunContext
from ainews.fake import FakeLLM
from ainews.models import Item, SourceConfig
from ainews.settings import Settings
from ainews.util.cost import CostTracker
from ainews.util.http import Http

FIX = Path(__file__).parent / "fixtures"
NOW = dt.datetime(2026, 11, 10, 22, 0, tzinfo=dt.UTC)  # 2026-11-11 07:00 KST

Route = httpx.Response | Callable[[httpx.Request], httpx.Response]


def fixture(name: str, status: int = 200) -> httpx.Response:
    return httpx.Response(status, content=(FIX / name).read_bytes())


class Net:
    """URL(쿼리 제외) → 응답. 등록하지 않은 URL은 404. 받은 요청을 기록한다."""

    def __init__(self, routes: dict[str, Route]) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        route = self.routes.get(str(request.url.copy_with(query=None)))
        if route is None:
            return httpx.Response(404)
        return route(request) if callable(route) else route

    def hits(self, url: str) -> int:
        return sum(1 for r in self.requests if str(r.url.copy_with(query=None)) == url)


@pytest.fixture
def make_ctx(tmp_path: Path) -> Iterator[Callable[..., tuple[RunContext, Net]]]:
    clients: list[Http] = []

    def make(
        routes: dict[str, Route],
        *,
        sources: list[dict[str, Any]] | None = None,
        github_token: str | None = None,
    ) -> tuple[RunContext, Net]:
        net = Net(routes)
        path = tmp_path / "sources.yaml"
        path.write_text(yaml.safe_dump({"sources": sources or []}), encoding="utf-8")
        settings = Settings(sources_path=path, github_token=github_token)
        http = Http("ainews-test", transport=httpx.MockTransport(net), sleep=lambda s: None)
        clients.append(http)
        ctx = RunContext(
            settings=settings,
            http=http,
            llm=FakeLLM(),
            cost=CostTracker(1.0),
            now=NOW,
            log=logging.getLogger("test"),
        )
        return ctx, net

    yield make
    for c in clients:
        c.close()


def src(**fields: Any) -> SourceConfig:
    base: dict[str, Any] = {"name": "S", "category": "official", "kind": "changelog"}
    return SourceConfig.model_validate(base | fields)


def since(hours: int) -> dict[str, dt.datetime]:
    return {"S": NOW - dt.timedelta(hours=hours)}


# ---- RSS ----

RSS_URL = "https://example.com/changelog/feed"


def test_rss_filters_keywords_period_and_undated(make_ctx: Any) -> None:
    ctx, _ = make_ctx({RSS_URL: fixture("rss.xml")})
    s = src(method="rss", url=RSS_URL, options={"keywords": ["AI", "Copilot"]})

    result = collect([s], since(36), ctx)

    assert [i.title for i in result.items] == ["Copilot agent mode in more IDEs"]
    item = result.items[0]
    assert item.url == "https://example.com/changelog/2026-11-10-copilot-agent"  # utm 제거
    assert item.key == "https://example.com/changelog/2026-11-10-copilot-agent"
    assert item.published_at == dt.datetime(2026, 11, 10, 14, 20, 5, tzinfo=dt.UTC)
    assert item.summary == "Agent mode is now available."
    assert (item.source, item.category, item.kind) == ("S", "official", "changelog")
    stat = result.stats[0]
    assert stat.ok and stat.count == 1
    # 멈춘 피드 감지용: 기간 밖이어도 가장 새 항목 날짜를 남긴다
    assert stat.newest_at == dt.datetime(2026, 11, 10, 14, 20, 5, tzinfo=dt.UTC)


def test_rss_relative_link_becomes_absolute_and_max_items(make_ctx: Any) -> None:
    ctx, _ = make_ctx({RSS_URL: fixture("rss.xml")})
    s = src(method="rss", url=RSS_URL, max_items=2)

    items = collect([s], since(24 * 60), ctx).items

    assert [i.title for i in items] == [
        "Copilot agent mode in more IDEs",
        "Dependabot alerts will pause on inactive repositories",
    ]  # 새 글부터 2개
    s3 = src(method="rss", url=RSS_URL, max_items=10)
    urls = [i.url for i in collect([s3], since(24 * 60), ctx).items]
    assert "https://example.com/changelog/2026-10-01-old-model" in urls


def test_rss_one_line_keeps_only_first_line(make_ctx: Any) -> None:
    url = "https://cursor.example/changelog/rss.xml"
    ctx, _ = make_ctx({url: fixture("cursor.xml")})
    s = src(method="rss", url=url, options={"one_line": True})

    (item,) = collect([s], since(168), ctx).items

    assert item.title == "Voice Chat with Agents"
    assert item.summary == "Talk with your agents in a realtime voice call."


def test_rss_dates_from_sitemap_when_feed_has_none(make_ctx: Any) -> None:
    feed = (
        b'<?xml version="1.0"?><rss version="2.0"><channel><title>B</title>'
        b"<item><title>New post</title><link>https://blog.example/new/</link></item>"
        b"<item><title>Old post</title><link>https://blog.example/old/</link></item>"
        b"</channel></rss>"
    )
    sitemap = (
        b'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        b"<url><loc>https://blog.example/new/</loc><lastmod>2026-11-10</lastmod></url>"
        b"<url><loc>https://blog.example/old/</loc><lastmod>2026-10-01</lastmod></url>"
        b"</urlset>"
    )
    sm_url = "https://blog.example/sitemap.xml"
    ctx, _ = make_ctx(
        {RSS_URL: httpx.Response(200, content=feed), sm_url: httpx.Response(200, content=sitemap)}
    )
    s = src(method="rss", url=RSS_URL, options={"dates_from_sitemap": sm_url})

    result = collect([s], since(36), ctx)

    assert [i.title for i in result.items] == ["New post"]
    assert result.items[0].published_at == dt.datetime(2026, 11, 10, tzinfo=dt.UTC)


def test_html_page_instead_of_feed_fails_source(make_ctx: Any) -> None:
    ctx, _ = make_ctx({RSS_URL: httpx.Response(200, html="<html><body>blocked</body></html>")})

    result = collect([src(method="rss", url=RSS_URL)], since(36), ctx)

    assert result.items == []
    assert not result.stats[0].ok


# ---- GitHub releases ----

GH_URL = "https://api.github.com/repos/acme/tool/releases"


def test_github_releases_skip_prerelease_and_title(make_ctx: Any) -> None:
    ctx, net = make_ctx({GH_URL: fixture("releases.json")}, github_token="tok")
    s = src(
        kind="release", method="api", url=GH_URL, options={"api": "github_releases", "tool": "Tool"}
    )

    items = collect([s], since(168), ctx).items

    assert [i.title for i in items] == ["Tool v2.1.322", "Tool v2.1.321"]
    assert items[0].url == "https://github.com/acme/tool/releases/tag/v2.1.322"
    assert items[0].summary.startswith("## What's changed")
    assert items[1].summary == ""
    req = net.requests[0]
    assert req.headers["Authorization"] == "Bearer tok"
    assert req.url.params["per_page"] == "50"


def test_github_token_not_sent_to_other_hosts(make_ctx: Any) -> None:
    url = "https://mirror.example/repos/acme/tool/releases"
    ctx, net = make_ctx({url: fixture("releases.json")}, github_token="tok")
    s = src(kind="release", method="api", url=url, options={"api": "github_releases"})

    collect([s], since(168), ctx)

    assert "Authorization" not in net.requests[0].headers


# ---- HN ----

HN_URL = "https://hn.algolia.com/api/v1/search"


def test_hn_keywords_and_ask_hn_link(make_ctx: Any) -> None:
    ctx, net = make_ctx({HN_URL: fixture("hn.json")})
    s = src(
        category="community",
        kind="forum",
        method="api",
        url=HN_URL,
        options={"api": "hn_algolia", "keywords": ["Claude", "agents"], "min_points": 60},
    )

    items = collect([s], since(36), ctx).items

    assert [i.title for i in items] == [
        "Claude Code adds hooks",
        "Ask HN: How do you prompt agents?",
    ]
    hooks, ask = items
    assert (hooks.url, hooks.score, hooks.comments) == (
        "https://blog.example.org/claude-hooks",
        320,
        120,
    )
    assert hooks.summary == "HN 토론: https://news.ycombinator.com/item?id=111"
    assert ask.url == "https://news.ycombinator.com/item?id=222"
    assert "I use plans." in ask.summary
    start = int((NOW - dt.timedelta(hours=36)).timestamp())
    assert net.requests[0].url.params["numericFilters"] == f"created_at_i>{start},points>=60"


# ---- sitemap ----

SITEMAP_URL = "https://www.anthropic.example/sitemap.xml"


def test_sitemap_reads_only_url_lastmod_slug(make_ctx: Any) -> None:
    ctx, net = make_ctx({SITEMAP_URL: fixture("sitemap.xml")})
    s = src(
        kind="news",
        method="sitemap",
        url=SITEMAP_URL,
        options={"path_prefixes": ["/news/", "/engineering/"]},
    )

    items = collect([s], since(36), ctx).items

    assert [(i.title, i.url) for i in items] == [
        ("Roadshow launch", "https://www.anthropic.example/news/roadshow-launch"),
        (
            "Building effective agents",
            "https://www.anthropic.example/engineering/building-effective-agents",
        ),
    ]
    assert all(i.summary == "" for i in items)
    assert len(net.requests) == 1  # 글 페이지는 받지 않는다(D-12)


# ---- 날짜별 changelog 페이지 ----

PAGE_URL = "https://ai.example/docs/changelog"
ROBOTS_URL = "https://ai.example/robots.txt"


def test_dated_page_sections(make_ctx: Any) -> None:
    ctx, _ = make_ctx({PAGE_URL: fixture("dated.html")})  # robots.txt 404 = 모두 허용
    s = src(
        method="html", url=PAGE_URL, options={"parser": "dated_sections", "label": "Gemini API"}
    )

    items = collect([s], since(168), ctx).items

    assert [(i.title, i.url) for i in items] == [
        (
            "Gemini 4 Carbon (`gemini-4-carbon`) generally available (GA) (November 10, 2026)",
            f"{PAGE_URL}#11-10-2026",
        ),
        ("Gemini API — November 6, 2026", f"{PAGE_URL}#11-06-2026"),
    ]
    assert items[0].published_at == dt.datetime(2026, 11, 10, tzinfo=dt.UTC)
    assert "$2.00 per 1M input tokens" in items[0].summary
    assert "{%" not in items[0].summary


def test_dated_page_respects_robots(make_ctx: Any) -> None:
    ctx, net = make_ctx(
        {
            PAGE_URL: fixture("dated.html"),
            ROBOTS_URL: httpx.Response(200, text="User-agent: *\nDisallow: /docs/\n"),
        }
    )
    s = src(method="html", url=PAGE_URL, options={"parser": "dated_sections"})

    result = collect([s], since(168), ctx)

    assert not result.stats[0].ok and "robots" in (result.stats[0].error or "")
    assert net.hits(PAGE_URL) == 0


# ---- 실패 격리 ----


def test_one_failing_source_does_not_stop_others(make_ctx: Any) -> None:
    ctx, net = make_ctx({RSS_URL: fixture("rss.xml"), GH_URL: httpx.Response(503)})
    sources = [
        src(name="Broken", method="api", url=GH_URL, options={"api": "github_releases"}),
        src(name="Unsupported", method="file_diff", url="https://example.com/CHANGELOG.md"),
        src(name="S", method="rss", url=RSS_URL),
    ]

    result = collect(sources, since(36), ctx)

    by_name = {s.source: s for s in result.stats}
    assert not by_name["Broken"].ok and "503" in (by_name["Broken"].error or "")
    assert net.hits(GH_URL) == 4  # 처음 1회 + 재시도 3회(FR-OPS-01)
    assert not by_name["Unsupported"].ok
    assert by_name["S"].ok and len(result.items) == 2


# ---- 본문 보강 ----


def _item(source: str, url: str) -> Item:
    return Item.new(source=source, category="official", kind="changelog", title=url, url=url)


def test_enrich_uses_source_text_refetching_once(make_ctx: Any) -> None:
    ctx, net = make_ctx(
        {RSS_URL: fixture("rss.xml")},
        sources=[
            {
                "name": "S",
                "category": "official",
                "kind": "changelog",
                "method": "rss",
                "url": RSS_URL,
            }
        ],
    )
    items = [
        _item("S", "https://example.com/changelog/2026-11-10-copilot-agent"),
        _item("S", "https://example.com/changelog/2026-11-09-dependabot"),
        _item("S", "https://example.com/changelog/not-in-feed"),
        _item("Unknown", "https://elsewhere.example/a"),
    ]

    out = enrich_bodies(items, ctx)

    assert out[0].body == "Agent mode is now available in more IDEs.\nSecond paragraph."
    assert out[1].body == "Unrelated maintenance note."  # content가 없으면 요약
    assert out[2].body is None and out[3].body is None
    assert net.hits(RSS_URL) == 1


def test_enrich_skips_body_none_and_caps_docs_and_chars(make_ctx: Any) -> None:
    long_text = "word " * 5000  # 25,000자
    page = f"<html><body><article><p>{long_text}</p></article></body></html>"
    routes: dict[str, Route] = {
        f"https://blog.example/p{n}": httpx.Response(200, html=page) for n in range(12)
    }
    ctx, net = make_ctx(
        routes,
        sources=[
            {"name": "HN", "category": "community", "kind": "forum", "method": "api",
             "url": HN_URL, "options": {"api": "hn_algolia", "body": "page"}},
            {"name": "Cursor", "category": "official", "kind": "changelog", "method": "rss",
             "url": "https://cursor.example/changelog/rss.xml", "options": {"body": "none"}},
        ],
    )  # fmt: skip
    items = [_item("Cursor", "https://cursor.example/changelog/voice-chat")]
    items += [_item("HN", f"https://blog.example/p{n}") for n in range(12)]

    out = enrich_bodies(items, ctx)

    assert out[0].body is None
    assert net.hits("https://cursor.example/changelog/voice-chat") == 0  # D-12 원문 페이지 안 받음
    filled = [i for i in out if i.body]
    assert len(filled) == BODY_MAX_DOCS
    assert all(len(i.body or "") == BODY_MAX_CHARS for i in filled)
    assert out[-1].body is None and net.hits("https://blog.example/p11") == 0


def test_enrich_page_respects_robots(make_ctx: Any) -> None:
    deny_all = httpx.Response(200, text="User-agent: *\nDisallow: /\n")
    ctx, net = make_ctx(
        {
            "https://blog.example/robots.txt": deny_all,
            "https://blog.example/p": httpx.Response(200, html="<article>x</article>"),
        },
        sources=[{"name": "HN", "category": "community", "kind": "forum", "method": "api",
                  "url": HN_URL, "options": {"api": "hn_algolia", "body": "page"}}],
    )  # fmt: skip

    (out,) = enrich_bodies([_item("HN", "https://blog.example/p")], ctx)

    assert out.body is None and net.hits("https://blog.example/p") == 0


# ---- HTML → 평문 ----


def test_extract_main_text_prefers_article() -> None:
    text = extract_main_text((FIX / "article.html").read_text(encoding="utf-8"))

    assert text.startswith("Using hooks in Claude Code\nHooks let you run shell commands")
    for noise in ("Home", "Subscribe", "Copyright", "track()"):
        assert noise not in text


def test_html_to_text_fragment() -> None:
    assert html_to_text("<p>a &amp; <b>b</b></p><p>c</p>") == "a & b\nc"
