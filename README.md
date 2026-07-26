# TechnicalEngine

A free, always-on intraday scanner for US equities. Every 5 minutes during
market hours it pulls 5-minute bars for the Nasdaq-100, checks five classic
day-trading long setups, and posts high-confidence **BUY NOW** alerts to a
Discord channel — with entry, stop, and 1R/2R targets. Every alert is journaled
and scored against what the market actually did, so the channel's hit rate is
measurable, not vibes.

**This is not financial advice. Signals are mechanical patterns, not
predictions. Day trading is risky; most day traders lose money.**

## Signals (long only)

| Signal | Idea |
|---|---|
| `orb_breakout` | First close above the 30-min opening range on ≥2x volume |
| `vwap_reclaim` | Close crosses back above VWAP after ≥3 bars below |
| `hod_breakout` | New high-of-day after 10:00 ET on elevated volume |
| `gap_go` | ≥2% gap up that holds and breaks the first bar's high |
| `ema9_vwap_pullback` | Uptrend pullback tags EMA9, holds VWAP, bounces |

An alert only fires when the ticker also passes the confluence gate: time-of-day
relative volume in the top 15% of the universe **and** ≥2x its own normal,
price above VWAP, and up on the day. One alert per ticker per session.

## Setup

Requires Python 3.12+ and a Discord webhook
(Server Settings → Integrations → Webhooks → New Webhook → Copy URL).

```
py -3.12 -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env      # then paste your webhook URL into .env
.venv\Scripts\python -m engine test-webhook
```

## Run

```
run.bat                     # scan loop + auto-restart on crash
```

Or directly: `python -m engine run`. Outside market hours it just sleeps.

## Other commands

```
python -m engine fetch              # one-off: seed/refresh the local bar store
python -m engine replay 2026-07-24  # dry-run a past session (no Discord posts)
python -m engine report             # per-signal hit rates from the alert journal
pytest                              # leakage, end-to-end, dedupe tests
```

## Design notes

- $0 stack: yfinance 5m bars (slightly delayed — this scans for setups, it is
  not an execution feed), parquet storage, plain webhook POSTs. No API keys
  except your own webhook URL, which lives only in `.env` (gitignored).
- Rule-based on purpose. No ML: honest walk-forward evaluation in a sibling
  project showed intraday ML edge barely beats a coin flip. The journal
  (`engine report`) is the arbiter of whether these signals earn their pings.
- All signals are vectorized and look-ahead-safe (enforced by tests).
