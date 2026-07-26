"""Hand-rolled pandas indicators, session-aware for intraday use.

All are look-ahead-safe: the value at bar t uses only bars <= t.

Note on NaN: we use np.nan, never pd.NA. pd.NA upcasts a float column to
object dtype, and a downstream `&`/`~` on the resulting comparison raises
"boolean value of NA is ambiguous" -- which a single zero-volume bar (yfinance
emits them on thin names) would otherwise turn into a whole-cycle outage.
"""
import numpy as np
import pandas as pd

from . import config


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def session(df: pd.DataFrame) -> pd.Series:
    """Session key (local trading date) per bar."""
    return pd.Series(df.index.date, index=df.index)


def ema_session(df: pd.DataFrame, period: int = 9, col: str = "Close") -> pd.Series:
    """EMA restarted each session. A whole-frame EMA still carries ~21% of
    yesterday's price at the 10:00 bar (0.8^7), which is exactly the overnight
    gap contamination an intraday pullback level must not inherit."""
    return df[col].groupby(session(df), group_keys=False).apply(lambda s: ema(s, period))


def atr(df: pd.DataFrame, period: int = config.ATR_PERIOD) -> pd.Series:
    """Wilder's ATR, with the overnight gap excluded.

    On each session's first bar prev_close is yesterday's close, so the raw
    true range would absorb the entire gap -- inflating ATR at precisely the
    bars the opening-range setups use for sizing. There, TR = High - Low.
    """
    prev = df["Close"].shift(1)
    first_bar = session(df) != session(df).shift(1)
    prev = prev.where(~first_bar, np.nan)
    tr = pd.concat([
        df["High"] - df["Low"],
        (df["High"] - prev).abs(),
        (df["Low"] - prev).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def session_vwap(df: pd.DataFrame) -> pd.Series:
    """Cumulative typical-price VWAP, restarted each session."""
    s = session(df)
    tp = (df["High"] + df["Low"] + df["Close"]) / 3
    pv = (tp * df["Volume"]).groupby(s).cumsum()
    v = df["Volume"].groupby(s).cumsum()
    return pv / v.replace(0, np.nan)


def rvol_tod(df: pd.DataFrame, min_samples: int = config.RVOL_MIN_SAMPLES,
             lookback: int = config.RVOL_LOOKBACK_SESSIONS) -> pd.Series:
    """Bar volume / mean volume of the SAME time-of-day slot on prior sessions.

    Within a slot group, shift(1) drops the current bar before the rolling
    mean, so the baseline is strictly prior sessions (leak-safe). min_samples
    keeps a ratio-against-one-observation from being treated as a signal.

    The baseline is a fixed `lookback`-session window rather than an expanding
    mean so that the value at a given bar does not depend on how much history
    the caller happened to load -- that is what keeps the live scan and the
    backtest computing the same number.
    """
    slot = pd.Series(df.index.strftime("%H:%M"), index=df.index)
    grp = df["Volume"].groupby(slot)
    baseline = grp.transform(
        lambda x: x.shift(1).rolling(lookback, min_periods=min_samples).mean())
    return df["Volume"] / baseline.replace(0, np.nan)
