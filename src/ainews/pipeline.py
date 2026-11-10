"""실행 순서(orchestrator). collect → classify → select → enrich → edit → render → send."""

from __future__ import annotations

import datetime as dt
import difflib
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal

from ainews import collect as collect_mod
from ainews import edit as edit_mod
from ainews import judge as judge_mod
from ainews import render as render_mod
from ainews import state as st
from ainews.context import RunContext
from ainews.fake import FakeLLM, fake_collect, fake_enrich_bodies
from ainews.mail import smtp
from ainews.models import (
    Candidate,
    CollectResult,
    Digest,
    Item,
    Judgment,
    KnowledgeUpdate,
    RenderedMail,
)
from ainews.ops import notify, runlog
from ainews.settings import KST, Settings, load_sources
from ainews.util.cost import CHARS_PER_TOKEN_ESTIMATE, CostTracker
from ainews.util.http import Http
from ainews.util.llm import LLM, ClaudeLLM

log = logging.getLogger("ainews")

EDIT_MAX_TOKENS = 8000
KNOWLEDGE_MAX_TOKENS = 4000


@dataclass
class Stages:
    """단계 함수 묶음. 테스트와 --offline에서 일부를 갈아 끼운다."""

    collect: Callable[..., CollectResult] = collect_mod.collect
    enrich_bodies: Callable[..., list[Item]] = collect_mod.enrich_bodies
    classify: Callable[..., list[Judgment]] = judge_mod.classify
    select: Callable[..., list[Candidate]] = judge_mod.select
    edit: Callable[..., Digest] = edit_mod.edit
    update_knowledge: Callable[..., KnowledgeUpdate | None] = edit_mod.update_knowledge
    render: Callable[..., RenderedMail] = render_mod.render
    send: Callable[..., None] = smtp.send


@dataclass
class RunOptions:
    dry_run: bool = False
    offline: bool = False  # 가짜 수집·가짜 LLM. 항상 dry-run으로 돌고 state를 전혀 쓰지 않는다
    force: bool = False

    def __post_init__(self) -> None:
        if self.offline:  # 가짜 다이제스트를 실제로 보내지 않는다
            self.dry_run = True


@dataclass
class RunResult:
    status: Literal["ok", "skipped", "failed"]
    sent: bool = False
    failed_stage: str | None = None
    previews: list[Path] = field(default_factory=list)


def run(
    settings: Settings,
    opts: RunOptions,
    *,
    stages: Stages | None = None,
    llm: LLM | None = None,
    now: dt.datetime | None = None,
) -> RunResult:
    now = now or dt.datetime.now(dt.UTC)
    today = now.astimezone(KST).date()
    stages = stages or Stages()
    if opts.offline:
        stages = replace(stages, collect=fake_collect, enrich_bodies=fake_enrich_bodies)

    store = st.StateStore(settings.state_dir)
    sent = store.load_sent()
    runs = store.load_runs()
    run_id = f"{now:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"
    record = st.RunRecord(run_id=run_id, date=today, started_at=now, dry_run=opts.dry_run)
    # sent.json·메모는 발송 실행만 쓴다. runs.json은 실제 Claude를 부르는 dry-run도 쓴다
    # (그 비용을 하루 상한에 넣기 위해서다). --offline은 아무것도 쓰지 않는다.
    write_runs = not opts.offline
    cost = CostTracker(settings.daily_budget_usd, spent_before=st.spent_on(runs, today))

    if not opts.force and not opts.dry_run and st.sent_today(sent, runs, today):
        log.info("오늘(%s) 이미 발송했다. 종료.", today)
        record.status = "skipped"
        _finish(store, runs, record, cost, settings, write=write_runs)
        return RunResult(status="skipped")

    if llm is None:
        llm = (
            FakeLLM()
            if opts.offline
            else ClaudeLLM(
                api_key=settings.anthropic_api_key,
                cost=cost,
                fallback_model=settings.model_fallback,
            )
        )
    http = Http(settings.user_agent)
    ctx = RunContext(
        settings=settings, http=http, llm=llm, cost=cost, now=now, log=log, offline=opts.offline
    )
    result = RunResult(status="ok")
    stage = "collect"
    try:
        sources = [s for s in load_sources(settings.sources_path) if s.enabled]
        collected = stages.collect(sources, st.since_map(runs, sources, now), ctx)
        record.source_stats = collected.stats
        for s in collected.stats:
            if not s.ok:
                log.warning("소스 실패: %s: %s", s.source, s.error)
        index = st.SentIndex(sent)
        items = [i for i in _dedupe(collected.items) if not index.has_url(i.key)]
        log.info("수집 %d건, 중복·발송 이력 제외 후 %d건", len(collected.items), len(items))

        stage = "classify"
        judgments = stages.classify(items, ctx)
        stage = "select"
        candidates = stages.select(items, judgments, index, ctx)
        stage = "enrich"
        candidates = _enrich(candidates, stages.enrich_bodies(_items_of(candidates), ctx))
        candidates = _fit_budget(candidates, cost, settings.model_edit)

        stage = "edit"
        knowledge = store.load_knowledge()
        digest = stages.edit(candidates, [i for i in items if i.is_changelog], knowledge, ctx)
        if digest.is_empty and not settings.send_empty and not opts.dry_run:
            log.warning("게재할 항목이 없어 발송하지 않는다(FR-MAIL-05).")
            result.status = record.status = "skipped"
            return result

        stage = "render"
        mail = stages.render(digest)
        if opts.dry_run:
            result.previews = _write_previews(settings.out_dir, today, mail.html, mail.text)
            log.info("dry-run: %s", ", ".join(str(p) for p in result.previews))
        else:
            stage = "send"
            stages.send(mail, settings)
            result.sent = record.sent = True
            # 이중 발송 방지: 발송 직후 바로 기록한다(FR-MAIL-04). 이 기록이 실패해도
            # 메일은 이미 나갔으므로 실패로 끝내지 않는다. runs.json의 sent=True가 재발송을 막는다.
            stage = "record"
            try:
                sent = st.record_delivery(sent, digest, run_id=run_id, now=now, day=today)
                store.save_sent(sent)
            except Exception:
                log.exception(
                    "발송 기록(sent.json) 저장 실패. 같은 항목이 다음 날 다시 실릴 수 있다"
                )

        stage = "knowledge"
        update = _update_knowledge(stages, knowledge, items, ctx)
        if update is not None and not opts.dry_run:
            store.save_knowledge(update.markdown)
        return result
    except Exception as exc:
        log.exception("단계 %s에서 실패: %s", stage, type(exc).__name__)
        result.status = record.status = "failed"
        result.failed_stage = record.failed_stage = stage
        record.error = type(exc).__name__
        return result
    finally:
        http.close()
        _finish(store, runs, record, cost, settings, write=write_runs)


