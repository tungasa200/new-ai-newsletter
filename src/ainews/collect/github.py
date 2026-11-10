"""GitHub REST API 릴리스 수집기(`GET /repos/<o>/<r>/releases`). releases.atom은 robots가 막는다."""

from __future__ import annotations

import datetime as dt
from urllib.parse import urlsplit

from ainews.collect.base import Entry, make_item, parse_iso
from ainews.context import RunContext
from ainews.models import SourceConfig

API_HOST = "api.github.com"
PER_PAGE = 50  # 사전 릴리스를 거른 뒤에도 정식 릴리스가 남도록 넉넉히 받는다


def collect_github_releases(src: SourceConfig, since: dt.datetime, ctx: RunContext) -> list[Entry]:
    """options:
    - tool: 제목 앞에 붙일 도구 이름(없으면 소스 이름)
    - include_prerelease: true면 사전 릴리스도 넣는다(기본 제외. nightly·preview가 많다)
    """
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    # 토큰은 GitHub API에만 보낸다(설정 실수로 다른 호스트에 새지 않게)
    parts = urlsplit(src.url)
    if ctx.settings.github_token and parts.scheme == "https" and parts.hostname == API_HOST:
        headers["Authorization"] = f"Bearer {ctx.settings.github_token}"
    resp = ctx.http.get(src.url, params={"per_page": PER_PAGE}, headers=headers)
    releases = resp.json()
    if not isinstance(releases, list):
        raise ValueError("릴리스 목록 형식이 아니다")
    tool = src.options.get("tool") or src.name
    include_pre = bool(src.options.get("include_prerelease"))
    entries: list[Entry] = []
    for r in releases:
        if r.get("draft") or (r.get("prerelease") and not include_pre):
            continue
        body = (r.get("body") or "").strip()
        item = make_item(
            src,
            url=r["html_url"],
            title=f"{tool} {r.get('name') or r['tag_name']}",
            published_at=parse_iso(r.get("published_at")),
            summary=body,
        )
        entries.append((item, body))
    return entries
