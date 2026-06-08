"""
v2/alerts.py
============
Module 6 — Signal Output & Alerts

Responsibilities:
  - Log every signal to a structured rotating log file
  - Write signals to a dated CSV file in signals/
  - Send Telegram Bot API notifications (optional)
  - Send AWS SNS notifications (optional)
  - Send CloudWatch custom metrics (optional)
  - Provide print_signal() for clean CLI output

All alert channels are independently togglable in config.yaml.
"""

import csv
import json
import logging
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from v2.signals import Signal

log = logging.getLogger("alerts")


# ══════════════════════════════════════════════════════════════════════════════
# ALERT MANAGER
# ══════════════════════════════════════════════════════════════════════════════

class AlertManager:
    """
    Routes signals to all configured output channels.

    Usage:
        alerter = AlertManager(cfg["alerts"], cfg["paths"])
        alerter.send_signal(signal)
        alerter.send_error("component", "error message")
        alerter.send_startup(pairs=["XAUUSD","EURUSD"])
    """

    def __init__(self, alerts_cfg: dict, paths_cfg: dict):
        self._tg_cfg   = alerts_cfg.get("telegram", {})
        self._sns_cfg  = alerts_cfg.get("aws_sns", {})
        self._sig_dir  = Path(paths_cfg.get("signal_dir", "signals"))
        self._sig_dir.mkdir(parents=True, exist_ok=True)

        # Initialise optional SNS client
        self._sns_client = None
        if self._sns_cfg.get("enabled"):
            self._sns_client = self._init_sns()

        log.info(
            f"AlertManager ready | "
            f"Telegram={'ON' if self._tg_cfg.get('enabled') else 'OFF'} | "
            f"SNS={'ON' if self._sns_cfg.get('enabled') else 'OFF'}"
        )

    # ── public ────────────────────────────────────────────────────────────────

    def send_signal(self, sig: Signal):
        """Route a trade signal to all channels."""
        self._log_signal(sig)
        self._csv_signal(sig)
        self._print_signal(sig)

        msg = self._format_signal_msg(sig)
        if self._tg_cfg.get("enabled"):
            self._telegram(msg)
        if self._sns_cfg.get("enabled"):
            self._sns(subject=f"OB Signal: {sig.signal_type} {sig.pair}", body=msg)

    def send_error(self, context: str, error: str):
        """Alert on system errors."""
        msg = f"[!] ERROR [{context}]\n{error}"
        log.error(msg)
        if self._tg_cfg.get("enabled"):
            self._telegram(msg)
        if self._sns_cfg.get("enabled"):
            self._sns(subject=f"OB Bot Error: {context}", body=msg)

    def send_startup(self, pairs: list[str], dry_run: bool = True, source: str = "ccxt"):
        """Announce bot startup."""
        mode = "DRY RUN" if dry_run else "LIVE"
        msg  = (
            f"[+] OB Trading Bot v2 started\n"
            f"Mode: {mode}\n"
            f"Source: {source}\n"
            f"Pairs: {', '.join(pairs)}\n"
            f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}"
        )
        log.info(msg)
        if self._tg_cfg.get("enabled"):
            self._telegram(msg)
        if self._sns_cfg.get("enabled"):
            self._sns(subject="OB Bot Started", body=msg)

    def send_shutdown(self):
        """Announce graceful shutdown."""
        msg = ">>> OB Trading Bot v2 stopped."
        log.info(msg)
        if self._tg_cfg.get("enabled"):
            self._telegram(msg)

    # ── log ──────────────────────────────────────────────────────────────────

    def _log_signal(self, sig: Signal):
        """Write structured log entry to signals.log."""
        sig_log = logging.getLogger("signals")
        sig_log.info(
            f"SIGNAL | {sig.signal_type} {sig.pair} | "
            f"TF={sig.timeframe} | Bias={sig.bias} | Session={sig.session} | "
            f"Confirm={sig.confirmation} | "
            f"E={sig.entry:.5f} SL={sig.sl:.5f} "
            f"TP1={sig.tp1:.5f} TP2={sig.tp2:.5f} | "
            f"Risk={sig.risk_pips:.1f}pips RR={sig.rr:.2f} | "
            f"OB_ID={sig.ob_id} | ts={sig.timestamp}"
        )

    # ── CSV ──────────────────────────────────────────────────────────────────

    def _csv_signal(self, sig: Signal):
        """Append signal to a daily CSV file in signals/."""
        today    = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        csv_path = self._sig_dir / f"signals_{today}.csv"
        write_header = not csv_path.exists()

        fields = [
            "timestamp", "pair", "timeframe", "signal_type",
            "entry", "sl", "tp1", "tp2",
            "risk_pips", "rr", "bias", "session", "confirmation",
            "ob_id", "status",
        ]
        row = {f: getattr(sig, f, "") for f in fields}

        with open(csv_path, "a", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fields)
            if write_header:
                writer.writeheader()
            writer.writerow(row)

        log.debug(f"Signal written to {csv_path}")

    # ── CLI print ─────────────────────────────────────────────────────────────

    def _print_signal(self, sig: Signal):
        """Pretty-print signal to stdout."""
        line = "═" * 58
        direction_icon = "[BUY]" if sig.signal_type == "BUY" else "[SELL]"
        bias_icon      = "▲" if sig.bias == "BULLISH" else "▼" if sig.bias == "BEARISH" else "─"

        print(f"\n{line}")
        print(f"  [SIGNAL] OB SIGNAL  [{sig.session}]")
        print(f"  {direction_icon}  {sig.signal_type} {sig.pair}  |  TF: {sig.timeframe}")
        print(f"  Bias: {bias_icon} {sig.bias}  |  Confirm: {sig.confirmation}")
        print(f"{'-' * 58}")
        print(f"  Entry    :  {sig.entry:.5f}")
        print(f"  Stop Loss:  {sig.sl:.5f}  ({sig.risk_pips:.1f} pips)")
        print(f"  TP 1     :  {sig.tp1:.5f}  (1:{self._fmt_rr(sig.entry, sig.sl, sig.tp1)})")
        print(f"  TP 2     :  {sig.tp2:.5f}  (1:{sig.rr:.2f})")
        print(f"  OB ID    :  #{sig.ob_id}")
        print(f"  Time     :  {sig.timestamp}")
        print(f"{line}\n")

    # ── Telegram ──────────────────────────────────────────────────────────────

    def _format_signal_msg(self, sig: Signal) -> str:
        icon = "🟢" if sig.signal_type == "BUY" else "🔴"
        return (
            f"{icon} <b>OB Signal — {sig.pair}</b>\n"
            f"Direction: <b>{sig.signal_type}</b> | TF: {sig.timeframe}\n"
            f"Bias: {sig.bias} | Session: {sig.session}\n"
            f"Confirmation: {sig.confirmation}\n"
            f"─────────────────────\n"
            f"Entry:  <code>{sig.entry:.5f}</code>\n"
            f"SL:     <code>{sig.sl:.5f}</code>  (−{sig.risk_pips:.1f} pips)\n"
            f"TP1:    <code>{sig.tp1:.5f}</code>\n"
            f"TP2:    <code>{sig.tp2:.5f}</code>  (RR 1:{sig.rr:.2f})\n"
            f"OB ID:  #{sig.ob_id} | <code>{sig.timestamp[:16]}</code>"
        )

    def _telegram(self, message: str) -> bool:
        """Send a message via Telegram Bot API."""
        token   = self._tg_cfg.get("token", "")
        chat_id = self._tg_cfg.get("chat_id", "")
        if not token or not chat_id:
            log.warning("Telegram enabled but token/chat_id not configured.")
            return False

        url  = f"https://api.telegram.org/bot{token}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id":    chat_id,
            "text":       message,
            "parse_mode": "HTML",
        }).encode()
        try:
            req  = urllib.request.Request(url, data=data)
            resp = urllib.request.urlopen(req, timeout=8)
            result = json.loads(resp.read())
            if result.get("ok"):
                log.debug("Telegram message sent OK")
                return True
            log.warning(f"Telegram API error: {result}")
        except Exception as e:
            log.warning(f"Telegram send failed: {e}")
        return False

    # ── AWS SNS ───────────────────────────────────────────────────────────────

    def _init_sns(self):
        """Initialise boto3 SNS client."""
        try:
            import boto3
            region = self._sns_cfg.get("region", "us-east-1")
            client = boto3.client("sns", region_name=region)
            log.info(f"AWS SNS client initialised | region={region}")
            return client
        except ImportError:
            log.warning("boto3 not installed — SNS disabled. Run: pip install boto3")
            return None
        except Exception as e:
            log.warning(f"SNS init failed: {e}")
            return None

    def _sns(self, subject: str, body: str) -> bool:
        """Publish a message to AWS SNS topic."""
        if self._sns_client is None:
            return False
        topic_arn = self._sns_cfg.get("topic_arn", "")
        if not topic_arn:
            log.warning("SNS enabled but topic_arn not configured.")
            return False
        try:
            self._sns_client.publish(
                TopicArn=topic_arn,
                Subject=subject[:100],   # SNS subject max 100 chars
                Message=body,
            )
            log.debug(f"SNS message published → {topic_arn}")
            return True
        except Exception as e:
            log.warning(f"SNS publish failed: {e}")
            return False

    # ── helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _fmt_rr(entry: float, sl: float, tp: float) -> str:
        risk   = abs(entry - sl)
        reward = abs(tp - entry)
        return f"{(reward / risk):.2f}" if risk > 0 else "?"
