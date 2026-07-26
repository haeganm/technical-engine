"""Judge one closed bar across the universe and emit alerts.

`scan_once(frames, as_of)` is the single scan path -- live and backtest both
call it with the same shape of input, so a replayed result is what live would
have produced. It enforces its own precondition: every frame is truncated to
`as_of`, and a ticker without a bar exactly at `as_of` is skipped rather than
judged on a stale one.
"""
import math
from dataclasses import dataclass, field

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
    target_1: float       # config.TARGET_MULTIPLES[0] * R above price
    target_2: float       # config.TARGET_MULTIPLES[1] * R above price
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


def enrich(df: pd.DataFrame, ema_period: int = 9) -> pd.DataFrame:
    df = df.copy()
    df["VWAP"] = session_vwap(df)
    df["RVOL"] = rvol_tod(df)
    df["EMA9"] = ema_session(df, ema_period)
    df["ATR"] = atr(df)
    return df


def _signal_kwargs(profile: dict) -> dict[str, dict]:
    """Per-signal overrides derived from the interval profile.

    Every signal with an interval-sensitive window must appear here, otherwise
    it silently reads the 5m constants and computes something meaningless at
    another interval while still reporting backtest numbers.
    """
    return {
        "orb_breakout": {"or_bars": profile["orb_bars"]},
        "gap_go": {"min_prev_bars": profile["prev_session_min_bars"]},
        "squeeze_breakout": {"bars": profile["squeeze_bars"]},
        "momentum_thrust": {"bars": profile["thrust_bars"]},
    }


def active_signals(profile: dict) -> list[str]:
    """Registry order, minus signals this interval cannot express."""
    disabled = set(profile.get("disabled", ()))
    return [n for n in REGISTRY if n not in disabled]


def _stop_kwargs(profile: dict) -> dict[str, dict]:
    """Window overrides for the stop/context functions (same rule as the fns)."""
    return {
        "orb_breakout": {"or_bars": profile["orb_bars"]},
        "squeeze_breakout": {"bars": profile["squeeze_bars"]},
        "momentum_thrust": {"bars": profile["thrust_bars"]},
    }


def _score(last: pd.Series, prev_close: float | None, n_signals: int,
           market_up: bool | None = None,
           spy_change: float | None = None) -> tuple[int, list[str]]:
    """Independent confirmations, each worth a point (max 6). Reported alongside
    the alert so `backtest --min-score` can tune the threshold against outcomes
    instead of against intuition. market_up/spy_change are None when SPY data is
    unavailable -- no point awarded, never a crash."""
    rng = last["High"] - last["Low"]
    day_change = (last["Close"] / prev_close - 1) if prev_close else None
    points = {
        "up on day": prev_close is not None and last["Close"] > prev_close,
        f"rvol>={config.STRONG_RVOL:g}": last["RVOL"] >= config.STRONG_RVOL,
        "closed strong": rng > 0 and (last["Close"] - last["Low"]) >= config.STRONG_CLOSE * rng,
        "confluence": n_signals > 1,
        "market up": bool(market_up),
        "rs leader": (day_change is not None and spy_change is not None
                      and day_change > spy_change),
    }
    return sum(points.values()), [k for k, v in points.items() if v]


def _market_context(prep: dict[str, "Prepared"], as_of: pd.Timestamp) -> tuple[bool | None, float | None]:
    """SPY regime at as_of: (above its VWAP?, its day change). (None, None) when absent."""
    spy = prep.get("SPY")
    if spy is None:
        return None, None
    pos = spy.df.index.get_indexer([as_of])[0]
    if pos < 0:
        return None, None
    row = spy.df.iloc[pos]
    up = bool(row["Close"] > row["VWAP"]) if pd.notna(row["VWAP"]) else None
    pc = spy.prev_close.iloc[pos]
    change = (float(row["Close"] / pc - 1)) if pd.notna(pc) else None
    return up, change


