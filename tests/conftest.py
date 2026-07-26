from pathlib import Path

import pandas as pd
import pytest

from engine import config
from engine.data import OHLCV

FIXTURE = Path(__file__).parent / "fixtures" / "bars_5m.parquet"


def frames_from(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    out = {}
    for t, g in df.groupby("ticker"):
        f = g.set_index("ts")[OHLCV].sort_index()
        f.index = pd.DatetimeIndex(f.index).tz_convert(config.ET)
        out[t] = f
    return out


@pytest.fixture(scope="session")
def bars() -> pd.DataFrame:
    return pd.read_parquet(FIXTURE)


@pytest.fixture(scope="session")
def frames(bars) -> dict[str, pd.DataFrame]:
    return frames_from(bars)


@pytest.fixture(scope="session")
def prep(frames):
    """Precomputed frames, the same path the backtest uses. Reused across tests
    so the suite doesn't re-enrich the fixture hundreds of times."""
    from engine import scanner
    return scanner.prepare(frames)


@pytest.fixture(scope="session")
def all_ts(frames):
    return sorted({ts for df in frames.values() for ts in df.index})


def make_bars(closes, highs=None, lows=None, volumes=None, opens=None,
              start="2026-07-20 09:30", sessions=1, per_session=None):
    """Build a synthetic ET-indexed 5m OHLCV frame from close prices."""
    n = len(closes)
    per_session = per_session or n // sessions
    idx = []
    day = pd.Timestamp(start, tz=config.ET)
    for s in range(sessions):
        base = day + pd.Timedelta(days=s)
        idx += list(pd.date_range(base, periods=per_session, freq="5min"))
    idx = idx[:n]
    return pd.DataFrame({
        "Open": opens if opens is not None else closes,
        "High": highs if highs is not None else [c + 0.3 for c in closes],
        "Low": lows if lows is not None else [c - 0.3 for c in closes],
        "Close": closes,
        "Volume": volumes if volumes is not None else [1000.0] * n,
    }, index=pd.DatetimeIndex(idx))
