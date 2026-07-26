"""All constants + tiny .env loader. No YAML, no secrets in code.

Every path is derived from this file's location so the repo is portable and
safe to share -- nothing user-specific is ever committed.
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

INTERVAL = "5m"
PERIOD = "5d"          # backfill window per fetch; grows local store to ~10 sessions
BAR_MINUTES = 5
WAKE_LAG_S = 40        # scan ~40s after each 5m boundary so the closed bar is in
KEEP_DAYS = 15         # prune bars store beyond this
STALE_BAR_MIN = 20     # skip a ticker if its last bar is older than this

RVOL_MIN_PCTILE = 0.85  # universe-relative time-of-day RVOL gate ("high confidence")
RVOL_MIN_ABS = 2.0      # absolute RVOL floor -- percentile alone passes on quiet tapes
MIN_BARS = 20           # skip tickers with fewer usable bars


def env(key: str) -> str | None:
    """Real environment wins; otherwise read KEY=VALUE lines from .env."""
    if os.environ.get(key):
        return os.environ[key]
    p = ROOT / ".env"
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
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
