"""Command line: run | fetch | replay | backtest | report | test-webhook.

Exit codes: 0 normal, 2 configuration error (run.bat stops rather than
restarting a process that can never succeed).
"""
import argparse
import json
import logging
import logging.handlers
import sys
import time

import pandas as pd

from . import config, data, journal, notify, scanner

log = logging.getLogger("engine")

CONFIG_ERROR = 2


def setup_logging(verbose: bool = False) -> None:
    config.DATA.mkdir(exist_ok=True)
    # Windows consoles default to cp437/cp1252 and raise on any non-ASCII we log
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.handlers.RotatingFileHandler(
            config.DATA / "engine.log", maxBytes=config.LOG_MAX_BYTES,
            backupCount=config.LOG_BACKUPS, encoding="utf-8"),
    ]
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S", handlers=handlers, force=True)


def now_et() -> pd.Timestamp:
    return pd.Timestamp.now(tz=config.ET)


def market_open(ts: pd.Timestamp) -> bool:
    # ponytail: no holiday calendar -- a closed weekday simply yields no new
    # bars, so the scan finds nothing. Add pandas_market_calendars only if the
    # wasted fetches ever matter.
    return ts.weekday() < 5 and config.MARKET_OPEN <= ts.time() < config.MARKET_CLOSE


def next_wake(now: pd.Timestamp) -> pd.Timestamp:
    """The moment just after the next bar closes."""
    return (now.floor(f"{config.BAR_MINUTES}min")
            + pd.Timedelta(minutes=config.BAR_MINUTES, seconds=config.WAKE_LAG_S))


def webhook_or_exit() -> str:
    url = config.env("DISCORD_WEBHOOK_URL")
    if not url:
        log.error("DISCORD_WEBHOOK_URL not set — copy .env.example to .env and fill it in")
        sys.exit(CONFIG_ERROR)
    return url


def _describe(a) -> str:
    return (f"{a.ts:%Y-%m-%d %H:%M} {a.ticker:<6} {'+'.join(a.signals):<34} "
            f"score {a.score}  px {a.price:.2f}  stop {a.stop:.2f}  "
            f"1R {a.target_1r:.2f}  rvol {a.rvol:.1f}")


# ---------------------------------------------------------------- commands

def cmd_fetch(period: str = config.PERIOD) -> None:
    tickers = config.universe()
    frames = data.fetch_intraday(tickers, period=period)
    rows = data.store_bars(frames)
    log.info("fetched %d/%d tickers (period=%s); store: %d rows across %d sessions",
             len(frames), len(tickers), period, rows, data.store_sessions())


def _cycle(tickers: list[str], as_of: pd.Timestamp, sent: set, url: str | None,
           dry_run: bool) -> list:
    """One scan: fetch -> store -> scan the store -> dispatch -> journal."""
    t0 = time.monotonic()
    frames = data.fetch_intraday(tickers)
    data.store_bars(frames)
    stored = data.load_frames()
    alerts = scanner.scan_once(stored, as_of)

    if dry_run:
        for a in alerts:
            log.info("[dry-run] %s", _describe(a))
            log.debug("[dry-run] embed: %s", json.dumps(notify._embed(a))[:400])
        fired = alerts
    else:
        fired = notify.dispatch(alerts, sent, lambda batch: notify.send_batch(batch, url))
        journal.append(fired)
        for a in fired:
            log.info("ALERT %s", _describe(a))

    at_as_of = sum(1 for df in stored.values()
                   if len(df) and df.index[-1] == as_of)
    log.info("cycle as_of=%s fetched=%d in_store=%d at_bar=%d alerts=%d sent=%d "
             "sessions=%d elapsed=%.1fs",
             as_of.strftime("%Y-%m-%d %H:%M"), len(frames), len(stored), at_as_of,
             len(alerts), len(fired), data.store_sessions(), time.monotonic() - t0)
    if stored and at_as_of < len(stored) * 0.5:
        log.warning("only %d/%d tickers have a bar at %s — data may be lagging",
                    at_as_of, len(stored), as_of.strftime("%H:%M"))
    return fired


