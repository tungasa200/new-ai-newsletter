"""선별·판단(SEL·DUP). 담당: AN-backend3. 지금은 stub이다(Claude를 부르지 않는다)."""

from __future__ import annotations

from ainews.context import RunContext
from ainews.models import Candidate, Item, Judgment
from ainews.state import SentIndex

MAX_CANDIDATES = 25


def classify(items: list[Item], ctx: RunContext) -> list[Judgment]:
    """1단계(Haiku): 항목마다 관련성·유형·중요도·적용성·사건 키를 매긴다."""
    return [
        Judgment(
            item_id=i.id,
            relevant=True,
            type="tip"
            if i.category == "community"
            else "release"
            if i.is_changelog
            else "industry",
            importance=3,
            practicality=3,
            event_key=i.id,
        )
        for i in items
    ]


def select(
    items: list[Item], judgments: list[Judgment], sent: SentIndex, ctx: RunContext
) -> list[Candidate]:
    """사건 단위로 묶고 이미 보낸 사건을 빼고 우선순위 순으로 후보를 고른다.

    결과는 우선순위가 높은 순이다. 비용 상한에 걸리면 pipeline이 뒤에서부터 줄인다.
    """
    by_id = {i.id: i for i in items}
    groups: dict[str, list[Judgment]] = {}
    for j in judgments:
        if j.relevant and j.item_id in by_id and not sent.has_event(j.event_key):
            groups.setdefault(j.event_key, []).append(j)
    candidates = []
    for key, js in groups.items():
        group_items = sorted((by_id[j.item_id] for j in js), key=lambda i: i.category != "official")
        best = max(js, key=lambda j: j.importance + j.practicality)
        candidates.append(Candidate(event_key=key, items=group_items, judgment=best))
    candidates.sort(key=lambda c: c.judgment.importance + c.judgment.practicality, reverse=True)
    return candidates[:MAX_CANDIDATES]
