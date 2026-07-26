"""Intraday long setups. Registry pattern from mrm-momentum's research/signals.py.

Contract: each signal is (df, **params) -> bool Series over the bars of an
ENRICHED frame (OHLCV + VWAP, RVOL, EMA9 columns added by scanner.enrich).
Look-ahead-safe: the value at bar t depends only on bars <= t. Levels fns
return (entry, stop) for the frame's last session.
"""
from dataclasses import dataclass
from datetime import time
from typing import Callable

import pandas as pd

from .indicators import session

OR_BARS = 6  # 30-minute opening range on 5m bars


def _bar_n(df: pd.DataFrame) -> pd.Series:
    s = session(df)
    return s.groupby(s).cumcount()


def _first_in_session(mask: pd.Series, df: pd.DataFrame) -> pd.Series:
    return mask & (mask.groupby(session(df)).cumsum() == 1)


def _fresh(mask: pd.Series) -> pd.Series:
    """Only False->True transitions fire (debounce)."""
    return mask & ~mask.shift(1, fill_value=False)


def _today(df: pd.DataFrame) -> pd.DataFrame:
    return df[df.index.date == df.index[-1].date()]


# ---------------------------------------------------------------- signals

def orb_breakout(df, or_bars=OR_BARS, rvol_min=2.0):
    """First close above the 30-min opening-range high, strong RVOL, above VWAP."""
    n = _bar_n(df)
    or_high = df["High"].where(n < or_bars).groupby(session(df)).transform("max")
    raw = ((n >= or_bars) & (df["Close"] > or_high)
           & (df["RVOL"] >= rvol_min) & (df["Close"] > df["VWAP"]))
    return _first_in_session(raw, df)


def vwap_reclaim(df, below_bars=3, rvol_min=1.5):
    """Close crosses back above session VWAP after >= below_bars bars below."""
    below = df["Close"] < df["VWAP"]
    was_below = below.shift(1).rolling(below_bars).sum() == below_bars
    raw = ((df["Close"] > df["VWAP"]) & was_below
           & (_bar_n(df) >= below_bars) & (df["RVOL"] >= rvol_min))
    return _fresh(raw)


def hod_breakout(df, rvol_min=1.5):
    """Close above prior high-of-day after 10:00 ET on elevated RVOL."""
    s = session(df)
    prior_hod = df["High"].groupby(s).cummax().groupby(s).shift(1)
    after_open = pd.Series(df.index.time, index=df.index) >= time(10, 0)
    raw = ((df["Close"] > prior_hod) & after_open
           & (df["RVOL"] >= rvol_min) & (df["Close"] > df["VWAP"]))
    return _fresh(raw)


def gap_go(df, gap_min=0.02, rvol_min=1.5):
    """Gap up >= gap_min vs prior close, holding above open, breaks first-bar high."""
    s = session(df)
    n = _bar_n(df)
    prev_close = s.map(df["Close"].groupby(s).last().shift(1))
    sess_open = df["Open"].groupby(s).transform("first")
    first_high = df["High"].where(n == 0).groupby(s).transform("max")
    raw = ((sess_open / prev_close - 1 >= gap_min) & (n >= 1)
           & (df["Close"] > sess_open) & (df["Close"] > first_high)
           & (df["RVOL"] >= rvol_min))
    return _first_in_session(raw, df)


def ema9_vwap_pullback(df, rvol_min=1.5):
    """Uptrend (close & EMA9 above VWAP), pullback tags EMA9 but holds above VWAP,
    bounce closes back above EMA9. After 10:00 only -- early EMA9 is gap-polluted."""
    up = (df["Close"] > df["VWAP"]) & (df["EMA9"] > df["VWAP"])
    tagged = (df["Low"].shift(1) <= df["EMA9"].shift(1)) & (df["Low"].shift(1) > df["VWAP"].shift(1))
    after_open = pd.Series(df.index.time, index=df.index) >= time(10, 0)
    raw = up & tagged & after_open & (df["Close"] > df["EMA9"]) & (df["RVOL"] >= rvol_min)
    return _fresh(raw)


# ---------------------------------------------------------------- levels

def _orb_levels(df):
    t = _today(df)
    oh, ol = t["High"].iloc[:OR_BARS].max(), t["Low"].iloc[:OR_BARS].min()
    return float(oh), float((oh + ol) / 2)


def _reclaim_levels(df):
    return float(df["Close"].iloc[-1]), float(df["Low"].iloc[-1])


def _hod_levels(df):
    return float(df["Close"].iloc[-1]), float(df["VWAP"].iloc[-1])


def _gap_levels(df):
    return float(df["Close"].iloc[-1]), float(_today(df)["Low"].iloc[0])


def _pullback_levels(df):
    t = _today(df)
    return float(t["Close"].iloc[-1]), float(t["Low"].iloc[-3:].min())


# ---------------------------------------------------------------- registry

@dataclass(frozen=True)
class SignalSpec:
    fn: Callable[..., pd.Series]
    defaults: dict
    levels: Callable[[pd.DataFrame], tuple[float, float]]


REGISTRY: dict[str, SignalSpec] = {
    "orb_breakout": SignalSpec(orb_breakout, {}, _orb_levels),
    "vwap_reclaim": SignalSpec(vwap_reclaim, {}, _reclaim_levels),
    "hod_breakout": SignalSpec(hod_breakout, {}, _hod_levels),
    "gap_go": SignalSpec(gap_go, {}, _gap_levels),
    "ema9_vwap_pullback": SignalSpec(ema9_vwap_pullback, {}, _pullback_levels),
}
