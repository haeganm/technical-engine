"""Interval-profile plumbing: the 1h tier must be a real second universe,
not a fork of the code path."""
import numpy as np
import pandas as pd

from engine import config, data, scanner
from engine.indicators import ema_session
from engine.scanner import scan_once

P1H = config.INTERVALS["1h"]


def make_hourly(closes, highs=None, lows=None, volumes=None, sessions=1):
    """7-bar hourly sessions: 09:30, 10:30, ... 15:30 ET."""
    per = 7
    idx = []
    for s in range(sessions):
        base = pd.Timestamp("2026-07-20 09:30", tz=config.ET) + pd.Timedelta(days=s)
        idx += list(pd.date_range(base, periods=per, freq="60min"))
    idx = idx[:len(closes)]
    n = len(idx)
    return pd.DataFrame({
        "Open": closes[:n],
        "High": highs[:n] if highs else [c + 0.5 for c in closes[:n]],
        "Low": lows[:n] if lows else [c - 0.5 for c in closes[:n]],
        "Close": closes[:n],
        "Volume": volumes[:n] if volumes else [1000.0] * n,
    }, index=pd.DatetimeIndex(idx))


def _hourly_breakout(sessions=12):
    # > RVOL_LOOKBACK_SESSIONS, else the RVOL baseline is NaN and nothing gates through
    """Final bar breaks the first-hour range on huge volume."""
    closes, vols = [], []
    for s in range(sessions):
        c = [100.0] * 7
        v = [1000.0] * 7
        if s == sessions - 1:
            c = [100.0, 103.0, 103.0, 103.0, 103.0, 103.0, 103.0]
            v = [1000.0, 20000.0, 20000.0, 20000.0, 20000.0, 20000.0, 20000.0]
        closes += c
        vols += v
    return make_hourly(closes, volumes=vols, sessions=sessions)


def test_orb_fires_with_one_bar_opening_range():
    df = _hourly_breakout()
    # judge the first bar after the opening hour of the last session
    as_of = df.index[-6]
    alerts = scan_once({"AAA": df}, as_of, min_score=0, profile=P1H)
    assert any(a.signal == "orb_breakout" for a in alerts)


def test_5m_profile_orb_stays_inside_its_six_bar_window():
    """With orb_bars=6, a 7-bar session's bar-1 breakout is still inside the
    opening-range window, so specifically orb_breakout must not fire."""
    df = _hourly_breakout()
    as_of = df.index[-6]
    alerts = scan_once({"AAA": df}, as_of, min_score=0)
    assert all("orb_breakout" not in a.signals for a in alerts)


def test_ema_period_4_warms_up_inside_an_hourly_session():
    df = make_hourly([100.0] * 14, sessions=2)
    e9 = ema_session(df, 9)
    e4 = ema_session(df, 4)
    assert e9.isna().all(), "a 9-bar EMA never exists in a 7-bar session"
    assert np.isfinite(e4.iloc[-1]), "the 1h profile's EMA must reach warm-up"


def test_1h_store_is_separate(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA", tmp_path)
    df = make_hourly([100.0] * 7)
    data.store_bars({"AAA": df}, now=df.index[-1] + pd.Timedelta(hours=1),
                    store=P1H["store"], keep_days=P1H["keep_days"])
    assert data.load_frames(store=P1H["store"])["AAA"].shape == df.shape
    assert data.load_frames(store="bars") == {}
    assert data.store_sessions(P1H["store"]) == 1


def test_prepared_carries_its_profile(frames):
    prep = scanner.prepare(dict(list(frames.items())[:1]), P1H)
    assert next(iter(prep.values())).profile is P1H
