"""Signal firing semantics."""
import pandas as pd

from engine import config
from engine.scanner import enrich
from engine.signals import REGISTRY, orb_breakout, prev_session_close
from tests.conftest import make_bars


def _orb_frame(breakout_vol=1000.0):
    """Session 2 breaks its opening range at bar 6."""
    closes = [100.0] * 12 + [100.0] * 6 + [102.0] * 6
    vols = [1000.0] * 18 + [breakout_vol] * 6
    return enrich(make_bars(closes, volumes=vols, sessions=2, per_session=12))


def test_orb_fires_on_the_breakout_bar_not_later():
    """Regression: the RVOL condition used to sit inside the once-per-session
    debounce, so a soft-volume breakout suppressed the setup and it re-fired
    hours later while still quoting the stale opening-range high as entry."""
    df = _orb_frame(breakout_vol=1000.0)   # deliberately unremarkable volume
    fires = orb_breakout(df)
    assert fires.iloc[18]                  # the true breakout bar
    assert fires.sum() == 1


def test_a_rejected_bar_does_not_burn_the_sessions_only_shot():
    """The gate lives outside the signal, so a bar the gate rejects must not
    consume the session's chance to fire."""
    # break, fall back inside the range, then break again
    closes = [100.0] * 6 + [102.0, 99.5, 99.5, 102.5, 102.5, 102.5]
    df = enrich(make_bars(closes, per_session=12))
    fires = orb_breakout(df)
    assert fires.sum() >= 2, "a re-break must be allowed to fire again"


def test_every_signal_returns_a_boolean_series(frames):
    df = enrich(next(iter(frames.values())))
    for name, spec in REGISTRY.items():
        mask = spec.fn(df)
        assert mask.dtype == bool, name
        assert len(mask) == len(df), name


def test_prev_session_close_rejects_a_partial_prior_session():
    partial = make_bars([100.0] * 100, sessions=2, per_session=50)
    assert prev_session_close(partial) is None, "50 bars < PREV_SESSION_MIN_BARS"

    long_enough = make_bars([100.0] * 160, sessions=2, per_session=80)
    assert prev_session_close(long_enough) == 100.0


def test_gap_go_silent_without_a_trustworthy_prior_close():
    df = enrich(make_bars([100.0] * 12, per_session=12))
    assert not REGISTRY["gap_go"].fn(df).any()


def test_squeeze_breakout_fires_after_contraction():
    # wide chop (fat bands) -> tight coil (bottom-quartile width) -> breakout.
    # The width baseline must contain the wide phase, otherwise "tight" is
    # meaningless -- percentile rank of a constant series is ~0.5, not low.
    wide = [100.0 + (1.0 if i % 2 else -1.0) for i in range(40)]
    tight = [100.0 + (0.02 if i % 2 else -0.02) for i in range(30)]
    closes = wide + tight + [102.0]
    df = enrich(make_bars(closes, per_session=78))
    fires = REGISTRY["squeeze_breakout"].fn(df)
    assert fires.iloc[-1], "the breakout bar must fire"
    assert not fires.iloc[40:70].any(), "no fire while still coiled inside the bands"


def test_momentum_thrust_needs_three_higher_closes_above_vwap():
    base = [100.0] * 30
    up = [100.5, 101.2, 102.1]     # 3 higher closes, expanding range
    highs = [c + 0.3 for c in base] + [101.2, 102.2, 103.4]
    lows = [c - 0.3 for c in base] + [100.0, 100.9, 101.8]
    df = enrich(make_bars(base + up, highs=highs, lows=lows, per_session=33))
    fires = REGISTRY["momentum_thrust"].fn(df)
    assert fires.iloc[-1]
    flat = enrich(make_bars([100.0] * 33, per_session=33))
    assert not REGISTRY["momentum_thrust"].fn(flat).any()


def test_signals_are_look_ahead_safe(frames):
    """Truncate-and-recompute across session boundaries and early bars."""
    for ticker in list(frames)[:3]:
        df = frames[ticker]
        for cut in range(config.MIN_BARS + 5, len(df), 37):
            full = enrich(df)
            part = enrich(df.iloc[:cut])
            for name, spec in REGISTRY.items():
                pd.testing.assert_series_equal(
                    spec.fn(full).iloc[:cut], spec.fn(part),
                    check_names=False, obj=f"{name} on {ticker} cut at {cut}")
