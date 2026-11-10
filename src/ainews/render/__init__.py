"""메일 렌더링(FR-EDT-06·07). 담당: AN-backend3. 지금은 최소 템플릿이다."""

from __future__ import annotations

from jinja2 import Environment, PackageLoader, select_autoescape

from ainews.models import Digest, RenderedMail

_env = Environment(
    loader=PackageLoader("ainews", "render/templates"),
    autoescape=select_autoescape(["html", "html.j2"]),
    trim_blocks=True,
    lstrip_blocks=True,
)


def render(digest: Digest) -> RenderedMail:
    subject = f"AI 실무 소식 — {digest.date:%Y.%m.%d}"
    return RenderedMail(
        subject=subject,
        html=_env.get_template("digest.html.j2").render(d=digest, subject=subject),
        text=_env.get_template("digest.txt.j2").render(d=digest, subject=subject),
    )
