"""Scan a set of enriched frames for last-bar signal firings."""
from dataclasses import dataclass

import pandas as pd

from . import config
from .indicators import ema, rvol_tod, session_vwap
from .signals import REGISTRY


@dataclass
class Alert:
    ticker: str
    signal: str
    ts: pd.Timestamp
    price: float
    entry: float
    stop: float
    target_1r: float
    target_2r: float
    rvol: float
    rvol_pctile: float

    def key(self) -> tuple:
        # one BUY NOW per ticker per session -- the first trigger is the call
        return (str(self.ts.date()), self.ticker)


def enrich(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["VWAP"] = session_vwap(df)
    df["RVOL"] = rvol_tod(df)
    df["EMA9"] = ema(df["Close"], 9)
    return df


def scan_once(frames: dict[str, pd.DataFrame]) -> list[Alert]:
    """Pure: frames in, alerts out. Frames must end on the bar being judged."""
    enriched = {t: enrich(df) for t, df in frames.items() if len(df) >= config.MIN_BARS}
    if not enriched:
        return []
    last_rvol = pd.Series({t: df["RVOL"].iloc[-1] for t, df in enriched.items()}).dropna()
    pctile = last_rvol.rank(pct=True)

    alerts = []
    for t, df in enriched.items():
        pct = float(pctile.get(t, 0.0))
        last = df.iloc[-1]
        # high-confidence gate: hot tape both relative (percentile) and absolute
        # (RVOL floor), price above VWAP, and up on the day (no knife-catching)
        prior = df["Close"][df.index.date < df.index[-1].date()]
        if (pct < config.RVOL_MIN_PCTILE or not last["RVOL"] >= config.RVOL_MIN_ABS
                or not last["Close"] > last["VWAP"]
                or (len(prior) and not last["Close"] > prior.iloc[-1])):
            continue
        for name, spec in REGISTRY.items():
            mask = spec.fn(df, **spec.defaults)
            if not bool(mask.iloc[-1]):
                continue
            entry, stop = spec.levels(df)
            r = entry - stop
            if not r > 0:
                continue
            alerts.append(Alert(  # first matching signal wins (registry order)
                ticker=t, signal=name, ts=df.index[-1], price=float(last["Close"]),
                entry=round(entry, 2), stop=round(stop, 2),
                target_1r=round(entry + r, 2), target_2r=round(entry + 2 * r, 2),
                rvol=round(float(last["RVOL"]), 2), rvol_pctile=round(pct, 2),
            ))
            break
    return alerts
