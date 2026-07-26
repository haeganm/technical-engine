# TechnicalEngine

A free, always-on intraday scanner for US equities. Every 5 minutes during
market hours it pulls 5-minute bars for the **S&P 500 + Nasdaq-100** (~590
tickers — the full Nasdaq exchange is ~3,400 names, which free Yahoo data
cannot sustain at this cadence), checks seven day-trading long setups, and
posts BUY alerts to a Discord channel with price, stop, and 1R/2R targets.
Every alert is journaled and scored against what the market actually did.

The Discord channel is **buy-alerts only** (plus a morning "scanner online"
heartbeat). Outcomes — including every loss — are tracked in the local
dashboard (`engine serve`) and `engine report`, not posted to the channel.

**This is not financial advice, and it is not a profitable system.** See
[Does it work?](#does-it-work) — the honest answer, measured over 41 sessions,
is *no demonstrated edge*. The engine is built to measure that truthfully
rather than to hide it. Day trading is risky; most day traders lose money.

## Does it work?

Run `python -m engine backtest` yourself, or browse it in `python -m engine
serve`. Over 41 sessions of 5m bars (2026-05-27 to 2026-07-24), 869 alerts,
~21/day — cumulative result **−40R**:

| signal | decided | win rate | first half | second half |
|---|---|---|---|---|
| vwap_reclaim | 230 | 53.9% | 59.0% | 46.2% |
| orb_breakout | 196 | 52.0% | 59.3% | 43.2% |
| ema9_vwap_pullback | 202 | 42.1% | 43.9% | 40.5% |
| hod_breakout | 141 | 39.7% | 38.5% | 41.3% |
| gap_go | 16 | n<20 | — | — |

At 1:1 reward:risk you need to win **more than 50% plus costs** to break even.
Two signals are clearly below that. The two that look acceptable overall fall
apart in the second half of the sample — and the highest-confidence bucket
(score 4) swings from 73.7% to 42.5% between halves. That is a signal tracking
the market regime, not an edge. Tuning the threshold to the good half is
exactly how a backtest lies to you, which is why `backtest` prints the
stability table underneath every other number.

Treat the alerts as *"go look at this chart now"*, never as trade instructions.

## Signals (long only)

| Signal | Structure |
|---|---|
| `orb_breakout` | First close above the 30-minute opening-range high |
| `vwap_reclaim` | Close crosses back above VWAP after ≥3 bars below |
| `hod_breakout` | Close above the prior high-of-day, after 10:00 ET |
| `gap_go` | ≥2% gap up that holds its open and breaks the first bar's high |
| `ema9_vwap_pullback` | Uptrend pullback tags EMA9, holds VWAP, bounces |
| `squeeze_breakout` | Bollinger-width contraction (bottom quartile) then a band break |
| `momentum_thrust` | 3 straight higher closes above VWAP on expanding range |

Signals describe **structure only**. Every alert must additionally clear a
central gate — relative volume ≥2× its own time-of-day normal, and price above
VWAP — and score enough of 6 confirmations (threshold in `config.MIN_SCORE`):

- up on the day
- RVOL ≥ 3×
- closed in the top 40% of the bar's range
- more than one signal firing on the same bar
- market up (SPY above its own VWAP at the same bar)
- relative-strength leader (day change beats SPY's)

Stops come from the setup's structure, then are clamped to between 1.0 and 2.5
ATR — raw structural stops ranged from 0.09% (noise) to 5.6% (not a day trade).
One alert per ticker per session.

## Setup

Requires Python 3.12+ and a Discord webhook (Server Settings → Integrations →
Webhooks → New Webhook → Copy URL).

```bat
py -3.12 -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
copy .env.example .env         :: paste your webhook URL into .env
.venv\Scripts\python -m engine fetch --period 60d
.venv\Scripts\python -m engine test-webhook
```

The `fetch --period 60d` step is not optional. Relative volume is measured
against prior sessions at the same time of day, so **a fresh clone with an
empty store produces zero alerts** until it has more than 3 sessions of
history. That is by design, not a bug.

## Run

```bat
run.bat
```

Loops during market hours, sleeps otherwise, restarts on crash, and stops on a
configuration error. Or run it directly with `python -m engine run`.

## Commands

```
python -m engine run [--once] [--dry-run]   scan loop; --dry-run never posts
python -m engine fetch [--interval 1h]      refresh a bar store (5m or 1h)
python -m engine backtest [--interval 1h] [--days 200]
python -m engine replay 2026-07-24          print one past session's alerts
python -m engine report [--by score]        hit rates from the live journal
python -m engine serve [--port 8765]        results dashboard at localhost:8765
python -m engine test-webhook               post dummy alerts to Discord
pytest                                      81 tests, offline, ~40s
```

## Dashboard

`python -m engine serve` opens a local, read-only dashboard (no accounts, no
external requests — the chart library is vendored): cumulative-R equity curve,
win-rate tiles, per-signal and stability tables, and a click-through candlestick
chart for every alert with VWAP and the entry/stop/1R/2R levels drawn on it.

## The two backtest tiers

- **5m** (`backtest`) — the real system, full fidelity. yfinance caps 5m history
  at ~60 days, so ~40 sessions is all that can exist for free.
- **1h** (`backtest --interval 1h --days 200`) — the same signal *ideas* on
  hourly bars over 200 trading days. This is a **proxy for regime coverage**,
  not a measurement of the live 5m system: the opening range becomes the first
  hour, the pullback EMA shortens to 4 bars, and hourly ATR stops are wide
  enough that most trades end the day with neither target nor stop hit (small
  "decided" counts). Measured result: win rates hug 50% there too, in every
  quarter of the sample.

`run --once --dry-run` is the useful one when the market is closed: it runs the
exact live cycle against the last completed bar and prints what it *would* have
posted.

## Tuning

- **Universe**: `engine/nasdaq100.txt`, one ticker per line. Dots become dashes
  automatically (`BRK.B` → `BRK-B`).
- **Thresholds**: all of them live in `engine/config.py` — gates, score
  components, stop clamps, cadence, retention. Change one, rerun `backtest`,
  and read the stability table before believing the result.

## Design notes

- $0 stack: yfinance 5-minute bars, parquet storage, plain webhook POSTs. The
  only secret is your webhook URL, which lives in `.env` (gitignored). If it
  leaks, anyone can post to your channel — delete the webhook in Discord and
  make a new one; there is no key to rotate.
- Yahoo intraday data is delayed, often by 15 minutes, and labels bars by
  interval *start*. The engine only ever judges bars whose interval has fully
  elapsed. This is a setup scanner, not an execution feed.
- Rule-based on purpose. No ML: honest walk-forward evaluation in a sibling
  project showed intraday ML barely beating a coin flip, and as the backtest
  above shows, the rules don't beat one either.
- Signal functions and indicators are look-ahead-safe, enforced two ways: each
  signal is truncated and recomputed bar by bar, and the whole scan is checked
  to produce identical output whether or not future bars are present. That
  causality is also what lets the backtest precompute each ticker once — 41
  sessions score in ~50 seconds instead of 2.3 hours.
- Outcomes are 5-minute-bar approximations. When 1R and the stop are both
  touched inside one bar, the order is unknowable at this resolution, so it is
  reported as its own `ambiguous` bucket rather than silently booked as a loss.
  No fills, slippage, or commissions are modeled.
