"""The Mac screen-time forwarder's pure logic (tools/screentime/daylog_screentime.py)."""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "tools" / "screentime" / "daylog_screentime.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("daylog_screentime", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["daylog_screentime"] = module
    spec.loader.exec_module(module)
    return module


st = _load()
T0 = datetime(2026, 11, 20, 18, 0, tzinfo=UTC)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def ev(start: float, duration: float, **data: Any) -> dict[str, Any]:
    return {"timestamp": at(start).isoformat(), "duration": duration, "data": data}


def win(start: float, duration: float, app: str, title: str = "x") -> dict[str, Any]:
    return ev(start, duration, app=app, title=title)


def afk(start: float, duration: float, status: str = "not-afk") -> dict[str, Any]:
    return ev(start, duration, status=status)


def web(start: float, duration: float, url: str, **extra: Any) -> dict[str, Any]:
    return ev(start, duration, url=url, title="secret subject", **extra)


def spans(pieces: list[Any]) -> list[tuple[str, str, float, float]]:
    return [(p.app, p.site, (p.start - T0).total_seconds(), p.seconds) for p in pieces]


def sessions(
    window: list[dict[str, Any]],
    away: list[dict[str, Any]] | None,
    tabs: dict[str, list[dict[str, Any]]] | None = None,
    min_seconds: float = 20,
) -> list[tuple[str, str, float, float]]:
    out = st.compute_sessions(window, away, tabs or {}, at(-3600), at(36000), min_seconds)
    return spans(out)


# -- timestamps --------------------------------------------------------------------


def test_parse_timestamp_variants() -> None:
    want = datetime(2026, 11, 20, 22, 10, 5, 123000, tzinfo=UTC)
    assert st.parse_timestamp("2026-11-20T22:10:05.123Z") == want
    assert st.parse_timestamp("2026-11-20T22:10:05.123000+00:00") == want
    assert st.parse_timestamp("2026-11-20T22:10:05.123456789Z").microsecond == 123456
    assert st.parse_timestamp("2026-11-20T16:10:05-0600") == want.replace(microsecond=0)
    with pytest.raises(ValueError):
        st.parse_timestamp("yesterday")


def test_event_interval_skips_bad_and_empty() -> None:
    assert st.event_interval({"timestamp": "nope", "duration": 3}) is None
    assert st.event_interval(win(0, 0, "Dia")) is None
    assert st.event_interval(win(10, 5, "Dia")) == (at(10), at(15))


# -- AFK intersection --------------------------------------------------------------


def test_afk_time_is_cut_out() -> None:
    window = [win(0, 600, "Mail")]
    away = [afk(0, 200), afk(200, 100, "afk"), afk(300, 300)]
    assert sessions(window, away) == [("Mail", "", 0, 200), ("Mail", "", 300, 300)]


def test_overlapping_not_afk_events_are_merged() -> None:
    away = [afk(0, 100), afk(50, 100), afk(150, 50)]
    assert st.active_intervals(away) == [(at(0), at(200))]


def test_no_afk_bucket_takes_window_time_as_is() -> None:
    assert sessions([win(0, 90, "Notes")], None) == [("Notes", "", 0, 90)]


def test_ignored_apps_and_overlapping_window_events() -> None:
    window = [win(0, 100, "Mail"), win(90, 100, "Notes"), win(190, 300, "loginwindow")]
    assert sessions(window, None) == [("Mail", "", 0, 100), ("Notes", "", 100, 90)]


def test_window_pieces_never_keep_titles() -> None:
    pieces = st.window_pieces([win(0, 60, "Mail", "Re: payslip")], at(-10), at(100))
    assert "payslip" not in repr(pieces)


# -- browser split -----------------------------------------------------------------


