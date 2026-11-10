"""일시 오류 재시도(FR-OPS-01): 지수 백오프, 최대 3회 재시도."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

log = logging.getLogger(__name__)


def retry[T](
    fn: Callable[[], T],
    *,
    is_transient: Callable[[BaseException], bool],
    retries: int = 3,
    base_delay: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """fn을 부르고, is_transient인 오류면 base_delay*2^n초 쉬고 다시 부른다."""
    for attempt in range(retries + 1):
        try:
            return fn()
        except Exception as exc:
            if attempt == retries or not is_transient(exc):
                raise
            delay = base_delay * 2**attempt
            log.warning("일시 오류, %.1f초 뒤 재시도(%d/%d): %s", delay, attempt + 1, retries, exc)
            sleep(delay)
    raise AssertionError("unreachable")
