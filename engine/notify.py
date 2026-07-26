"""Discord webhook notifier. Fire-and-forget; never raises into the scan loop."""
import requests

COLOR_GREEN = 0x2ECC71


def _embed(a) -> dict:
    return {
        "title": f"BUY SIGNAL: {a.ticker} — {a.signal}",
        "color": COLOR_GREEN,
        "timestamp": a.ts.isoformat(),
        "fields": [
            {"name": "Price", "value": f"${a.price:.2f}", "inline": True},
            {"name": "Entry", "value": f"${a.entry:.2f}", "inline": True},
            {"name": "Stop", "value": f"${a.stop:.2f}", "inline": True},
            {"name": "1R / 2R", "value": f"${a.target_1r:.2f} / ${a.target_2r:.2f}", "inline": True},
            {"name": "RVOL", "value": f"{a.rvol:.1f}x (p{a.rvol_pctile:.0%})", "inline": True},
        ],
        "footer": {"text": "TechnicalEngine — not financial advice"},
    }


def send(alert, webhook_url: str) -> bool:
    try:
        r = requests.post(webhook_url, json={"embeds": [_embed(alert)]}, timeout=10)
        return r.status_code < 300
    except requests.RequestException:
        return False


def dispatch(alerts, sent: set, send_fn) -> list:
    """Send each alert at most once per (session, ticker, signal). Mutates `sent`.
    Returns the alerts actually sent."""
    out = []
    for a in alerts:
        if a.key() in sent:
            continue
        sent.add(a.key())
        send_fn(a)
        out.append(a)
    return out
