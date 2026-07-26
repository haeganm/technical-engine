"""Dashboard API: pure JSON builders + one socket smoke test."""
import http.client
import json
import threading

import pytest

from engine import config, data, journal, scanner, server


@pytest.fixture(autouse=True)
def tmp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA", tmp_path)
    server._scored.cache_clear()
    yield tmp_path
    server._scored.cache_clear()


def _seed(frames):
    """Store fixture bars and journal one real scanned alert into the replay."""
    data.store_bars(frames, now=max(df.index[-1] for df in frames.values()))
    all_ts = sorted({ts for df in frames.values() for ts in df.index})
    alerts = []
    for ts in all_ts[-40:]:
        alerts += scanner.scan_once(frames, ts, min_score=0)
    assert alerts, "fixture must produce alerts for the API tests"
    journal.append(alerts, "journal_replay")
    return alerts


def test_meta_lists_only_populated_sources(frames):
    assert server.api_meta() == {"sources": []}
    _seed(frames)
    meta = server.api_meta()
    assert [s["key"] for s in meta["sources"]] == ["replay_5m"]
    assert meta["sources"][0]["alerts"] > 0


def test_summary_shape(frames):
    _seed(frames)
    s = server.api_summary("replay_5m")
    assert s["alerts"] > 0 and s["sessions"] > 0
    assert isinstance(s["total_r"], float)
    assert s["cum_r"] and {"date", "value"} <= set(s["cum_r"][0])
    assert s["by_signal"] and "win_lo" in s["by_signal"][0]
    # every value must be JSON-strict (no bare NaN)
    json.loads(json.dumps(s, allow_nan=False))


def test_summary_unknown_source_is_an_error():
    assert "error" in server.api_summary("nope")


def test_alerts_and_bars_round_trip(frames):
    _seed(frames)
    alerts = server.api_alerts("replay_5m")["alerts"]
    a = alerts[0]
    d = server.api_bars("replay_5m", a["ticker"], a["session_date"])
    assert d["bars"] and {"time", "open", "high", "low", "close"} <= set(d["bars"][0])
    assert d["alert"] is not None and d["alert"]["stop"] == a["stop"]
    assert d["vwap"], "VWAP overlay data must be present"
    json.loads(json.dumps(d, allow_nan=False))


def test_bars_errors_are_specific(frames):
    _seed(frames)
    assert "error" in server.api_bars("replay_5m", "NOPE", "2026-07-24")
    t = next(iter(frames))
    assert "error" in server.api_bars("replay_5m", t, "1999-01-01")
    assert "error" in server.api_bars("replay_5m", t, "garbage")


def test_http_smoke(frames):
    _seed(frames)
    httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/")
        r = conn.getresponse()
        assert r.status == 200 and b"TechnicalEngine" in r.read()
        conn.request("GET", "/api/meta")
        r = conn.getresponse()
        assert r.status == 200
        assert json.loads(r.read())["sources"]
        conn.request("GET", "/api/summary?source=bogus")
        assert conn.getresponse().status == 400
    finally:
        httpd.shutdown()
