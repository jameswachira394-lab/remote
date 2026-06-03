"""
utils/notifier.py
=================
Optional Telegram alert sender.
Set TELEGRAM.enabled = True in settings and fill in token + chat_id.
"""

import urllib.request
import urllib.parse
import json
from utils.logger import get_logger

log = get_logger("notifier")


def send(message: str) -> bool:
    """Send a Telegram message. Returns True on success."""
    from config.settings import TELEGRAM
    if not TELEGRAM.get("enabled"):
        return False

    token   = TELEGRAM["token"]
    chat_id = TELEGRAM["chat_id"]
    if not token or not chat_id:
        log.warning("Telegram enabled but token/chat_id not set.")
        return False

    url  = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text":    message,
        "parse_mode": "HTML",
    }).encode()

    try:
        req  = urllib.request.Request(url, data=data)
        resp = urllib.request.urlopen(req, timeout=5)
        result = json.loads(resp.read())
        if result.get("ok"):
            return True
        log.warning(f"Telegram API error: {result}")
    except Exception as e:
        log.warning(f"Telegram send failed: {e}")
    return False


def alert_signal(symbol: str, direction: str, entry: float,
                 sl: float, tp1: float, tp2: float, risk_pips: float):
    msg = (
        f"🔔 <b>OB Signal — {symbol}</b>\n"
        f"Direction: <b>{direction}</b>\n"
        f"Entry:  <code>{entry:.5f}</code>\n"
        f"SL:     <code>{sl:.5f}</code>  (−{risk_pips:.1f} pips)\n"
        f"TP1:    <code>{tp1:.5f}</code>\n"
        f"TP2:    <code>{tp2:.5f}</code>\n"
    )
    send(msg)


def alert_fill(symbol: str, direction: str, ticket: int,
               entry: float, volume: float):
    msg = (
        f"✅ <b>Order Filled — {symbol}</b>\n"
        f"Ticket: <code>{ticket}</code>\n"
        f"Direction: <b>{direction}</b>\n"
        f"Entry: <code>{entry:.5f}</code>  Vol: {volume:.2f}\n"
    )
    send(msg)


def alert_close(symbol: str, ticket: int, profit_pips: float, status: str):
    icon = "💚" if profit_pips > 0 else "🔴" if profit_pips < 0 else "⚪"
    msg = (
        f"{icon} <b>Trade Closed — {symbol}</b>\n"
        f"Ticket: <code>{ticket}</code>\n"
        f"Result: <b>{status}</b>  ({profit_pips:+.1f} pips)\n"
    )
    send(msg)


def alert_error(context: str, error: str):
    msg = f"⚠️ <b>Bot Error</b>\n{context}\n<code>{error}</code>"
    send(msg)
