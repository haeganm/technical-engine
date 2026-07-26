"""Batched intraday fetch (yfinance) + atomic parquet store.

Store format is long: columns ts, ticker, Open, High, Low, Close, Volume.
Parquet io (load/save/merge_save) adapted from BTCTA's atomic-replace pattern.
"""
import os

import pandas as pd
import yfinance as yf

from . import config

OHLCV = ["Open", "High", "Low", "Close", "Volume"]


# ---------------------------------------------------------------- parquet io

def _path(name: str):
    return config.DATA / f"{name}.parquet"


def load(name: str) -> pd.DataFrame | None:
    p = _path(name)
    return pd.read_parquet(p) if p.exists() else None


def save(name: str, df: pd.DataFrame):
    config.DATA.mkdir(exist_ok=True)
    tmp = _path(name).with_suffix(".tmp")
    df.to_parquet(tmp, index=False)
    try:
        os.replace(tmp, _path(name))
    except PermissionError:
        raise PermissionError(
            f"{_path(name)} is locked (open in another program?). Close it and retry.")


def merge_save(name: str, new: pd.DataFrame, keys: list[str]) -> int:
    """Append new rows, dedupe on keys keeping latest, sort, save. Returns total rows."""
    if new is None or new.empty:
        old = load(name)
        return 0 if old is None else len(old)
    old = load(name)
    df = pd.concat([old, new], ignore_index=True) if old is not None else new
    df = df.drop_duplicates(subset=keys, keep="last").sort_values(keys).reset_index(drop=True)
    save(name, df)
    return len(df)


# ---------------------------------------------------------------- fetch

def _clean(df: pd.DataFrame) -> pd.DataFrame | None:
    """Minimal OHLCV hygiene (subset of mrm-momentum's validate_ohlcv)."""
    if df is None or df.empty:
        return None
    df = df.dropna(subset=["Close"])
    df = df[(df[["Open", "High", "Low", "Close"]] > 0).all(axis=1)]
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df["Volume"] = df["Volume"].fillna(0).clip(lower=0)
    return df if len(df) else None


def fetch_intraday(tickers: list[str], interval: str = config.INTERVAL,
                   period: str = config.PERIOD) -> dict[str, pd.DataFrame]:
    """One batched download; returns {ticker: OHLCV frame, ET tz-aware index}."""
    raw = yf.download(tickers, interval=interval, period=period, group_by="ticker",
                      threads=True, progress=False, auto_adjust=False)
    if raw is None or raw.empty:
        return {}
    frames = {}
    for t in tickers:
        try:
            df = raw[t] if isinstance(raw.columns, pd.MultiIndex) else raw
        except KeyError:
            continue
        df = _clean(df.reindex(columns=OHLCV))
        if df is None:
            continue
        idx = pd.DatetimeIndex(df.index)
        idx = idx.tz_localize("UTC") if idx.tz is None else idx
        df.index = idx.tz_convert(config.ET)
        frames[t] = df
    return frames


# ---------------------------------------------------------------- store <-> frames

def store_bars(frames: dict[str, pd.DataFrame]) -> int:
    rows = []
    for t, df in frames.items():
        r = df.reset_index(names="ts")
        r.insert(1, "ticker", t)
        rows.append(r)
    if not rows:
        return 0
    new = pd.concat(rows, ignore_index=True)
    cutoff = pd.Timestamp.now(tz=config.ET) - pd.Timedelta(days=config.KEEP_DAYS)
    old = load("bars")
    if old is not None:
        old = old[old["ts"] >= cutoff]
        save("bars", old)
    return merge_save("bars", new[new["ts"] >= cutoff], keys=["ticker", "ts"])


def load_frames(upto: pd.Timestamp | None = None) -> dict[str, pd.DataFrame]:
    """Rebuild per-ticker frames from the store, optionally truncated (replay)."""
    df = load("bars")
    if df is None or df.empty:
        return {}
    if upto is not None:
        df = df[df["ts"] <= upto]
    out = {}
    for t, g in df.groupby("ticker"):
        f = g.set_index("ts")[OHLCV].sort_index()
        f.index = pd.DatetimeIndex(f.index).tz_convert(config.ET)
        out[t] = f
    return out
