"""메일 렌더링(FR-EDT-06·07). 담당: AN-backend3.

Claude는 HTML을 쓰지 않는다. Digest를 템플릿에 넣어 HTML(인라인 스타일)과 텍스트 대체본을 만든다.
"""

from __future__ import annotations

from jinja2 import Environment, PackageLoader, select_autoescape

from ainews.models import Digest, RenderedMail

KIND_LABEL = {"news": "뉴스", "tip": "실무팁"}
FRESHNESS_LABEL = {"current": "현행", "caution": "주의", "outdated": "구식"}
SOURCE_LABEL = {"official": "공식", "community": "커뮤니티"}
EMPTY_ITEMS = "오늘은 해설로 다룰 소식이 없습니다."

_env = Environment(
    loader=PackageLoader("ainews", "render/templates"),
    autoescape=select_autoescape(["html", "html.j2"]),
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=True,
)
_env.globals.update(
    kind_label=KIND_LABEL,
    freshness_label=FRESHNESS_LABEL,
    source_label=SOURCE_LABEL,
    empty_items=EMPTY_ITEMS,
)


def render(digest: Digest) -> RenderedMail:
    subject = f"AI 실무 소식 — {digest.date:%Y.%m.%d}"
    if digest.items:
        subject += f" (핵심 {len(digest.items)}건)"
    return RenderedMail(
        subject=subject,
        html=_env.get_template("digest.html.j2").render(d=digest, subject=subject),
        text=_env.get_template("digest.txt.j2").render(d=digest),
    )
