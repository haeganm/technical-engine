"""Discord webhook notifier.

Two rules everything else follows from:
  1. Only alerts that Discord actually accepted count as sent. Marking an
     undelivered alert as sent loses it permanently (the journal re-seeds the
     dedupe set on restart) while reporting success.
  2. Respect the rate limit. Discord takes <=10 embeds per message and about
     5 requests per 2 seconds; a burst of alerts posted in a tight loop gets
     429'd, and ignoring Retry-After escalates to an IP-level block.
"""
import logging
import time

import requests

from . import config

log = logging.getLogger(__name__)

GREEN = 0x2ECC71
BLUE = 0x3498DB
GREY = 0x95A5A6
TRADINGVIEW = "https://www.tradingview.com/chart/?symbol={ticker}"

OUTCOME_LABEL = {
    "one_r": "hit 1R",
    "stop": "stopped",
    "ambiguous": "1R and stop same bar",
    "open": "open at close",
    "unresolved": "unresolved",
}


def _embed(a) -> dict:
    risk = a.price - a.stop
    title = f"BUY NOW: {a.ticker} — {a.signal}"
    if len(a.signals) > 1:
        title += f" +{len(a.signals) - 1}"
    fields = [
        {"name": "Price / Entry", "value": f"${a.price:,.2f}", "inline": True},
        {"name": "Stop", "value": f"${a.stop:,.2f}  ({a.risk_pct:.2%})", "inline": True},
        {"name": "Targets", "value": f"1R ${a.target_1r:,.2f}\n2R ${a.target_2r:,.2f}",
         "inline": True},
        {"name": "Risk / share", "value": f"${risk:,.2f}", "inline": True},
        {"name": "RVOL", "value": f"{a.rvol:.1f}x", "inline": True},
        {"name": "ATR", "value": f"${a.atr:,.2f}", "inline": True},
        {"name": f"Score {a.score}/4", "value": ", ".join(a.context) or "—", "inline": False},
    ]
    if a.day_change is not None:
        fields.insert(3, {"name": "Day", "value": f"{a.day_change:+.2%}", "inline": True})
    return {
        "title": title,
        "url": TRADINGVIEW.format(ticker=a.ticker),
        "color": GREEN,
        "timestamp": a.ts.isoformat(),
        "fields": fields,
        "footer": {"text": "TechnicalEngine — mechanical signal, not financial advice"},
    }


def summary_embed(session_date: str, rows: list[dict], tape: float | None = None,
                  title: str | None = None) -> dict:
    """Post-close recap: what fired and what it did."""
    if rows:
        lines = [f"`{r['ticker']:<6}` {r['signal']:<20} {OUTCOME_LABEL.get(r['outcome'], r['outcome'])}"
                 for r in rows]
        desc = "\n".join(lines[:25])
        if len(rows) > 25:
            desc += f"\n…and {len(rows) - 25} more"
    else:
        desc = "No alerts fired today."
    fields = []
    if tape is not None:
        fields.append({"name": "Tape heat",
                       "value": f"{tape:.0%} of universe above {config.RVOL_MIN_ABS:g}x RVOL",
                       "inline": True})
    return {
        "title": title or f"Daily summary — {session_date}",
        "description": desc,
        "color": BLUE if rows else GREY,
        "fields": fields,
        "footer": {"text": "Outcomes are 5-minute-bar approximations; "
                           "'both' = 1R and stop hit in the same bar"},
    }


def status_embed(title: str, lines: list[str]) -> dict:
    return {"title": title, "description": "\n".join(lines), "color": GREY}


# ---------------------------------------------------------------- transport

def post(payload: dict, url: str, sleep=time.sleep) -> bool:
    """POST one message. Honors Retry-After once. Never raises."""
    for attempt in (1, 2):
        try:
            r = requests.post(url, json=payload, timeout=config.HTTP_TIMEOUT_S)
        except requests.RequestException as e:
            log.warning("webhook POST failed: %s", e.__class__.__name__)
            return False
        if r.status_code < 300:
            return True
        if r.status_code == 429 and attempt == 1:
            wait = r.headers.get("Retry-After")
            if wait is None:
                try:
                    wait = r.json().get("retry_after")
                except ValueError:
                    wait = None
            wait = float(wait) if wait is not None else config.POST_GAP_S
            log.warning("webhook rate-limited, waiting %.1fs", wait)
            sleep(min(wait, 60))
            continue
        log.warning("webhook rejected the message: HTTP %s", r.status_code)
        return False
    return False


def send_batch(alerts: list, url: str, sleep=time.sleep) -> list:
    """Post alerts in <=10-embed messages. Returns only those actually delivered."""
    delivered = []
    chunks = [alerts[i:i + config.EMBEDS_PER_MSG]
              for i in range(0, len(alerts), config.EMBEDS_PER_MSG)]
    for i, chunk in enumerate(chunks):
        if i:
            sleep(config.POST_GAP_S)
        if post({"embeds": [_embed(a) for a in chunk]}, url, sleep=sleep):
            delivered.extend(chunk)
        else:
            log.error("dropped %d alert(s): %s", len(chunk),
                      ", ".join(a.ticker for a in chunk))
    return delivered


def dispatch(alerts: list, sent: set, send_fn) -> list:
    """Send alerts not already sent this session. `send_fn(batch) -> delivered`.

    Only delivered alerts join `sent`, so a failed POST is retried next cycle
    instead of being silently swallowed and permanently suppressed.
    """
    fresh, seen = [], set()
    for a in alerts:
        k = a.key()
        if k in sent or k in seen:
            continue
        seen.add(k)
        fresh.append(a)
    if not fresh:
        return []
    delivered = send_fn(fresh)
    for a in delivered:
        sent.add(a.key())
    return delivered
