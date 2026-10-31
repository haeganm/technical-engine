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


class FetchStarved(RuntimeError):
    """Too few tickers returned bars -- almost always Yahoo throttling us."""


def setup_logging(verbose: bool = False) -> None:
    config.DATA.mkdir(exist_ok=True)
    # Windows consoles default to cp437/cp1252 and raise on any non-ASCII we log
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    handlers = [
        logging.handlers.RotatingFileHandler(
            config.DATA / "engine.log", maxBytes=config.LOG_MAX_BYTES,
            backupCount=config.LOG_BACKUPS, encoding="utf-8"),
    ]
    if sys.stdout is not None:  # absent under pythonw.exe -- file-only there
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S", handlers=handlers, force=True)
    # yfinance logs one ERROR per failed symbol per call. At 506 tickers a bad
    # Yahoo day writes ~5 MB and rotates the whole log window away in days --
    # destroying the cycle history you need to diagnose the outage. Our own
    # aggregate "fetch: N/M tickers returned usable bars" line carries the
    # signal; the per-symbol spam carries none.
    if not verbose:
        logging.getLogger("yfinance").setLevel(logging.CRITICAL)


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
            f"T1 {a.target_1:.2f}  rvol {a.rvol:.1f}")


# ---------------------------------------------------------------- commands

def cmd_fetch(period: str | None = None, interval: str = "5m") -> None:
    tickers = config.universe()
    p = config.INTERVALS[interval]
    period = period or p["fetch_period"]
    frames = data.fetch_intraday(tickers, interval=p["interval"], period=period,
                                 bar_minutes=p["bar_minutes"])
    rows = data.store_bars(frames, store=p["store"], keep_days=p["keep_days"])
    log.info("fetched %d/%d tickers (%s, period=%s); store %s: %d rows across %d sessions",
             len(frames), len(tickers), interval, period, p["store"], rows,
             data.store_sessions(p["store"]))


def _cycle(tickers: list[str], as_of: pd.Timestamp, sent: set, url: str | None,
           dry_run: bool) -> list:
    """One scan: fetch -> store -> scan the store -> dispatch -> journal."""
    t0 = time.monotonic()
    p = config.LIVE
    frames = data.fetch_intraday(tickers)
    if len(frames) < len(tickers) * config.FETCH_OK_FRACTION:
        # yfinance swallows rate limits into empty frames and returns normally,
        # so a near-empty fetch IS the throttle signal. Raising here reaches the
        # loop's backoff instead of quietly scanning nothing every 5 minutes.
        raise FetchStarved(f"only {len(frames)}/{len(tickers)} tickers returned bars")
    data.store_bars(frames, store=p["store"], keep_days=p["keep_days"])
    stored = data.load_frames(
        since=as_of - pd.Timedelta(days=config.LIVE_LOOKBACK_DAYS), store=p["store"])
    alerts = scanner.scan_once(stored, as_of)

    if dry_run:
        for a in alerts:
            log.info("[dry-run] %s", _describe(a))
            log.debug("[dry-run] embed: %s", json.dumps(notify._embed(a))[:400])
        fired = alerts
    else:
        fired = notify.dispatch(alerts, sent, lambda batch: notify.send_batch(batch, url))
        # journal everything generated, flagging delivery: an alert Discord
        # refused is never regenerated (the bar has passed), so recording only
        # delivered ones would bias the win rate toward what the webhook liked
        journal.append(alerts, delivered={a.key() for a in fired})
        for a in fired:
            log.info("ALERT %s", _describe(a))
        undelivered = len(alerts) - len(fired)
        if undelivered:
            log.warning("%d alert(s) generated but not delivered", undelivered)

    at_as_of = sum(1 for df in stored.values()
                   if len(df) and df.index[-1] == as_of)
    # store_sessions() is a full parquet read; it changes once a day, so it is
    # not worth 0.5-1.5s on every cycle just to print it.
    log.info("cycle as_of=%s fetched=%d in_store=%d at_bar=%d alerts=%d sent=%d elapsed=%.1fs",
             as_of.strftime("%Y-%m-%d %H:%M"), len(frames), len(stored), at_as_of,
             len(alerts), len(fired), time.monotonic() - t0)
    elapsed = time.monotonic() - t0
    if stored and at_as_of < len(stored) * 0.5:
        log.warning("only %d/%d tickers have a bar at %s — data may be lagging",
                    at_as_of, len(stored), as_of.strftime("%H:%M"))
    if elapsed > 240:
        log.warning("cycle took %.0fs — approaching the 5-minute cadence", elapsed)
    return fired


def _post_close(tickers: list[str]) -> None:
    """Final fetch only -- captures the true 15:55 bar in the store.
    Nothing is posted: the channel is BUY alerts (+ morning heartbeat) only."""
    p = config.LIVE
    frames = data.fetch_intraday(tickers)
    data.store_bars(frames, store=p["store"], keep_days=p["keep_days"])
    log.info("post-close fetch stored the final bars")


