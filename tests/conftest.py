"""공용 fixture(AN-backend1 담당)."""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from ainews.context import RunContext
from ainews.fake import FakeLLM
from ainews.settings import Settings
from ainews.util.cost import CostTracker
from ainews.util.http import Http

# 2026-11-10 22:00 UTC = 2026-11-11 07:00 KST
NOW = dt.datetime(2026, 11, 10, 22, 0, tzinfo=dt.UTC)


@pytest.fixture
def now() -> dt.datetime:
    return NOW


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        state_dir=tmp_path / "state",
        out_dir=tmp_path / "out",
        sources_path=tmp_path / "sources.yaml",
        gmail_address="sender@example.invalid",
        gmail_app_password="x",
        recipients=("a@example.invalid", "b@example.invalid"),
    )


@pytest.fixture
def ctx(settings: Settings, now: dt.datetime) -> Iterator[RunContext]:
    """단계 함수 단위 테스트용. llm은 FakeLLM이라 필요한 스키마 응답을 handlers에 등록한다."""
    http = Http("test-agent")
    yield RunContext(
        settings=settings,
        http=http,
        llm=FakeLLM(),
        cost=CostTracker(settings.daily_budget_usd),
        now=now,
        log=logging.getLogger("test"),
        offline=True,
    )
    http.close()
