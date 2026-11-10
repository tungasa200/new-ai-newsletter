from __future__ import annotations

import datetime as dt

from ainews.models import CodeBlock, DeepItem, Digest, ReleaseLine, SourceRef
from ainews.render import EMPTY_ITEMS, render

DAY = dt.date(2026, 11, 11)


def _deep(**kw: object) -> DeepItem:
    fields: dict[str, object] = {
        "kind": "news",
        "title": "새 모델 <출시>",
        "background": "배경 문단",
        "details": ["핵심 1", "핵심 2"],
        "how_to_apply": "적용 문단",
        "caveats": ["주의 1"],
        "freshness": "current",
        "sources": [
            SourceRef(title="공식 글", url="https://example.com/a?x=1&y=2", kind="official"),
            SourceRef(title="토론", url="https://example.net/t", kind="community"),
        ],
        "event_key": "e",
        "importance": 5,
        "practicality": 3,
    }
    fields.update(kw)
    return DeepItem.model_validate(fields)


def test_render_deep_items_and_releases() -> None:
    tip = _deep(
        kind="tip",
        title="설정 팁",
        freshness="caution",
        is_update=True,
        steps=["단계 A"],
        code_blocks=[CodeBlock(lang="json", code='{"a": "<b>"}')],
        applies_to="tool 2.x",
    )
    digest = Digest(
        date=DAY,
        summary="오늘 요약",
        items=[_deep(), tip],
        releases=[
            ReleaseLine(tool="CLI", version="v1.2", one_line="한 줄", url="https://e.com/r"),
            ReleaseLine(tool="Cursor", one_line="Faster tab", url="https://cursor.com/changelog/x"),
        ],
    )
    mail = render(digest)
    assert mail.subject == "AI Daily — 2026.11.11 (핵심 2건)"

    html = mail.html
    assert "<link" not in html and "<style" not in html and "<script" not in html
    assert "새 모델 &lt;출시&gt;" in html  # 자동 이스케이프
    assert 'href="https://example.com/a?x=1&amp;y=2"' in html
    for text in (
        "오늘의 핵심",
        "배경 문단",
        "핵심 2",
        "주의 1",
        "실무팁",
        "업데이트",
        "신선도: 주의",
    ):
        assert text in html
    assert "적용 대상:</strong> tool 2.x" in html and "단계 A" in html
    assert "{&#34;a&#34;: &#34;&lt;b&gt;&#34;}" in html
    assert ">CLI v1.2</strong>: 한 줄" in html and ">Cursor</strong>: Faster tab" in html
    assert EMPTY_ITEMS not in html

    text = mail.text
    assert "[1] 새 모델 <출시>" in text and "[2] 설정 팁" in text
    assert "(실무팁 · 업데이트 · 신선도: 주의)" in text
    assert '```json\n{"a": "<b>"}\n```' in text
    assert "- 공식 글 (공식) https://example.com/a?x=1&y=2" in text
    assert "- CLI v1.2: 한 줄\n  https://e.com/r" in text


def test_render_empty_day() -> None:
    mail = render(Digest(date=DAY, summary="새 소식 없음"))
    assert mail.subject == "AI Daily — 2026.11.11"
    assert EMPTY_ITEMS in mail.html and EMPTY_ITEMS in mail.text
    assert "도구·릴리스 변경" not in mail.html and "도구·릴리스 변경" not in mail.text
