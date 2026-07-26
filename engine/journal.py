"""Alert journal: the audit trail, and the only honest arbiter of whether the
signals earn their pings.

Every alert is logged once per (session, ticker) and later scored against the
bars that actually printed. Two deliberate honesty rules:

  * When 1R and the stop are both hit inside the SAME 5-minute bar, the order
    is unknowable at this resolution. That is its own `ambiguous` outcome --
    booking it as a loss (or a win) would be inventing a fact. It is reported
    as a lo/hi band. (Rare now -- ~0.1% of resolved setups -- because the ATR
    stop clamp means a single 5m bar seldom spans both levels. It was ~22%
    before the clamp, and would return if the clamp were loosened.)
  * Alerts whose bars have aged out of the store stay in the frame as
    `unresolved` with a reason. They are never silently dropped from the
    denominator.
"""
import logging

import numpy as np
import pandas as pd

from . import config, data

log = logging.getLogger(__name__)

KEYS = ["session_date", "ticker"]  # one journaled call per ticker per session
LIVE, REPLAY = "journal", "journal_replay"


def append(alerts, name: str = LIVE) -> None:
    if not alerts:
        return
    now = pd.Timestamp.now(tz=config.ET).round("s")
    new = pd.DataFrame([{
        "ts": a.ts, "session_date": str(a.ts.date()), "ticker": a.ticker,
        "signal": a.signal, "signals": ",".join(a.signals), "score": a.score,
        "price": a.price, "stop": a.stop,
        "target_1r": a.target_1r, "target_2r": a.target_2r,
        "rvol": a.rvol, "atr": a.atr, "created_at": now,
    } for a in alerts])
    old = data.load(name)
    df = pd.concat([old, new], ignore_index=True) if old is not None else new
    # keep="first": the first alert for a (session, ticker) is THE alert
    df = df.drop_duplicates(KEYS, keep="first").sort_values("ts").reset_index(drop=True)
    data.save(name, df)


def sent_keys(session_date: str, name: str = LIVE) -> set[tuple]:
    """Seed the in-memory dedupe set on restart so we never re-notify."""
    j = data.load(name)
    if j is None or j.empty:
        return set()
    j = j[j["session_date"] == session_date]
    return set(map(tuple, j[KEYS].to_numpy()))


def clear(name: str = REPLAY) -> None:
    p = data._path(name)
    if p.exists():
        p.unlink()


# ---------------------------------------------------------------- scoring

def score_alerts(name: str = LIVE, bars: pd.DataFrame | None = None) -> pd.DataFrame:
    """One row per journaled alert with its realized outcome. One schema, always.

    Outcomes: one_r | stop | ambiguous | open | unresolved.
    """
    cols = ["session_date", "ticker", "signal", "score", "ts", "price",
            "outcome", "r_result", "fwd_30m", "fwd_60m", "fwd_eod", "reason"]
    j = data.load(name)
    if j is None or j.empty:
        return pd.DataFrame(columns=cols)
    bars = data.load("bars") if bars is None else bars
    if bars is None or bars.empty:
        out = j[["session_date", "ticker", "signal", "score", "ts", "price"]].copy()
        out["outcome"], out["reason"] = "unresolved", "no_bars"
        out["r_result"] = 0.0
        for c in ("fwd_30m", "fwd_60m", "fwd_eod"):
            out[c] = np.nan
        return out[cols]

    b = bars.copy()
    # derive the session key ONCE -- rebuilding it per alert is quadratic.
    # .astype(str) on the normalized dates is vectorized; .strftime() on a
    # 2.4M-row DatetimeIndex is elementwise Python and cost ~17s here.
    b["session_date"] = (pd.DatetimeIndex(b["ts"]).tz_convert(config.ET)
                         .normalize().tz_localize(None).astype("datetime64[s]").astype(str))
    by_session = {k: g.sort_values("ts") for k, g in b.groupby(["ticker", "session_date"])}
    stored_sessions = set(b["session_date"])

    rows = []
    for a in j.itertuples(index=False):
        base = {"session_date": a.session_date, "ticker": a.ticker, "signal": a.signal,
                "score": getattr(a, "score", np.nan), "ts": a.ts, "price": a.price,
                "fwd_30m": np.nan, "fwd_60m": np.nan, "fwd_eod": np.nan, "reason": ""}
        g = by_session.get((a.ticker, a.session_date))
        after = g[g["ts"] > a.ts] if g is not None else None
        if after is None or after.empty:
            base["outcome"] = "unresolved"
            base["reason"] = "pruned" if a.session_date not in stored_sessions else "no_bars_after"
            rows.append(base)
            continue

        close = after.set_index("ts")["Close"]
        for label, mins in (("fwd_30m", 30), ("fwd_60m", 60)):
            later = close[close.index >= a.ts + pd.Timedelta(minutes=mins)]
            if len(later):
                base[label] = float(later.iloc[0] / a.price - 1)
        base["fwd_eod"] = float(close.iloc[-1] / a.price - 1)

        hit_1r = (after["High"] >= a.target_1r).to_numpy()
        hit_stop = (after["Low"] <= a.stop).to_numpy()
        i_1r = int(hit_1r.argmax()) if hit_1r.any() else None
        i_stop = int(hit_stop.argmax()) if hit_stop.any() else None
        if i_1r is None and i_stop is None:
            base["outcome"] = "open"
        elif i_stop is None or (i_1r is not None and i_1r < i_stop):
            base["outcome"] = "one_r"
        elif i_1r is None or i_stop < i_1r:
            base["outcome"] = "stop"
        else:
            base["outcome"] = "ambiguous"  # same bar -- intrabar order unknowable
        rows.append(base)
    out = pd.DataFrame(rows)
    # +1R on a win, -1R on a stop, 0 otherwise -- the frontend's equity curve
    out["r_result"] = out["outcome"].map({"one_r": 1.0, "stop": -1.0}).fillna(0.0)
    return out[cols]


