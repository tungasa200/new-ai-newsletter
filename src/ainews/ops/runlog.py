"""실행 요약(FR-OPS-05): 소스별 수집 수·실패 소스·토큰·비용·발송 결과.

GitHub Actions에서는 같은 내용을 job 요약(GITHUB_STEP_SUMMARY)에도 쓴다.
비밀값·수신자는 넣지 않는다.
"""

from __future__ import annotations

from pathlib import Path

from ainews.state import RunRecord, RunsFile, spent_on


def summarize(record: RunRecord, runs: RunsFile, *, budget_usd: float) -> str:
    """실행 하나의 요약을 Markdown으로 만든다. runs는 이 실행을 더한 뒤의 상태다."""
    spent = spent_on(runs, record.date)
    mode = "dry-run" if record.dry_run else "발송"
    lines = [
        f"## ainews 실행 {record.run_id}",
        "",
        f"- 날짜(KST): {record.date} / 모드: {mode}",
        f"- 결과: {record.status}"
        + (f" (실패 단계: {record.failed_stage})" if record.failed_stage else "")
        + f" / 발송: {'예' if record.sent else '아니오'}",
        f"- 비용: 이번 ${record.cost_usd:.4f} / 오늘 합계 ${spent:.4f} / 상한 ${budget_usd:.2f}",
    ]
    lines += [f"- 경고: {w}" for w in record.warnings]
    if record.tokens:
        lines.append("- 토큰: " + ", ".join(f"{k}={v}" for k, v in sorted(record.tokens.items())))
    if record.source_stats:
        lines += ["", "| 소스 | 결과 | 수집 | 연속 실패 | 오류 |", "|---|---|---|---|---|"]
        for s in record.source_stats:
            health = runs.sources.get(s.source)
            fails = health.consecutive_failures if health else 0
            result = "성공" if s.ok else "실패"
            error = _cell(s.error or "")[:120]
            lines.append(f"| {_cell(s.source)} | {result} | {s.count} | {fails} | {error} |")
    return "\n".join(lines) + "\n"


def _cell(text: str) -> str:
    return text.replace("|", "/").replace("\n", " ")


def write_step_summary(text: str, path: Path | None) -> None:
    """job 요약 파일이 있으면(Actions 안이면) 요약을 덧붙인다."""
    if path:
        with path.open("a", encoding="utf-8") as f:
            f.write(text)
