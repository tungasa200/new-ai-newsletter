from __future__ import annotations

import httpx
import pytest

from ainews.models import Item, SourceRef
from ainews.util.cost import BudgetExceeded, CostTracker, Usage, price_of
from ainews.util.http import Http
from ainews.util.retry import retry
from ainews.util.urlnorm import normalize_url, url_id


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("http://www.Example.com/a/b/?utm_source=x&b=2&a=1", "https://example.com/a/b?a=1&b=2"),
        ("https://example.com/", "https://example.com"),
        (
            "https://news.ycombinator.com/item?id=1&fbclid=z",
            "https://news.ycombinator.com/item?id=1",
        ),
        ("https://example.com/notes#2026-11-10", "https://example.com/notes#2026-11-10"),
        ("https://example.com:8443/x", "https://example.com:8443/x"),
    ],
)
def test_normalize_url(raw: str, expected: str) -> None:
    assert normalize_url(raw) == expected


def test_item_keeps_original_link_and_normalized_key() -> None:
    item = Item.new(
        url="http://www.Site.com/a/?source=feed&utm_source=rss",
        source="s",
        category="community",
        kind="blog",
        title="t",
    )
    assert item.url == "http://www.Site.com/a/?source=feed"  # 메일 링크는 utm_*만 뺀다
    assert item.key == "https://site.com/a"
    assert item.id == url_id(item.key)


def test_url_id_stable() -> None:
    assert url_id("https://example.com") == url_id("https://example.com")
    assert len(url_id("https://example.com")) == 12


def test_retry_transient_then_success() -> None:
    calls = []
    sleeps: list[float] = []

    def fn() -> str:
        calls.append(1)
        if len(calls) < 3:
            raise TimeoutError
        return "ok"

    assert retry(fn, is_transient=lambda e: True, sleep=sleeps.append) == "ok"
    assert sleeps == [1.0, 2.0]


def test_retry_gives_up_after_three_retries() -> None:
    calls = []

    def fn() -> None:
        calls.append(1)
        raise TimeoutError

    with pytest.raises(TimeoutError):
        retry(fn, is_transient=lambda e: True, sleep=lambda s: None)
    assert len(calls) == 4


def test_retry_non_transient_raises_immediately() -> None:
    calls = []

    def fn() -> None:
        calls.append(1)
        raise ValueError

    with pytest.raises(ValueError):
        retry(fn, is_transient=lambda e: False, sleep=lambda s: None)
    assert len(calls) == 1


def test_http_retries_5xx_and_sets_user_agent() -> None:
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.headers["User-Agent"])
        return httpx.Response(503 if len(seen) == 1 else 200, text="ok")

    http = Http("ainews-test", transport=httpx.MockTransport(handler), sleep=lambda s: None)
    assert http.get("https://example.com/feed").text == "ok"
    assert seen == ["ainews-test", "ainews-test"]


def test_http_404_not_retried() -> None:
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(404)

    http = Http("t", transport=httpx.MockTransport(handler), sleep=lambda s: None)
    with pytest.raises(httpx.HTTPStatusError):
        http.get("https://example.com/x")
    assert len(calls) == 1


def test_http_waits_between_requests_to_same_host() -> None:
    clock = iter([0.0, 0.2, 0.2])
    sleeps: list[float] = []
    http = Http(
        "t",
        min_interval=1.0,
        transport=httpx.MockTransport(lambda r: httpx.Response(200)),
        sleep=sleeps.append,
        clock=lambda: next(clock),
    )
    http.get("https://a.example/1")
    http.get("https://a.example/2")
    assert sleeps == [pytest.approx(0.8)]


def test_price_and_budget() -> None:
    assert price_of("claude-sonnet-5-5", Usage(input_tokens=1_000_000)) == pytest.approx(2.0)
    assert price_of("claude-haiku-5-5", Usage(output_tokens=1_000_000)) == pytest.approx(0.5)
    tracker = CostTracker(1.0, spent_before=0.4)
    tracker.record("claude-sonnet-5-5", Usage(input_tokens=100_000, output_tokens=10_000))
    assert tracker.spent == pytest.approx(0.7)
    assert tracker.can_afford(0.3)
    with pytest.raises(BudgetExceeded):
        tracker.check(0.31)


def test_unknown_model_priced_conservatively() -> None:
    assert price_of("unknown", Usage(input_tokens=1_000_000)) == pytest.approx(2.0)


def test_non_web_scheme_never_reaches_mail_link() -> None:
    item = Item.new(
        url="javascript:alert(1)", source="s", category="community", kind="blog", title="t"
    )
    assert item.url.startswith("https:")  # 스킴이 https로 바뀐다
    with pytest.raises(ValueError):
        SourceRef(title="t", url="javascript:alert(1)", kind="official")