def test_browser_time_split_by_site() -> None:
    window = [win(0, 300, "Dia")]
    tabs = {
        "chromium": [
            web(0, 120, "https://www.youtube.com/watch?v=abc"),
            web(120, 180, "https://mail.example.org/inbox/123"),
        ]
    }
    assert sessions(window, [afk(0, 300)], tabs) == [
        ("Dia", "youtube.com", 0, 120),
        ("Dia", "mail.example.org", 120, 180),
    ]


def test_uncovered_browser_time_keeps_no_site() -> None:
    tabs = {"chromium": [web(100, 100, "https://example.com/")]}
    assert sessions([win(0, 300, "Dia")], None, tabs) == [
        ("Dia", "", 0, 100),
        ("Dia", "example.com", 100, 100),
        ("Dia", "", 200, 100),
    ]


def test_web_events_only_split_browsers_of_their_family() -> None:
    tabs = {"chromium": [web(0, 300, "https://example.com/")]}
    assert sessions([win(0, 100, "Safari"), win(100, 100, "Terminal")], None, tabs) == [
        ("Safari", "", 0, 100),
        ("Terminal", "", 100, 100),
    ]
    tabs = {"safari": [web(0, 300, "https://example.com/")]}
    assert sessions([win(0, 100, "Safari")], None, tabs) == [("Safari", "example.com", 0, 100)]


def test_web_time_outside_browser_focus_is_ignored() -> None:
    # The tab keeps reporting while another app is in front.
    tabs = {"chromium": [web(0, 600, "https://news.example.com/")]}
    window = [win(0, 100, "Dia"), win(100, 500, "Xcode")]
    assert sessions(window, None, tabs) == [
        ("Dia", "news.example.com", 0, 100),
        ("Xcode", "", 100, 500),
    ]


def test_private_tabs_have_no_site() -> None:
    tabs = {"chromium": [web(0, 100, "https://secret.example/", incognito=True)]}
    assert sessions([win(0, 100, "Dia")], None, tabs) == [("Dia", "", 0, 100)]


def test_heartbeat_gaps_in_tabs_do_not_split_a_site() -> None:
    tabs = {
        "chromium": [web(0, 100, "https://example.com/a"), web(102, 98, "https://example.com/b")]
    }
    assert sessions([win(0, 200, "Dia")], None, tabs) == [("Dia", "example.com", 0, 200)]


def test_browser_family() -> None:
    assert st.browser_family("Dia") == "chromium"
    assert st.browser_family("Google Chrome Canary") == "chromium"
    assert st.browser_family("Safari Technology Preview") == "safari"
    assert st.browser_family("Orion") == "any"
    assert st.browser_family("Diary") is None
    assert st.web_bucket_family("aw-watcher-web-firefox_host") == "firefox"
    assert st.web_bucket_family("aw-watcher-web-chrome") == "chromium"


# -- domains -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "want"),
    [
        ("https://www.YouTube.com/watch?v=1&t=2", "youtube.com"),
        ("http://docs.example.org:8080/a/b#c", "docs.example.org"),
        ("https://user:pw@shop.example.com/cart", "shop.example.com"),
        ("https://www.example.com./", "example.com"),
        ("chrome://newtab/", ""),
        ("about:blank", ""),
        ("file:///Users/someone/notes.html", ""),
        ("not a url", ""),
        ("", ""),
    ],
)
def test_domain_of(url: str, want: str) -> None:
    assert st.domain_of(url) == want


# -- coalescing and short fragments ------------------------------------------------


def test_coalesce_joins_small_gaps_only() -> None:
    window = [win(0, 60, "Code"), win(70, 60, "Code"), win(160, 60, "Code")]
    assert sessions(window, None) == [("Code", "", 0, 130), ("Code", "", 160, 60)]


def test_coalesce_does_not_join_different_apps_or_sites() -> None:
    window = [win(0, 60, "Code"), win(60, 60, "Mail"), win(120, 60, "Code")]
    assert sessions(window, None) == [
        ("Code", "", 0, 60),
        ("Mail", "", 60, 60),
        ("Code", "", 120, 60),
    ]