def prepare(frames: dict[str, pd.DataFrame],
            profile: dict | None = None) -> dict[str, "Prepared"]:
    """Enrich each ticker and evaluate every signal once, over its whole frame.

    Every indicator and signal is causal, so a value at bar t is identical
    whether computed on the full frame or on the frame truncated at t (proved
    by tests/test_bar_closure.py). That makes this precomputation safe -- and
    it turns a bar-by-bar backtest from hours into seconds, because each bar
    becomes a row lookup instead of a full recomputation.
    """
    profile = profile or config.LIVE
    sig_kw = _signal_kwargs(profile)
    names = active_signals(profile)
    out = {}
    for ticker, raw in frames.items():
        if len(raw) < config.MIN_BARS:
            continue
        df = enrich(raw, ema_period=profile["ema_period"])
        masks = pd.DataFrame(
            {n: REGISTRY[n].fn(df, **sig_kw.get(n, {})) for n in names},
            index=df.index)
        out[ticker] = Prepared(
            df=df, masks=masks,
            prev_close=prev_close_series(df, min_bars=profile["prev_session_min_bars"]),
            profile=profile)
    return out


@dataclass
class Prepared:
    df: pd.DataFrame       # enriched OHLCV
    masks: pd.DataFrame    # one bool column per signal
    prev_close: pd.Series  # prior-session close per bar (NaN when untrustworthy)
    profile: dict          # interval profile the masks were computed under


def scan_prepared(prep: dict[str, "Prepared"], as_of: pd.Timestamp,
                  min_score: int = config.MIN_SCORE) -> list[Alert]:
    market_up, spy_change = _market_context(prep, as_of)
    alerts = []
    for ticker, p in prep.items():
        if ticker in config.EXTRA_TICKERS:
            continue  # context tickers (SPY) inform the score, never alert
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
        score, why = _score(last, prev_close, len(fired), market_up, spy_change)
        if score < min_score:
            continue

        df = p.df.iloc[:pos + 1]
        price = float(last["Close"])
        stop_kw = _stop_kwargs(p.profile)
        # widest structural stop among co-firing setups, then clamped to a sane
        # ATR band -- raw stops range from 0.09% (noise) to 5.6% (not a day trade)
        stop = min(REGISTRY[name].stop(df, **stop_kw.get(name, {})) for name in fired)
        r = price - stop
        # Reject a bad structural stop BEFORE clamping. The clamp would happily
        # turn a negative r into a plausible-looking 1-ATR risk, which is how a
        # broken stop() function hides from every invariant test.
        if not (math.isfinite(r) and r > 0):
            continue
        r = min(max(r, config.MIN_STOP_ATR * atr_now), config.MAX_STOP_ATR * atr_now)

        alerts.append(Alert(
            ticker=ticker,
            signals=tuple(fired),
            score=score,
            ts=as_of,
            price=round(price, 2),
            stop=round(price - r, 2),
            target_1=round(price + config.TARGET_MULTIPLES[0] * r, 2),
            target_2=round(price + config.TARGET_MULTIPLES[1] * r, 2),
            rvol=round(float(last["RVOL"]), 2),
            atr=round(atr_now, 3),
            day_change=(None if prev_close is None
                        else round(price / prev_close - 1, 4)),
            context=tuple(why) + tuple(
                REGISTRY[name].context(df, **stop_kw.get(name, {})) for name in fired),
        ))
    return alerts


def scan_once(frames: dict[str, pd.DataFrame], as_of: pd.Timestamp,
              min_score: int = config.MIN_SCORE, profile: dict | None = None) -> list[Alert]:
    """Frames in, alerts out. Pure -- no network, no clock, no side effects."""
    return scan_prepared(prepare(frames, profile), as_of, min_score)


def last_closed_bar(now: pd.Timestamp, bar_minutes: int = config.BAR_MINUTES) -> pd.Timestamp:
    """The most recent bar whose interval has fully elapsed."""
    return now.floor(f"{bar_minutes}min") - pd.Timedelta(minutes=bar_minutes)
