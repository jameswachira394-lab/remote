"""
mt5/connector.py
================
Handles MT5 terminal connection, health checks, and shutdown.
All other MT5 modules import `mt5` from here to ensure a single session.
"""

import time
import sys
from utils.logger import get_logger

log = get_logger("mt5.connector")

try:
    import MetaTrader5 as mt5
except ImportError:
    log.critical(
        "MetaTrader5 package not installed. Run: pip install MetaTrader5\n"
        "Note: MT5 Python API requires Windows (or Wine on Linux)."
    )
    sys.exit(1)


class MT5Connector:
    """Manages a single MT5 terminal session."""

    def __init__(self, cfg: dict):
        self.cfg       = cfg
        self.connected = False

    # ── public ────────────────────────────────

    def connect(self) -> bool:
        """Initialize and log into the MT5 terminal."""
        path     = self.cfg.get("path") or None
        login    = self.cfg.get("login")    or None
        password = self.cfg.get("password") or None
        server   = self.cfg.get("server")   or None
        timeout  = self.cfg.get("timeout",  10_000)

        kwargs = {"timeout": timeout}
        if path:     kwargs["path"]     = path
        if login:    kwargs["login"]    = int(login)
        if password: kwargs["password"] = password
        if server:   kwargs["server"]   = server

        log.info("Connecting to MT5 terminal...")
        if not mt5.initialize(**kwargs):
            err = mt5.last_error()
            log.error(f"MT5 initialize failed: {err}")
            return False

        info = mt5.terminal_info()
        acct = mt5.account_info()
        if info is None or acct is None:
            log.error("Connected but could not fetch terminal/account info.")
            mt5.shutdown()
            return False

        self.connected = True
        log.info(
            f"MT5 connected | Build {info.build} | "
            f"Account #{acct.login} | {acct.name} | "
            f"Balance: {acct.balance:.2f} {acct.currency} | "
            f"Leverage: 1:{acct.leverage}"
        )
        return True

    def disconnect(self):
        if self.connected:
            mt5.shutdown()
            self.connected = False
            log.info("MT5 disconnected.")

    def is_alive(self) -> bool:
        """Ping the terminal — returns False if connection lost."""
        if not self.connected:
            return False
        try:
            acct = mt5.account_info()
            return acct is not None
        except Exception:
            return False

    def reconnect(self, retries: int = 5, delay: float = 5.0) -> bool:
        """Attempt to reconnect with exponential back-off."""
        self.disconnect()
        for attempt in range(1, retries + 1):
            log.info(f"Reconnect attempt {attempt}/{retries}...")
            if self.connect():
                return True
            wait = delay * (2 ** (attempt - 1))
            log.info(f"Waiting {wait:.0f}s before next attempt...")
            time.sleep(wait)
        log.error("All reconnect attempts failed.")
        return False

    def account_info(self) -> dict:
        """Return key account fields as a dict."""
        acct = mt5.account_info()
        if acct is None:
            return {}
        return {
            "login":    acct.login,
            "balance":  acct.balance,
            "equity":   acct.equity,
            "margin":   acct.margin,
            "free_margin": acct.margin_free,
            "currency": acct.currency,
            "leverage": acct.leverage,
            "profit":   acct.profit,
        }

    def get_mt5(self):
        """Expose the raw mt5 module for other modules that need it."""
        return mt5


# ── module-level singleton ─────────────────────────────────────────────────
_connector: MT5Connector | None = None

def get_connector() -> MT5Connector:
    global _connector
    if _connector is None:
        from config.settings import MT5 as MT5_CFG
        _connector = MT5Connector(MT5_CFG)
    return _connector
