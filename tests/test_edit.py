from __future__ import annotations

import dataclasses
import json
from typing import Any

import pytest

from ainews import edit
from ainews.context import RunContext
from ainews.fake import FakeLLM
from ainews.judge import untag
from ainews.models import Candidate, Category, Item, ItemType, Judgment, Kind


def _item(
    url: str,
    category: Category = "official",
    kind: Kind = "news",
    summary: str = "요약",
    body: str | None = None,
) -> Item:
    item = Item.new(
        source=url.split("/")[2], category=category, kind=kind, title=url, url=url, summary=summary
    )
    return item.model_copy(update={"body": body})


def _cand(
    key: str, *items: Item, imp: int = 5, prac: int = 3, t: ItemType = "release"
) -> Candidate:
    j = Judgment(
        item_id=items[0].id,
        relevant=True,
        type=t,
        importance=imp,
        practicality=prac,
        event_key=key,
    )
    return Candidate(event_key=key, items=list(items), judgment=j)


def _with(ctx: RunContext, **handlers: Any) -> tuple[RunContext, FakeLLM, list[str]]:
    prompts: list[str] = []

    def wrap(fn: Any) -> Any:
        def h(user: str) -> Any:
            prompts.append(user)
            return fn(user)

        return h

    schemas = {"edit": edit.EditOutput, "knowledge": edit.KnowledgeOutput}
    llm = FakeLLM({schemas[k]: wrap(v) for k, v in handlers.items()})
    return dataclasses.replace(ctx, llm=llm), llm, prompts


def _entry(cand: int, ids: list[int], **kw: Any) -> dict[str, Any]:
    return {
        "candidate": cand,
        "kind": "news",
        "title": f"해설 {cand}",
        "background": "배경",
        "details": ["문단1", "문단2"],
        "how_to_apply": "적용",
        "freshness": "current",
        "source_ids": ids,
        **kw,
    }


def _payload(tag: str, user: str) -> Any:
    return json.loads(untag(tag, user))


def test_edit_empty_skips_llm_but_keeps_cursor_lines(ctx: RunContext) -> None:
    cursor = _item(
        "https://cursor.com/changelog/2-1", kind="changelog", summary="Faster tab. More.\nmore"
    )
    long = _item("https://cursor.com/changelog/2-2", kind="changelog", summary="x" * 500)
    c, llm, _ = _with(ctx, edit=lambda u: {})
    d = edit.edit([], [cursor, long], "", c)
    assert llm.calls == [] and d.items == [] and d.summary == edit.EMPTY_SUMMARY
    first, cut = d.releases
    assert first.one_line == f"{cursor.title} — Faster tab." and first.url == cursor.url
    assert cut.one_line.endswith("…") and len(cut.one_line) < len(long.title) + 210


def test_edit_changelog_overflow_becomes_title_lines(ctx: RunContext) -> None:
    rels = [_item(f"https://example.com/changelog#{n}", kind="changelog") for n in range(63)]
    seen: list[str] = []

    def respond(user: str) -> dict[str, Any]:
        seen.append(user)
        return {"summary": "s", "items": [], "releases": []}

    c, _, _ = _with(ctx, edit=respond)
    d = edit.edit([], rels, "", c)
    assert len(_payload("changelog", seen[0])) == edit.NORMAL.changelog_items
    assert [(r.url, r.one_line) for r in d.releases] == [(i.url, i.title) for i in rels[60:]]


