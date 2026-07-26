"""Hand-rolled pandas indicators. ema/atr from mrm-momentum; vwap/rvol are
session-aware intraday versions. All look-ahead-safe: bar t uses bars <= t only.
"""
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    prev = close.shift(1)
    tr = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def session(df: pd.DataFrame) -> pd.Series:
    """Session key (trading date) per bar."""
    return pd.Series(df.index.date, index=df.index)


def session_vwap(df: pd.DataFrame) -> pd.Series:
    """Cumulative typical-price VWAP, restarted each session."""
    s = session(df)
    tp = (df["High"] + df["Low"] + df["Close"]) / 3
    pv = (tp * df["Volume"]).groupby(s).cumsum()
    v = df["Volume"].groupby(s).cumsum()
    return pv / v.replace(0, pd.NA)


def rvol_tod(df: pd.DataFrame) -> pd.Series:
    """Bar volume / expanding mean of the SAME time-of-day slot on prior sessions.

    Within a slot group, shift(1) is the previous session's bar at that time, so
    the baseline never includes the current bar (leak-safe).
    """
    slot = pd.Series(df.index.strftime("%H:%M"), index=df.index)
    baseline = df["Volume"].groupby(slot).transform(lambda x: x.shift(1).expanding().mean())
    return df["Volume"] / baseline.replace(0, pd.NA)
