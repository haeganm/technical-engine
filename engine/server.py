"""Local results dashboard: stdlib http.server over the parquet files.

Read-only, binds 127.0.0.1 only. JSON payloads are built by pure functions so
they can be tested without sockets.
"""
import json
import logging
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import numpy as np
import pandas as pd

from . import config, data, journal
from .indicators import session_vwap

log = logging.getLogger(__name__)

WEB = Path(__file__).parent / "web"

SOURCES = {
    "replay_5m": dict(journal="journal_replay", store="bars",
                      label="5m backtest", interval="5m"),
    "replay_1h": dict(journal="journal_replay_1h", store="bars_1h",
                      label="1h backtest (200d proxy)", interval="1h"),
    "live": dict(journal="journal", store="bars", label="live alerts", interval="5m"),
}


def _clean_nan(obj):
    """json.dumps writes bare NaN, which JSON.parse rejects -- nullify it."""
    if isinstance(obj, float) and not np.isfinite(obj):
        return None
    if isinstance(obj, dict):
        return {k: _clean_nan(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clean_nan(v) for v in obj]
    return obj


@lru_cache(maxsize=8)
def _scored(source: str) -> pd.DataFrame:
    s = SOURCES[source]
    return journal.score_alerts(s["journal"], bars=data.load(s["store"]))


def api_meta() -> dict:
    out = {"sources": []}
    for key, s in SOURCES.items():
        j = data.load(s["journal"])
        if j is None or j.empty:
            continue
        out["sources"].append({
            "key": key, "label": s["label"], "interval": s["interval"],
            "alerts": int(len(j)),
            "from": str(j["session_date"].min()), "to": str(j["session_date"].max()),
        })
    return out


def api_summary(source: str) -> dict:
    if source not in SOURCES:
        return {"error": f"unknown source {source!r}"}
    s = SOURCES[source]
    scored = _scored(source)
    if scored.empty:
        return {"error": "no alerts in this source yet"}
    bars_df = data.load(s["store"])

    decided = scored[scored["outcome"].isin(["one_r", "stop", "ambiguous"])]
    wins = int((decided["outcome"] == "one_r").sum())
    ambig = int((decided["outcome"] == "ambiguous").sum())
    n_dec = len(decided)
    sessions = scored["session_date"].nunique()

    by_day = (scored.sort_values("ts").groupby("session_date")["r_result"].sum())
    cum = by_day.cumsum()
    n_splits = 4 if sessions >= 80 else 2

    def table(by):
        df = journal.stats(s["journal"], by=by, bars=bars_df)
        return df.to_dict("records") if not df.empty else []

    def stab(by):
        df = journal.stability(s["journal"], by=by, n_splits=n_splits, bars=bars_df)
        return df.to_dict("records") if not df.empty else []

    return _clean_nan({
        "label": s["label"],
        "alerts": int(len(scored)),
        "sessions": int(sessions),
        "per_day": round(len(scored) / sessions, 1),
        "win_lo": round(wins / n_dec, 4) if n_dec else None,
        "win_hi": round((wins + ambig) / n_dec, 4) if n_dec else None,
        "decided": n_dec,
        "ambiguous": ambig,
        "open": int((scored["outcome"] == "open").sum()),
        "unresolved": int((scored["outcome"] == "unresolved").sum()),
        "total_r": round(float(scored["r_result"].sum()), 1),
        "cum_r": [{"date": d, "value": round(float(v), 1)} for d, v in cum.items()],
        "by_signal": table("signal"),
        "by_score": table("score"),
        "stability_signal": stab("signal"),
        "stability_score": stab("score"),
        "n_splits": n_splits,
    })


def api_alerts(source: str) -> dict:
    if source not in SOURCES:
        return {"error": f"unknown source {source!r}"}
    scored = _scored(source)
    if scored.empty:
        return {"alerts": []}
    j = data.load(SOURCES[source]["journal"])
    merged = j.merge(scored[["session_date", "ticker", "outcome", "r_result", "fwd_eod"]],
                     on=["session_date", "ticker"], how="left")
    merged = merged.sort_values("ts", ascending=False)
    return _clean_nan({"alerts": [{
        "ts": r.ts.isoformat(), "session_date": r.session_date, "ticker": r.ticker,
        "signals": r.signals, "score": int(r.score), "price": r.price, "stop": r.stop,
        "target_1r": r.target_1r, "target_2r": r.target_2r, "rvol": r.rvol,
        "outcome": getattr(r, "outcome", None) or "unresolved",
        "fwd_eod": getattr(r, "fwd_eod", None),
    } for r in merged.itertuples(index=False)]})


def api_bars(source: str, ticker: str, date: str) -> dict:
    if source not in SOURCES:
        return {"error": f"unknown source {source!r}"}
    s = SOURCES[source]
    bars = data.load(s["store"])
    if bars is None or bars.empty:
        return {"error": "bars store is empty"}
    b = bars[bars["ticker"] == ticker].sort_values("ts")
    if b.empty:
        return {"error": f"no bars for {ticker}"}
    b = b.set_index(pd.DatetimeIndex(b["ts"]).tz_convert(config.ET))
    days = sorted({d for d in b.index.date})
    try:
        target = pd.Timestamp(date).date()
    except ValueError:
        return {"error": f"bad date {date!r}"}
    if target not in days:
        return {"error": f"no session {date} for {ticker}"}
    keep = [d for d in days if d <= target][-3:]          # session + 2 for context
    df = b[[d in keep for d in b.index.date]].copy()
    df["VWAP"] = session_vwap(df)

    j = data.load(s["journal"])
    row = j[(j["ticker"] == ticker) & (j["session_date"] == date)]
    alert = None
    if not row.empty:
        r = row.iloc[0]
        alert = {"ts": pd.Timestamp(r["ts"]).isoformat(), "price": r["price"],
                 "stop": r["stop"], "target_1r": r["target_1r"],
                 "target_2r": r["target_2r"], "signals": r["signals"],
                 "score": int(r["score"])}
    return _clean_nan({
        "ticker": ticker,
        "bars": [{"time": int(ts.timestamp()), "open": float(r["Open"]),
                  "high": float(r["High"]), "low": float(r["Low"]),
                  "close": float(r["Close"]), "volume": float(r["Volume"])}
                 for ts, r in df.iterrows()],
        "vwap": [{"time": int(ts.timestamp()), "value": float(v)}
                 for ts, v in df["VWAP"].items() if np.isfinite(v)],
        "alert": alert,
    })


# ---------------------------------------------------------------- http

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log.debug("http: " + fmt, *args)

    def _send(self, body: bytes, ctype: str, code: int = 200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: dict):
        code = 400 if "error" in obj else 200
        self._send(json.dumps(obj).encode(), "application/json", code)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path == "/api/meta":
                self._json(api_meta())
            elif u.path == "/api/summary":
                self._json(api_summary(q.get("source", "replay_5m")))
            elif u.path == "/api/alerts":
                self._json(api_alerts(q.get("source", "replay_5m")))
            elif u.path == "/api/bars":
                self._json(api_bars(q.get("source", "replay_5m"),
                                    q.get("ticker", ""), q.get("date", "")))
            elif u.path in ("/", "/index.html"):
                self._send((WEB / "index.html").read_bytes(), "text/html; charset=utf-8")
            elif u.path == "/app.js":
                self._send((WEB / "app.js").read_bytes(), "text/javascript")
            elif u.path == "/lightweight-charts.js":
                self._send((WEB / "lightweight-charts.js").read_bytes(), "text/javascript")
            else:
                self._send(b"not found", "text/plain", 404)
        except BrokenPipeError:
            pass
        except Exception:
            log.exception("request failed: %s", self.path)
            self._send(b'{"error":"internal error"}', "application/json", 500)


def serve(port: int = 8765):
    _scored.cache_clear()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    log.info("dashboard at http://localhost:%d  (Ctrl+C to stop)", port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("dashboard stopped")