def stats(name: str = LIVE, by: str = "signal", bars: pd.DataFrame | None = None,
          scored: pd.DataFrame | None = None) -> pd.DataFrame:
    """Aggregate outcomes. `one_r_lo/hi` bracket the same-bar ambiguity.

    Pass `scored` to reuse a single score_alerts pass across several tables --
    scoring groups the whole bars store, which is millions of rows.
    """
    scored = score_alerts(name, bars=bars) if scored is None else scored
    if scored.empty:
        return pd.DataFrame()
    resolved = scored[scored["outcome"] != "unresolved"]
    if resolved.empty:
        return pd.DataFrame()
    grp = resolved.groupby(by, dropna=False)
    out = pd.DataFrame({
        "n": grp.size(),
        "one_r": grp["outcome"].apply(lambda s: (s == "one_r").sum()),
        "stopped": grp["outcome"].apply(lambda s: (s == "stop").sum()),
        "ambiguous": grp["outcome"].apply(lambda s: (s == "ambiguous").sum()),
        "open": grp["outcome"].apply(lambda s: (s == "open").sum()),
        "avg_fwd_30m": grp["fwd_30m"].mean().round(4),
        "avg_fwd_60m": grp["fwd_60m"].mean().round(4),
        "avg_fwd_eod": grp["fwd_eod"].mean().round(4),
    })
    # Rate among trades that actually resolved to 1R or a stop. The `open`
    # bucket (neither touched by the close) is not a loss, so including it in
    # the denominator understates every signal.
    decided = out["one_r"] + out["stopped"] + out["ambiguous"]
    out["decided"] = decided
    out["win_lo"] = (out["one_r"] / decided.replace(0, np.nan)).round(3)
    out["win_hi"] = ((out["one_r"] + out["ambiguous"]) / decided.replace(0, np.nan)).round(3)
    out["one_r_lo"] = (out["one_r"] / out["n"]).round(3)
    out["one_r_hi"] = ((out["one_r"] + out["ambiguous"]) / out["n"]).round(3)
    unresolved = scored[scored["outcome"] == "unresolved"].groupby(by, dropna=False).size()
    out["unresolved"] = unresolved.reindex(out.index).fillna(0).astype(int)
    return out.reset_index()


def stability(name: str = REPLAY, by: str = "signal", n_splits: int = 2,
              bars: pd.DataFrame | None = None,
              scored: pd.DataFrame | None = None) -> pd.DataFrame:
    """Win rate across contiguous time slices of the sample.

    The single most important table here. A signal that looks strong overall
    but swings wildly between slices is tracking the market regime, not an
    edge -- and tuning a threshold on the good slice is how a backtest lies.
    """
    scored = score_alerts(name, bars=bars) if scored is None else scored
    if scored.empty:
        return pd.DataFrame()
    d = scored[scored["outcome"].isin(["one_r", "stop", "ambiguous"])].copy()
    if d.empty:
        return pd.DataFrame()
    d["win"] = d["outcome"].isin(["one_r", "ambiguous"])
    days = sorted(d["session_date"].unique())
    if len(days) < 2 * n_splits:
        return pd.DataFrame()
    edges = [days[(len(days) * i) // n_splits] for i in range(n_splits)]
    labels = [f"p{i + 1}" for i in range(n_splits)]
    d["slice"] = labels[0]
    for i in range(1, n_splits):
        d.loc[d["session_date"] >= edges[i], "slice"] = labels[i]
    g = d.groupby([by, "slice"])["win"].agg(["mean", "size"]).unstack("slice")
    out = pd.DataFrame(index=g.index)
    for lab in labels:
        out[f"n_{lab}"] = g.get(("size", lab), pd.Series(index=g.index)).fillna(0).astype(int)
        out[f"win_{lab}"] = g.get(("mean", lab), pd.Series(index=g.index)).round(3)
    out["swing"] = (out[f"win_{labels[-1]}"] - out[f"win_{labels[0]}"]).round(3)
    return out.reset_index()


def format_stability(df: pd.DataFrame) -> str:
    if df.empty:
        return "Not enough sessions to split."
    d = df.copy()
    for c in d.columns:
        if c.startswith("win_") or c == "swing":
            fmt = "{:+.1%}" if c == "swing" else "{:.1%}"
            d[c] = d[c].map(lambda v, f=fmt: "-" if pd.isna(v) else f.format(v))
    return d.to_string(index=False)


COLUMNS = ["n", "decided", "one_r", "stopped", "ambiguous", "open",
           "win_lo", "win_hi", "avg_fwd_30m", "avg_fwd_eod", "unresolved"]


def format_stats(df: pd.DataFrame, min_n: int = 20) -> str:
    """Render with the sample size front and centre. Rates below min_n print as
    'n<20' -- a win rate from a dozen alerts is worse than no win rate.

    `win_lo`/`win_hi` bracket the same-bar ambiguity: lo counts those as losses,
    hi as wins. At 1:1 reward:risk, anything at or below 50% is not an edge.
    """
    if df.empty:
        return "No resolved alerts yet."
    d = df.copy()
    thin = d["n"] < min_n
    for c in ("win_lo", "win_hi"):
        d[c] = d[c].map(lambda v: "-" if pd.isna(v) else f"{v:.1%}")
        d.loc[thin, c] = f"n<{min_n}"
    keep = [c for c in [d.columns[0], *COLUMNS] if c in d.columns]
    return d[keep].to_string(index=False)
