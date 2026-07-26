"""Market-hours gating and wake scheduling -- pure functions of a timestamp."""
import pandas as pd
import pytest

from engine import config
from engine.__main__ import market_open, next_wake


def ts(s):
    return pd.Timestamp(s, tz=config.ET)


@pytest.mark.parametrize("when,expected", [
    ("2026-07-24 09:29:59", False),   # Friday, one second early
    ("2026-07-24 09:30:00", True),
    ("2026-07-24 15:59:59", True),
    ("2026-07-24 16:00:00", False),   # close is exclusive
    ("2026-07-25 12:00:00", False),   # Saturday
    ("2026-07-26 12:00:00", False),   # Sunday
])
def test_market_open(when, expected):
    assert market_open(ts(when)) is expected


def test_next_wake_lands_just_after_the_next_bar_closes():
    assert next_wake(ts("2026-07-24 10:02:13")) == ts("2026-07-24 10:05:00") + \
        pd.Timedelta(seconds=config.WAKE_LAG_S)
    # exactly on a boundary: wait for the bar that is only now beginning
    assert next_wake(ts("2026-07-24 10:05:00")) == ts("2026-07-24 10:10:00") + \
        pd.Timedelta(seconds=config.WAKE_LAG_S)


def test_next_wake_is_always_in_the_future():
    for m in range(0, 60, 7):
        for s in (0, 17, 59):
            now = ts(f"2026-07-24 10:{m:02d}:{s:02d}")
            assert next_wake(now) > now


def test_dst_transition_does_not_raise():
    # US DST ends 2026-11-01; the 01:00-02:00 hour is ambiguous in ET
    for when in ("2026-11-01 00:58:00", "2026-03-08 01:58:00"):
        now = pd.Timestamp(when, tz=config.ET)
        assert not market_open(now)
