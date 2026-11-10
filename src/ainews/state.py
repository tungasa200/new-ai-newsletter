"""실행 상태(D-03): sent.json, runs.json, knowledge/current.md.

코드는 폴더의 파일만 읽고 쓴다. Actions에서 state 가지를 받고 커밋하는 일은 workflow가 한다.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from ainews.models import Digest, SourceConfig, SourceStat
from ainews.util.urlnorm import normalize_url

SENT_RETENTION_DAYS = 90
RUNS_KEEP = 60
DEFAULT_LOOKBACK = dt.timedelta(hours=36)


class Delivery(BaseModel):
    date: dt.date  # KST 날짜
    sent_at: dt.datetime
    run_id: str
    count: int


class SentEntry(BaseModel):
    url: str
    event_key: str
    title_hash: str
    sent_on: dt.date


class SentFile(BaseModel):
    version: int = 1
    deliveries: list[Delivery] = Field(default_factory=list)
    entries: list[SentEntry] = Field(default_factory=list)


class SourceHealth(BaseModel):
    last_success_at: dt.datetime | None = None  # 상태 신호(FR-OPS-07): 마지막으로 수집에 성공한 때
    cursor_at: dt.datetime | None = None  # 다음 조회 시작점: 발송까지 마친 실행의 시작 시각
    last_new_item_at: dt.datetime | None = None
    consecutive_failures: int = 0


class RunRecord(BaseModel):
    run_id: str
    date: dt.date  # KST 날짜
    started_at: dt.datetime
    finished_at: dt.datetime | None = None
    status: Literal["ok", "skipped", "failed"] = "ok"
    failed_stage: str | None = None
    error: str | None = None  # 실패한 예외의 종류만(메시지에는 주소가 섞일 수 있다)
    sent: bool = False
    dry_run: bool = False  # 비용은 하루 상한에 넣되 발송 실행과 구분한다
    cost_usd: float = 0.0
    tokens: dict[str, int] = Field(default_factory=dict)
    source_stats: list[SourceStat] = Field(default_factory=list)


class RunsFile(BaseModel):
    version: int = 1
    runs: list[RunRecord] = Field(default_factory=list)
    sources: dict[str, SourceHealth] = Field(default_factory=dict)


def title_hash(title: str) -> str:
    norm = re.sub(r"\s+", " ", title).strip().lower()
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:12]


class SentIndex:
    """발송 이력 조회용(FR-DUP-03)."""

    def __init__(self, sent: SentFile) -> None:
        self.urls = {e.url for e in sent.entries}
        self.event_keys = {e.event_key for e in sent.entries}
        self.title_hashes = {e.title_hash for e in sent.entries}

    def has_url(self, key: str) -> bool:
        """key는 정규화 URL(Item.key)."""
        return key in self.urls

    def has_event(self, event_key: str) -> bool:
        return event_key in self.event_keys

    def has_title(self, title: str) -> bool:
        return title_hash(title) in self.title_hashes


class StateStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    @property
    def knowledge_path(self) -> Path:
        return self.root / "knowledge" / "current.md"

    def load_sent(self) -> SentFile:
        return _load(self.root / "sent.json", SentFile)

    def save_sent(self, sent: SentFile) -> None:
        _write(self.root / "sent.json", sent.model_dump_json(indent=1))

    def load_runs(self) -> RunsFile:
        return _load(self.root / "runs.json", RunsFile)

    def save_runs(self, runs: RunsFile) -> None:
        _write(self.root / "runs.json", runs.model_dump_json(indent=1))

    def load_knowledge(self) -> str:
        path = self.knowledge_path
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def save_knowledge(self, markdown: str) -> None:
        _write(self.knowledge_path, markdown)


def _load[M: BaseModel](path: Path, model: type[M]) -> M:
    if not path.exists():
        return model()
    return model.model_validate_json(path.read_text(encoding="utf-8"))


def _write(path: Path, text: str) -> None:
    """임시 파일에 쓴 뒤 교체한다. 중간에 죽어도 기존 파일이 깨지지 않는다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


