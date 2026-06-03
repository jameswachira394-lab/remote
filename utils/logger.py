"""
utils/logger.py
===============
Structured rotating logger — writes to console + daily log file.
"""

import logging
import os
import sys
from logging.handlers import TimedRotatingFileHandler
from datetime import datetime

_loggers = {}

def get_logger(name: str = "ob_bot") -> logging.Logger:
    if name in _loggers:
        return _loggers[name]

    from config.settings import LOG_DIR

    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # Console handler
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    # File handler — rotates daily, keeps 14 days
    log_path = os.path.join(LOG_DIR, f"{name}.log")
    fh = TimedRotatingFileHandler(
        log_path, when="midnight", interval=1, backupCount=14, encoding="utf-8"
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    _loggers[name] = logger
    return logger


def log_trade(action: str, symbol: str, direction: str, volume: float,
              entry: float, sl: float, tp1: float, tp2: float,
              ticket: int = 0, extra: dict = None):
    """Structured trade log entry."""
    logger = get_logger("trades")
    msg = (
        f"ACTION={action} | SYMBOL={symbol} | DIR={direction} | "
        f"VOL={volume:.2f} | ENTRY={entry:.5f} | SL={sl:.5f} | "
        f"TP1={tp1:.5f} | TP2={tp2:.5f} | TICKET={ticket}"
    )
    if extra:
        msg += " | " + " | ".join(f"{k}={v}" for k, v in extra.items())
    logger.info(msg)
