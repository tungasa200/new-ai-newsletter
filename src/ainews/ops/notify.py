"""운영 알림(FR-OPS-03·07, D-07): OPS_EMAIL이 있으면 운영자에게 메일로 알린다.

알림 사유: 실행 실패, 이번 실행에서 하루 비용이 상한의 80%를 넘음, 3회 연속 실패한 소스,
실행 경고(RunRecord.warnings, 예: 권장 사항 메모 갱신 실패).
알림에는 단계·오류 종류·숫자만 넣고 비밀값·수신자 주소는 넣지 않는다. 로그에도 주소를 남기지 않는다.
알림 발송이 실패해도 실행 결과를 바꾸지 않는다. Gmail 자체가 막힌 경우는 Actions 실패 알림이
맡는다(D-04).
"""

from __future__ import annotations

import dataclasses
import html
import logging

from ainews.mail import smtp
from ainews.models import RenderedMail
from ainews.settings import Settings
from ainews.state import RunRecord, RunsFile, spent_on

log = logging.getLogger(__name__)

BUDGET_WARN_RATIO = 0.8  # D-07
SOURCE_FAILURE_ALERT = 3  # FR-OPS-07


def build_alert(record: RunRecord, runs: RunsFile, *, budget_usd: float) -> RenderedMail | None:
    """알릴 사유가 있으면 알림 메일을, 없으면 None. runs는 이 실행을 더한 뒤의 상태다.

    dry-run은 사람이 직접 돌려 결과를 보고 있으므로 알리지 않는다.
    """
    if record.dry_run:
        return None
    reasons: list[tuple[str, str]] = []  # (제목에 쓰는 이름, 본문)
    if record.status == "failed":
        detail = f" ({record.error})" if record.error else ""
        mail = "메일은 이미 발송했다" if record.sent else "오늘 메일이 나가지 않았다"
        reasons.append(("실행 실패", f"단계 {record.failed_stage}{detail}. {mail}"))
    if _first_over_budget_warning(record, runs, budget_usd * BUDGET_WARN_RATIO):
        spent = spent_on(runs, record.date)
        reasons.append(
            (
                "비용 경고",
                f"오늘 합계 ${spent:.4f}, 상한 ${budget_usd:.2f}의 {BUDGET_WARN_RATIO:.0%} 이상",
            )
        )
    # 연속 실패가 문턱에 막 닿은 소스만 알린다(멈춘 소스가 매일 다시 알리지 않게).
    failing = sorted(
        s.source
        for s in record.source_stats
        if not s.ok
        and (h := runs.sources.get(s.source)) is not None
        and h.consecutive_failures == SOURCE_FAILURE_ALERT
    )
    if failing:
        reasons.append(
            ("소스 점검 필요", f"{SOURCE_FAILURE_ALERT}회 연속 실패: " + ", ".join(failing))
        )
    reasons += [("실행 경고", w) for w in record.warnings]
    if not reasons:
        return None

    title = "실패" if record.status == "failed" else "경고"
    subject = f"[ainews 운영 {title}] {record.date} {reasons[0][0]}"
    body = "\n".join(
        [f"실행 {record.run_id} ({record.date} KST)", ""]
        + [f"- {label}: {detail}" for label, detail in reasons]
        + [""]
        + ["자세한 내용은 GitHub Actions 실행 로그를 본다."]
    )
    return RenderedMail(subject=subject, html=f"<pre>{html.escape(body)}</pre>", text=body)


def _first_over_budget_warning(record: RunRecord, runs: RunsFile, threshold: float) -> bool:
    """이 실행이 그날 누적 비용이 문턱 이상인 첫 발송 실행인지.

    dry-run이 문턱을 넘겼으면 그 뒤 첫 발송 실행이 알린다. 같은 날 다음 실행은 다시 알리지 않는다.
    """
    total = 0.0
    for r in runs.runs:
        if r.date != record.date:
            continue
        total += r.cost_usd
        if total >= threshold and not r.dry_run:
            return r.run_id == record.run_id
    return False


def send_alert(alert: RenderedMail, settings: Settings) -> None:
    """OPS_EMAIL로 보낸다. 설정이 없거나 발송이 실패하면 로그만 남긴다."""
    if not settings.ops_email:
        log.info("OPS_EMAIL이 없어 운영 알림을 메일로 보내지 않는다: %s", alert.subject)
        return
    try:
        smtp.send(alert, dataclasses.replace(settings, recipients=(settings.ops_email,)))
    except Exception as exc:
        # smtp.send는 주소를 뗀 MailError를 올린다. 그래도 메시지 대신 종류만 남긴다.
        log.error("운영 알림 발송 실패: %s", type(exc).__name__)
        return
    log.info("운영 알림 발송: %s", alert.subject)
