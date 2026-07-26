"""Numeric correctness of the indicators.

The leakage test only proves stability under truncation, which a consistently
wrong formula passes perfectly. These pin the actual definitions.
"""
import numpy as np
import pandas as pd
import pytest

from engine.indicators import atr, ema, ema_session, rvol_tod, session_vwap
from tests.conftest import make_bars


def test_vwap_matches_hand_computation():
    df = make_bars([10.0, 11.0, 12.0], highs=[11, 12, 13], lows=[9, 10, 11],
                   volumes=[100.0, 200.0, 300.0])
    v = session_vwap(df)
    tp = [(11 + 9 + 10) / 3, (12 + 10 + 11) / 3, (13 + 11 + 12) / 3]
    assert v.iloc[0] == pytest.approx(tp[0])
    assert v.iloc[1] == pytest.approx((tp[0] * 100 + tp[1] * 200) / 300)
    assert v.iloc[2] == pytest.approx((tp[0] * 100 + tp[1] * 200 + tp[2] * 300) / 600)


def test_vwap_resets_each_session():
    df = make_bars([10.0, 20.0, 30.0, 40.0], sessions=2, per_session=2)
    v = session_vwap(df)
    # first bar of session 2 must equal its own typical price, not carry session 1
    tp = (df["High"].iloc[2] + df["Low"].iloc[2] + df["Close"].iloc[2]) / 3
    assert v.iloc[2] == pytest.approx(tp)


def test_zero_volume_bar_yields_nan_not_object_dtype():
    """Regression: pd.NA upcast to object made `&` raise and killed whole cycles."""
    df = make_bars([10.0, 11.0, 12.0], volumes=[0.0, 0.0, 500.0])
    v = session_vwap(df)
    assert v.dtype == np.float64
    assert np.isnan(v.iloc[0])
    # the comparison chain a signal would build must stay boolean, never raise
    mask = (df["Close"] > v) & (df["Close"] > 0)
    assert mask.dtype == bool
    assert not mask.iloc[0]


def test_rvol_is_ratio_to_prior_sessions_same_slot():
    # 4 sessions x 2 slots; slot 0 volumes 100,200,300,400
    vols = [100.0, 50.0, 200.0, 50.0, 300.0, 50.0, 400.0, 50.0]
    df = make_bars([10.0] * 8, volumes=vols, sessions=4, per_session=2)
    r = rvol_tod(df, min_samples=1)
    assert np.isnan(r.iloc[0])                       # no prior session
    assert r.iloc[2] == pytest.approx(200 / 100)
    assert r.iloc[4] == pytest.approx(300 / 150)     # mean(100,200)
    assert r.iloc[6] == pytest.approx(400 / 200)     # mean(100,200,300)


def test_rvol_requires_min_samples():
    vols = [100.0, 200.0, 300.0, 400.0]
    df = make_bars([10.0] * 4, volumes=vols, sessions=4, per_session=1)
    r = rvol_tod(df, min_samples=3)
    assert np.isnan(r.iloc[1]) and np.isnan(r.iloc[2])   # 1 and 2 priors: not enough
    assert r.iloc[3] == pytest.approx(400 / 200)         # 3 priors: trusted


def test_ema_matches_known_recursion():
    s = pd.Series([1.0, 2.0, 3.0, 4.0])
    e = ema(s, 2)
    # alpha = 2/(2+1); seeded at the first value once min_periods is met
    assert e.iloc[1] == pytest.approx(1 * (1 / 3) + 2 * (2 / 3))
    assert np.isnan(e.iloc[0])


def test_ema_session_does_not_carry_across_sessions():
    df = make_bars([100.0] * 5 + [200.0] * 5, sessions=2, per_session=5)
    whole, per = ema(df["Close"], 3), ema_session(df, 3)
    i = 7  # third bar of session 2
    assert whole.iloc[i] < 190          # still dragged down by yesterday
    assert per.iloc[i] == pytest.approx(200.0)


def test_atr_excludes_the_overnight_gap():
    # session 1 flat at 100, session 2 gaps to 150 and stays flat
    closes = [100.0] * 3 + [150.0] * 3
    df = make_bars(closes, highs=[c + 1 for c in closes], lows=[c - 1 for c in closes],
                   sessions=2, per_session=3)
    a = atr(df, period=2)
    # true range on the gap bar must be the bar's own range (2.0), not the 50pt gap
    assert a.iloc[-1] == pytest.approx(2.0, abs=1e-9)
