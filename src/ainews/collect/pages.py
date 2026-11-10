"""HTML 페이지 수집: robots.txt 확인, 날짜별 changelog 페이지, 기사 본문 받기."""

from __future__ import annotations

import datetime as dt
import html
import re
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from ainews.collect.base import Entry, make_item
from ainews.collect.text import extract_main_text
from ainews.context import RunContext
from ainews.models import SourceConfig

ROBOTS_AGENT = "ainews"

_NOSCRIPT_PRE = re.compile(r"<noscript>\s*<pre>(.*?)</pre>", re.DOTALL | re.IGNORECASE)
_DATE_HEADING = re.compile(
    r"^#{1,6}\s*([A-Z][a-z]+ \d{1,2}, \d{4})[^\n]*$", re.MULTILINE
)  # "## November 10, 2026  {% id="11-10-2026" %}"
_ANCHOR = re.compile(r'\{%\s*id="([^"]+)"\s*%\}')
_TEMPLATE_TAG = re.compile(r"\{%.*?%\}")
_BOLD = re.compile(r"\*\*(.+?)\*\*")


class Robots:
    """호스트별 robots.txt를 한 번만 받아 판정한다. 401·403·429·5xx·네트워크 오류면 막는다."""

    def __init__(self, ctx: RunContext) -> None:
        self._ctx = ctx
        self._cache: dict[str, RobotFileParser | bool] = {}  # bool = 모두 허용/모두 막음

    def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._cache:
            self._cache[origin] = self._load(origin)
        rules = self._cache[origin]
        return rules if isinstance(rules, bool) else rules.can_fetch(ROBOTS_AGENT, url)

    def _load(self, origin: str) -> RobotFileParser | bool:
        try:
            resp = self._ctx.http.get(f"{origin}/robots.txt")
        except httpx.HTTPStatusError as exc:
            # 404 등 4xx: robots.txt가 없으면 모두 허용. 401·403·429·5xx는 모두 막음(RFC 9309)
            code = exc.response.status_code
            return code < 500 and code not in (401, 403, 429)
        except httpx.HTTPError:
            return False
        parser = RobotFileParser()
        parser.parse(resp.text.splitlines())
        return parser


def get_allowed(url: str, ctx: RunContext, robots: Robots) -> str:
    """robots.txt가 허락할 때만 페이지를 받는다."""
    if not robots.allowed(url):
        raise PermissionError(f"robots.txt가 막는다: {url}")
    return ctx.http.get(url).text


def fetch_page_text(url: str, ctx: RunContext, robots: Robots) -> str:
    return extract_main_text(get_allowed(url, ctx, robots))


def page_markdown(page: str) -> str:
    """페이지 텍스트. JS 앱이 <noscript><pre>에 넣어 둔 markdown이 있으면 그것을 쓴다."""
    m = _NOSCRIPT_PRE.search(page)
    return html.unescape(m.group(1)) if m else extract_main_text(page)


def collect_dated_page(src: SourceConfig, since: dt.datetime, ctx: RunContext) -> list[Entry]:
    """`## Month D, YYYY` 제목으로 나뉜 changelog 페이지(Gemini API). 날짜 구역 하나가 항목 하나다.

    options:
    - label: 제목에 굵은 글씨가 없을 때 쓸 이름(없으면 소스 이름)
    """
    text = page_markdown(get_allowed(src.url, ctx, Robots(ctx)))
    heads = list(_DATE_HEADING.finditer(text))
    if not heads:
        raise ValueError("날짜 제목을 찾지 못했다(페이지 구조가 바뀌었을 수 있다)")
    label = src.options.get("label") or src.name
    base = src.url.split("#", 1)[0]
    entries: list[Entry] = []
    for n, head in enumerate(heads):
        try:
            day = dt.datetime.strptime(head.group(1), "%B %d, %Y").replace(tzinfo=dt.UTC)
        except ValueError:
            continue
        end = heads[n + 1].start() if n + 1 < len(heads) else len(text)
        body = _TEMPLATE_TAG.sub("", text[head.end() : end]).strip()
        anchor = _ANCHOR.search(head.group(0))
        bold = _BOLD.search(body)
        if bold:
            title = f"{bold.group(1).rstrip(':')} ({head.group(1)})"
        else:
            title = f"{label} — {head.group(1)}"
        item = make_item(
            src,
            url=f"{base}#{anchor.group(1) if anchor else day.date().isoformat()}",
            title=title,
            published_at=day,
            summary=body,
        )
        entries.append((item, body))
    return entries
