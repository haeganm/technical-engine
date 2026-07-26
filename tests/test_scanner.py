"""Gate, scoring, stop clamping, and the alert invariants."""
import pandas as pd
import pytest

from engine import config, scanner
from engine.scanner import scan_once
from tests.conftest import make_bars


def _breakout(or_low=99.95, sessions=12, per_session=12, spike=20000.0):
    # sessions must exceed RVOL_LOOKBACK_SESSIONS or the baseline is NaN by design
    """Breaks its opening range on the FINAL bar (the one being judged).

    Prior sessions have a 1.0-wide range, so ATR ~= 1.0 and the clamp bounds
    are predictable. `or_low` sets how far the structural stop (the opening
    range midpoint) sits from price.
    """
    closes, highs, lows, vols = [], [], [], []
    for _ in range(sessions - 1):                      # quiet history
        closes += [100.0] * per_session
        highs += [100.5] * per_session
        lows += [99.5] * per_session
        vols += [1000.0] * per_session
    closes += [100.0] * 6 + [100.06]                   # opening range, then the break
    highs += [100.05] * 6 + [100.07]
    lows += [or_low] + [99.95] * 5 + [100.0]
    vols += [1000.0] * 6 + [spike]
    return make_bars(closes, highs=highs, lows=lows, volumes=vols,
                     sessions=sessions, per_session=per_session)


def _scan(df, **kw):
    as_of = df.index[-1]
    return scan_once({"AAA": df}, as_of, **kw)


def test_alert_invariants_hold_on_the_fixture(prep, all_ts, frames):
    """Regression: the old ORB entry was the opening-range high, which could sit
    below the alert price -- 2 of 24 fixture alerts had 1R already exceeded.
    price is now always the judged bar's close, so that cannot recur."""
    seen = 0
    for as_of in all_ts[-60:]:
        for a in scanner.scan_prepared(prep, as_of, min_score=0):
            seen += 1
            assert a.stop < a.price < a.target_1 < a.target_2
            assert a.rvol >= config.RVOL_MIN_ABS
            assert a.price == round(float(frames[a.ticker].loc[as_of, "Close"]), 2)
            assert a.ts == as_of
    assert seen, "fixture should produce at least one alert"


def test_stop_clamped_up_when_structure_is_absurdly_tight():
    """A 0.06-wide structural stop is noise, not risk -- widen it to 1 ATR."""
    alerts = _scan(_breakout(or_low=99.95), min_score=0)
    assert alerts
    a = alerts[0]
    assert (a.price - a.stop) == pytest.approx(config.MIN_STOP_ATR * a.atr, rel=0.02)


def test_stop_clamped_down_when_structure_is_absurdly_wide():
    """A 5-point stop on a $100 stock is not a day trade -- cap it at 2.5 ATR."""
    alerts = _scan(_breakout(or_low=90.0), min_score=0)
    assert alerts
    a = alerts[0]
    assert (a.price - a.stop) == pytest.approx(config.MAX_STOP_ATR * a.atr, rel=0.02)


def test_no_alert_before_atr_warm_up():
    df = _breakout(sessions=2, per_session=12)   # too few bars for ATR(14)
    assert _scan(df, min_score=0) == []


def test_low_rvol_is_gated_out():
    df = _breakout(spike=1000.0)           # flat volume => RVOL ~1
    assert _scan(df, min_score=0) == []


def test_cofiring_signals_merge_into_one_alert(prep, all_ts):
    """Regression: scan_once used to `break` on the first registry match, so
    dict order picked the winner and the confluence was discarded."""
    multi = [a for ts in all_ts[-120:] for a in scanner.scan_prepared(prep, ts, min_score=0)
             if len(a.signals) > 1]
    assert multi, "fixture should contain at least one co-firing bar"
    a = multi[0]
    assert len(set(a.signals)) == len(a.signals)
    assert a.signal == a.signals[0]
    assert "confluence" in a.context


def test_min_score_filters(prep, all_ts):
    window = all_ts[-40:]
    loose = sum(len(scanner.scan_prepared(prep, ts, min_score=0)) for ts in window)
    strict = sum(len(scanner.scan_prepared(prep, ts, min_score=4)) for ts in window)
    assert strict < loose, "the score gate must actually remove alerts"


def test_spy_never_alerts_but_scores_the_market():
    """SPY rides along for regime context and must never emit an alert itself."""
    df = _breakout()
    spy = _breakout(or_low=99.9)          # SPY also 'breaking out' -> market up
    alerts = scanner.scan_once({"AAA": df, "SPY": spy}, df.index[-1], min_score=0)
    assert all(a.ticker != "SPY" for a in alerts)
    assert alerts, "the real ticker still alerts"
    assert "market up" in alerts[0].context


def test_market_down_withholds_the_regime_point():
    df = _breakout()
    n = len(df)
    spy_closes = [100.0] * (n - 1) + [90.0]   # SPY dumping under its VWAP
    spy = make_bars(spy_closes, sessions=12, per_session=12)
    with_spy = scanner.scan_once({"AAA": df, "SPY": spy}, df.index[-1], min_score=0)
    without = scanner.scan_once({"AAA": df}, df.index[-1], min_score=0)
    assert with_spy and without
    assert "market up" not in with_spy[0].context
    assert "market up" not in without[0].context, "missing SPY = no point, no crash"


def test_alert_key_is_per_ticker_per_session():
    ts = pd.Timestamp("2026-07-24 10:05", tz=config.ET)
    a = scanner.Alert("AAPL", ("orb_breakout",), 3, ts, 100.0, 99.0, 101.0, 102.0,
                      2.5, 0.4, 0.01)
    assert a.key() == ("2026-07-24", "AAPL")
    assert a.risk_pct == 0.01
