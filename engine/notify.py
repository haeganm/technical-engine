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
GREY = 0x95A5A6
TRADINGVIEW = "https://www.tradingview.com/chart/?symbol={ticker}"


def _embed(a) -> dict:
    risk = a.price - a.stop
    m1, m2 = config.TARGET_MULTIPLES
    title = f"BUY NOW: {a.ticker} — {a.signal}"
    if len(a.signals) > 1:
        title += f" +{len(a.signals) - 1}"
    fields = [
        {"name": "Price / Entry", "value": f"${a.price:,.2f}", "inline": True},
        {"name": "Stop", "value": f"${a.stop:,.2f}  ({a.risk_pct:.2%})", "inline": True},
        {"name": "Targets",
         "value": (f"{m1:g}R ${a.target_1:,.2f}  (+{(a.target_1 / a.price - 1):.1%})\n"
                   f"{m2:g}R ${a.target_2:,.2f}  (+{(a.target_2 / a.price - 1):.1%})"),
         "inline": True},
        {"name": "Risk / share", "value": f"${risk:,.2f}", "inline": True},
        {"name": "RVOL", "value": f"{a.rvol:.1f}x", "inline": True},
        {"name": "ATR", "value": f"${a.atr:,.2f}", "inline": True},
        {"name": f"Score {a.score}/6", "value": ", ".join(a.context) or "—", "inline": False},
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


def status_embed(title: str, lines: list[str]) -> dict:
    """Neutral liveness post (morning 'scanner online'). The channel carries BUY
    alerts and this heartbeat only -- no outcomes, no summaries, nothing red.
    Honest win/loss accounting lives in the dashboard and `engine report`."""
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

    Only delivered alerts join `sent`, so a failed POST leaves the ticker
    eligible again. Note that the same BAR will not be re-judged (the scan has
    moved on), so a refused alert is effectively lost for that bar -- which is
    why the caller journals what was generated, not just what was delivered.
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