def _post_open_status(url: str | None, tickers: list[str], dry_run: bool) -> None:
    if dry_run or not url or not config.POST_OPEN_STATUS:
        return
    embed = notify.status_embed(
        "Scanner online",
        [f"Watching {len(tickers)} tickers · store has {data.store_sessions()} sessions",
         "BUY alerts will post here as they trigger."])
    notify.post({"embeds": [embed]}, url)
    log.info("posted morning status")


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
            stored = data.load_frames(
                since=now_et() - pd.Timedelta(days=config.LIVE_LOOKBACK_DAYS))
            newest = max((df.index[-1] for df in stored.values() if len(df)), default=None)
            if newest is not None and newest < as_of:
                log.info("market closed — judging the last stored bar (%s) instead of %s",
                         newest.strftime("%Y-%m-%d %H:%M"), as_of.strftime("%Y-%m-%d %H:%M"))
                as_of = newest
        _cycle(tickers, as_of, set(), url, dry_run)
        return

    sent: set = set()
    seeded_for = None
    closed_for = None
    opened_for = None
    backoff = 0
    while True:
        # Everything is inside the guard: a locked parquet (the dashboard holds
        # one open) or a blip in the post-close fetch must not end the process.
        # Under pythonw there is no stdout, so an escaping exception would
        # vanish without a traceback and the scanner would just be gone.
        try:
            now = now_et()
            today = str(now.date())
            if seeded_for != today:
                sent = journal.sent_keys(today)
                seeded_for = today
                log.info("session %s: %d alert(s) already sent today", today, len(sent))
            if not market_open(now):
                if (closed_for != today and now.weekday() < 5
                        and now.time() >= config.MARKET_CLOSE):
                    _post_close(tickers)
                    closed_for = today
                time.sleep(300)
                continue
            if opened_for != today:
                _post_open_status(url, tickers, dry_run)
                opened_for = today
            _cycle(tickers, scanner.last_closed_bar(now), sent, url, dry_run)
            backoff = 0
        except Exception as e:
            starved = isinstance(e, FetchStarved) or e.__class__.__name__ == "YFRateLimitError"
            if starved:
                backoff = min(backoff * 2 or 5, 60)
                log.warning("fetch starved (%s); backing off %d min", e, backoff)
                time.sleep(backoff * 60)
                continue
            log.exception("cycle failed")
            time.sleep(30)
        now = now_et()
        time.sleep(max((next_wake(now) - now).total_seconds(), 5))


def cmd_replay(date_str: str, min_score: int = config.MIN_SCORE,
               interval: str = "5m") -> None:
    p_int = config.INTERVALS[interval]
    frames = data.load_frames(store=p_int["store"])
    try:
        day = pd.Timestamp(date_str).date()
    except ValueError:
        log.error("could not parse date %r (expected YYYY-MM-DD)", date_str)
        sys.exit(CONFIG_ERROR)
    bars = sorted({ts for df in frames.values() for ts in df.index if ts.date() == day})
    if not bars:
        log.error("no bars for %s in the store — run `fetch` first", date_str)
        sys.exit(CONFIG_ERROR)
    prep = scanner.prepare(frames, p_int)
    sent: set = set()
    total = 0
    for ts in bars:
        for a in notify.dispatch(scanner.scan_prepared(prep, ts, min_score), sent, lambda b: b):
            print(_describe(a))
            total += 1
    print(f"replay {date_str}: {total} alerts across {len(bars)} bars")


def cmd_backtest(days: int | None = None, min_score: int = config.MIN_SCORE,
                 interval: str = "5m") -> None:
    """Replay every stored session through the live scan path and score it."""
    p = config.INTERVALS[interval]
    frames = data.load_frames(store=p["store"])
    if not frames:
        log.error("store %s is empty — run `fetch --interval %s` first", p["store"], interval)
        sys.exit(CONFIG_ERROR)
    all_days = sorted({ts.date() for df in frames.values() for ts in df.index})
    if days:
        all_days = all_days[-days:]
        cutoff = pd.Timestamp(all_days[0], tz=config.ET)
        frames = {t: df[df.index >= cutoff - pd.Timedelta(days=14)]  # RVOL warm-up margin
                  for t, df in frames.items()}
    journal.clear(p["replay"])
    print(f"replaying {len(all_days)} sessions at {interval} (min_score={min_score})...")
    t0 = time.monotonic()
    prep = scanner.prepare(frames, p)
    fired_all = []
    for day in all_days:
        bars = sorted({ts for df in frames.values() for ts in df.index if ts.date() == day})
        sent: set = set()
        for ts in bars:
            fired_all += notify.dispatch(scanner.scan_prepared(prep, ts, min_score),
                                         sent, lambda b: b)
    journal.append(fired_all, p["replay"])
    log.info("replayed %d sessions in %.1fs", len(all_days), time.monotonic() - t0)

    # score once and reuse: scoring groups the whole bars store (millions of rows)
    scored = journal.score_alerts(p["replay"], bars=data.load(p["store"]))
    for by in ("signal", "score"):
        print(f"\n=== by {by} ===")
        print(journal.format_stats(journal.stats(p["replay"], by=by, scored=scored)))

    # 4 slices once the sample is long enough to support them; halves otherwise
    n_splits = 4 if len(all_days) >= 80 else 2
    print(f"\n=== stability: win rate across {n_splits} time slices ===")
    for by in ("signal", "score"):
        print(journal.format_stability(
            journal.stability(p["replay"], by=by, n_splits=n_splits, scored=scored)))

    j = data.load(p["replay"])
    n = 0 if j is None else len(j)
    per_day = n / len(all_days) if all_days else 0
    print(f"\n{n} alerts over {len(all_days)} sessions ({all_days[0]} to {all_days[-1]}), "
          f"{per_day:.1f}/day, interval={interval}, min_score={min_score}.")
    if interval != "5m":
        print("NOTE: this tier runs the same signal IDEAS on hourly bars -- a proxy\n"
              "for regime coverage, not a measurement of the live 5m system.")
    m1 = config.TARGET_MULTIPLES[0]
    print("Read the stability table before believing anything above it. A win rate\n"
          "that swings between slices is tracking the market regime, not an edge --\n"
          "and picking a threshold from the better slice is how a backtest lies.\n"
          f"Targets are {m1:g}R, so breakeven needs a win rate above "
          f"{100 / (1 + m1):.0f}% plus costs.\n"
          "exp_r is expectancy in R and is the only figure comparable across\n"
          "different target settings -- positive is the bar, not a high win rate.")


