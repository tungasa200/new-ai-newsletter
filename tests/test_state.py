from __future__ import annotations

import datetime as dt
from pathlib import Path

from ainews import state as st
from ainews.models import DeepItem, Digest, ReleaseLine, SourceConfig, SourceStat

DAY = dt.date(2026, 11, 11)


def _item(title: str, event_key: str, urls: list[str], **kw: object) -> DeepItem:
    return DeepItem.model_validate(
        {
            "kind": "news",
            "title": title,
            "background": "b",
            "details": ["d"],
            "how_to_apply": "h",
            "freshness": "current",
            "sources": [{"title": "s", "url": u, "kind": "official"} for u in urls],
            "event_key": event_key,
            "importance": 3,
            "practicality": 3,
            **kw,
        }
    )


def _digest() -> Digest:
    return Digest(
        date=DAY,
        summary="s",
        items=[
            _item("팁", "ev-tip", ["https://e.com/t"], kind="tip", steps=["a"], applies_to="x"),
            _item("낡은 팁", "ev-old", ["https://e.com/old"], freshness="outdated"),
            _item(
                "뉴스",
                "ev-news",
                ["https://e.com/n", "http://www.e.org/hn/?utm_source=x"],
                freshness="caution",
            ),
        ],
        releases=[
            ReleaseLine(tool="cli", version="1.0", one_line="출시", url="https://e.com/r"),
            ReleaseLine(tool="sdk", one_line="출시", url="https://e.com/s", event_key="ev-sdk"),
        ],
    )


def test_digest_drops_outdated() -> None:
    assert [i.event_key for i in _digest().items] == ["ev-tip", "ev-news"]


def test_store_roundtrip_and_missing_files(tmp_path: Path) -> None:
    store = st.StateStore(tmp_path / "state")
    assert store.load_sent() == st.SentFile()
    assert store.load_runs() == st.RunsFile()
    assert store.load_knowledge() == ""
    now = dt.datetime(2026, 11, 10, 22, tzinfo=dt.UTC)
    sent = st.record_delivery(st.SentFile(), _digest(), run_id="r1", now=now, day=DAY)
    store.save_sent(sent)
    store.save_knowledge("# 메모\n")
    assert store.load_sent() == sent
    assert store.load_knowledge() == "# 메모\n"
    assert not list((tmp_path / "state").glob("*.tmp"))


def test_record_delivery_and_index() -> None:
    now = dt.datetime(2026, 11, 10, 22, tzinfo=dt.UTC)
    sent = st.record_delivery(st.SentFile(), _digest(), run_id="r1", now=now, day=DAY)
    assert st.sent_on(sent, DAY)
    assert not st.sent_on(sent, DAY + dt.timedelta(days=1))
    assert sent.deliveries[0].count == 4
    index = st.SentIndex(sent)
    assert index.has_url("https://e.org/hn")  # LLM이 낸 URL도 정규화해서 남긴다
    assert index.has_event("ev-news")
    assert index.has_event("ev-sdk")  # 릴리스 줄의 사건 키
    assert index.has_event("https://e.com/r")  # 사건 키가 없으면 URL로 대신한다
    assert index.has_title("  팁 ")
    assert not index.has_url("https://e.com/old")


def test_prune_after_90_days() -> None:
    old = DAY - dt.timedelta(days=90)
    sent = st.SentFile(
        entries=[
            st.SentEntry(url="u1", event_key="e", title_hash="h", sent_on=old),
            st.SentEntry(
                url="u2", event_key="e", title_hash="h", sent_on=old + dt.timedelta(days=1)
            ),
        ]
    )
    assert [e.url for e in st.prune_sent(sent, DAY).entries] == ["u2"]


def test_spent_on_sums_same_day() -> None:
    t = dt.datetime(2026, 11, 10, tzinfo=dt.UTC)
    runs = st.RunsFile(
        runs=[
            st.RunRecord(run_id="a", date=DAY, started_at=t, cost_usd=0.3),
            st.RunRecord(run_id="b", date=DAY, started_at=t, cost_usd=0.2),
            st.RunRecord(run_id="c", date=DAY - dt.timedelta(days=1), started_at=t, cost_usd=9),
        ]
    )
    assert st.spent_on(runs, DAY) == 0.5


def test_since_map_and_source_health() -> None:
    now = dt.datetime(2026, 11, 10, 22, tzinfo=dt.UTC)
    srcs = [
        SourceConfig(name="a", category="official", kind="changelog", method="rss", url="u"),
        SourceConfig(
            name="b", category="official", kind="release", method="rss", url="u", lookback_hours=168
        ),
    ]
    runs = st.RunsFile()
    assert st.since_map(runs, srcs, now)["a"] == now - dt.timedelta(hours=36)
    assert st.since_map(runs, srcs, now)["b"] == now - dt.timedelta(days=7)

    rec = st.RunRecord(
        run_id="r",
        date=DAY,
        started_at=now,
        sent=True,
        source_stats=[
            SourceStat(source="a", ok=True, newest_at=now),
            SourceStat(source="b", ok=False, error="timeout"),
        ],
    )
    runs = st.add_run(runs, rec)
    assert st.since_map(runs, srcs, now)["a"] == now
    assert runs.sources["b"].consecutive_failures == 1
    runs = st.add_run(runs, rec.model_copy(update={"run_id": "r2"}))
    assert runs.sources["b"].consecutive_failures == 2
    assert runs.sources["a"].consecutive_failures == 0


def test_cursor_moves_only_when_sent() -> None:
    now = dt.datetime(2026, 11, 10, 22, tzinfo=dt.UTC)
    stats = [SourceStat(source="a", ok=True, newest_at=now)]
    rec = st.RunRecord(run_id="r", date=DAY, started_at=now, source_stats=stats)
    for status in ("failed", "skipped", "ok"):  # ok인 dry-run도 sent=False
        runs = st.add_run(st.RunsFile(), rec.model_copy(update={"status": status}))
        assert runs.sources["a"].cursor_at is None
        assert runs.sources["a"].last_success_at == now  # 상태 신호는 그대로 갱신한다
    runs = st.add_run(st.RunsFile(), rec.model_copy(update={"sent": True}))
    assert runs.sources["a"].cursor_at == now


def test_sent_today_falls_back_to_runs() -> None:
    t = dt.datetime(2026, 11, 10, tzinfo=dt.UTC)
    runs = st.RunsFile(runs=[st.RunRecord(run_id="a", date=DAY, started_at=t, sent=True)])
    assert st.sent_today(st.SentFile(), runs, DAY)
    assert not st.sent_today(st.SentFile(), runs, DAY + dt.timedelta(days=1))
