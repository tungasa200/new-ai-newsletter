"""명령줄 진입점: python -m ainews [--dry-run] [--offline] [--force]."""

from __future__ import annotations

import argparse
import logging
import sys

from ainews.pipeline import RunOptions, run
from ainews.settings import load_settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ainews", description="AI 실무 소식 뉴스레터 배치")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="메일을 보내지 않고 out/preview-*.html·txt를 만든다. runs.json에 비용만 남긴다",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="가짜 수집기와 가짜 LLM을 쓴다(키 불필요). 늘 dry-run이고 state를 쓰지 않는다",
    )
    parser.add_argument(
        "--force", action="store_true", help="오늘 이미 발송했어도 다시 실행한다(수동 재발송용)"
    )
    args = parser.parse_args(argv)
    # Windows 콘솔 기본 인코딩(cp949)에서도 로그가 깨지지 않게 한다.
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    result = run(
        load_settings(), RunOptions(dry_run=args.dry_run, offline=args.offline, force=args.force)
    )
    return 1 if result.status == "failed" else 0
