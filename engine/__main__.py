"""Entry points: run | fetch | replay <date> | report | test-webhook."""
import sys
import time

import pandas as pd

from . import config, data, journal, notify, scanner


def _now() -> pd.Timestamp:
    return pd.Timestamp.now(tz=config.ET)


def _market_open(ts: pd.Timestamp) -> bool:
    # ponytail: no holiday/half-day calendar -- closed days just scan an empty tape
    return ts.weekday() < 5 and config.MARKET_OPEN <= ts.time() < config.MARKET_CLOSE


def _sleep_to_next_bar():
    now = _now()
    nxt = (now.floor(f"{config.BAR_MINUTES}min")
           + pd.Timedelta(minutes=config.BAR_MINUTES, seconds=config.WAKE_LAG_S))
    time.sleep(max((nxt - now).total_seconds(), 5))


def _print(a):
    print(f"{a.ts:%Y-%m-%d %H:%M} {a.ticker:<6} {a.signal:<20} "
          f"px {a.price:.2f} entry {a.entry:.2f} stop {a.stop:.2f} rvol {a.rvol:.1f}")


def cmd_fetch():
    frames = data.fetch_intraday(config.universe())
    n = data.store_bars(frames)
    print(f"fetched {len(frames)} tickers, bars store now {n} rows")


def cmd_run():
    webhook = config.env("DISCORD_WEBHOOK_URL")
    if not webhook:
        sys.exit("DISCORD_WEBHOOK_URL not set — copy .env.example to .env and fill it in")
    tickers = config.universe()
    sent = journal.sent_keys(str(_now().date()))
    print(f"scanning {len(tickers)} tickers every {config.BAR_MINUTES}m during RTH…")
    while True:
        now = _now()
        if not _market_open(now):
            time.sleep(300)
            continue
        try:
            frames = data.fetch_intraday(tickers)
            data.store_bars(frames)
            cut = now - pd.Timedelta(minutes=config.STALE_BAR_MIN)
            frames = {t: df for t, df in frames.items() if df.index[-1] >= cut}
            alerts = scanner.scan_once(frames)
            fired = notify.dispatch(alerts, sent, lambda a: notify.send(a, webhook))
            journal.append(fired)
            for a in fired:
                _print(a)
        except Exception as e:  # a failed cycle is skipped; the next is 5 min away
            print(f"cycle failed: {e}")
        _sleep_to_next_bar()


def cmd_replay(date_str: str):
    frames = data.load_frames()
    day = pd.Timestamp(date_str).date()
    ts_all = sorted({ts for df in frames.values() for ts in df.index if ts.date() == day})
    if not ts_all:
        sys.exit(f"no bars for {date_str} in store — run `py -m engine fetch` first")
    sent: set = set()
    fired_total = 0
    for ts in ts_all:
        cut = {t: df[df.index <= ts] for t, df in frames.items()}
        cut = {t: df for t, df in cut.items() if len(df) and df.index[-1] == ts}
        fired = notify.dispatch(scanner.scan_once(cut), sent, lambda a: None)
        for a in fired:
            _print(a)
        fired_total += len(fired)
    print(f"replay {date_str}: {fired_total} alerts across {len(ts_all)} bars")


def cmd_report():
    df = journal.stats()
    print("journal empty — nothing to score yet" if df.empty else df.to_string(index=False))


def cmd_test_webhook():
    webhook = config.env("DISCORD_WEBHOOK_URL")
    if not webhook:
        sys.exit("DISCORD_WEBHOOK_URL not set — copy .env.example to .env and fill it in")
    dummy = scanner.Alert(ticker="TEST", signal="test_webhook", ts=_now(), price=100.0,
                          entry=100.5, stop=99.5, target_1r=101.5, target_2r=102.5,
                          rvol=2.5, rvol_pctile=0.95)
    ok = notify.send(dummy, webhook)
    print("webhook OK — check your Discord channel" if ok else "webhook FAILED")


def main():
    cmds = {"run": cmd_run, "fetch": cmd_fetch, "report": cmd_report,
            "test-webhook": cmd_test_webhook}
    if len(sys.argv) >= 3 and sys.argv[1] == "replay":
        cmd_replay(sys.argv[2])
    elif len(sys.argv) >= 2 and sys.argv[1] in cmds:
        cmds[sys.argv[1]]()
    else:
        sys.exit("usage: py -m engine [run | fetch | replay YYYY-MM-DD | report | test-webhook]")


if __name__ == "__main__":
    main()
