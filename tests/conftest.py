from pathlib import Path

import pandas as pd
import pytest

from engine import config
from engine.data import OHLCV

FIXTURE = Path(__file__).parent / "fixtures" / "bars_5m.parquet"


@pytest.fixture(scope="session")
def frames() -> dict[str, pd.DataFrame]:
    df = pd.read_parquet(FIXTURE)
    out = {}
    for t, g in df.groupby("ticker"):
        f = g.set_index("ts")[OHLCV].sort_index()
        f.index = pd.DatetimeIndex(f.index).tz_convert(config.ET)
        out[t] = f
    return out
