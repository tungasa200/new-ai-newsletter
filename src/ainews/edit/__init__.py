"""편집·신선도(EDT) + 권장 사항 메모 갱신. 담당: AN-backend3.

- edit(3단계, Sonnet): 신선도 판정, 최종 선정(상한 publish_max_items), 한국어 해설형 편집(D-13).
- update_knowledge(4단계, Sonnet): 그날 공식 소스로 확인한 변경만 knowledge/current.md에 반영(D-09).
- 링크는 LLM이 쓰지 않는다. 문서 번호(id)만 받아 코드가 Item.url로 바꾼다(FR-EDT-05).
- 약관 조건 소스(D-12)는 요약·본문을 LLM에 넘기지 않는다. Cursor는 RSS 원문 한 줄을 코드가 싣는다.
- 하루 누적 비용이 상한의 80%에 이르면 입력을 줄인다(NFR-01, D-07). 상한 초과 예상 시
  메모 갱신 건너뛰기는 pipeline이 한다.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from importlib.resources import files
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from ainews.context import RunContext
from ainews.judge import rank, tagged, untag
from ainews.models import (
    Candidate,
    CodeBlock,
    DeepItem,
    Digest,
    Freshness,
    Item,
    KnowledgeUpdate,
    ReleaseLine,
    SourceRef,
)
from ainews.util.cost import CHARS_PER_TOKEN_ESTIMATE, CostTracker

EDIT_MAX_TOKENS = 16000  # 해설 5건 × 약 1,800 + 한 줄 목록·요약(D-13 추정 12k) + 여유
KNOWLEDGE_MAX_TOKENS = 6000  # 메모 전문(최대 KNOWLEDGE_MAX_LINES줄)을 다시 낸다
KNOWLEDGE_MAX_LINES = 150  # 넘으면 출력이 잘린다. 프롬프트로 이 안에 두게 한다
REDUCE_AT = 0.8  # 하루 상한 대비 누적 비용 비율. 넘으면 입력 축소(D-07)
EMPTY_SUMMARY = "오늘은 게재 기준을 넘은 새 소식이 없습니다."


@dataclass(frozen=True)
class _Limits:
    body_chars: int  # 후보 문서 본문 발췌(FR-SEL-08: 문서당 12,000자)
    changelog_items: int
    changelog_chars: int
    official_items: int  # 메모 갱신 입력
    official_chars: int


NORMAL = _Limits(
    body_chars=12000,
    changelog_items=60,
    changelog_chars=400,
    official_items=40,
    official_chars=500,
)
REDUCED = _Limits(
    body_chars=4000, changelog_items=20, changelog_chars=200, official_items=15, official_chars=200
)


def _limits(ctx: RunContext) -> _Limits:
    return REDUCED if ctx.cost.spent >= ctx.cost.budget_usd * REDUCE_AT else NORMAL


def _prompt(name: str) -> str:
    return files("ainews.edit").joinpath("prompts", name).read_text(encoding="utf-8")


# --- 약관 조건 소스(D-12) ---


def _host_path(item: Item) -> tuple[str, str]:
    parts = urlsplit(item.key)  # key는 www를 뺀 정규화 URL
    return parts.hostname or "", parts.path


def is_verbatim_only(item: Item) -> bool:
    """Cursor changelog: RSS 제목·한 줄 요약·링크를 영어 원문 그대로만 싣는다."""
    return _host_path(item)[0] == "cursor.com"


def is_restricted(item: Item) -> bool:
    """원문 내용을 해설 근거로 쓸 수 없는 소스(Anthropic News·Engineering, Cursor)."""
    host, path = _host_path(item)
    return is_verbatim_only(item) or (
        host == "anthropic.com" and path.startswith(("/news/", "/engineering/"))
    )


# --- 3단계 편집 ---


class _EditedItem(BaseModel):
    candidate: int
    kind: Literal["news", "tip"]
    title: str
    background: str
    details: list[str]
    how_to_apply: str
    caveats: list[str] = Field(default_factory=list)
    freshness: Freshness
    source_ids: list[int]
    steps: list[str] = Field(default_factory=list)
    code_blocks: list[CodeBlock] = Field(default_factory=list)
    applies_to: str | None = None


class _ReleaseOut(BaseModel):
    id: int
    tool: str
    version: str | None = None
    one_line: str


class EditOutput(BaseModel):
    """edit의 tool 입력 스키마."""

    summary: str
    items: list[_EditedItem]
    releases: list[_ReleaseOut] = Field(default_factory=list)


def _doc(doc_id: int, item: Item, text_chars: int, with_body: bool) -> dict[str, Any]:
    d: dict[str, Any] = {
        "id": doc_id,
        "source": item.source,
        "category": item.category,
        "title": item.title,
        "url": item.url,
    }
    if item.published_at:
        d["published"] = item.published_at.date().isoformat()
    if is_restricted(item):
        d["restricted"] = True  # 요약·본문을 넘기지 않는다(D-12)
        return d
    d["summary"] = item.summary[:text_chars]
    if with_body and item.body:
        d["body"] = item.body[:text_chars]
    return d


def _pick(docs: list[Item], ids: list[int]) -> list[Item]:
    """LLM이 고른 문서 번호를 문서로 바꾼다. 중복·범위 밖 번호는 버린다."""
    return [docs[i] for i in dict.fromkeys(ids) if 0 <= i < len(docs)]


VERBATIM_CHARS = 200  # D-12 '한 줄 요약': 원문 첫 문장, 이 길이를 넘으면 자른다


def _verbatim_line(item: Item) -> ReleaseLine:
    first = item.summary.strip().splitlines()[0].strip() if item.summary.strip() else ""
    first = re.split(r"(?<=[.!?])\s", first, maxsplit=1)[0]
    if len(first) > VERBATIM_CHARS:
        first = first[: VERBATIM_CHARS - 1].rstrip() + "…"
    one_line = f"{item.title} — {first}" if first and first != item.title else item.title
    return ReleaseLine(tool=item.source, one_line=one_line, url=item.url)


@dataclass
class _EditInput:
    candidates: list[Candidate]  # 축소 모드면 잘린 목록
    docs: list[Item]  # 문서 번호표(id = 위치)
    extra: list[ReleaseLine]  # LLM을 거치지 않고 코드가 싣는 한 줄
    system: str
    user: str | None  # None이면 LLM을 부르지 않는다(후보·changelog 없음)


def _edit_input(
    candidates: list[Candidate], changelog_items: list[Item], knowledge: str, ctx: RunContext
) -> _EditInput:
    max_items = ctx.settings.publish_max_items
    lim = _limits(ctx)
    if lim is REDUCED:
        candidates = candidates[:max_items]

    # 문서 번호표: 후보 문서 먼저, 그다음 후보에 없는 changelog 문서
    docs: list[Item] = []
    cand_payload = []
    for n, c in enumerate(candidates):
        entries = []
        for item in c.items:
            entries.append(_doc(len(docs), item, lim.body_chars, with_body=True))
            docs.append(item)
        cand_payload.append(
            {
                "candidate": n,
                "type": c.judgment.type,
                "importance": c.judgment.importance,
                "practicality": c.judgment.practicality,
                "is_update": c.is_update,
                "documents": entries,
            }
        )
    in_candidates = {i.key for i in docs}
    verbatim = [i for i in changelog_items if is_verbatim_only(i)]  # 해설 출처가 아니면 한 줄로
    changelog = [
        i for i in changelog_items if not is_verbatim_only(i) and i.key not in in_candidates
    ]
    # FR-EDT-02 ③은 건수 제한이 없다. LLM에 못 넘긴 문서는 코드가 제목 한 줄로 싣는다.
    changelog, overflow = changelog[: lim.changelog_items], changelog[lim.changelog_items :]
    extra = [_verbatim_line(i) for i in verbatim] + [
        ReleaseLine(tool=i.source, one_line=i.title, url=i.url) for i in overflow
    ]
    cl_payload = []
    for item in changelog:
        cl_payload.append(_doc(len(docs), item, lim.changelog_chars, with_body=False))
        docs.append(item)

    user = None
    if candidates or changelog:
        user = "\n".join(
            [
                tagged("knowledge", knowledge or "(비어 있음)"),
                tagged("changelog", cl_payload),
                tagged("candidates", cand_payload),
            ]
        )
    system = _prompt("edit_system.md").replace("{max_items}", str(max_items))
    return _EditInput(candidates, docs, extra, system, user)


def estimate_edit(
    candidates: list[Candidate], changelog_items: list[Item], knowledge: str, ctx: RunContext
) -> float:
    """edit이 실제로 보낼 입력으로 잰 예상 비용(pipeline의 후보 줄이기, D-07)."""
    inp = _edit_input(candidates, changelog_items, knowledge, ctx)
    return _estimate(inp.system, inp.user, EDIT_MAX_TOKENS, ctx)


def _estimate(system: str, user: str | None, max_tokens: int, ctx: RunContext) -> float:
    if user is None:
        return 0.0
    tokens = (len(system) + len(user)) // CHARS_PER_TOKEN_ESTIMATE  # ClaudeLLM과 같은 셈
    return CostTracker.estimate(ctx.settings.model_edit, tokens, max_tokens)


def edit(
    candidates: list[Candidate], changelog_items: list[Item], knowledge: str, ctx: RunContext
) -> Digest:
    """3단계(Sonnet): 신선도 판정, 최종 선정, 한국어 해설형 편집(D-13).

    candidates는 우선순위 순(judge.select). 해설은 최대 publish_max_items건.
    """
    if _limits(ctx) is REDUCED:
        ctx.log.warning(
            "누적 비용 $%.4f가 상한의 %d%% 이상이라 입력을 줄인다",
            ctx.cost.spent,
            int(REDUCE_AT * 100),
        )
    inp = _edit_input(candidates, changelog_items, knowledge, ctx)
    if inp.user is None:
        return Digest(date=ctx.today, summary=EMPTY_SUMMARY, releases=inp.extra)

    out = ctx.llm.structured(
        model=ctx.settings.model_edit,
        system=inp.system,
        user=inp.user,
        schema=EditOutput,
        max_tokens=EDIT_MAX_TOKENS,
    )
    items = _build_items(out, inp.candidates, inp.docs, ctx)[: ctx.settings.publish_max_items]
    used = {s.url for i in items for s in i.sources}
    releases = _build_releases(out, inp.docs, used)
    releases += [r for r in inp.extra if r.url not in used]
    return Digest(
        date=ctx.today,
        summary=out.summary.strip() or EMPTY_SUMMARY,
        items=items,
        releases=releases,
    )


def _build_items(
    out: EditOutput, candidates: list[Candidate], docs: list[Item], ctx: RunContext
) -> list[DeepItem]:
    items: list[DeepItem] = []
    done: set[int] = set()
    for e in out.items:
        if not 0 <= e.candidate < len(candidates) or e.candidate in done:
            continue
        done.add(e.candidate)
        c = candidates[e.candidate]
        if e.freshness == "outdated":
            ctx.log.info("구식 판정으로 제외: %s", c.event_key)
            continue
        refs = _pick(docs, e.source_ids) or list(c.items)
        refs.sort(key=lambda i: i.category != "official")
        kind = e.kind
        if kind == "tip" and not (e.steps and e.applies_to):  # FR-EDT-04 필수 칸이 비었다
            ctx.log.warning(
                "실무팁 필수 칸(steps·applies_to)이 비어 news로 싣는다: %s", c.event_key
            )
            kind = "news"
        is_tip = kind == "tip"
        items.append(
            DeepItem(
                kind=kind,
                title=e.title,
                background=e.background,
                details=e.details,
                how_to_apply=e.how_to_apply,
                caveats=e.caveats,
                freshness=e.freshness,
                sources=[SourceRef(title=i.title, url=i.url, kind=i.category) for i in refs],
                event_key=c.event_key,
                importance=c.judgment.importance,
                practicality=c.judgment.practicality,
                is_update=c.is_update,
                steps=e.steps if is_tip else [],
                code_blocks=e.code_blocks if is_tip else [],
                applies_to=e.applies_to if is_tip else None,
            )
        )
    items.sort(key=rank, reverse=True)
    return items


def _build_releases(out: EditOutput, docs: list[Item], used: set[str]) -> list[ReleaseLine]:
    # 후보 문서에서 나온 줄도 event_key를 달지 않는다. 달면 sent에 사건이 기록되어
    # 나중에 같은 사건을 해설하지 못한다. 같은 문서의 재게재는 url로 막힌다.
    lines: list[ReleaseLine] = []
    seen = set(used)
    for r in out.releases:
        if not 0 <= r.id < len(docs):
            continue
        item = docs[r.id]
        if item.url in seen or is_verbatim_only(item):
            continue
        seen.add(item.url)
        lines.append(
            ReleaseLine(
                tool=r.tool,
                version=r.version,
                one_line=r.one_line,
                url=item.url,
            )
        )
    return lines


# --- 4단계 메모 갱신 ---


class KnowledgeOutput(BaseModel):
    """update_knowledge의 tool 입력 스키마."""

    markdown: str
    changes: list[str] = Field(default_factory=list)
    source_ids: list[int] = Field(default_factory=list)


def _knowledge_input(
    knowledge: str, official_items: list[Item], ctx: RunContext
) -> tuple[list[Item], str, str | None]:
    lim = _limits(ctx)
    # changelog·릴리스를 먼저 넣는다(축소 시 블로그부터 빠진다)
    docs = sorted(official_items, key=lambda i: not i.is_changelog)[: lim.official_items]
    system = _prompt("knowledge_system.md").replace("{max_lines}", str(KNOWLEDGE_MAX_LINES))
    if not docs:
        return docs, system, None
    payload = [_doc(n, i, lim.official_chars, with_body=False) for n, i in enumerate(docs)]
    user = "\n".join([tagged("knowledge", knowledge or "(비어 있음)"), tagged("official", payload)])
    return docs, system, user


def estimate_knowledge(knowledge: str, official_items: list[Item], ctx: RunContext) -> float:
    """update_knowledge가 실제로 보낼 입력으로 잰 예상 비용. 부를 일이 없으면 0."""
    _, system, user = _knowledge_input(knowledge, official_items, ctx)
    return _estimate(system, user, KNOWLEDGE_MAX_TOKENS, ctx)


def update_knowledge(
    knowledge: str, official_items: list[Item], ctx: RunContext
) -> KnowledgeUpdate | None:
    """4단계(Sonnet): 그날 공식 소스에서 확인한 변경만 반영한 current.md 갱신안. 없으면 None."""
    docs, system, user = _knowledge_input(knowledge, official_items, ctx)
    if user is None:
        return None
    if knowledge.count("\n") > KNOWLEDGE_MAX_LINES:
        ctx.log.warning("권장 사항 메모가 %d줄을 넘었다(줄이도록 지시함)", KNOWLEDGE_MAX_LINES)
    out = ctx.llm.structured(
        model=ctx.settings.model_edit,
        system=system,
        user=user,
        schema=KnowledgeOutput,
        max_tokens=KNOWLEDGE_MAX_TOKENS,
    )
    markdown = out.markdown.strip()
    if not out.changes or not markdown or markdown == knowledge.strip():
        return None
    urls = [d.url for d in _pick(docs, out.source_ids)]
    if not urls:  # D-09: 공식 소스 근거가 없는 갱신은 받지 않는다
        ctx.log.warning("근거 문서 없는 메모 갱신안을 버린다: %s", out.changes)
        return None
    return KnowledgeUpdate(markdown=markdown + "\n", changes=out.changes, source_urls=urls)


# --- --offline·테스트용 가짜 응답(FakeLLM handler) ---


def fake_edit(user: str) -> dict[str, Any]:
    """후보마다 해설 한 건, changelog 문서마다 한 줄을 만든다."""
    cands = json.loads(untag("candidates", user) or "[]")
    changelog = json.loads(untag("changelog", user) or "[]")
    items = []
    for c in cands:
        doc = c["documents"][0]
        tip = c["type"] == "tip"
        items.append(
            {
                "candidate": c["candidate"],
                "kind": "tip" if tip else "news",
                "title": f"(가짜 해설) {doc['title']}",
                "background": "가짜 배경 문단.",
                "details": ["가짜 핵심 문단 1.", "가짜 핵심 문단 2."],
                "how_to_apply": "가짜 실무 적용 문단.",
                "caveats": ["가짜 주의점."],
                "freshness": "current",
                "source_ids": [d["id"] for d in c["documents"]],
                "steps": ["가짜 단계 1"] if tip else [],
                "code_blocks": [{"lang": "bash", "code": "echo fake"}] if tip else [],
                "applies_to": "fake-cli 1.x" if tip else None,
            }
        )
    releases = [
        {"id": d["id"], "tool": d["source"], "version": None, "one_line": f"(가짜) {d['title']}"}
        for d in changelog
    ]
    return {"summary": f"(가짜 요약) 해설 {len(items)}건.", "items": items, "releases": releases}


def fake_knowledge(user: str) -> dict[str, Any]:
    """바꿀 것이 없다고 답한다."""
    return {"markdown": untag("knowledge", user), "changes": [], "source_ids": []}
