from __future__ import annotations

import dataclasses
import json
from typing import Any

from ainews import judge
from ainews.context import RunContext
from ainews.fake import FakeLLM
from ainews.models import Category, Item, Judgment, Kind
from ainews.state import SentEntry, SentFile, SentIndex


def _item(n: int, category: Category = "official", kind: Kind = "news") -> Item:
    return Item.new(
        source=f"src{n}",
        category=category,
        kind=kind,
        title=f"title {n}",
        url=f"https://example.com/{n}",
        summary="x" * 2000,
    )


def _with(ctx: RunContext, handler: Any) -> tuple[RunContext, FakeLLM]:
    llm = FakeLLM({judge.ClassifyOutput: handler})
    return dataclasses.replace(ctx, llm=llm), llm


def _j(item: Item, imp: int, prac: int, key: str, relevant: bool = True) -> Judgment:
    return Judgment(
        item_id=item.id,
        relevant=relevant,
        type="release",
        importance=imp,
        practicality=prac,
        event_key=key,
    )


def test_classify_empty_skips_llm(ctx: RunContext) -> None:
    c, llm = _with(ctx, lambda u: {"judgments": []})
    assert judge.classify([], c) == []
    assert llm.calls == []


def test_classify_batches_and_carries_event_keys(ctx: RunContext) -> None:
    items = [_item(n) for n in range(judge.BATCH_SIZE + 3)]
    prompts: list[str] = []

    def handler(user: str) -> dict[str, Any]:
        prompts.append(user)
        rows = json.loads(judge.untag("items", user))
        assert all(len(r["summary"]) == judge.SUMMARY_CHARS for r in rows)
        out = [
            {
                "idx": r["idx"],
                "relevant": True,
                "type": "release",
                "importance": 3,
                "practicality": 3,
                "event_key": " Same-Event ",
            }
            for r in rows
        ]
        # 범위 밖·중복 idx는 버린다
        out += [{**out[0], "idx": 999}, {**out[0], "event_key": "dup"}]
        return {"judgments": out}

    c, llm = _with(ctx, handler)
    result = judge.classify(items, c)
    assert len(llm.calls) == 2 and llm.calls[0][0] == ctx.settings.model_classify
    assert [j.item_id for j in result] == [i.id for i in items]
    assert {j.event_key for j in result} == {"same-event"}
    assert '"same-event"' in prompts[1]  # 앞 묶음의 사건 키를 넘긴다


def test_classify_missing_items_are_dropped(ctx: RunContext) -> None:
    items = [_item(0), _item(1)]
    c, _ = _with(
        ctx,
        lambda u: {
            "judgments": [
                {
                    "idx": 1,
                    "relevant": False,
                    "type": "industry",
                    "importance": 1,
                    "practicality": 1,
                    "event_key": "",
                }
            ]
        },
    )
    [j] = judge.classify(items, c)
    assert j.item_id == items[1].id and j.event_key == items[1].id  # 빈 키는 항목 id로


def test_select_applies_criteria_groups_and_orders(ctx: RunContext) -> None:
    official, forum, blog, low, off, sent_item = (
        _item(0),
        _item(1, "community", "forum"),
        _item(2, "community", "blog"),
        _item(3),
        _item(4),
        _item(5),
    )
    items = [forum, official, blog, low, off, sent_item]
    judgments = [
        _j(forum, 3, 2, "model"),  # 같은 사건: 커뮤니티가 먼저 들어와도 공식이 대표
        _j(official, 5, 3, "model"),
        _j(blog, 2, 4, "tip"),
        _j(low, 4, 3, "low"),  # 기준 미달
        _j(off, 5, 5, "off", relevant=False),
        _j(sent_item, 5, 5, "sent"),
    ]
    sent = SentIndex(
        SentFile(
            entries=[
                SentEntry(url="u", event_key="sent", title_hash="h", sent_on=ctx.today),
            ]
        )
    )
    result = judge.select(items, judgments, sent, ctx)
    assert [c.event_key for c in result] == ["model", "tip"]
    assert [i.id for i in result[0].items] == [official.id, forum.id]
    assert result[0].judgment.importance == 5


def test_select_zero_and_cap(ctx: RunContext) -> None:
    items = [_item(n) for n in range(10)]
    weak = [_j(i, 3, 3, f"e{n}") for n, i in enumerate(items)]
    assert judge.select(items, weak, SentIndex(SentFile()), ctx) == []

    strong = [_j(i, 5, 1 + n % 5, f"e{n}") for n, i in enumerate(items)]
    c = dataclasses.replace(ctx, settings=dataclasses.replace(ctx.settings, publish_max_items=3))
    result = judge.select(items, strong, SentIndex(SentFile()), c)
    assert len(result) == 3 + judge.ENRICH_MARGIN
    assert [c.judgment.practicality for c in result][:2] == [5, 5]


def _row(idx: int, type_: str = "release", importance: int = 3) -> dict[str, Any]:
    return {
        "idx": idx,
        "relevant": True,
        "type": type_,
        "importance": importance,
        "practicality": 3,
        "event_key": f"k{idx}",
    }


def test_classify_bad_rows_do_not_fail_the_batch(ctx: RunContext) -> None:
    """Actions 첫 실행: Haiku가 type='community'를 내 묶음 전체가 실패했다."""
    items = [_item(0, "community", "forum"), _item(1), _item(2)]
    replies = [
        {"judgments": [_row(0, "community"), _row(1), _row(2, importance=9)]},
        {"judgments": [_row(0, importance=9)]},  # 다시 물어도 틀리면 버린다
    ]
    c, llm = _with(ctx, lambda u: replies.pop(0))
    result = judge.classify(items, c)
    assert [(j.item_id, j.type) for j in result] == [
        (items[0].id, judge.FALLBACK_TYPE),  # 목록 밖 유형은 중립 값으로
        (items[1].id, "release"),
    ]
    assert len(llm.calls) == 2


def test_classify_re_asks_only_missing_items(ctx: RunContext) -> None:
    items = [_item(n) for n in range(3)]
    asked: list[list[str]] = []

    def handler(user: str) -> dict[str, Any]:
        rows = json.loads(judge.untag("items", user))
        asked.append([r["title"] for r in rows])
        return {"judgments": [_row(0)] if len(asked) == 1 else [_row(r["idx"]) for r in rows]}

    c, _ = _with(ctx, handler)
    result = judge.classify(items, c)
    assert asked == [["title 0", "title 1", "title 2"], ["title 1", "title 2"]]
    assert [j.item_id for j in result] == [i.id for i in items]
