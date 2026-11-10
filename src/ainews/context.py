"""각 단계에 넘기는 실행 문맥."""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

from ainews.settings import KST, Settings
from ainews.util.cost import CostTracker
from ainews.util.http import Http
from ainews.util.llm import LLM


@dataclass
class RunContext:
    settings: Settings
    http: Http
    llm: LLM
    cost: CostTracker
    now: dt.datetime  # UTC, aware
    log: logging.Logger
    offline: bool = False

    @property
    def today(self) -> dt.date:
        """발송 기준 날짜(KST)."""
        return self.now.astimezone(KST).date()
