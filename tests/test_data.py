"""Store round-trip, pruning, and the live/backtest equivalence proof."""
import pandas as pd
import pytest

from engine import config, data, journal, notify, scanner
from tests.conftest import make_bars


@pytest.fixture(autouse=True)
def tmp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA", tmp_path)
    yield tmp_path


def test_store_round_trip(frames):
    small = dict(list(frames.items())[:3])
    data.store_bars(small, now=pd.Timestamp("2026-07-24 16:00", tz=config.ET))
    back = data.load_frames()
    assert set(back) == set(small)
    for t in small:
        pd.testing.assert_frame_equal(back[t], small[t], check_freq=False)


def test_live_keep_days_never_prunes_the_archive():
    """Regression: `store_bars(frames)` bare took a 60-day default while the 5m
    profile said 3650, so the first live cycle deleted 19 irreplaceable sessions.
    The profile is now the single source of truth."""
    assert config.KEEP_DAYS == config.INTERVALS["5m"]["keep_days"] == 3650
    assert config.ORB_BARS == config.INTERVALS["5m"]["orb_bars"]
    assert config.PREV_SESSION_MIN_BARS == config.INTERVALS["5m"]["prev_session_min_bars"]


def test_store_prunes_only_beyond_the_profile_window(frames):
    now = pd.Timestamp("2026-07-24 16:00", tz=config.ET)
    data.store_bars(frames, now=now)
    before = data.store_sessions()
    assert before > 0
    # a year later the fixture is still inside the 3650-day window
    data.store_bars(frames, now=now + pd.Timedelta(days=365))
    assert data.store_sessions() == before
    # only an absurd horizon prunes it
    data.store_bars(frames, now=now + pd.Timedelta(days=config.KEEP_DAYS + 1))
    assert data.store_sessions() == 0


def test_empty_fetch_does_not_rewrite_or_prune(frames):
    """During an outage the archive must not erode: an empty fetch is a no-op."""
    now = pd.Timestamp("2026-07-24 16:00", tz=config.ET)
    n = data.store_bars(frames, now=now)
    sessions = data.store_sessions()
    # far past the retention horizon, but with nothing new there is nothing to do
    assert data.store_bars({}, now=now + pd.Timedelta(days=99999)) == n
    assert data.store_sessions() == sessions


def test_load_frames_since_bounds_live_history(frames):
    """The store is never pruned, so the live path must bound how far back it
    loads or every cycle gets slower forever."""
    now = max(df.index[-1] for df in frames.values())
    data.store_bars(frames, now=now)
    full = data.load_frames()
    recent = data.load_frames(since=now - pd.Timedelta(days=2))
    assert full and recent
    assert sum(len(d) for d in recent.values()) < sum(len(d) for d in full.values())
    for d in recent.values():
        assert d.index.min() >= now - pd.Timedelta(days=2)


def test_merge_save_dedupes_on_key(frames):
    one = dict(list(frames.items())[:1])
    now = pd.Timestamp("2026-07-24 16:00", tz=config.ET)
    n1 = data.store_bars(one, now=now)
    n2 = data.store_bars(one, now=now)      # same data twice
    assert n1 == n2


def test_single_ticker_download_is_not_multiindex(monkeypatch):
    df = make_bars([10.0, 11.0, 12.0])
    monkeypatch.setattr(data.yf, "download", lambda *a, **k: df)
    frames = data.fetch_intraday(["AAA"], now=pd.Timestamp("2026-07-20 10:00", tz=config.ET))
    assert list(frames) == ["AAA"] and len(frames["AAA"]) == 3


def test_backtest_matches_live_bar_by_bar(frames, all_ts):
    """The equivalence proof: replaying the store must produce exactly what a
    live run would have produced, bar for bar.

    Backtest precomputes once over the whole frame; live re-scans a store that
    grows one bar at a time and never contains a forming bar. Both must agree.
    """
    window = all_ts[-25:]
    data.store_bars(frames, now=all_ts[-1])

    prep = scanner.prepare(data.load_frames())
    backtest, sent_b = [], set()
    for as_of in window:
        backtest += notify.dispatch(scanner.scan_prepared(prep, as_of), sent_b, lambda b: b)

    live, sent_l = [], set()
    for as_of in window:
        # what the fetch would have returned at the moment the loop wakes
        now = as_of + pd.Timedelta(minutes=config.BAR_MINUTES, seconds=config.WAKE_LAG_S)
        served = {t: data.complete_bars(df, now) for t, df in frames.items()}
        assert all(df.index[-1] <= as_of for df in served.values() if len(df))
        live += notify.dispatch(
            scanner.scan_once(served, scanner.last_closed_bar(now)), sent_l, lambda b: b)

    def key(a):
        return (a.ticker, a.ts, a.signals, a.price, a.stop, a.score)

    assert [key(a) for a in backtest] == [key(a) for a in live]


def test_journal_replay_is_separate_from_live(frames):
    a = scanner.Alert("AAA", ("orb_breakout",), 3,
                      pd.Timestamp("2026-07-24 10:00", tz=config.ET),
                      100.0, 99.0, 101.0, 102.0, 2.5, 0.4, 0.01)
    journal.append([a], journal.LIVE)
    journal.append([a], journal.REPLAY)
    journal.clear(journal.REPLAY)
    assert data.load(journal.REPLAY) is None
    assert len(data.load(journal.LIVE)) == 1
