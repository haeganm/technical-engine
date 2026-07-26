"""Intraday long setups.

Contract: each signal is `(df, **params) -> bool Series` over an ENRICHED frame
(OHLCV plus VWAP/RVOL/EMA9/ATR, added by scanner.enrich). Look-ahead-safe: the
value at bar t depends only on bars <= t.

Signals describe *structure only*. Volume, trend and confidence conditions live
in scanner's central gate and score, so a setup is never silently consumed by a
condition it doesn't own. Each spec's `stop` returns the raw structural stop for
the frame's last bar; scanner clamps it to a sane ATR band.
"""
from collections.abc import Callable
from dataclasses import dataclass
from datetime import time

import pandas as pd

from . import config
from .indicators import session


def _bar_n(df: pd.DataFrame) -> pd.Series:
    s = session(df)
    return s.groupby(s).cumcount()


def _fresh(mask: pd.Series) -> pd.Series:
    """Only False->True transitions fire (debounce within a run).

    Deliberately NOT once-per-session: that belongs to the dedupe layer, which
    keys on (date, ticker) and survives restarts. Suppressing here would let a
    gate-rejected bar burn the session's only chance to alert.
    """
    return mask & ~mask.shift(1, fill_value=False)


def _after(df: pd.DataFrame, hh: int, mm: int) -> pd.Series:
    return pd.Series(df.index.time, index=df.index) >= time(hh, mm)


def _today(df: pd.DataFrame) -> pd.DataFrame:
    return df[df.index.date == df.index[-1].date()]


def prev_close_series(df: pd.DataFrame,
                      min_bars: int = config.PREV_SESSION_MIN_BARS) -> pd.Series:
    """Per-bar prior-session close; NaN where the prior session is too partial
    to trust. A truncated prior day would otherwise silently shift 'up on the
    day' and turn gap_go's gap into a two-day move.

    Computed per session (not just for the frame's last one) so the result is
    slice-invariant, which is what lets the scanner precompute it once.
    """
    s = session(df)
    counts = s.value_counts()
    last = df["Close"].groupby(s).last()
    days = sorted(last.index)
    prev = {
        d: float(last[days[i - 1]])
        for i, d in enumerate(days)
        if i and counts[days[i - 1]] >= min_bars
    }
    return s.map(prev).astype(float)


def prev_session_close(df: pd.DataFrame) -> float | None:
    """Prior-session close for the frame's last bar, or None if untrustworthy."""
    v = prev_close_series(df).iloc[-1]
    return None if pd.isna(v) else float(v)


# ---------------------------------------------------------------- signals

def orb_breakout(df, or_bars=config.ORB_BARS):
    """First close above the 30-minute opening-range high."""
    n = _bar_n(df)
    or_high = df["High"].where(n < or_bars).groupby(session(df)).transform("max")
    return _fresh((n >= or_bars) & (df["Close"] > or_high))


def vwap_reclaim(df, below_bars=config.RECLAIM_BELOW_BARS):
    """Close crosses back above session VWAP after >= below_bars bars below."""
    below = df["Close"] < df["VWAP"]
    was_below = below.shift(1).rolling(below_bars).sum() == below_bars
    return _fresh((df["Close"] > df["VWAP"]) & was_below & (_bar_n(df) >= below_bars))


def hod_breakout(df):
    """Close above the prior high-of-day, after 10:00 ET."""
    s = session(df)
    prior_hod = df["High"].groupby(s).cummax().groupby(s).shift(1)
    return _fresh((df["Close"] > prior_hod) & _after(df, 10, 0))


def gap_go(df, gap_min=config.GAP_MIN, min_prev_bars=config.PREV_SESSION_MIN_BARS):
    """Gap up >= gap_min vs the prior close, holding above the open, breaking
    the first bar's high. Silent when the prior session is missing/partial
    (NaN prev close makes the comparison False)."""
    s, n = session(df), _bar_n(df)
    sess_open = df["Open"].groupby(s).transform("first")
    first_high = df["High"].where(n == 0).groupby(s).transform("max")
    gap = sess_open / prev_close_series(df, min_bars=min_prev_bars) - 1
    return _fresh((gap >= gap_min) & (n >= 1)
                  & (df["Close"] > sess_open) & (df["Close"] > first_high))


def ema9_vwap_pullback(df):
    """Uptrend (close & EMA9 above VWAP), pullback tags EMA9 but holds VWAP,
    bounce closes back above EMA9. After 10:00 only."""
    up = (df["Close"] > df["VWAP"]) & (df["EMA9"] > df["VWAP"])
    prev_low, prev_ema, prev_vwap = df["Low"].shift(1), df["EMA9"].shift(1), df["VWAP"].shift(1)
    tagged = (prev_low <= prev_ema) & (prev_low > prev_vwap)
    return _fresh(up & tagged & _after(df, 10, 0) & (df["Close"] > df["EMA9"]))


# ---------------------------------------------------------------- stops

def _orb_stop(df, or_bars=config.ORB_BARS) -> float:
    t = _today(df)
    return float((t["High"].iloc[:or_bars].max()
                  + t["Low"].iloc[:or_bars].min()) / 2)


def _reclaim_stop(df) -> float:
    return float(df["Low"].iloc[-1])


def _hod_stop(df) -> float:
    return float(df["VWAP"].iloc[-1])


def _gap_stop(df) -> float:
    return float(_today(df)["Low"].iloc[0])


def _pullback_stop(df) -> float:
    return float(_today(df)["Low"].iloc[-3:].min())


# ---------------------------------------------------------------- context

def _orb_context(df, or_bars=config.ORB_BARS) -> str:
    t = _today(df)
    return f"OR high {t['High'].iloc[:or_bars].max():.2f}"


def _reclaim_context(df) -> str:
    return f"VWAP {df['VWAP'].iloc[-1]:.2f}"


def _hod_context(df) -> str:
    s = session(df)
    prior = df["High"].groupby(s).cummax().groupby(s).shift(1)
    return f"prior HOD {prior.iloc[-1]:.2f}"


def _gap_context(df) -> str:
    prev = prev_session_close(df)
    t = _today(df)
    gap = t["Open"].iloc[0] / prev - 1 if prev else 0.0
    return f"gap +{gap:.1%}"


def _pullback_context(df) -> str:
    return f"EMA9 {df['EMA9'].iloc[-1]:.2f}"


# ---------------------------------------------------------------- registry

@dataclass(frozen=True)
class SignalSpec:
    fn: Callable[..., pd.Series]
    stop: Callable[[pd.DataFrame], float]
    context: Callable[[pd.DataFrame], str]


REGISTRY: dict[str, SignalSpec] = {
    "orb_breakout": SignalSpec(orb_breakout, _orb_stop, _orb_context),
    "vwap_reclaim": SignalSpec(vwap_reclaim, _reclaim_stop, _reclaim_context),
    "hod_breakout": SignalSpec(hod_breakout, _hod_stop, _hod_context),
    "gap_go": SignalSpec(gap_go, _gap_stop, _gap_context),
    "ema9_vwap_pullback": SignalSpec(ema9_vwap_pullback, _pullback_stop, _pullback_context),
}
