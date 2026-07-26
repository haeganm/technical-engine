"""Batched intraday fetch (yfinance) + atomic parquet store.

Store format is long: columns ts, ticker, Open, High, Low, Close, Volume.

Yahoo labels intraday bars by interval START, so the bar at index[-1] during
market hours is still forming. `complete_bars` is applied inside the fetch, so
a partial bar can never reach the store or any caller -- which is what makes
backtest results equal to what live would have produced.
"""
import logging
import os

import pandas as pd
import yfinance as yf

from . import config

log = logging.getLogger(__name__)

OHLCV = ["Open", "High", "Low", "Close", "Volume"]


# ---------------------------------------------------------------- parquet io

def _path(name: str):
    return config.DATA / f"{name}.parquet"


def load(name: str) -> pd.DataFrame | None:
    p = _path(name)
    return pd.read_parquet(p) if p.exists() else None


def save(name: str, df: pd.DataFrame):
    config.DATA.mkdir(exist_ok=True)
    # pid-unique temp: two engine processes must not share a scratch file
    tmp = _path(name).with_suffix(f".tmp{os.getpid()}")
    df.to_parquet(tmp, index=False)
    try:
        os.replace(tmp, _path(name))
    except PermissionError as e:
        raise PermissionError(
            f"{_path(name)} is locked (open in another program?). Close it and retry.") from e


def merge_save(name: str, new: pd.DataFrame, keys: list[str],
               cutoff: pd.Timestamp | None = None, cutoff_col: str = "ts") -> int:
    """Append, dedupe on keys keeping latest, prune below cutoff, sort, save.

    One load and one save -- pruning is folded in rather than done as a
    separate read-modify-write pass.
    """
    old = load(name)
    if (new is None or new.empty) and old is None:
        return 0
    parts = [p for p in (old, new) if p is not None and not p.empty]
    df = pd.concat(parts, ignore_index=True) if len(parts) > 1 else parts[0].copy()
    if cutoff is not None:
        df = df[df[cutoff_col] >= cutoff]
    df = df.drop_duplicates(subset=keys, keep="last").sort_values(keys).reset_index(drop=True)
    save(name, df)
    return len(df)


# ---------------------------------------------------------------- fetch

def complete_bars(df: pd.DataFrame, now: pd.Timestamp,
                  bar_minutes: int = config.BAR_MINUTES) -> pd.DataFrame:
    """Drop bars whose interval has not fully elapsed."""
    if df is None or df.empty:
        return df
    return df[df.index + pd.Timedelta(minutes=bar_minutes) <= now]


def _clean(df: pd.DataFrame) -> pd.DataFrame | None:
    """Minimal OHLCV hygiene."""
    if df is None or df.empty:
        return None
    df = df.dropna(subset=["Close"])
    df = df[(df[["Open", "High", "Low", "Close"]] > 0).all(axis=1)]
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df["Volume"] = df["Volume"].fillna(0).clip(lower=0)
    return df if len(df) else None


def fetch_intraday(tickers: list[str], interval: str = config.INTERVAL,
                   period: str = config.PERIOD, now: pd.Timestamp | None = None,
                   bar_minutes: int = config.BAR_MINUTES) -> dict[str, pd.DataFrame]:
    """One batched download; returns {ticker: OHLCV frame, ET index, closed bars}."""
    now = now if now is not None else pd.Timestamp.now(tz=config.ET)
    raw = yf.download(tickers, interval=interval, period=period, group_by="ticker",
                      threads=True, progress=False, auto_adjust=False)
    if raw is None or raw.empty:
        log.warning("fetch returned nothing for %d tickers", len(tickers))
        return {}
    multi = isinstance(raw.columns, pd.MultiIndex)
    if not multi and len(tickers) > 1:
        # Yahoo flattens when all but one ticker fail; assigning that frame to
        # every name would fabricate identical data across the universe
        log.warning("fetch returned a flat frame for %d tickers -- discarding", len(tickers))
        return {}
    frames = {}
    for t in tickers:
        try:
            df = raw[t] if multi else raw
        except KeyError:
            continue
        df = _clean(df.reindex(columns=OHLCV))
        if df is None:
            continue
        idx = pd.DatetimeIndex(df.index)
        idx = idx.tz_localize("UTC") if idx.tz is None else idx
        df.index = idx.tz_convert(config.ET)
        df = complete_bars(df, now, bar_minutes=bar_minutes)
        if df is not None and len(df):
            frames[t] = df
    missing = len(tickers) - len(frames)
    if missing:
        # A large shortfall is the only visible symptom of Yahoo throttling,
        # since yfinance turns per-ticker errors into empty frames and returns
        # normally. Make it loud enough to find in the log.
        level = logging.WARNING if len(frames) < len(tickers) * 0.9 else logging.INFO
        log.log(level, "fetch: %d/%d tickers returned usable bars",
                len(frames), len(tickers))
    return frames


# ---------------------------------------------------------------- store <-> frames

def store_bars(frames: dict[str, pd.DataFrame], now: pd.Timestamp | None = None,
               store: str = "bars", keep_days: int = config.KEEP_DAYS) -> int:
    if not frames:
        # Nothing to add. Rewriting here would re-apply the prune during an
        # outage -- i.e. erode the archive fastest exactly when no new data
        # is arriving. Report the existing size instead.
        old = load(store)
        return 0 if old is None else len(old)
    now = now if now is not None else pd.Timestamp.now(tz=config.ET)
    rows = []
    for t, df in frames.items():
        r = df.reset_index(names="ts")
        r.insert(1, "ticker", t)
        rows.append(r)
    new = pd.concat(rows, ignore_index=True) if rows else None
    cutoff = now - pd.Timedelta(days=keep_days)
    return merge_save(store, new, keys=["ticker", "ts"], cutoff=cutoff)


def load_frames(upto: pd.Timestamp | None = None, store: str = "bars",
                since: pd.Timestamp | None = None) -> dict[str, pd.DataFrame]:
    """Rebuild per-ticker frames from the store. This is what both the live
    scan and the backtest read, so they see identical history.

    `since` bounds how far back the live path loads: the store is never pruned
    (Yahoo cannot re-serve old 5m bars), so without a bound every cycle would
    re-enrich the entire archive and get slower forever.
    """
    df = load(store)
    if df is None or df.empty:
        return {}
    if upto is not None:
        df = df[df["ts"] <= upto]
    if since is not None:
        df = df[df["ts"] >= since]
    out = {}
    for t, g in df.groupby("ticker"):
        f = g.set_index("ts")[OHLCV].sort_index()
        f.index = pd.DatetimeIndex(f.index).tz_convert(config.ET)
        out[t] = f
    return out


def store_sessions(store: str = "bars") -> int:
    """Distinct trading dates in the store -- warm-up readout."""
    df = load(store)
    if df is None or df.empty:
        return 0
    return int(pd.DatetimeIndex(df["ts"]).tz_convert(config.ET).normalize().nunique())