def cmd_report(by: str = "signal", interval: str = "5m", replay: bool = False) -> None:
    """Hit rates from a journal, scored against the MATCHING bar store.

    The store must match the interval: scoring hourly alerts against 5m bars
    (the old default) silently produces garbage.
    """
    p = config.INTERVALS[interval]
    name = p["replay"] if replay else journal.LIVE
    df = journal.stats(name, by=by, bars=data.load(p["store"]))
    if df.empty:
        print(f"No resolved alerts in {name!r} yet.")
        return
    print(journal.format_stats(df))


def cmd_test_webhook() -> None:
    url = webhook_or_exit()
    ts = now_et().floor("5min")
    dummies = [
        scanner.Alert(ticker=f"TST{i}", signals=("orb_breakout",) if i % 2 else
                      ("orb_breakout", "hod_breakout"), score=3, ts=ts,
                      price=100.0 + i, stop=99.0 + i, target_1=102.0 + i,
                      target_2=104.0 + i, rvol=2.5, atr=0.5, day_change=0.012,
                      context=("up on day", "closed strong"))
        for i in range(12)  # 12 > 10 embeds, so this also proves batching
    ]
    delivered = notify.send_batch(dummies, url)
    print(f"delivered {len(delivered)}/{len(dummies)} test alerts in "
          f"{-(-len(dummies) // config.EMBEDS_PER_MSG)} messages. "
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
    f.add_argument("--period", default=None,
                   help="yfinance period (default: the interval's max history)")
    f.add_argument("--interval", default="5m", choices=list(config.INTERVALS))

    rp = sub.add_parser("replay", help="print alerts for one past session")
    rp.add_argument("date", help="YYYY-MM-DD")
    rp.add_argument("--min-score", type=int, default=config.MIN_SCORE)
    rp.add_argument("--interval", default="5m", choices=list(config.INTERVALS))

    bt = sub.add_parser("backtest", help="score every stored session")
    bt.add_argument("--days", type=int, default=None)
    bt.add_argument("--min-score", type=int, default=config.MIN_SCORE)
    bt.add_argument("--interval", default="5m", choices=list(config.INTERVALS))

    rep = sub.add_parser("report", help="hit rates from the live journal")
    rep.add_argument("--by", default="signal", choices=["signal", "score", "ticker"])
    rep.add_argument("--interval", default="5m", choices=list(config.INTERVALS))
    rep.add_argument("--replay", action="store_true",
                     help="score the backtest journal instead of the live one")

    srv = sub.add_parser("serve", help="local results dashboard")
    srv.add_argument("--port", type=int, default=8765)

    sub.add_parser("test-webhook", help="post dummy alerts to Discord")

    a = p.parse_args(argv)
    setup_logging(a.verbose)
    try:
        _dispatch(a)
    except Exception:
        # The scheduled task runs under pythonw, which discards stderr; without this
        # a startup crash leaves no trace (it hid a corrupt store for 25 days).
        log.exception("fatal: %s exited on an unhandled error", a.cmd)
        raise


def _dispatch(a) -> None:
    if a.cmd == "run":
        cmd_run(once=a.once, dry_run=a.dry_run)
    elif a.cmd == "fetch":
        cmd_fetch(period=a.period, interval=a.interval)
    elif a.cmd == "replay":
        cmd_replay(a.date, a.min_score, interval=a.interval)
    elif a.cmd == "backtest":
        cmd_backtest(a.days, a.min_score, interval=a.interval)
    elif a.cmd == "report":
        cmd_report(by=a.by, interval=a.interval, replay=a.replay)
    elif a.cmd == "serve":
        from .server import serve
        serve(a.port)
    elif a.cmd == "test-webhook":
        cmd_test_webhook()


if __name__ == "__main__":
    main()
