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
UNIVERSE_FILES = [Path(__file__).resolve().parent / f
                  for f in ("sp500.txt", "nasdaq100.txt")]
EXTRA_TICKERS = ["SPY"]  # fetched for regime/RS context; never alertable

ET = ZoneInfo("America/New_York")
MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)

# ---------------------------------------------------------------- data
# Interval profiles are the SINGLE SOURCE OF TRUTH for anything that differs
# between the live 5m system and the 1h long-history backtest proxy. The
# module-level names below are derived from the 5m profile -- they must never
# be hand-set, because a divergence here already cost 19 sessions of
# irreplaceable history once (a bare store_bars() took a 60-day default while
# the profile said never prune).
#
# 1h adaptations: orb_bars=1 (the opening range IS the first hour); ema_period=4
# (a 9-bar EMA never reaches min_periods inside a 7-bar session, so the pullback
# signal would silently die); prev_session_min_bars=5 of 7.
INTERVALS = {
    # keep_days is effectively "never prune": Yahoo serves only ~60d of 5m
    # history, so anything older in our store is irreplaceable. ~10 MB/month.
    "5m": dict(interval="5m", bar_minutes=5, orb_bars=6, ema_period=9,
               prev_session_min_bars=60, keep_days=3650, fetch_period="60d",
               store="bars", replay="journal_replay",
               squeeze_bars=20, thrust_bars=3,
               # signals whose windows cannot be made meaningful at this
               # interval; empty here, see the 1h profile
               disabled=()),
    # A 7-bar hourly session cannot host a 20-bar coil or a 3-bar thrust that
    # still leaves room for the opening drive, so both are disabled rather than
    # left to compute something meaningless and report it as evidence.
    "1h": dict(interval="1h", bar_minutes=60, orb_bars=1, ema_period=4,
               prev_session_min_bars=5, keep_days=440, fetch_period="440d",
               store="bars_1h", replay="journal_replay_1h",
               squeeze_bars=4, thrust_bars=2,
               disabled=("squeeze_breakout",)),
}
LIVE = INTERVALS["5m"]

INTERVAL = LIVE["interval"]
BAR_MINUTES = LIVE["bar_minutes"]
KEEP_DAYS = LIVE["keep_days"]
ORB_BARS = LIVE["orb_bars"]
PREV_SESSION_MIN_BARS = LIVE["prev_session_min_bars"]

# Incremental per-cycle fetch. The live scan reads the local store, so each
# cycle only needs to top up today's bars -- at ~500 tickers a 5d payload
# per cycle would be pointless load on Yahoo.
PERIOD = "1d"
# Yahoo labels bars by interval START, so a bar is only complete at ts+BAR_MINUTES.
# We wake a full minute past the boundary to give Yahoo time to publish it.
WAKE_LAG_S = 60
MIN_BARS = 20          # skip tickers with fewer usable bars
# How much history each LIVE cycle loads. The store grows without bound, but a
# scan only needs the RVOL baseline plus indicator warm-up. ~20 calendar days is
# ~14 sessions: enough to fill RVOL_LOOKBACK_SESSIONS (so live and backtest
# agree exactly) while keeping the 5-minute cycle fast as the archive grows.
LIVE_LOOKBACK_DAYS = 20
# Fraction of the universe that must return usable bars before we trust a fetch.
# yfinance swallows per-ticker errors (including rate limits) into empty frames
# and returns normally, so a near-empty result IS the throttling signal.
FETCH_OK_FRACTION = 0.25

# ---------------------------------------------------------------- gates
RVOL_MIN_ABS = 2.0      # hard gate: bar volume vs its own time-of-day normal
RVOL_MIN_SAMPLES = 3    # deprecated: RVOL_LOOKBACK_SESSIONS is now both the window
                        # and the minimum-sample rule. Kept only for the warm-up message.
# Fixed-length RVOL baseline. It must be a WINDOW, not an expanding mean: live
# loads a bounded slice of the store while the backtest loads all of it, and an
# expanding baseline would therefore compute different RVOL for the same bar in
# the two paths -- against a hard 2.0 gate that silently reclassifies alerts.
RVOL_LOOKBACK_SESSIONS = 10
STRONG_RVOL = 3.0       # score point: exceptional volume
STRONG_CLOSE = 0.6      # score point: closed in the top 40% of the bar's range
MIN_SCORE = 2           # of 6 possible points -- tune with `backtest --min-score`

# ---------------------------------------------------------------- risk
MIN_STOP_ATR = 1.0      # clamp: a stop tighter than 1 ATR is noise
MAX_STOP_ATR = 2.5      # clamp: a stop wider than 2.5 ATR is not a day trade
ATR_PERIOD = 14

# ---------------------------------------------------------------- signals
# ORB_BARS and PREV_SESSION_MIN_BARS are derived from the interval profile above
# -- do not redefine them here.
RECLAIM_BELOW_BARS = 3
GAP_MIN = 0.02
SQUEEZE_BARS = 20            # Bollinger window for squeeze_breakout
SQUEEZE_PCTILE = 0.25        # band width must be in the bottom quartile of its history
SQUEEZE_MIN_SESSION_BARS = 6  # bars into the session before a coil is in-session
THRUST_BARS = 3              # consecutive higher closes for momentum_thrust

# ---------------------------------------------------------------- discord
EMBEDS_PER_MSG = 10     # Discord's hard limit
POST_GAP_S = 1.1        # stay inside ~5 requests / 2s
HTTP_TIMEOUT_S = 10
POST_OPEN_STATUS = True  # morning "scanner online" post -- the dead-man's switch.
                         # The channel is otherwise BUY alerts only: no summaries,
                         # no outcomes, nothing negative. Honesty lives in the
                         # dashboard and `engine report`.

# ---------------------------------------------------------------- logging
LOG_MAX_BYTES = 5_000_000
LOG_BACKUPS = 9          # ~50 MB; a noisy failure day must not erase the history
                         # of the failure (see the yfinance muting in setup_logging)


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
    """S&P 500 + Nasdaq-100, deduped and normalized. SPY etc. ride along via
    EXTRA_TICKERS for market context but are excluded from alerting."""
    seen: dict[str, None] = {}
    for path in UNIVERSE_FILES:
        for t in path.read_text().splitlines():
            t = t.strip().upper().replace(".", "-")  # yfinance class-share form
            if t and not t.startswith("#"):
                seen[t] = None
    return sorted(seen) + [t for t in EXTRA_TICKERS if t not in seen]
