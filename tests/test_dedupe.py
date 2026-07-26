"""The same (session, ticker) must never notify twice."""
import pandas as pd

from engine.notify import dispatch
from engine.scanner import Alert


def _alert(ticker="AAPL", signal="orb_breakout"):
    return Alert(ticker=ticker, signal=signal,
                 ts=pd.Timestamp("2026-07-24 10:05", tz="America/New_York"),
                 price=100.0, entry=100.5, stop=99.5, target_1r=101.5,
                 target_2r=102.5, rvol=2.5, rvol_pctile=0.9)


def test_dispatch_dedupes():
    sent_log, sent_keys = [], set()
    dispatch([_alert(), _alert()], sent_keys, sent_log.append)
    assert len(sent_log) == 1
    # second cycle, same alert (even under a different signal name): still silent
    dispatch([_alert(signal="hod_breakout")], sent_keys, sent_log.append)
    assert len(sent_log) == 1
    # a different ticker goes through
    dispatch([_alert(ticker="NVDA")], sent_keys, sent_log.append)
    assert len(sent_log) == 2
