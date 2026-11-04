# TechnicalEngine

[![CI](https://github.com/haeganm/technical-engine/actions/workflows/ci.yml/badge.svg)](https://github.com/haeganm/technical-engine/actions/workflows/ci.yml) [![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

A free, always-on intraday scanner for US equities. Every 5 minutes during
market hours it pulls 5-minute bars for the **S&P 500 + Nasdaq-100** (506
tickers — the full Nasdaq exchange is ~3,400 names, which free Yahoo data
cannot sustain at this cadence), checks five day-trading long setups, and
posts BUY alerts to a Discord channel with price, stop, and 2R/3R targets.
Every alert is journaled and scored against what the market actually did.

The Discord channel is **buy-alerts only** (plus a morning "scanner online"
heartbeat). Outcomes — including every loss — are tracked in the local
dashboard (`engine serve`) and `engine report`, not posted to the channel.

**This is not financial advice, and it is not a profitable system.** See
[Does it work?](#does-it-work) — the honest answer, measured over 60 sessions,
is *no demonstrated edge*. The engine is built to measure that truthfully
rather than to hide it. Day trading is risky; most day traders lose money.

## Does it work?

No. Run `python -m engine backtest` yourself, or browse it in `python -m engine
serve`. **The shipped configuration**, over 60 sessions of 5m bars across 506
tickers (2026-04-29 to 2026-07-24) — 5,400 alerts. Expect a **median of 99
alerts per day** (mean 108, range 48–235; the backtest's headline 90/day is
diluted by the first ~10 sessions, which produce nothing while the RVOL
baseline fills). The busiest single 5-minute cycle observed was 127 alerts,
which posts as 13 Discord messages over ~13 seconds:

Targets are **2R**, so breakeven needs a win rate above **33.3%**. `exp_r` is
expectancy in R — the only figure comparable across target settings, and the
one that has to be positive:

| signal | decided | win rate | exp_r | first half | second half |
|---|---|---|---|---|---|
| gap_go | 107 | 34.6% | **+0.030** | 34.3% | 35.0% |
| vwap_reclaim | 879 | 32.7% | −0.018 | 36.3% | 28.7% |
| orb_breakout | 1,091 | 25.3% | −0.151 | 30.3% | 19.4% |
| ema9_vwap_pullback | 1,236 | 26.3% | −0.186 | 28.8% | 24.0% |
| momentum_thrust | 833 | 25.0% | −0.190 | 27.6% | 22.6% |

Overall: **−748R over 5,400 alerts** (−0.139R each; 30.8% win in the first half,
23.7% in the second). Three things were tested and each came back negative:

- **Wider targets do not rescue it.** Sweeping exit multiples over every alert:
  1R −226R, 1.5R −200R, 2R −247R, 3R −295R. The win rate falls faster than the
  payoff rises (2R needs 33.3%, delivers 27.3%), so wider targets lose *more*.
  The shipped 2R setting was chosen for the shape of the alert, not for edge.
- **No sub-population survives split-half validation.** Hour of day, price
  bucket, and confluence score were all sliced; with n≥100 in both halves,
  **nothing** clears the breakeven bar in both.
- **Tuning does not transfer.** A grid over signal sets × threshold found every
  configuration in the 50–100 alerts/day band lands at **44.5–45.3%
  out-of-sample**, while first-half figures spread 50.1–51.8%. That spread is
  noise; selecting on it is exactly how a backtest lies to you, which is why
  `backtest` prints a stability table under every other number.

Two signals — `hod_breakout` (46.7% / 44.5%) and `squeeze_breakout` (45.1% /
42.0%) — were retired for being worst in *both* halves, the only non-overfit
criterion available. Fixing `squeeze_breakout`'s session-boundary bug made it
measurably worse (47.6% → 43.7%): it had been scoring overnight gaps, not
volatility coils.

`gap_go` is the one setup with **positive expectancy** (+0.030R) that also
holds up across both halves (34.3% → 35.0%, the only signal that does not
decay). At ~2 alerts/day and 107 decided trades it is far too thin to build on
— interesting, not proven, and a single bad month would erase it. Everything
else decays into the second half by 5–11 points, which is one regime effect
rather than four independent failures.

Treat the alerts as *"go look at this chart now"*, never as trade instructions.

## Signals (long only)

| Signal | Structure |
|---|---|
| `orb_breakout` | First close above the 30-minute opening-range high |
| `vwap_reclaim` | Close crosses back above VWAP after ≥3 bars below |
| `gap_go` | ≥2% gap up that holds its open and breaks the first bar's high |
| `ema9_vwap_pullback` | Uptrend pullback tags EMA9, holds VWAP, bounces |
| `momentum_thrust` | 3 straight higher closes above VWAP on expanding range |

Two more — `hod_breakout` and `squeeze_breakout` — are implemented and tested
but **retired**: they measured worst in both halves of the sample. Clear
`config.INTERVALS["5m"]["disabled"]` to put them back under measurement.

Signals describe **structure only**. Every alert must additionally clear a
central gate — relative volume ≥2× its own time-of-day normal, and price above
VWAP — and score enough of 6 confirmations (threshold in `config.MIN_SCORE`,
currently 4, chosen for ~100 alerts/day rather than for edge):

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
.venv\Scripts\python -m engine fetch
.venv\Scripts\python -m engine test-webhook
```

The `fetch` step is not optional. Relative volume is measured against the same
time of day on the previous 10 sessions, so **a fresh clone with an empty store
produces zero alerts** until it has that much history — the baseline returns
NaN rather than a number computed from too few samples. That is by design: a
partial window would give a different answer depending on how much history
happened to be loaded. The default `fetch` pulls Yahoo's full ~60 days.

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
python -m engine report [--by score] [--replay]  hit rates from a journal
python -m engine serve [--port 8765]        results dashboard at localhost:8765
python -m engine test-webhook               post dummy alerts to Discord
pytest                                      99 tests, offline, ~60s
```

## Dashboard

`python -m engine serve` opens a local, read-only dashboard (no accounts, no
external requests — the chart library is vendored): cumulative-R equity curve,
win-rate tiles, per-signal and stability tables, and a click-through candlestick
chart for every alert with VWAP and the entry/stop/target levels drawn on it.

## The two backtest tiers

- **5m** (`backtest`) — the real system, full fidelity. yfinance only serves
  ~60 days of 5m history, but the local store is **never pruned**, so the
  sample grows past Yahoo's window every day the scanner runs.
- **1h** (`backtest --interval 1h --days 200`) — the same signal *ideas* on
  hourly bars over 200 trading days. This is a **proxy for regime coverage**,
  not a measurement of the live 5m system: the opening range becomes the first
  hour, the pullback EMA shortens to 4 bars, `squeeze_breakout` is disabled
  outright (a 7-bar session cannot host a 20-bar coil), and hourly ATR stops
  are wide enough that most trades end the day with neither target nor stop
  hit (small "decided" counts). Measured result: win rates hug 50% there too,
  in every quarter of the sample.

`run --once --dry-run` is the useful one when the market is closed: it runs the
exact live cycle against the last completed bar and prints what it *would* have
posted.

## Tuning

- **Universe**: `engine/sp500.txt` + `engine/nasdaq100.txt`, one ticker per
  line, merged and deduped by `config.universe()`. Dots become dashes
  automatically (`BRK.B` → `BRK-B`). A few valid index members are commented
  out because Yahoo serves no intraday bars for them. `config.EXTRA_TICKERS`
  (SPY) is fetched for market context and never alerts.
- **Thresholds**: all of them live in `engine/config.py` — gates, score
  components, stop clamps, cadence, retention. Change one, rerun `backtest`,
  and read the stability table before believing the result.
- **Interval profiles** (`config.INTERVALS`) are the single source of truth for
  anything that differs between the live 5m system and the 1h backtest tier.
  The module-level constants are derived from the 5m profile — never hand-set
  them. A divergence here once silently deleted 19 sessions of irreplaceable
  history.

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