def _post_close(session_date: str, tickers: list[str], url: str | None, dry_run: bool) -> None:
    """Final fetch (captures the true closing bar) + daily summary."""
    frames = data.fetch_intraday(tickers)
    data.store_bars(frames)
    rows = journal.daily_summary(session_date)
    embed = notify.summary_embed(session_date, rows)
    if dry_run or not url:
        log.info("[dry-run] daily summary: %d alert(s)", len(rows))
        return
    notify.post({"embeds": [embed]}, url)
    log.info("posted daily summary for %s (%d alerts)", session_date, len(rows))


def cmd_run(once: bool = False, dry_run: bool = False) -> None:
    url = None if dry_run else webhook_or_exit()
    tickers = config.universe()
    sessions = data.store_sessions()
    log.info("TechnicalEngine starting: %d tickers, store has %d sessions%s",
             len(tickers), sessions,
             "" if sessions >= config.RVOL_MIN_SAMPLES + 1 else
             f" — WARM-UP: need >{config.RVOL_MIN_SAMPLES} for RVOL, "
             f"run `fetch --period 60d` to backfill")
    if once:
        as_of = scanner.last_closed_bar(now_et())
        if dry_run:
            # off-hours there is no bar at the current wall clock; fall back to
            # the newest stored bar so this stays a useful verification command
            stored = data.load_frames()
            newest = max((df.index[-1] for df in stored.values() if len(df)), default=None)
            if newest is not None and newest < as_of:
                log.info("market closed — judging the last stored bar (%s) instead of %s",
                         newest.strftime("%Y-%m-%d %H:%M"), as_of.strftime("%Y-%m-%d %H:%M"))
                as_of = newest
        _cycle(tickers, as_of, set(), url, dry_run)
        return

    sent: set = set()
    seeded_for = None
    summarized_for = None
    backoff = 0
    while True:
        now = now_et()
        today = str(now.date())
        if seeded_for != today:
            sent = journal.sent_keys(today)
            seeded_for = today
            log.info("session %s: %d alert(s) already sent today", today, len(sent))
        if not market_open(now):
            if (summarized_for != today and now.weekday() < 5
                    and now.time() >= config.MARKET_CLOSE):
                _post_close(today, tickers, url, dry_run)
                summarized_for = today
            time.sleep(300)
            continue
        try:
            _cycle(tickers, scanner.last_closed_bar(now), sent, url, dry_run)
            backoff = 0
        except Exception as e:
            if e.__class__.__name__ == "YFRateLimitError":
                backoff = min(backoff * 2 or 5, 60)
                log.warning("yfinance rate-limited; backing off %d min", backoff)
                time.sleep(backoff * 60)
                continue
            log.exception("cycle failed")
        now = now_et()
        time.sleep(max((next_wake(now) - now).total_seconds(), 5))


def cmd_replay(date_str: str, min_score: int = config.MIN_SCORE) -> None:
    frames = data.load_frames()
    try:
        day = pd.Timestamp(date_str).date()
    except ValueError:
        log.error("could not parse date %r (expected YYYY-MM-DD)", date_str)
        sys.exit(CONFIG_ERROR)
    bars = sorted({ts for df in frames.values() for ts in df.index if ts.date() == day})
    if not bars:
        log.error("no bars for %s in the store — run `fetch` first", date_str)
        sys.exit(CONFIG_ERROR)
    prep = scanner.prepare(frames)
    sent: set = set()
    total = 0
    for ts in bars:
        for a in notify.dispatch(scanner.scan_prepared(prep, ts, min_score), sent, lambda b: b):
            print(_describe(a))
            total += 1
    print(f"replay {date_str}: {total} alerts across {len(bars)} bars")