def _dedupe(items: list[Item]) -> list[Item]:
    seen: dict[str, Item] = {}
    for i in items:
        seen.setdefault(i.key, i)
    return list(seen.values())


def _items_of(candidates: list[Candidate]) -> list[Item]:
    return list({i.id: i for c in candidates for i in c.items}.values())


def _enrich(candidates: list[Candidate], enriched: list[Item]) -> list[Candidate]:
    by_id = {i.id: i for i in enriched}
    return [
        c.model_copy(update={"items": [by_id.get(i.id, i) for i in c.items]}) for c in candidates
    ]


def _estimate_edit(candidates: list[Candidate], model: str) -> float:
    chars = sum(
        len(i.title) + len(i.summary) + len(i.body or "") for c in candidates for i in c.items
    )
    return CostTracker.estimate(model, chars // CHARS_PER_TOKEN_ESTIMATE, EDIT_MAX_TOKENS)


def _fit_budget(candidates: list[Candidate], cost: CostTracker, model: str) -> list[Candidate]:
    """편집 비용이 상한을 넘을 것 같으면 우선순위가 낮은 후보부터 뺀다(D-07)."""
    before = len(candidates)
    while len(candidates) > 1 and not cost.can_afford(_estimate_edit(candidates, model)):
        candidates = candidates[:-1]
    if len(candidates) < before:
        log.warning("비용 상한 때문에 후보를 %d → %d건으로 줄였다", before, len(candidates))
    return candidates


def _update_knowledge(
    stages: Stages,
    knowledge: str,
    items: list[Item],
    ctx: RunContext,
) -> KnowledgeUpdate | None:
    """메모 갱신은 실패해도 실행을 실패로 만들지 않는다(메일은 이미 나갔다)."""
    official = [i for i in items if i.category == "official"]
    est = CostTracker.estimate(
        ctx.settings.model_edit,
        (len(knowledge) + sum(len(i.summary) for i in official)) // CHARS_PER_TOKEN_ESTIMATE,
        KNOWLEDGE_MAX_TOKENS,
    )
    if not official or not ctx.cost.can_afford(est):
        log.info(
            "권장 사항 메모 갱신을 건너뛴다(공식 항목 %d건, 남은 예산 $%.4f)",
            len(official),
            ctx.cost.remaining,
        )
        return None
    try:
        update = stages.update_knowledge(knowledge, official, ctx)
    except Exception:
        log.exception("권장 사항 메모 갱신 실패(무시하고 계속)")
        return None
    if update is None:
        return None
    diff = "".join(
        difflib.unified_diff(
            knowledge.splitlines(True),
            update.markdown.splitlines(True),
            "current.md",
            "current.md(new)",
        )
    )
    log.info("권장 사항 메모 갱신 diff:\n%s", diff or "(변경 없음)")
    return update


def _write_previews(out_dir: Path, day: dt.date, html: str, text: str) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = [out_dir / f"preview-{day:%Y%m%d}.html", out_dir / f"preview-{day:%Y%m%d}.txt"]
    paths[0].write_text(html, encoding="utf-8")
    paths[1].write_text(text, encoding="utf-8")
    return paths


def _finish(
    store: st.StateStore,
    runs: st.RunsFile,
    record: st.RunRecord,
    cost: CostTracker,
    settings: Settings,
    *,
    write: bool,
) -> None:
    record.finished_at = dt.datetime.now(dt.UTC)
    record.cost_usd = round(cost.run_cost, 6)
    record.tokens = dict(cost.tokens)
    log.info(
        "실행 %s: status=%s sent=%s cost=$%.4f",
        record.run_id,
        record.status,
        record.sent,
        record.cost_usd,
    )
    if not write:  # --offline: 기록도 알림도 남기지 않는다
        return
    runs = st.add_run(runs, record)
    budget = cost.budget_usd
    # 요약·알림이 실패해도 runs.json 저장과 실행 결과를 막지 않는다.
    try:
        summary = runlog.summarize(record, runs, budget_usd=budget)
        runlog.write_step_summary(summary, settings.step_summary_path)
        alert = notify.build_alert(record, runs, budget_usd=budget)
        if alert is not None:
            notify.send_alert(alert, settings)
    except Exception:
        log.exception("실행 요약·운영 알림 실패(무시하고 계속)")
    store.save_runs(runs)
