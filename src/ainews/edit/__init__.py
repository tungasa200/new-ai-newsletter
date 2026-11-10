"""편집·신선도(EDT) + 권장 사항 메모 갱신. 담당: AN-backend3. 지금은 stub이다."""

from __future__ import annotations

from ainews.context import RunContext
from ainews.models import (
    Candidate,
    DeepItem,
    Digest,
    Item,
    KnowledgeUpdate,
    ReleaseLine,
    SourceRef,
)


def edit(
    candidates: list[Candidate], changelog_items: list[Item], knowledge: str, ctx: RunContext
) -> Digest:
    """3단계(Sonnet): 신선도 판정, 최종 선정, 한국어 해설형 편집(D-13)."""
    items: list[DeepItem] = []
    releases: list[ReleaseLine] = []
    for c in candidates:
        lead = c.items[0]
        if c.judgment.type == "release":
            releases.append(
                ReleaseLine(
                    tool=lead.source, one_line=lead.title, url=lead.url, event_key=c.event_key
                )
            )
            continue
        is_tip = c.judgment.type == "tip"
        items.append(
            DeepItem(
                kind="tip" if is_tip else "news",
                title=lead.title,
                background=lead.summary[:300],
                details=["(stub)"],
                how_to_apply="(stub)",
                freshness="current",
                sources=[SourceRef(title=i.title, url=i.url, kind=i.category) for i in c.items],
                event_key=c.event_key,
                importance=c.judgment.importance,
                practicality=c.judgment.practicality,
                is_update=c.is_update,
                steps=["(stub)"] if is_tip else [],
                applies_to="(stub)" if is_tip else None,
            )
        )
    return Digest(
        date=ctx.today,
        summary="(stub) 편집 단계가 아직 구현되지 않았다.",
        items=items,
        releases=releases,
    )


def update_knowledge(
    knowledge: str, official_items: list[Item], ctx: RunContext
) -> KnowledgeUpdate | None:
    """4단계(Sonnet): 그날 공식 소스에서 확인한 변경만 반영한 current.md 갱신안. 없으면 None."""
    return None