def test_edit_builds_items_and_releases_from_ids(ctx: RunContext) -> None:
    official = _item("https://example.com/news/a?utm_source=x", body="본문" * 10)
    forum = _item("https://news.example.net/item?id=1", category="community")
    tip_src = _item("https://blog.example.org/tip", category="community", kind="blog")
    stale = _item("https://example.com/old")
    rel = _item("https://example.com/changelog#1", kind="changelog")
    rel2 = _item("https://example.com/changelog#2", kind="changelog")
    cands = [
        _cand("a", official, forum),
        _cand("tip", tip_src, imp=2, prac=5, t="tip"),
        _cand("old", stale),
    ]

    def respond(user: str) -> dict[str, Any]:
        cl = _payload("changelog", user)
        assert [d["id"] for d in cl] == [4, 5]  # 후보 문서 0–3 다음 번호
        assert _payload("candidates", user)[0]["documents"][0]["body"].startswith("본문")
        return {
            "summary": " 오늘 요약 ",
            "items": [
                _entry(
                    1,
                    [2],
                    kind="tip",
                    steps=["s"],
                    applies_to="tool 1.0",
                    code_blocks=[{"lang": "sh", "code": "x"}],
                ),
                _entry(0, [1, 0, 0, 99]),  # 공식이 앞으로, 중복·범위 밖 제거
                _entry(0, [0]),  # 같은 후보 중복은 버린다
                _entry(2, [3], freshness="outdated"),
                _entry(7, [0]),  # 없는 후보
            ],
            "releases": [
                {"id": 4, "tool": "T", "version": "1.2", "one_line": "한 줄"},
                {"id": 4, "tool": "T", "one_line": "중복"},
                {"id": 0, "tool": "X", "one_line": "해설에 쓴 문서"},
                {"id": 3, "tool": "Old", "one_line": "후보 문서도 한 줄로 갈 수 있다"},
                {"id": 50, "tool": "X", "one_line": "범위 밖"},
            ],
        }

    c, llm, _ = _with(ctx, edit=respond)
    d = edit.edit(cands, [rel, rel2], "메모", c)
    assert llm.calls == [(ctx.settings.model_edit, edit.EditOutput)]
    assert d.summary == "오늘 요약"
    assert [i.event_key for i in d.items] == ["a", "tip"]  # 중요도+적용성 순
    a, tip = d.items
    assert [s.url for s in a.sources] == [official.url, forum.url]
    assert a.sources[0].url == "https://example.com/news/a"  # Item.url(utm 제거)을 쓴다
    assert a.importance == 5 and a.steps == [] and a.applies_to is None
    assert tip.kind == "tip" and tip.applies_to == "tool 1.0" and tip.code_blocks[0].code == "x"
    assert [(r.url, r.version, r.event_key) for r in d.releases] == [
        (rel.url, "1.2", None),
        (stale.url, None, None),  # 후보 문서 줄도 사건 키를 달지 않는다(다음 해설을 막지 않게)
    ]


def test_edit_tip_without_required_fields_becomes_news(ctx: RunContext) -> None:
    src = _item("https://blog.example.org/t", category="community")
    c, _, _ = _with(
        ctx, edit=lambda u: {"summary": "s", "items": [_entry(0, [0], kind="tip", steps=["s"])]}
    )
    [item] = edit.edit([_cand("t", src, t="tip")], [], "", c).items
    assert item.kind == "news" and item.steps == []


def test_edit_caps_items(ctx: RunContext) -> None:
    cands = [_cand(f"e{n}", _item(f"https://example.com/{n}"), prac=n % 5 + 1) for n in range(7)]
    settings = dataclasses.replace(ctx.settings, publish_max_items=2)
    c, _, _ = _with(
        dataclasses.replace(ctx, settings=settings),
        edit=lambda u: {"summary": "s", "items": [_entry(n, [n]) for n in range(7)]},
    )
    items = edit.edit(cands, [], "", c).items
    assert len(items) == 2


