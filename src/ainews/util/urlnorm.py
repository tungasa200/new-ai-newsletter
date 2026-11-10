"""URL 정규화(FR-DUP-01)."""

from __future__ import annotations

import hashlib
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING_KEYS = frozenset(
    {"fbclid", "gclid", "mc_cid", "mc_eid", "igshid", "ref", "ref_src", "source", "si"}
)


def _is_utm(key: str) -> bool:
    return key.lower().startswith("utm_")


def normalize_url(url: str) -> str:
    """https 통일, 호스트 소문자·www 제거, 추적 파라미터 제거, 끝 슬래시 제거.

    fragment는 남긴다. changelog 한 페이지 안의 항목을 앵커로 구분하는 소스가 있다.
    """
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower().removeprefix("www.")
    if parts.port and parts.port not in (80, 443):
        host = f"{host}:{parts.port}"
    path = parts.path.rstrip("/")
    query = urlencode(
        sorted(
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not _is_utm(k) and k.lower() not in TRACKING_KEYS
        )
    )
    return urlunsplit(("https", host, path, query, parts.fragment))


def is_web_url(url: str) -> bool:
    return urlsplit(url.strip()).scheme.lower() in ("http", "https")


def strip_utm(url: str) -> str:
    """메일에 실을 링크: utm_* 파라미터만 빼고 스킴·호스트·경로·다른 파라미터는 그대로 둔다."""
    parts = urlsplit(url.strip())
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    kept = [(k, v) for k, v in pairs if not _is_utm(k)]
    if len(kept) == len(pairs):
        return url.strip()
    return urlunsplit(parts._replace(query=urlencode(kept)))


def url_id(normalized_url: str) -> str:
    return hashlib.sha1(normalized_url.encode("utf-8")).hexdigest()[:12]
