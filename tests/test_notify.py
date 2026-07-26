"""Delivery truth, batching, and rate-limit handling."""
import pandas as pd

from engine import config, notify, scanner


class FakeResponse:
    def __init__(self, status_code=204, headers=None, body=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._body = body or {}

    def json(self):
        return self._body


def _alert(i=0, ticker=None):
    ts = pd.Timestamp("2026-07-24 10:05", tz=config.ET)
    return scanner.Alert(ticker or f"T{i}", ("orb_breakout",), 3, ts,
                         100.0, 99.0, 101.0, 102.0, 2.5, 0.4, 0.01,
                         context=("up on day",))


def test_batches_at_ten_embeds(monkeypatch):
    posts = []

    def fake_post(url, json, timeout):
        posts.append(json)
        return FakeResponse()

    monkeypatch.setattr(notify.requests, "post", fake_post)
    alerts = [_alert(i) for i in range(23)]
    delivered = notify.send_batch(alerts, "http://x", sleep=lambda s: None)
    assert len(delivered) == 23
    assert [len(p["embeds"]) for p in posts] == [10, 10, 3]


def test_failed_post_is_not_delivered_and_not_marked_sent(monkeypatch):
    """Regression: a failed POST used to be journaled as delivered and then
    permanently suppressed by the dedupe set on restart."""
    monkeypatch.setattr(notify.requests, "post",
                        lambda *a, **k: FakeResponse(status_code=404))
    sent = set()
    alerts = [_alert(0)]
    fired = notify.dispatch(alerts, sent,
                            lambda b: notify.send_batch(b, "http://x", sleep=lambda s: None))
    assert fired == []
    assert sent == set(), "an undelivered alert must stay eligible for retry"


def test_retry_after_is_honored_then_succeeds(monkeypatch):
    calls, slept = [], []

    def fake_post(url, json, timeout):
        calls.append(json)
        if len(calls) == 1:
            return FakeResponse(status_code=429, headers={"Retry-After": "2.5"})
        return FakeResponse()

    monkeypatch.setattr(notify.requests, "post", fake_post)
    ok = notify.post({"embeds": []}, "http://x", sleep=slept.append)
    assert ok and slept == [2.5]


def test_network_error_is_swallowed(monkeypatch):
    def boom(*a, **k):
        raise notify.requests.RequestException("no route")

    monkeypatch.setattr(notify.requests, "post", boom)
    assert notify.post({}, "http://x", sleep=lambda s: None) is False


def test_dispatch_dedupes_within_and_across_cycles():
    log, sent = [], set()
    notify.dispatch([_alert(ticker="AAPL"), _alert(ticker="AAPL")], sent,
                    lambda b: (log.extend(b), b)[1])
    assert len(log) == 1
    notify.dispatch([_alert(ticker="AAPL")], sent, lambda b: (log.extend(b), b)[1])
    assert len(log) == 1
    notify.dispatch([_alert(ticker="NVDA")], sent, lambda b: (log.extend(b), b)[1])
    assert len(log) == 2


def test_embed_shape_is_serializable():
    import json
    e = notify._embed(_alert())
    assert json.loads(json.dumps(e))["title"].startswith("BUY NOW: T0")
    multi = _alert()
    multi.signals = ("orb_breakout", "hod_breakout")
    assert notify._embed(multi)["title"].endswith("+1")


def test_post_close_never_posts_to_discord(monkeypatch):
    """Buy-only channel: the end-of-day path stores bars but sends nothing."""
    from engine import __main__ as main_mod
    from engine import data

    monkeypatch.setattr(data, "fetch_intraday", lambda *a, **k: {})
    monkeypatch.setattr(data, "store_bars", lambda *a, **k: 0)

    def boom(*a, **k):
        raise AssertionError("post-close must not touch the webhook")

    monkeypatch.setattr(notify, "post", boom)
    monkeypatch.setattr(notify, "send_batch", boom)
    main_mod._post_close(["AAPL"])


def test_status_embed_is_neutral():
    e = notify.status_embed("Scanner online", ["watching 591 tickers"])
    assert e["color"] == notify.GREY
    for word in ("stop", "loss", "sold", "stopped"):
        assert word not in json_dump(e).lower()


def json_dump(obj):
    import json
    return json.dumps(obj)
