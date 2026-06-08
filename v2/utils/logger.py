"""
v2/utils/logger.py
==================
Structured rotating logger for EC2 + CloudWatch.

Features:
  - Console handler  (INFO+)
  - Rotating file handler per module (DEBUG+, 14-day retention)
  - Optional CloudWatch Logs handler via watchtower
  - Structured JSON formatter for CloudWatch ingestion
  - log_signal() / log_trade() helpers for consistent trade records
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Optional

_loggers: dict[str, logging.Logger] = {}
_log_dir: Path = Path("logs")
_cw_enabled: bool = False
_cw_group: str    = "/ob-trading/v2"
_cw_region: str   = "us-east-1"


# ══════════════════════════════════════════════════════════════════════════════
# INIT
# ══════════════════════════════════════════════════════════════════════════════

def init(cfg: dict):
    """
    Call once at startup with the 'logging' section of config.yaml.
    Sets the global log directory and optional CloudWatch config.
    """
    global _log_dir, _cw_enabled, _cw_group, _cw_region

    _log_dir = Path(cfg.get("dir", "logs"))
    _log_dir.mkdir(parents=True, exist_ok=True)

    cw_cfg = cfg.get("cloudwatch", {})
    _cw_enabled = cw_cfg.get("enabled", False)
    _cw_group   = cw_cfg.get("log_group", "/ob-trading/v2")
    _cw_region  = cw_cfg.get("region", "us-east-1")

    level_str = cfg.get("log_level", "INFO").upper()
    root = logging.getLogger()
    root.setLevel(getattr(logging, level_str, logging.INFO))


# ══════════════════════════════════════════════════════════════════════════════
# FORMATTERS
# ══════════════════════════════════════════════════════════════════════════════

class _PlainFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return (
            f"{self.formatTime(record, '%Y-%m-%d %H:%M:%S')} | "
            f"{record.levelname:<8} | {record.name} | {record.getMessage()}"
        )


class _JsonFormatter(logging.Formatter):
    """JSON-structured formatter for CloudWatch Logs Insights."""
    def format(self, record: logging.LogRecord) -> str:
        doc = {
            "ts":      datetime.now(timezone.utc).isoformat(),
            "level":   record.levelname,
            "logger":  record.name,
            "msg":     record.getMessage(),
            "func":    f"{record.filename}:{record.lineno}",
        }
        if record.exc_info:
            doc["exc"] = self.formatException(record.exc_info)
        return json.dumps(doc)


# ══════════════════════════════════════════════════════════════════════════════
# FACTORY
# ══════════════════════════════════════════════════════════════════════════════

def get_logger(name: str = "ob_bot") -> logging.Logger:
    """
    Return (or create) a named logger with console + file handlers.
    Thread-safe — idempotent on repeated calls.
    """
    if name in _loggers:
        return _loggers[name]

    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    plain = _PlainFormatter()
    json_fmt = _JsonFormatter()

    # ── Console (plain, INFO+) ────────────────────────────────────────────
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(plain)
    logger.addHandler(ch)

    # ── Rotating file (plain, DEBUG+) ─────────────────────────────────────
    log_path = _log_dir / f"{name}.log"
    fh = TimedRotatingFileHandler(
        log_path, when="midnight", interval=1, backupCount=14, encoding="utf-8"
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(plain)
    logger.addHandler(fh)

    # ── CloudWatch (JSON, INFO+) ──────────────────────────────────────────
    if _cw_enabled:
        try:
            import watchtower, boto3
            cw_handler = watchtower.CloudWatchLogHandler(
                log_group=_cw_group,
                stream_name=name,
                boto3_client=boto3.client("logs", region_name=_cw_region),
                send_interval=10,
            )
            cw_handler.setLevel(logging.INFO)
            cw_handler.setFormatter(json_fmt)
            logger.addHandler(cw_handler)
        except ImportError:
            logger.warning("watchtower/boto3 not installed — CloudWatch disabled.")
        except Exception as e:
            logger.warning(f"CloudWatch handler init failed: {e}")

    _loggers[name] = logger
    return logger


# ══════════════════════════════════════════════════════════════════════════════
# STRUCTURED HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def log_signal(
    pair:        str,
    signal_type: str,
    entry:       float,
    sl:          float,
    tp1:         float,
    tp2:         float,
    risk_pips:   float,
    rr:          float,
    bias:        str,
    session:     str,
    confirmation: str,
    ob_id:       Optional[int] = None,
):
    """Write a structured signal record to the dedicated 'signals' logger."""
    logger = get_logger("signals")
    logger.info(
        f"SIGNAL | {signal_type} {pair} | "
        f"E={entry:.5f} SL={sl:.5f} TP1={tp1:.5f} TP2={tp2:.5f} | "
        f"Risk={risk_pips:.1f}pips RR={rr:.2f} | "
        f"Bias={bias} Session={session} Confirm={confirmation} OB_ID={ob_id}"
    )


def log_trade(
    action:     str,       # OPEN | CLOSE | MODIFY
    symbol:     str,
    direction:  str,
    volume:     float,
    entry:      float,
    sl:         float,
    tp1:        float,
    tp2:        float,
    ticket:     int   = 0,
    extra:      dict  = None,
):
    """Write a structured trade execution record to the 'trades' logger."""
    logger = get_logger("trades")
    msg = (
        f"ACTION={action} | SYMBOL={symbol} | DIR={direction} | "
        f"VOL={volume:.2f} | ENTRY={entry:.5f} | SL={sl:.5f} | "
        f"TP1={tp1:.5f} | TP2={tp2:.5f} | TICKET={ticket}"
    )
    if extra:
        msg += " | " + " | ".join(f"{k}={v}" for k, v in extra.items())
    logger.info(msg)
