"""End-to-end: fixture bars through scan_once, plus a synthetic frame that must fire."""
import numpy as np
import pandas as pd

from engine import config
from engine.scanner import scan_once


def test_scan_fixture_runs_clean(frames):
    alerts = scan_once(frames)
    for a in alerts:
        assert a.entry > a.stop
        assert a.target_2r > a.target_1r > a.entry
        assert a.rvol >= config.RVOL_MIN_ABS


def _synthetic_orb() -> pd.DataFrame:
    """Two sessions; day 2 breaks the 30-min opening range on 10x volume at bar 10."""
    idx = []
    for day in ("2026-07-20", "2026-07-21"):
        idx += list(pd.date_range(f"{day} 09:30", periods=20, freq="5min", tz=config.ET))
    n = len(idx)
    close = np.full(n, 100.0)
    close[20:26] = 100.5            # day-2 opening range
    close[30:] = 102.0              # breakout at bar 10 of day 2
    high, low = close + 0.3, close - 0.3
    vol = np.full(n, 1000.0)
    vol[30:] = 10000.0
    return pd.DataFrame(
        {"Open": close, "High": high, "Low": low, "Close": close, "Volume": vol},
        index=pd.DatetimeIndex(idx))


def test_synthetic_orb_fires():
    df = _synthetic_orb().iloc[:31]  # end exactly on the breakout bar
    alerts = scan_once({"FAKE": df})
    assert [a.signal for a in alerts] == ["orb_breakout"]
    a = alerts[0]
    assert a.ticker == "FAKE" and a.entry > a.stop and a.rvol >= 2
