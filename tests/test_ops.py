from __future__ import annotations

import dataclasses
import datetime as dt
from pathlib import Path

import pytest

from ainews import state as st
from ainews.fake import FakeLLM, fake_collect
from ainews.mail import smtp
from ainews.models import RenderedMail, SourceStat
from ainews.ops import notify, runlog
from ainews.pipeline import RunOptions, Stages, run
from ainews.settings import Settings

DAY = dt.date(2026, 11, 11)
STARTED = dt.datetime(2026, 11, 10, 22, 0, tzinfo=dt.UTC)  # DAY 07:00 KST


def _record(**kw: object) -> st.RunRecord:
    return st.RunRecord.model_validate({"run_id": "r1", "date": DAY, "started_at": STARTED, **kw})


def _runs_after(record: st.RunRecord, before: st.RunsFile | None = None) -> st.RunsFile:
    return st.add_run(before or st.RunsFile(), record)


def test_no_alert_when_all_fine() -> None:
    rec = _record(cost_usd=0.2, source_stats=[SourceStat(source="A", ok=True, count=3)])
    assert notify.build_alert(rec, _runs_after(rec), budget_usd=1.0) is None


def test_failure_alert_has_stage_and_error_only() -> None:
    rec = _record(status="failed", failed_stage="edit", error="MailError")
    alert = notify.build_alert(rec, _runs_after(rec), budget_usd=1.0)
    assert alert is not None
    assert "실패" in alert.subject and "edit" in alert.text and "MailError" in alert.text


def test_budget_warning_only_on_crossing() -> None:
    first = _record(cost_usd=0.5)
    runs = _runs_after(first)
    assert notify.build_alert(first, runs, budget_usd=1.0) is None
    second = _record(run_id="r2", cost_usd=0.35)  # 0.5 → 0.85: 80% 문턱을 넘는다
    runs = _runs_after(second, runs)
    alert = notify.build_alert(second, runs, budget_usd=1.0)
    assert alert is not None and "비용 경고" in alert.text
    third = _record(run_id="r3", cost_usd=0.05)  # 이미 넘은 뒤라 다시 알리지 않는다
    assert notify.build_alert(third, _runs_after(third, runs), budget_usd=1.0) is None


def test_budget_warning_after_dry_run_crossed() -> None:
    dry = _record(cost_usd=0.85, dry_run=True)
    runs = _runs_after(dry)
    assert notify.build_alert(dry, runs, budget_usd=1.0) is None  # dry-run은 알리지 않는다
    real = _record(run_id="r2", cost_usd=0.1)
    alert = notify.build_alert(real, _runs_after(real, runs), budget_usd=1.0)
    assert alert is not None and "비용 경고" in alert.text


def test_failure_after_send_says_mail_was_sent() -> None:
    rec = _record(status="failed", failed_stage="knowledge", sent=True)
    alert = notify.build_alert(rec, _runs_after(rec), budget_usd=1.0)
    assert alert is not None and "이미 발송" in alert.text and "나가지 않았다" not in alert.text


def test_source_alert_after_three_consecutive_failures() -> None:
    runs = st.RunsFile()
    for i in range(3):
        rec = _record(run_id=f"r{i}", source_stats=[SourceStat(source="A", ok=False)])
        runs = _runs_after(rec, runs)
        alert = notify.build_alert(rec, runs, budget_usd=1.0)
        assert (alert is not None) == (i == 2)
    assert alert is not None and "소스 점검 필요" in alert.text and "A" in alert.text
    skipped = _record(run_id="r9", status="skipped")  # 수집하지 않은 실행은 다시 알리지 않는다
    runs = _runs_after(skipped, runs)
    assert notify.build_alert(skipped, runs, budget_usd=1.0) is None
    fourth = _record(run_id="r4", source_stats=[SourceStat(source="A", ok=False)])
    assert notify.build_alert(fourth, _runs_after(fourth, runs), budget_usd=1.0) is None


def test_send_alert_goes_to_ops_only(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    alert = RenderedMail(subject="s", html="h", text="t")
    calls: list[Settings] = []
    monkeypatch.setattr(smtp, "send", lambda m, s: calls.append(s))
    notify.send_alert(alert, settings)
    assert calls == []  # OPS_EMAIL 없음

    notify.send_alert(alert, dataclasses.replace(settings, ops_email="ops@example.invalid"))
    assert calls[0].recipients == ("ops@example.invalid",)


def test_send_alert_failure_is_swallowed_without_address(
    settings: Settings, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(m: RenderedMail, s: Settings) -> None:
        raise smtp.MailError("ops@example.invalid")

    monkeypatch.setattr(smtp, "send", fail)
    ops = dataclasses.replace(settings, ops_email="ops@example.invalid")
    notify.send_alert(RenderedMail(subject="s", html="h", text="t"), ops)
    assert "발송 실패" in caplog.text and "ops@example.invalid" not in caplog.text


def test_summary_and_step_summary(tmp_path: Path) -> None:
    rec = _record(
        cost_usd=0.1234,
        tokens={"m:input": 10},
        source_stats=[SourceStat(source="A|B", ok=False, error="HTTP 500|x")],
    )
    text = runlog.summarize(rec, _runs_after(rec), budget_usd=1.0)
    assert "$0.1234" in text and "| A/B | 실패 | 0 | 1 | HTTP 500/x |" in text
    out = tmp_path / "summary.md"
    runlog.write_step_summary(text, out)
    runlog.write_step_summary(text, None)  # Actions 밖에서는 아무것도 하지 않는다
    assert out.read_text(encoding="utf-8") == text


def test_pipeline_failure_sends_ops_alert(
    settings: Settings, now: dt.datetime, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[tuple[RenderedMail, Settings]] = []
    monkeypatch.setattr(smtp, "send", lambda m, s: sent.append((m, s)))

    def boom(*a: object) -> None:
        raise RuntimeError("secret detail")

    ops = dataclasses.replace(settings, ops_email="ops@example.invalid")
    stages = Stages(collect=fake_collect, edit=boom)  # type: ignore[arg-type]
    assert run(ops, RunOptions(), stages=stages, llm=FakeLLM(), now=now).status == "failed"
    [(mail, to)] = sent
    assert to.recipients == ("ops@example.invalid",)
    assert "edit" in mail.text and "RuntimeError" in mail.text and "secret detail" not in mail.text

    sent.clear()  # dry-run은 알리지 않는다
    run(ops, RunOptions(dry_run=True), stages=stages, llm=FakeLLM(), now=now)
    assert sent == []


def test_run_warning_shows_in_alert_and_summary() -> None:
    rec = _record(warnings=["권장 사항 메모 갱신 실패: 출력이 max_tokens에서 잘림"])
    alert = notify.build_alert(rec, _runs_after(rec), budget_usd=1.0)
    assert alert is not None and "경고" in alert.subject and "max_tokens" in alert.text
    assert "- 경고: 권장 사항 메모 갱신 실패" in runlog.summarize(
        rec, _runs_after(rec), budget_usd=1.0
    )