def test_short_fragments_dropped_and_neighbours_rejoined() -> None:
    window = [win(0, 60, "Code"), win(60, 5, "Slack"), win(65, 60, "Code"), win(300, 10, "Mail")]
    assert sessions(window, None) == [("Code", "", 0, 125)]


def test_many_short_events_of_one_app_add_up() -> None:
    window = [win(i * 10, 10, "Code", f"file{i}.py") for i in range(6)]
    assert sessions(window, None) == [("Code", "", 0, 60)]
    assert sessions(window[:1], None) == []
    assert sessions(window[:1], None, min_seconds=5) == [("Code", "", 0, 10)]


def test_split_long() -> None:
    piece = st.Piece("Code", "", at(0), at(30 * 3600))
    chunks = st.split_long([piece])
    assert [c.seconds / 3600 for c in chunks] == [12, 12, 6]
    assert chunks[1].start == chunks[0].end


# -- what a run sends --------------------------------------------------------------


def test_select_for_sending() -> None:
    fetched, send_from, now = at(0), at(3600), at(7200)
    made = [
        st.Piece("A", "", at(0), at(4000)),  # starts at the fetch edge: true start unknown
        st.Piece("B", "", at(100), at(3000)),  # ended before the send window
        st.Piece("C", "", at(3000), at(3700)),  # crosses into the send window: sent whole
        st.Piece("D", "", at(5000), at(6000)),
        st.Piece("E", "", at(7100), at(7200)),  # too new: next run
    ]
    assert [p.app for p in st.select_for_sending(made, send_from, fetched, now)] == ["C", "D"]


def test_session_starts_are_stable_across_runs() -> None:
    """A later run, over a longer window, keeps every earlier session's start."""
    window = [win(i * 50, 50, "Code" if i % 7 else "Mail") for i in range(100)]
    away = [afk(0, 2000), afk(2000, 300, "afk"), afk(2300, 4000)]
    first = st.compute_sessions(window[:60], away, {}, at(-100), at(3000))
    later = st.compute_sessions(window, away, {}, at(-100), at(5000))
    starts = {(p.app, p.start) for p in later}
    assert all((p.app, p.start) in starts for p in first[:-1])


# -- records -----------------------------------------------------------------------


