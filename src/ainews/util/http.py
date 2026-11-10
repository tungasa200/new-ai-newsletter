"""수집용 HTTP 클라이언트(FR-SRC-08): 정직한 User-Agent, 호스트별 요청 간격, 일시 오류 재시도."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import httpx

from ainews.util.retry import retry


def is_transient_http(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code == 429 or code >= 500
    return isinstance(exc, httpx.TransportError)  # 시간 초과·연결 오류 포함


class Http:
    def __init__(
        self,
        user_agent: str,
        *,
        min_interval: float = 1.0,
        timeout: float = 20.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = httpx.Client(
            headers={"User-Agent": user_agent},
            timeout=timeout,
            follow_redirects=True,
            transport=transport,
        )
        self._min_interval = min_interval
        self._sleep = sleep
        self._clock = clock
        self._last: dict[str, float] = {}

    def _wait_turn(self, url: str) -> None:
        host = urlsplit(url).hostname or ""
        last = self._last.get(host)
        if last is not None:
            wait = self._min_interval - (self._clock() - last)
            if wait > 0:
                self._sleep(wait)
        self._last[host] = self._clock()

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        """GET. 4xx는 바로, 429·5xx·전송 오류는 재시도 후 예외를 낸다."""

        def once() -> httpx.Response:
            self._wait_turn(url)
            resp = self._client.get(url, **kwargs)
            resp.raise_for_status()
            return resp

        return retry(once, is_transient=is_transient_http, sleep=self._sleep)

    def close(self) -> None:
        self._client.close()
