"""HTML → 평문. 피드 본문 조각과 기사 페이지 본문 추출에 쓴다(표준 라이브러리만)."""

from __future__ import annotations

import re
from html.parser import HTMLParser

# 피드 조각에서도 버리는 태그
_ALWAYS_SKIP = frozenset({"script", "style", "template", "svg", "noscript"})
# 페이지 본문 추출 때 더 버리는 태그(메뉴·머리·꼬리·광고 자리)
_PAGE_SKIP = _ALWAYS_SKIP | {"nav", "header", "footer", "aside", "form", "button", "iframe"}
_BLOCK = frozenset(
    {
        "p", "div", "br", "li", "ul", "ol", "pre", "blockquote", "section", "article", "main",
        "table", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "dd", "dt", "figure",
    }
)  # fmt: skip
_MIN_FOCUS_CHARS = 200


class _TextParser(HTMLParser):
    def __init__(self, skip: frozenset[str], focus: str | None) -> None:
        super().__init__(convert_charrefs=True)
        self._skip = skip
        self._focus = focus
        self._skip_depth = 0
        self._focus_depth = 0 if focus else 1
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._skip:
            self._skip_depth += 1
        elif tag == self._focus:
            self._focus_depth += 1
        if tag in _BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._skip:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag == self._focus:
            self._focus_depth = max(0, self._focus_depth - 1)
        if tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and self._focus_depth > 0:
            self.parts.append(re.sub(r"\s+", " ", data))


def _run(html: str, skip: frozenset[str], focus: str | None) -> str:
    parser = _TextParser(skip, focus)
    parser.feed(html)
    parser.close()
    lines = (line.strip() for line in "".join(parser.parts).splitlines())
    return "\n".join(line for line in lines if line)


def html_to_text(html: str) -> str:
    """피드 본문 같은 HTML 조각을 평문으로."""
    return _run(html, _ALWAYS_SKIP, None)


def extract_main_text(html: str) -> str:
    """기사 페이지에서 본문을 고른다: <article> → <main> → 문서 전체(메뉴·머리·꼬리 제외)."""
    for focus in ("article", "main"):
        text = _run(html, _PAGE_SKIP, focus)
        if len(text) >= _MIN_FOCUS_CHARS:
            return text
    return _run(html, _PAGE_SKIP, None)
