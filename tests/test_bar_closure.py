"""The forming-bar contract.

Yahoo labels 5m bars by interval START, so a bar is only complete at ts+5min.
Judging index[-1] mid-interval means judging ~40s of tape: it suppressed the
RVOL gate ~75x and halved every stop. These tests pin the fix at both the
fetch boundary and the scan boundary.
"""
import pandas as pd

from engine import config, data, scanner
from tests.conftest import make_bars


def test_complete_bars_drops_the_forming_bar():
    df = make_bars([10.0, 11.0, 12.0], start="2026-07-20 09:30")
    # 09:40 bar covers [09:40, 09:45); at 09:44 it is still forming
    now = pd.Timestamp("2026-07-20 09:44", tz=config.ET)
    kept = data.complete_bars(df, now)
    assert list(kept.index.strftime("%H:%M")) == ["09:30", "09:35"]
    # one minute later it has closed
    kept = data.complete_bars(df, pd.Timestamp("2026-07-20 09:45", tz=config.ET))
    assert list(kept.index.strftime("%H:%M")) == ["09:30", "09:35", "09:40"]


def test_last_closed_bar():
    now = pd.Timestamp("2026-07-20 10:01:30", tz=config.ET)
    assert scanner.last_closed_bar(now) == pd.Timestamp("2026-07-20 09:55", tz=config.ET)
    exact = pd.Timestamp("2026-07-20 10:00:00", tz=config.ET)
    assert scanner.last_closed_bar(exact) == pd.Timestamp("2026-07-20 09:55", tz=config.ET)


def test_fetch_never_returns_a_forming_bar(monkeypatch):
    df = make_bars([10.0, 11.0, 12.0], start="2026-07-20 09:30")
    raw = pd.concat({"AAA": df}, axis=1)
    monkeypatch.setattr(data.yf, "download", lambda *a, **k: raw)
    now = pd.Timestamp("2026-07-20 09:44", tz=config.ET)
    frames = data.fetch_intraday(["AAA"], now=now)
    assert frames["AAA"].index[-1] == pd.Timestamp("2026-07-20 09:35", tz=config.ET)


def test_flat_frame_for_many_tickers_is_discarded(monkeypatch):
    """Yahoo flattens when all but one ticker fails; broadcasting that one
    frame across the universe would fabricate 98 identical tickers."""
    monkeypatch.setattr(data.yf, "download",
                        lambda *a, **k: make_bars([10.0, 11.0, 12.0]))
    assert data.fetch_intraday(["AAA", "BBB"], now=pd.Timestamp.now(tz=config.ET)) == {}


def test_scan_skips_tickers_without_a_bar_at_as_of(frames):
    as_of = max(df.index[-1] for df in frames.values())
    stale = {t: (df.iloc[:-3] if i == 0 else df)
             for i, (t, df) in enumerate(frames.items())}
    lagging = list(frames)[0]
    assert all(a.ticker != lagging for a in scanner.scan_once(stale, as_of))


def test_scan_is_invariant_to_future_bars(frames):
    """Structural leakage test: passing full frames must equal passing frames
    pre-truncated at as_of. Covers stops and the cross-sectional path, which
    the per-signal truncation test cannot reach."""
    all_ts = sorted({ts for df in frames.values() for ts in df.index})
    for as_of in all_ts[-40::7]:
        full = scanner.scan_once(frames, as_of)
        cut = scanner.scan_once({t: df[df.index <= as_of] for t, df in frames.items()}, as_of)
        assert [ (a.ticker, a.signals, a.price, a.stop) for a in full ] == \
               [ (a.ticker, a.signals, a.price, a.stop) for a in cut ]