def cmd_backtest(days: int | None = None, min_score: int = config.MIN_SCORE) -> None:
    """Replay every stored session through the live scan path and score it."""
    frames = data.load_frames()
    if not frames:
        log.error("bars store is empty — run `fetch --period 60d` first")
        sys.exit(CONFIG_ERROR)
    all_days = sorted({ts.date() for df in frames.values() for ts in df.index})
    if days:
        all_days = all_days[-days:]
    journal.clear(journal.REPLAY)
    print(f"replaying {len(all_days)} sessions (min_score={min_score})...")
    t0 = time.monotonic()
    prep = scanner.prepare(frames)
    fired_all = []
    for day in all_days:
        bars = sorted({ts for df in frames.values() for ts in df.index if ts.date() == day})
        sent: set = set()
        for ts in bars:
            fired_all += notify.dispatch(scanner.scan_prepared(prep, ts, min_score),
                                         sent, lambda b: b)
    journal.append(fired_all, journal.REPLAY)
    log.info("replayed %d sessions in %.1fs", len(all_days), time.monotonic() - t0)

    for by in ("signal", "score"):
        print(f"\n=== by {by} ===")
        print(journal.format_stats(journal.stats(journal.REPLAY, by=by)))

    print("\n=== stability: first half vs second half ===")
    print(journal.format_stability(journal.stability(journal.REPLAY, by="signal")))
    print(journal.format_stability(journal.stability(journal.REPLAY, by="score")))

    j = data.load(journal.REPLAY)
    n = 0 if j is None else len(j)
    per_day = n / len(all_days) if all_days else 0
    print(f"\n{n} alerts over {len(all_days)} sessions ({all_days[0]} to {all_days[-1]}), "
          f"{per_day:.1f}/day, min_score={min_score}.")
    print("Read the stability table before believing anything above it. A win rate\n"
          "that swings between halves is tracking the market regime, not an edge --\n"
          "and picking a threshold from the better half is how a backtest lies.\n"
          "At 1:1 reward:risk you need >50% plus costs to break even.")


def cmd_report(name: str = journal.LIVE, by: str = "signal") -> None:
    print(journal.format_stats(journal.stats(name, by=by)))


def cmd_test_webhook() -> None:
    url = webhook_or_exit()
    ts = now_et().floor("5min")
    dummies = [
        scanner.Alert(ticker=f"TST{i}", signals=("orb_breakout",) if i % 2 else
                      ("orb_breakout", "hod_breakout"), score=3, ts=ts,
                      price=100.0 + i, stop=99.0 + i, target_1r=102.0 + i,
                      target_2r=104.0 + i, rvol=2.5, atr=0.5, day_change=0.012,
                      context=("up on day", "closed strong"))
        for i in range(12)  # 12 > 10 embeds, so this also proves batching
    ]
    delivered = notify.send_batch(dummies, url)
    summary = notify.summary_embed(
        str(ts.date()),
        [{"ticker": "TST0", "signal": "orb_breakout", "outcome": "one_r"},
         {"ticker": "TST1", "signal": "hod_breakout", "outcome": "ambiguous"}],
        tape=0.12, title="Test summary")
    notify.post({"embeds": [summary]}, url)
    print(f"delivered {len(delivered)}/{len(dummies)} test alerts in "
          f"{-(-len(dummies) // config.EMBEDS_PER_MSG)} messages, plus a summary. "
          "Check your Discord channel.")


# ---------------------------------------------------------------- entry

def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="engine", description="Intraday BUY-signal scanner")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="continuous scan loop")
    r.add_argument("--once", action="store_true", help="one cycle, then exit")
    r.add_argument("--dry-run", action="store_true", help="scan and print, never post")

    f = sub.add_parser("fetch", help="refresh the local bar store")
    f.add_argument("--period", default=config.PERIOD, help="yfinance period (max 60d for 5m)")

    rp = sub.add_parser("replay", help="print alerts for one past session")
    rp.add_argument("date", help="YYYY-MM-DD")
    rp.add_argument("--min-score", type=int, default=config.MIN_SCORE)

    bt = sub.add_parser("backtest", help="score every stored session")
    bt.add_argument("--days", type=int, default=None)
    bt.add_argument("--min-score", type=int, default=config.MIN_SCORE)

    rep = sub.add_parser("report", help="hit rates from the live journal")
    rep.add_argument("--by", default="signal", choices=["signal", "score", "ticker"])

    sub.add_parser("test-webhook", help="post dummy alerts to Discord")

    a = p.parse_args(argv)
    setup_logging(a.verbose)
    if a.cmd == "run":
        cmd_run(once=a.once, dry_run=a.dry_run)
    elif a.cmd == "fetch":
        cmd_fetch(period=a.period)
    elif a.cmd == "replay":
        cmd_replay(a.date, a.min_score)
    elif a.cmd == "backtest":
        cmd_backtest(a.days, a.min_score)
    elif a.cmd == "report":
        cmd_report(by=a.by)
    elif a.cmd == "test-webhook":
        cmd_test_webhook()


if __name__ == "__main__":
    main()
