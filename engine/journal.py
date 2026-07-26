"""Alert journal: every fired alert is logged once per (session, ticker, signal)
and later scored against realized bars -- live hit rates, not backtest claims.
Adapted from BTCTA's journal (keep="first" audit-trail rule).
"""
import pandas as pd

from . import config, data

KEYS = ["session_date", "ticker"]  # one journaled call per ticker per session


def append(alerts) -> None:
    if not alerts:
        return
    now = pd.Timestamp.now(tz=config.ET).round("s")
    new = pd.DataFrame([{
        "ts": a.ts, "session_date": str(a.ts.date()), "ticker": a.ticker,
        "signal": a.signal, "price": a.price, "entry": a.entry, "stop": a.stop,
        "target_1r": a.target_1r, "target_2r": a.target_2r,
        "rvol": a.rvol, "rvol_pctile": a.rvol_pctile, "created_at": now,
    } for a in alerts])
    old = data.load("journal")
    df = pd.concat([old, new], ignore_index=True) if old is not None else new
    # keep="first": the first alert for a (session, ticker, signal) is THE alert
    df = df.drop_duplicates(KEYS, keep="first").sort_values("ts").reset_index(drop=True)
    data.save("journal", df)


def sent_keys(session_date: str) -> set[tuple]:
    """Seed the in-memory dedupe set on restart so we never re-notify."""
    j = data.load("journal")
    if j is None or j.empty:
        return set()
    j = j[j["session_date"] == session_date]
    return set(map(tuple, j[KEYS].to_numpy()))


def stats() -> pd.DataFrame:
    """Score each journaled alert against realized bars: forward returns at
    +30m/+60m/EOD, EOD hit rate, and whether 1R printed before the stop."""
    j, bars = data.load("journal"), data.load("bars")
    if j is None or j.empty or bars is None or bars.empty:
        return pd.DataFrame()
    rows = []
    for _, a in j.iterrows():
        b = bars[(bars["ticker"] == a["ticker"])
                 & (bars["ts"].dt.date.astype(str) == a["session_date"])
                 & (bars["ts"] > a["ts"])].sort_values("ts")
        ident = a[["session_date", "ticker", "signal"]]
        if b.empty:
            rows.append({**ident, "resolved": False})
            continue
        close = b.set_index("ts")["Close"]
        fwd = {}
        for label, mins in (("fwd_30m", 30), ("fwd_60m", 60)):
            later = close[close.index >= a["ts"] + pd.Timedelta(minutes=mins)]
            fwd[label] = float(later.iloc[0] / a["price"] - 1) if len(later) else None
        hit_1r = (b["High"] >= a["target_1r"]).to_numpy()
        hit_stop = (b["Low"] <= a["stop"]).to_numpy()
        first_1r = hit_1r.argmax() if hit_1r.any() else len(b)
        first_stop = hit_stop.argmax() if hit_stop.any() else len(b)
        rows.append({
            **ident, "resolved": True, **fwd,
            "fwd_eod": float(close.iloc[-1] / a["price"] - 1),
            "eod_win": bool(close.iloc[-1] > a["price"]),
            "one_r_first": bool(hit_1r.any() and first_1r < first_stop),
        })
    scored = pd.DataFrame(rows)
    done = scored[scored["resolved"]]
    if done.empty:
        return scored
    return (done.groupby("signal")
            .agg(n=("signal", "size"), eod_win_rate=("eod_win", "mean"),
                 one_r_rate=("one_r_first", "mean"), avg_fwd_30m=("fwd_30m", "mean"),
                 avg_fwd_60m=("fwd_60m", "mean"), avg_fwd_eod=("fwd_eod", "mean"))
            .round(4).reset_index())
