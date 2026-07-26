"""Judge one closed bar across the universe and emit alerts.

`scan_once(frames, as_of)` is the single scan path -- live and backtest both
call it with the same shape of input, so a replayed result is what live would
have produced. It enforces its own precondition: every frame is truncated to
`as_of`, and a ticker without a bar exactly at `as_of` is skipped rather than
judged on a stale one.
"""
import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import config
from .indicators import atr, ema_session, rvol_tod, session_vwap
from .signals import REGISTRY, prev_close_series


@dataclass
class Alert:
    ticker: str
    signals: tuple[str, ...]
    score: int
    ts: pd.Timestamp
    price: float          # the alert bar's close -- also the assumed entry
    stop: float
    target_1r: float
    target_2r: float
    rvol: float
    atr: float
    day_change: float | None
    context: tuple[str, ...] = field(default_factory=tuple)

    @property
    def signal(self) -> str:
        """Primary signal, for display and per-signal grouping."""
        return self.signals[0]

    @property
    def risk_pct(self) -> float:
        return (self.price - self.stop) / self.price

    def key(self) -> tuple:
        # one BUY NOW per ticker per session -- the first trigger is the call
        return (str(self.ts.date()), self.ticker)


def enrich(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["VWAP"] = session_vwap(df)
    df["RVOL"] = rvol_tod(df)
    df["EMA9"] = ema_session(df, 9)
    df["ATR"] = atr(df)
    return df


def _score(last: pd.Series, prev_close: float | None, n_signals: int) -> tuple[int, list[str]]:
    """Independent confirmations, each worth a point. Reported alongside the
    alert so `backtest --min-score` can tune the threshold against outcomes
    instead of against intuition."""
    rng = last["High"] - last["Low"]
    points = {
        "up on day": prev_close is not None and last["Close"] > prev_close,
        f"rvol>={config.STRONG_RVOL:g}": last["RVOL"] >= config.STRONG_RVOL,
        "closed strong": rng > 0 and (last["Close"] - last["Low"]) >= config.STRONG_CLOSE * rng,
        "confluence": n_signals > 1,
    }
    return sum(points.values()), [k for k, v in points.items() if v]


def prepare(frames: dict[str, pd.DataFrame]) -> dict[str, "Prepared"]:
    """Enrich each ticker and evaluate every signal once, over its whole frame.

    Every indicator and signal is causal, so a value at bar t is identical
    whether computed on the full frame or on the frame truncated at t (proved
    by tests/test_bar_closure.py). That makes this precomputation safe -- and
    it turns a bar-by-bar backtest from hours into seconds, because each bar
    becomes a row lookup instead of a full recomputation.
    """
    out = {}
    for ticker, raw in frames.items():
        if len(raw) < config.MIN_BARS:
            continue
        df = enrich(raw)
        masks = pd.DataFrame({name: spec.fn(df) for name, spec in REGISTRY.items()},
                             index=df.index)
        out[ticker] = Prepared(df=df, masks=masks, prev_close=prev_close_series(df))
    return out


@dataclass
class Prepared:
    df: pd.DataFrame       # enriched OHLCV
    masks: pd.DataFrame    # one bool column per signal
    prev_close: pd.Series  # prior-session close per bar (NaN when untrustworthy)


def scan_prepared(prep: dict[str, "Prepared"], as_of: pd.Timestamp,
                  min_score: int = config.MIN_SCORE) -> list[Alert]:
    alerts = []
    for ticker, p in prep.items():
        # the bar being judged must be THIS bar, closed, for this ticker
        pos = p.df.index.get_indexer([as_of])[0]
        if pos < config.MIN_BARS - 1:
            continue
        last = p.df.iloc[pos]

        # hard gates: real volume, and buyers in control
        if not (last["RVOL"] >= config.RVOL_MIN_ABS and last["Close"] > last["VWAP"]):
            continue
        atr_now = float(last["ATR"])
        if not (math.isfinite(atr_now) and atr_now > 0):
            continue  # warm-up: an unsized trade is not a trade

        row = p.masks.iloc[pos]
        fired = [name for name in p.masks.columns if bool(row[name])]
        if not fired:
            continue

        pc = p.prev_close.iloc[pos]
        prev_close = None if pd.isna(pc) else float(pc)
        score, why = _score(last, prev_close, len(fired))
        if score < min_score:
            continue

        df = p.df.iloc[:pos + 1]
        price = float(last["Close"])
        # widest structural stop among co-firing setups, then clamped to a sane
        # ATR band -- raw stops range from 0.09% (noise) to 5.6% (not a day trade)
        stop = min(REGISTRY[name].stop(df) for name in fired)
        r = price - stop
        if not math.isfinite(r):
            continue
        r = min(max(r, config.MIN_STOP_ATR * atr_now), config.MAX_STOP_ATR * atr_now)
        if not r > 0:
            continue

        alerts.append(Alert(
            ticker=ticker,
            signals=tuple(fired),
            score=score,
            ts=as_of,
            price=round(price, 2),
            stop=round(price - r, 2),
            target_1r=round(price + r, 2),
            target_2r=round(price + 2 * r, 2),
            rvol=round(float(last["RVOL"]), 2),
            atr=round(atr_now, 3),
            day_change=(None if prev_close is None
                        else round(price / prev_close - 1, 4)),
            context=tuple(why) + tuple(REGISTRY[name].context(df) for name in fired),
        ))
    return alerts


def scan_once(frames: dict[str, pd.DataFrame], as_of: pd.Timestamp,
              min_score: int = config.MIN_SCORE) -> list[Alert]:
    """Frames in, alerts out. Pure -- no network, no clock, no side effects."""
    return scan_prepared(prepare(frames), as_of, min_score)


def last_closed_bar(now: pd.Timestamp, bar_minutes: int = config.BAR_MINUTES) -> pd.Timestamp:
    """The most recent bar whose interval has fully elapsed."""
    return now.floor(f"{bar_minutes}min") - pd.Timedelta(minutes=bar_minutes)


def universe_rvol_pctile(frames: dict[str, pd.DataFrame], as_of: pd.Timestamp) -> float:
    """Share of the universe clearing the RVOL floor -- a tape-heat readout for
    the daily summary. Not a gate: rank(pct) always hands the hottest ticker
    1.0, so a percentile gate passes something on even a dead tape."""
    vals = []
    for raw in frames.values():
        df = raw[raw.index <= as_of]
        if len(df) >= config.MIN_BARS and df.index[-1] == as_of:
            v = rvol_tod(df).iloc[-1]
            if np.isfinite(v):
                vals.append(v)
    if not vals:
        return 0.0
    return float(np.mean([v >= config.RVOL_MIN_ABS for v in vals]))