def test_wall_clock_with_offset() -> None:
    cst = timezone(timedelta(hours=-6))
    assert st.wall_clock(datetime(2026, 11, 21, 4, 10, 5, 999999, tzinfo=UTC), cst) == (
        "2026-11-20 22:10:05 -0600"
    )
    assert st.wall_clock(at(0), timezone(timedelta(hours=5, minutes=30))) == (
        "2026-11-20 23:30:00 +0530"
    )


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="needs time.tzset")
def test_wall_clock_uses_the_offset_of_each_instant(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TZ", "America/Chicago")
    time.tzset()
    try:
        summer = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
        assert st.wall_clock(summer) == "2026-07-01 07:00:00 -0500"
        assert st.wall_clock(at(0)) == "2026-11-20 12:00:00 -0600"
    finally:
        monkeypatch.undo()
        time.tzset()


def test_to_records_shape_and_privacy() -> None:
    utc = timezone.utc  # noqa: UP017
    pieces = [
        st.Piece("Dia", "youtube.com", at(0), at(90)),
        st.Piece("Mail", "", at(90), at(150)),
        st.Piece("Tiny", "", at(150), at(150.4)),
    ]
    records = st.to_records(pieces, utc)
    assert records == [
        {
            "device": "mac",
            "app": "Dia",
            "site": "youtube.com",
            "start": "2026-11-20 18:00:00 +0000",
            "end": "2026-11-20 18:01:30 +0000",
        },
        {
            "device": "mac",
            "app": "Mail",
            "start": "2026-11-20 18:01:30 +0000",
            "end": "2026-11-20 18:02:30 +0000",
        },
    ]
    assert "title" not in json.dumps(records)


def test_end_to_end_records_carry_no_titles_or_paths() -> None:
    window = [win(0, 300, "Dia", "Inbox (3) - private@example.com")]
    tabs = {"chromium": [web(0, 300, "https://mail.example.com/u/0/#inbox/abc")]}
    made = st.compute_sessions(window, None, tabs, at(-10), at(400))
    text = json.dumps(st.to_records(made))
    assert "mail.example.com" in text
    assert "Inbox" not in text and "inbox" not in text and "private@" not in text


def test_batches() -> None:
    items = list(range(4500))
    sizes = [len(b) for b in st.batches(items)]
    assert sizes == [2000, 2000, 500]
    assert list(st.batches([])) == []
    assert [b for b in st.batches(items[:3], 2)] == [[0, 1], [2]]


# -- buckets, config, state --------------------------------------------------------


def test_pick_buckets_prefers_latest_host() -> None:
    buckets = {
        "aw-watcher-window_old-mac": {"hostname": "old-mac", "last_updated": "2026-01-01"},
        "aw-watcher-window_mac": {"hostname": "mac", "last_updated": "2026-11-20"},
        "aw-watcher-afk_old-mac": {"hostname": "old-mac", "last_updated": "2026-11-21"},
        "aw-watcher-afk_mac": {"hostname": "mac", "last_updated": "2026-11-20"},
        "aw-watcher-web-chrome": {"hostname": "unknown"},
        "aw-watcher-web-safari": {"hostname": "mac"},
        "aw-watcher-web-firefox_phone": {"hostname": "phone"},
        "aw-stopwatch": {"hostname": "mac"},
    }
    window, away, tabs = st.pick_buckets(buckets)
    assert window == "aw-watcher-window_mac"
    assert away == "aw-watcher-afk_mac"
    assert tabs == {"chromium": ["aw-watcher-web-chrome"], "safari": ["aw-watcher-web-safari"]}


def test_pick_buckets_needs_a_window_watcher() -> None:
    with pytest.raises(LookupError):
        st.pick_buckets({"aw-watcher-afk_mac": {"hostname": "mac"}})
    window, away, tabs = st.pick_buckets({"aw-watcher-window_mac": {"hostname": "mac"}})
    assert (window, away, tabs) == ("aw-watcher-window_mac", None, {})


def test_load_config_env_over_file(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"DAYLOG_INLET_URL": "https://a.example/health/s1", "MIN_SECONDS": 5})
    )
    config = st.load_config({"MIN_SECONDS": "30"}, path)
    assert config["inlet_url"] == "https://a.example/health/s1"
    assert config["min_seconds"] == 30
    assert config["aw_url"] == "http://localhost:5600"
    assert config["lookback_hours"] == 26
    assert st.load_config({}, tmp_path / "missing.json")["inlet_url"] == ""
    with pytest.raises(ValueError):
        st.load_config({"DAYLOG_INLET_URL": "a.example/health/s1"}, path)


def test_host_of_hides_the_secret() -> None:
    assert st.host_of("https://daylog.example.com/health/s3cr3t") == "daylog.example.com"


def test_state_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    assert st.load_state(path) is None
    st.save_state(at(42), path)
    assert st.load_state(path) == at(42)
    path.write_text("{broken")
    assert st.load_state(path) is None


def test_dry_run_without_activitywatch_fails_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AW_URL", "http://127.0.0.1:9")
    monkeypatch.setattr(st, "CONFIG_FILE", Path("/nonexistent/config.json"))
    monkeypatch.setattr(st, "STATE_FILE", Path("/nonexistent/state.json"))
    assert st.main(["--dry-run"]) == 1


def test_missing_inlet_url_is_a_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DAYLOG_INLET_URL", raising=False)
    monkeypatch.setattr(st, "CONFIG_FILE", Path("/nonexistent/config.json"))
    assert st.main([]) == 2
