"""Outcome scoring -- the only arbiter of whether the signals earn their pings."""
import pandas as pd
import pytest

from engine import config, data, journal, scanner


@pytest.fixture(autouse=True)
def tmp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA", tmp_path)
    yield tmp_path


def _alert(ticker="AAA", price=100.0, stop=99.0, t1=101.0, hhmm="10:00"):
    ts = pd.Timestamp(f"2026-07-24 {hhmm}", tz=config.ET)
    return scanner.Alert(ticker, ("orb_breakout",), 3, ts, price, stop, t1,
                         price + 2 * (price - stop), 2.5, 0.4, 0.01)


def _bars(rows):
    """rows: list of (hhmm, high, low, close)"""
    return pd.DataFrame([{
        "ts": pd.Timestamp(f"2026-07-24 {h}", tz=config.ET), "ticker": "AAA",
        "Open": c, "High": hi, "Low": lo, "Close": c, "Volume": 1000.0,
    } for h, hi, lo, c in rows])


def test_same_bar_1r_and_stop_is_ambiguous_not_a_loss():
    """When both levels print inside one 5m bar the intrabar order is
    unknowable at this resolution, so it gets its own bucket."""
    journal.append([_alert()])
    bars = _bars([("10:05", 101.5, 98.5, 100.0)])   # both touched, same bar
    scored = journal.score_alerts(bars=bars)
    assert scored.iloc[0]["outcome"] == "ambiguous"

    s = journal.stats(bars=bars)
    assert s.iloc[0]["ambiguous"] == 1
    assert s.iloc[0]["one_r_lo"] == 0.0 and s.iloc[0]["one_r_hi"] == 1.0


def test_clean_win_and_clean_loss():
    journal.append([_alert()])
    win = _bars([("10:05", 100.5, 99.8, 100.2), ("10:10", 101.5, 100.0, 101.4)])
    assert journal.score_alerts(bars=win).iloc[0]["outcome"] == "one_r"

    loss = _bars([("10:05", 100.2, 98.5, 98.6)])
    assert journal.score_alerts(bars=loss).iloc[0]["outcome"] == "stop"


def test_still_open_at_close():
    journal.append([_alert()])
    flat = _bars([("10:05", 100.2, 99.8, 100.0)])
    assert journal.score_alerts(bars=flat).iloc[0]["outcome"] == "open"


def test_unresolved_alerts_are_counted_not_dropped():
    journal.append([_alert()])
    scored = journal.score_alerts(bars=_bars([]))
    assert scored.iloc[0]["outcome"] == "unresolved"
    assert scored.iloc[0]["reason"]
    assert journal.stats(bars=_bars([])).empty  # nothing resolved to aggregate


def test_forward_returns():
    journal.append([_alert()])
    bars = _bars([("10:05", 100.2, 99.9, 100.0), ("10:30", 102.2, 101.9, 102.0),
                  ("11:00", 103.2, 102.9, 103.0)])
    row = journal.score_alerts(bars=bars).iloc[0]
    assert row["fwd_30m"] == pytest.approx(0.02)
    assert row["fwd_60m"] == pytest.approx(0.03)
    assert row["fwd_eod"] == pytest.approx(0.03)


def test_append_is_idempotent_and_keeps_the_first_call():
    journal.append([_alert(price=100.0)])
    journal.append([_alert(price=555.0)])
    j = data.load(journal.LIVE)
    assert len(j) == 1 and j.iloc[0]["price"] == 100.0


def test_sent_keys_round_trip():
    journal.append([_alert(ticker="AAA"), _alert(ticker="BBB")])
    assert journal.sent_keys("2026-07-24") == {("2026-07-24", "AAA"), ("2026-07-24", "BBB")}
    assert journal.sent_keys("2026-07-23") == set()


def test_stability_splits_the_sample_and_reports_the_swing():
    """A signal that wins in one half and loses in the other is tracking the
    regime, not an edge. This table is what stops a backtest from lying."""
    alerts, bars = [], []
    for i, day in enumerate(["07-20", "07-21", "07-22", "07-23"]):
        ts = pd.Timestamp(f"2026-{day} 10:00", tz=config.ET)
        alerts.append(scanner.Alert("AAA", ("orb_breakout",), 3, ts,
                                    100.0, 99.0, 101.0, 102.0, 2.5, 0.4, 0.01))
        won = i < 2                      # wins in the first half, loses in the second
        hi, lo, close = (101.5, 99.8, 101.4) if won else (100.2, 98.5, 98.6)
        bars.append({"ts": ts + pd.Timedelta(minutes=5), "ticker": "AAA",
                     "Open": close, "High": hi, "Low": lo, "Close": close, "Volume": 1e3})
    journal.append(alerts)
    data.save("bars", pd.DataFrame(bars))
    st = journal.stability(journal.LIVE, by="signal")
    row = st.iloc[0]
    assert row["win_p1"] == 1.0 and row["win_p2"] == 0.0
    assert row["swing"] == -1.0
    assert "-100.0%" in journal.format_stability(st)
    # 4 sessions cannot support 4 splits (needs >= 2 per slice)
    assert journal.stability(journal.LIVE, n_splits=4).empty


def test_r_result_maps_outcomes():
    journal.append([_alert()])
    win = _bars([("10:05", 101.5, 100.0, 101.4)])
    assert journal.score_alerts(bars=win).iloc[0]["r_result"] == 1.0
    loss = _bars([("10:05", 100.2, 98.5, 98.6)])
    assert journal.score_alerts(bars=loss).iloc[0]["r_result"] == -1.0
    both = _bars([("10:05", 101.5, 98.5, 100.0)])
    assert journal.score_alerts(bars=both).iloc[0]["r_result"] == 0.0


def test_stability_needs_enough_sessions():
    journal.append([_alert()])
    assert journal.stability(journal.LIVE).empty


def test_stats_schema_is_stable_when_empty():
    assert journal.stats().empty
    assert "No resolved alerts" in journal.format_stats(journal.stats())


def test_thin_samples_are_reported_as_such():
    journal.append([_alert()])
    bars = _bars([("10:05", 100.2, 99.9, 100.0), ("10:10", 101.5, 100.0, 101.4)])
    text = journal.format_stats(journal.stats(bars=bars), min_n=20)
    assert "n<20" in text