# ── sent.json ──────────────────────────────────────────


def sent_on(sent: SentFile, day: dt.date) -> bool:
    """그 KST 날짜에 이미 발송했는지(FR-MAIL-04)."""
    return any(d.date == day for d in sent.deliveries)


def record_delivery(
    sent: SentFile, digest: Digest, *, run_id: str, now: dt.datetime, day: dt.date
) -> SentFile:
    # (URL, 사건 키, 제목). URL은 LLM이 낸 값이라 수집 쪽 Item.key와 같은 형태로 맞춘다.
    published = [(src.url, i.event_key, i.title) for i in digest.items for src in i.sources] + [
        (r.url, r.event_key or normalize_url(r.url), r.one_line) for r in digest.releases
    ]
    entries = [
        *sent.entries,
        *(
            SentEntry(url=normalize_url(u), event_key=k, title_hash=title_hash(t), sent_on=day)
            for u, k, t in published
        ),
    ]
    count = len(digest.items) + len(digest.releases)
    deliveries = [*sent.deliveries, Delivery(date=day, sent_at=now, run_id=run_id, count=count)]
    return prune_sent(SentFile(deliveries=deliveries, entries=entries), day)


def prune_sent(sent: SentFile, today: dt.date) -> SentFile:
    cutoff = today - dt.timedelta(days=SENT_RETENTION_DAYS)
    return SentFile(
        deliveries=[d for d in sent.deliveries if d.date > cutoff],
        entries=[e for e in sent.entries if e.sent_on > cutoff],
    )


# ── runs.json ──────────────────────────────────────────


def sent_today(sent: SentFile, runs: RunsFile, day: dt.date) -> bool:
    """sent.json 또는 runs.json 어느 한쪽에라도 그날 발송 기록이 있는지.

    발송 뒤 sent.json 저장이 실패해도 runs.json의 sent=True로 이중 발송을 막는다.
    """
    return sent_on(sent, day) or any(r.sent and r.date == day for r in runs.runs)


def spent_on(runs: RunsFile, day: dt.date) -> float:
    """같은 KST 날짜 실행들의 Claude 비용 합계(하루 상한 판정용)."""
    return sum(r.cost_usd for r in runs.runs if r.date == day)


def since_map(
    runs: RunsFile, sources: list[SourceConfig], now: dt.datetime
) -> dict[str, dt.datetime]:
    """소스별 조회 시작 시각(FR-SRC-05).

    lookback_hours가 있으면 그 기간, 없으면 직전 발송 실행 이후, 기록이 없으면 36시간.
    """
    result: dict[str, dt.datetime] = {}
    for src in sources:
        health = runs.sources.get(src.name)
        if src.lookback_hours is not None:
            result[src.name] = now - dt.timedelta(hours=src.lookback_hours)
        elif health and health.cursor_at:
            result[src.name] = health.cursor_at
        else:
            result[src.name] = now - DEFAULT_LOOKBACK
    return result


def add_run(runs: RunsFile, record: RunRecord) -> RunsFile:
    """실행 기록을 더하고 소스 상태(FR-OPS-07)를 갱신한다."""
    sources = {k: v.model_copy() for k, v in runs.sources.items()}
    for stat in record.source_stats:
        h = sources.setdefault(stat.source, SourceHealth())
        if stat.ok:
            h.last_success_at = record.started_at
            h.consecutive_failures = 0
            # 조회 시작점은 발송까지 마친 실행에서만 당긴다. 실패·건너뜀·dry-run에서
            # 당기면 그 실행에서 모은 항목을 다시 가져오지 못한다.
            if record.sent:
                h.cursor_at = record.started_at
            if stat.newest_at and (
                h.last_new_item_at is None or stat.newest_at > h.last_new_item_at
            ):
                h.last_new_item_at = stat.newest_at
        else:
            h.consecutive_failures += 1
    return RunsFile(runs=[*runs.runs, record][-RUNS_KEEP:], sources=sources)
