from __future__ import annotations

import datetime as dt

import pytest

from ainews import state as st
from ainews.cli import main
from ainews.fake import FakeLLM, fake_collect, fake_enrich_bodies
from ainews.models import KnowledgeUpdate, RenderedMail
from ainews.pipeline import RunOptions, RunResult, Stages, run
from ainews.settings import Settings
from ainews.util.llm import LLMTruncatedError


def _sending_stages(outbox: list[RenderedMail]) -> Stages:
    """가짜 수집 + 발송함. --offline은 발송하지 않으므로 발송 경로는 이것으로 시험한다."""
    return Stages(
        collect=fake_collect,
        enrich_bodies=fake_enrich_bodies,
        send=lambda mail, settings: outbox.append(mail),
    )


def _run(settings: Settings, opts: RunOptions, stages: Stages, now: dt.datetime) -> RunResult:
    return run(settings, opts, stages=stages, llm=FakeLLM(), now=now)


def test_offline_dry_run_writes_previews_and_no_state(settings: Settings, now: dt.datetime) -> None:
    result = run(settings, RunOptions(dry_run=True, offline=True), now=now)
    assert result.status == "ok" and not result.sent
    names = sorted(p.name for p in result.previews)
    assert names == ["preview-20261111.html", "preview-20261111.txt"]
    html = (settings.out_dir / "preview-20261111.html").read_text(encoding="utf-8")
    assert "https://example.com/news/new-model" in html  # 추적 파라미터 제거된 URL
    assert not settings.state_dir.exists()


def test_send_records_delivery_and_second_run_skips(settings: Settings, now: dt.datetime) -> None:
    outbox: list[RenderedMail] = []
    first = _run(settings, RunOptions(), _sending_stages(outbox), now)
    assert first.status == "ok" and first.sent and len(outbox) == 1

    store = st.StateStore(settings.state_dir)
    assert st.sent_on(store.load_sent(), dt.date(2026, 11, 11))

    later = now + dt.timedelta(hours=1)  # 07:30 KST 두 번째 cron
    second = _run(settings, RunOptions(), _sending_stages(outbox), later)
    assert second.status == "skipped" and len(outbox) == 1
    assert [r.status for r in store.load_runs().runs] == ["ok", "skipped"]


def test_force_resends_but_skips_already_sent_items(settings: Settings, now: dt.datetime) -> None:
    outbox: list[RenderedMail] = []
    _run(settings, RunOptions(), _sending_stages(outbox), now)
    result = _run(settings, RunOptions(force=True), _sending_stages(outbox), now)
    # 가짜 항목은 모두 이미 보냈으므로 다이제스트가 비어 발송하지 않는다(FR-MAIL-05 기본값).
    assert result.status == "skipped" and len(outbox) == 1


def test_stage_failure_is_recorded(settings: Settings, now: dt.datetime) -> None:
    def boom(*a: object) -> None:
        raise RuntimeError("edit down")

    stages = Stages(collect=fake_collect, edit=boom)  # type: ignore[arg-type]
    result = _run(settings, RunOptions(), stages, now)
    assert result.status == "failed" and result.failed_stage == "edit"
    [rec] = st.StateStore(settings.state_dir).load_runs().runs
    assert rec.status == "failed" and rec.failed_stage == "edit"
    assert rec.source_stats  # 실패해도 수집 결과는 남는다


def test_knowledge_update_saved_and_failure_ignored(settings: Settings, now: dt.datetime) -> None:
    outbox: list[RenderedMail] = []
    stages = _sending_stages(outbox)
    stages.update_knowledge = lambda k, items, ctx: KnowledgeUpdate(
        markdown="# 새 메모\n", changes=["c"], source_urls=["https://e.com"]
    )
    assert _run(settings, RunOptions(), stages, now).status == "ok"
    assert st.StateStore(settings.state_dir).load_knowledge() == "# 새 메모\n"

    def broken(*a: object) -> None:
        raise RuntimeError

    stages.update_knowledge = broken  # type: ignore[assignment]
    result = run(settings, RunOptions(dry_run=True, offline=True), stages=stages, now=now)
    assert result.status == "ok"


def test_offline_never_sends_or_writes_state(settings: Settings, now: dt.datetime) -> None:
    outbox: list[RenderedMail] = []
    stages = Stages(send=lambda mail, settings: outbox.append(mail))
    result = run(settings, RunOptions(offline=True), stages=stages, now=now)
    assert result.status == "ok" and not result.sent and result.previews
    assert outbox == [] and not settings.state_dir.exists()
    assert stages.collect is not fake_collect  # 넘겨준 Stages를 바꾸지 않는다


def test_dry_run_records_cost_but_not_delivery(settings: Settings, now: dt.datetime) -> None:
    outbox: list[RenderedMail] = []
    result = _run(settings, RunOptions(dry_run=True), _sending_stages(outbox), now)
    assert result.status == "ok" and not result.sent and outbox == []
    store = st.StateStore(settings.state_dir)
    [rec] = store.load_runs().runs
    assert rec.dry_run and not rec.sent
    assert store.load_sent() == st.SentFile()
    assert all(h.cursor_at is None for h in store.load_runs().sources.values())


def test_sent_record_failure_still_blocks_resend(
    settings: Settings, now: dt.datetime, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(self: st.StateStore, sent: st.SentFile) -> None:
        raise OSError("disk")

    monkeypatch.setattr(st.StateStore, "save_sent", fail)
    outbox: list[RenderedMail] = []
    first = _run(settings, RunOptions(), _sending_stages(outbox), now)
    assert first.status == "ok" and first.sent
    second = _run(settings, RunOptions(), _sending_stages(outbox), now + dt.timedelta(hours=1))
    assert second.status == "skipped" and len(outbox) == 1


def test_cli_offline_dry_run(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("AINEWS_OUT_DIR", str(tmp_path / "out"))
    monkeypatch.setenv("AINEWS_STATE_DIR", str(tmp_path / "state"))
    assert main(["--dry-run", "--offline"]) == 0
    assert list((tmp_path / "out").glob("preview-*.html"))


def test_knowledge_truncation_is_recorded_as_warning(settings: Settings, now: dt.datetime) -> None:
    def truncated(*a: object) -> None:
        raise LLMTruncatedError("cut")

    stages = _sending_stages([])
    stages.update_knowledge = truncated  # type: ignore[assignment]
    assert _run(settings, RunOptions(), stages, now).status == "ok"
    [rec] = st.StateStore(settings.state_dir).load_runs().runs
    assert rec.warnings == ["권장 사항 메모 갱신 실패: 출력이 max_tokens에서 잘림"]
