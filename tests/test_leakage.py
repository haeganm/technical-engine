"""Every signal must be look-ahead-safe: truncating the frame at bar t must not
change any value at or before t. Harness idea from mrm-momentum's leakage tests."""
import pandas as pd

from engine.scanner import enrich
from engine.signals import REGISTRY


def test_signals_no_lookahead(frames):
    for ticker in ("AAPL", "WBD"):
        df = enrich(frames[ticker])
        for name, spec in REGISTRY.items():
            full = spec.fn(df, **spec.defaults)
            for cut in range(len(df) - 60, len(df), 7):
                part = enrich(frames[ticker].iloc[:cut])
                trunc = spec.fn(part, **spec.defaults)
                pd.testing.assert_series_equal(
                    full.iloc[:cut], trunc, check_names=False,
                    obj=f"{name} on {ticker} truncated at {cut}")