def test_restricted_sources_send_no_content(ctx: RunContext) -> None:
    anth = _item("https://www.anthropic.com/news/new-model", summary="금지", body="금지")
    hn = _item("https://news.example.net/item?id=9", category="community", summary="토론 요약")
    cursor = _item("https://cursor.com/changelog/x", kind="changelog", summary="Cursor text")
    seen: list[str] = []

    def respond(user: str) -> dict[str, Any]:
        seen.append(user)
        return {"summary": "s", "items": [_entry(0, [0, 1])], "releases": []}

    c, _, _ = _with(ctx, edit=respond)
    d = edit.edit([_cand("m", anth, hn)], [cursor], "", c)
    docs = _payload("candidates", seen[0])[0]["documents"]
    assert docs[0] == {
        "id": 0,
        "source": anth.source,
        "category": "official",
        "title": anth.title,
        "url": anth.url,
        "restricted": True,
    }
    assert docs[1]["summary"] == "토론 요약"
    assert "Cursor text" not in seen[0] and "cursor.com" not in seen[0]
    assert [s.url for s in d.items[0].sources] == [anth.url, hn.url]  # 링크로는 둘 수 있다
    assert [r.url for r in d.releases] == [cursor.url]  # 원문 한 줄은 코드가 싣는다


def test_edit_reduces_input_near_budget(ctx: RunContext) -> None:
    ctx.cost.run_cost = ctx.cost.budget_usd * edit.REDUCE_AT
    cands = [_cand(f"e{n}", _item(f"https://example.com/{n}", body="b" * 20000)) for n in range(7)]
    seen: list[str] = []

    def respond(user: str) -> dict[str, Any]:
        seen.append(user)
        return {"summary": "s", "items": []}

    c, _, _ = _with(ctx, edit=respond)
    edit.edit(cands, [], "", c)
    payload = _payload("candidates", seen[0])
    assert len(payload) == ctx.settings.publish_max_items
    assert len(payload[0]["documents"][0]["body"]) == edit.REDUCED.body_chars


@pytest.mark.parametrize(
    ("out", "expected"),
    [
        ({"markdown": "메모", "changes": [], "source_ids": []}, None),  # 변경 없음
        ({"markdown": "새 메모", "changes": ["c"], "source_ids": [9]}, None),  # 근거 없음
        ({"markdown": " ", "changes": ["c"], "source_ids": [0]}, None),  # 빈 메모로 덮지 않는다
        (
            {"markdown": "새 메모", "changes": ["c"], "source_ids": [0, 0]},
            ["https://example.com/c"],
        ),
    ],
)
def test_update_knowledge(ctx: RunContext, out: dict[str, Any], expected: list[str] | None) -> None:
    docs = [_item("https://example.com/c", kind="changelog"), _item("https://example.com/blog")]
    c, _, prompts = _with(ctx, knowledge=lambda u: out)
    result = edit.update_knowledge("메모", docs[::-1], c)
    assert [d["url"] for d in _payload("official", prompts[0])] == [docs[0].url, docs[1].url]
    if expected is None:
        assert result is None
    else:
        assert result is not None
        assert result.markdown == "새 메모\n" and result.source_urls == expected


def test_update_knowledge_without_official_items(ctx: RunContext) -> None:
    c, llm, _ = _with(ctx, knowledge=lambda u: {})
    assert edit.update_knowledge("메모", [], c) is None and llm.calls == []


def test_estimates_count_what_is_sent(ctx: RunContext) -> None:
    def cands(body: str) -> list[Candidate]:
        return [_cand("e", _item("https://example.com/a", body=body))]

    assert edit.estimate_edit([], [], "메모", ctx) == 0.0  # LLM을 부르지 않는다
    capped = edit.estimate_edit(cands("b" * 20000), [], "", ctx)
    assert capped == edit.estimate_edit(cands("b" * edit.NORMAL.body_chars), [], "", ctx)
    assert capped > edit.estimate_edit(cands("b"), [], "", ctx)

    assert edit.estimate_knowledge("메모", [], ctx) == 0.0
    official = [_item("https://example.com/c", kind="changelog")]
    assert edit.estimate_knowledge("메모", official, ctx) > 0
    _, system, _ = edit._knowledge_input("메모", official, ctx)
    assert f"{edit.KNOWLEDGE_MAX_LINES}줄 이하" in system and "{max_lines}" not in system
