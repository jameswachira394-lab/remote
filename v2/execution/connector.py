"""
v2/execution/connector.py
=========================
Handles MT5 terminal connection, health checks, and shutdown.
"""

import time
import sys
import logging

log = logging.getLogger("mt5.connector")

try:
    import MetaTrader5 as mt5
except ImportError:
    # Optional dependency, fail softly if not running in Windows
    mt5 = None


class MT5Connector:
    """Manages a single MT5 terminal session."""

    def __init__(self, cfg: dict):
        self.cfg       = cfg
        self.connected = False

    def connect(self) -> bool:
        if mt5 is None:
            log.error("MetaTrader5 package not installed. Execution disabled.")
            return False

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
        if self.connected and mt5 is not None:
            mt5.shutdown()
            self.connected = False
            log.info("MT5 disconnected.")

    def is_alive(self) -> bool:
        if not self.connected or mt5 is None:
            return False
        try:
            return mt5.account_info() is not None
        except Exception:
            return False

    def account_info(self) -> dict:
        if not self.is_alive():
            return {}
        acct = mt5.account_info()
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
        return mt5

# Singleton
_connector: MT5Connector | None = None

def get_connector(cfg: dict = None) -> MT5Connector:
    global _connector
    if _connector is None:
        if cfg is None:
            raise ValueError("Connector must be initialised with MT5 config dict")
        _connector = MT5Connector(cfg)
    return _connector
