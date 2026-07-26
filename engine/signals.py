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


def _bollinger(df, bars: int):
    """Session-scoped Bollinger mid/sd. Grouping by session matters: an
    un-grouped window makes a session's first bars use a band built entirely
    from yesterday, which turns the squeeze into an overnight-gap detector."""
    s = session(df)
    grp = df["Close"].groupby(s)
    mid = grp.transform(lambda x: x.rolling(bars, min_periods=bars).mean())
    sd = grp.transform(lambda x: x.rolling(bars, min_periods=bars).std())
    return mid, sd


def _bb_width(df, bars=config.SQUEEZE_BARS) -> pd.Series:
    """Bollinger band width (2 sigma, as a fraction of the mid)."""
    mid, sd = _bollinger(df, bars)
    return (4 * sd) / mid


def squeeze_breakout(df, bars=config.SQUEEZE_BARS, pctile=config.SQUEEZE_PCTILE):
    """Volatility contraction then expansion: band width in the bottom quartile
    of THIS SESSION's widths so far, then the close breaks the prior bar's upper
    band.

    Everything is session-scoped, which buys two things: the band can never be
    yesterday's (see _bollinger), and an expanding within-session rank is
    parity-safe by construction -- live and backtest both always hold the whole
    session up to as_of, so they rank identically without needing a fixed window.
    """
    s = session(df)
    mid, sd = _bollinger(df, bars)
    width = (4 * sd) / mid
    # shift(1): the coil must precede the break, and the breakout bar (whose own
    # width is already expanding) never ranks itself
    rank = width.groupby(s).transform(lambda x: x.shift(1).expanding().rank(pct=True))
    upper = (mid + 2 * sd).groupby(s).shift(1)
    return _fresh((rank <= pctile) & (df["Close"] > upper))


def momentum_thrust(df, bars=config.THRUST_BARS):
    """N consecutive higher closes, all above VWAP, on expanding range --
    catches opening drives that never pull back for the other setups."""
    s = session(df)
    higher = df["Close"] > df["Close"].shift(1)
    streak = higher.rolling(bars, min_periods=bars).sum() == bars
    above = (df["Close"] > df["VWAP"]).rolling(bars, min_periods=bars).sum() == bars
    rng = df["High"] - df["Low"]
    # within-session baseline: parity-safe, and an overnight gap never sets the bar
    typical = rng.groupby(s).transform(lambda x: x.shift(1).expanding().median())
    # bar_n guard keeps the streak inside the session (no overnight carry)
    return _fresh(streak & above & (rng > typical) & (_bar_n(df) >= bars))


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


def _squeeze_stop(df, bars=config.SQUEEZE_BARS) -> float:
    # today's coil only -- a stop reaching into yesterday is a different trade
    return float(_today(df)["Low"].iloc[-bars:].min())


def _thrust_stop(df, bars=config.THRUST_BARS) -> float:
    return float(_today(df)["Low"].iloc[-bars:].min())


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


def _squeeze_context(df, bars=config.SQUEEZE_BARS) -> str:
    return f"band width {_bb_width(df, bars).iloc[-1]:.2%}"


def _thrust_context(df, bars=config.THRUST_BARS) -> str:
    t = df["Close"].iloc[-1] / df["Close"].iloc[-bars - 1] - 1
    return f"thrust +{t:.2%}"


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
    "squeeze_breakout": SignalSpec(squeeze_breakout, _squeeze_stop, _squeeze_context),
    "momentum_thrust": SignalSpec(momentum_thrust, _thrust_stop, _thrust_context),
}
