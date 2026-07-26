"""All constants + tiny .env loader. No YAML, no secrets in code.

Every path is derived from this file's location so the repo is portable and
safe to share -- nothing user-specific is ever committed.

This module is the single source of truth for every tunable. Signal functions
take their thresholds as keyword arguments defaulting to these constants, so
tests can vary them without touching globals.
"""
import os
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
UNIVERSE_FILE = Path(__file__).resolve().parent / "nasdaq100.txt"

ET = ZoneInfo("America/New_York")
MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)

# ---------------------------------------------------------------- data
INTERVAL = "5m"
PERIOD = "5d"          # incremental fetch window; use `fetch --period 60d` to backfill
BAR_MINUTES = 5
# Yahoo labels bars by interval START, so a bar is only complete at ts+BAR_MINUTES.
# We wake a full minute past the boundary to give Yahoo time to publish it.
WAKE_LAG_S = 60
KEEP_DAYS = 60         # yfinance caps 5m history at ~60d => ~40 sessions
MIN_BARS = 20          # skip tickers with fewer usable bars

# Interval profiles. "5m" is the live system; "1h" is the long-history backtest
# proxy (yfinance allows ~730d of hourly vs ~60d of 5m). Hourly adaptations:
# orb_bars=1 because the opening range IS the first hour; ema_period=4 because
# a 9-bar EMA never reaches min_periods inside a 7-bar session (the pullback
# signal would silently die); prev_session_min_bars=5 of 7.
INTERVALS = {
    "5m": dict(interval="5m", bar_minutes=5, orb_bars=6, ema_period=9,
               prev_session_min_bars=60, keep_days=60, fetch_period="60d",
               store="bars", replay="journal_replay"),
    "1h": dict(interval="1h", bar_minutes=60, orb_bars=1, ema_period=4,
               prev_session_min_bars=5, keep_days=440, fetch_period="440d",
               store="bars_1h", replay="journal_replay_1h"),
}

# ---------------------------------------------------------------- gates
RVOL_MIN_ABS = 2.0      # hard gate: bar volume vs its own time-of-day normal
RVOL_MIN_SAMPLES = 3    # prior same-slot observations required before RVOL is trusted
STRONG_RVOL = 3.0       # score point: exceptional volume
STRONG_CLOSE = 0.6      # score point: closed in the top 40% of the bar's range
MIN_SCORE = 2           # of 4 possible points -- tune with `backtest --min-score`

# ---------------------------------------------------------------- risk
MIN_STOP_ATR = 1.0      # clamp: a stop tighter than 1 ATR is noise
MAX_STOP_ATR = 2.5      # clamp: a stop wider than 2.5 ATR is not a day trade
ATR_PERIOD = 14

# ---------------------------------------------------------------- signals
ORB_BARS = 6                 # 30-minute opening range on 5m bars
RECLAIM_BELOW_BARS = 3
GAP_MIN = 0.02
PREV_SESSION_MIN_BARS = 60   # of 78; below this the prior session is too partial to trust

# ---------------------------------------------------------------- discord
EMBEDS_PER_MSG = 10     # Discord's hard limit
POST_GAP_S = 1.1        # stay inside ~5 requests / 2s
HTTP_TIMEOUT_S = 10

# ---------------------------------------------------------------- logging
LOG_MAX_BYTES = 5_000_000
LOG_BACKUPS = 3


def env(key: str) -> str | None:
    """Real environment wins; otherwise read KEY=VALUE lines from .env."""
    if os.environ.get(key):
        return os.environ[key]
    p = ROOT / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line.startswith("export "):
                line = line[7:].lstrip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                if k.strip() == key:
                    return v.strip().strip("'\"") or None
    return None


def universe() -> list[str]:
    return [
        t.strip().upper().replace(".", "-")  # yfinance uses '-' for class shares
        for t in UNIVERSE_FILE.read_text().splitlines()
        if t.strip() and not t.startswith("#")
    ]
