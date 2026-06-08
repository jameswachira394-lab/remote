"""
v2/data_feed.py
===============
Module 1 — Market Data Fetcher

Responsibilities:
  - Connect to data source: ccxt, MT5, or yfinance
  - Fetch OHLCV candles for 5M, 15M, 1H timeframes
  - Deduplicate and upsert candles into the 'candles' table
  - Expose fetch_candles(pair, timeframe, limit) → pd.DataFrame
  - Compute ATR and EMA indicators inline

Source adapters (selected via config.yaml data_source):
  CcxtAdapter   — crypto via ccxt library
  MT5Adapter    — forex/metals via MetaTrader5
  YFinanceAdapter — forex/stocks via yfinance (fallback/testing)
"""

import logging
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
import numpy as np

from v2.db import connection as db

log = logging.getLogger("data_feed")

# ── Timeframe string normalisation ────────────────────────────────────────────

_TF_TO_MINUTES = {
    "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "2h": 120, "4h": 240, "1d": 1440,
    "M1": 1, "M5": 5, "M15": 15, "M30": 30,
    "H1": 60, "H4": 240, "D1": 1440,
}

_MT5_TF_MAP = {
    "M1": "TIMEFRAME_M1", "M5": "TIMEFRAME_M5", "M15": "TIMEFRAME_M15",
    "M30": "TIMEFRAME_M30", "H1": "TIMEFRAME_H1", "H4": "TIMEFRAME_H4",
    "D1": "TIMEFRAME_D1",
    # Accept normalised forms too
    "1m": "TIMEFRAME_M1", "5m": "TIMEFRAME_M5", "15m": "TIMEFRAME_M15",
    "30m": "TIMEFRAME_M30", "1h": "TIMEFRAME_H1", "4h": "TIMEFRAME_H4",
}

def _normalise_tf(tf: str) -> str:
    """Normalise timeframe string to lowercase (e.g. 'M5' → '5m')."""
    mapping = {
        "M1": "1m", "M5": "5m", "M15": "15m", "M30": "30m",
        "H1": "1h", "H4": "4h", "D1": "1d",
    }
    return mapping.get(tf.upper(), tf.lower())


# ══════════════════════════════════════════════════════════════════════════════
# ADAPTER BASE
# ══════════════════════════════════════════════════════════════════════════════

class BaseAdapter:
    """Abstract data source adapter."""

    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
        """
        Returns DataFrame with columns:
          timestamp (UTC pd.Timestamp, index), open, high, low, close, volume
        """
        raise NotImplementedError


# ══════════════════════════════════════════════════════════════════════════════
# CCXT ADAPTER — crypto exchanges
# ══════════════════════════════════════════════════════════════════════════════

class CcxtAdapter(BaseAdapter):
    """
    Fetches OHLCV from any ccxt-supported exchange.
    Handles rate-limiting automatically via ccxt's built-in throttle.
    """

    def __init__(self, cfg: dict):
        try:
            import ccxt
        except ImportError:
            raise ImportError("ccxt not installed. Run: pip install ccxt")

        exchange_id = cfg.get("exchange", "binance")
        ExchangeClass = getattr(ccxt, exchange_id)

        params = {"enableRateLimit": True}
        if cfg.get("api_key"):
            params["apiKey"]  = cfg["api_key"]
            params["secret"]  = cfg["api_secret"]
        if cfg.get("sandbox"):
            params["sandbox"] = True

        self._exchange = ExchangeClass(params)
        self._exchange.load_markets()
        log.info(f"CcxtAdapter ready | exchange={exchange_id} | sandbox={cfg.get('sandbox')}")

    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
        tf = _normalise_tf(timeframe)
        raw = self._exchange.fetch_ohlcv(symbol, tf, limit=limit)
        if not raw:
            return pd.DataFrame()
        df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df.set_index("timestamp", inplace=True)
        return df.sort_index()


# ══════════════════════════════════════════════════════════════════════════════
# MT5 ADAPTER — forex / metals
# ══════════════════════════════════════════════════════════════════════════════

class MT5Adapter(BaseAdapter):
    """
    Fetches OHLCV from a connected MetaTrader 5 terminal.
    Re-uses the existing mt5/connector.py singleton.
    """

    def __init__(self, cfg: dict):
        self._cfg = cfg
        # Lazy import — MT5 is Windows-only
        try:
            import MetaTrader5 as _mt5
            self._mt5 = _mt5
        except ImportError:
            raise ImportError(
                "MetaTrader5 not installed. Run: pip install MetaTrader5\n"
                "Note: MT5 Python API requires Windows (or Wine on Linux)."
            )

        kw = {"timeout": cfg.get("timeout", 10000)}
        for k in ("path", "login", "password", "server"):
            if cfg.get(k):
                kw[k] = int(cfg[k]) if k == "login" else cfg[k]

        if not self._mt5.initialize(**kw):
            raise ConnectionError(f"MT5 initialize failed: {self._mt5.last_error()}")
        log.info(f"MT5Adapter connected | account={cfg.get('login')}")

    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
        tf_key = _MT5_TF_MAP.get(timeframe, _MT5_TF_MAP.get(_normalise_tf(timeframe).upper()))
        if tf_key is None:
            raise ValueError(f"Unknown MT5 timeframe: {timeframe}")
        tf_const = getattr(self._mt5, tf_key)

        rates = self._mt5.copy_rates_from_pos(symbol, tf_const, 0, limit)
        if rates is None or len(rates) == 0:
            log.warning(f"No MT5 rates for {symbol} {timeframe}: {self._mt5.last_error()}")
            return pd.DataFrame()

        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df.rename(columns={
            "time": "timestamp", "open": "open", "high": "high",
            "low": "low", "close": "close", "tick_volume": "volume",
        }, inplace=True)
        df.set_index("timestamp", inplace=True)
        return df[["open", "high", "low", "close", "volume"]].sort_index()


# ══════════════════════════════════════════════════════════════════════════════
# YFINANCE ADAPTER — fallback / testing
# ══════════════════════════════════════════════════════════════════════════════

class YFinanceAdapter(BaseAdapter):
    """
    Fetches OHLCV via yfinance.
    Suitable for development/testing — not recommended for live trading.
    """

    def __init__(self, cfg: dict):
        try:
            import yfinance as yf
            self._yf = yf
        except ImportError:
            raise ImportError("yfinance not installed. Run: pip install yfinance")
        log.info("YFinanceAdapter ready")

    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
        tf = _normalise_tf(timeframe)
        # yfinance interval mapping
        yf_map = {
            "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
            "1h": "1h", "4h": "4h", "1d": "1d",
        }
        yf_interval = yf_map.get(tf, "5m")
        # Choose period based on limit × interval
        minutes = limit * _TF_TO_MINUTES.get(tf, 5)
        if minutes <= 7 * 1440:
            period = "7d"
        elif minutes <= 60 * 1440:
            period = "60d"
        else:
            period = "max"

        ticker = self._yf.Ticker(symbol)
        df = ticker.history(period=period, interval=yf_interval)
        if df.empty:
            return pd.DataFrame()

        df.index = df.index.tz_convert("UTC")
        df.rename(columns=str.lower, inplace=True)
        df = df[["open", "high", "low", "close", "volume"]].tail(limit)
        df.index.name = "timestamp"
        return df.sort_index()


# ══════════════════════════════════════════════════════════════════════════════
# INDICATOR HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def add_indicators(df: pd.DataFrame, ema_period: int = 200, atr_period: int = 14) -> pd.DataFrame:
    """Compute EMA and ATR columns and append to DataFrame."""
    df = df.copy()
    df["ema"]  = df["close"].ewm(span=ema_period, adjust=False).mean()

    hl  = df["high"] - df["low"]
    hcp = (df["high"] - df["close"].shift(1)).abs()
    lcp = (df["low"]  - df["close"].shift(1)).abs()
    tr  = pd.concat([hl, hcp, lcp], axis=1).max(axis=1)
    df["atr"] = tr.ewm(span=atr_period, adjust=False).mean()
    return df


# ══════════════════════════════════════════════════════════════════════════════
# MAIN DATA FEED CLASS
# ══════════════════════════════════════════════════════════════════════════════

class DataFeed:
    """
    Central data fetcher used by all other modules.

    Usage:
        feed = DataFeed(cfg)
        df5  = feed.get(pair="XAUUSD", timeframe="5m", limit=500)
        df1h = feed.get(pair="XAUUSD", timeframe="1h", limit=200)
    """

    def __init__(self, cfg: dict):
        """
        cfg: full config dict loaded from config.yaml.
        """
        self._cfg    = cfg
        self._bars   = cfg.get("bars", {})
        self._source = cfg.get("data_source", "ccxt")
        self._adapter: Optional[BaseAdapter] = None
        self._cache: dict = {}          # {(pair, tf): pd.DataFrame}
        self._init_adapter()

    # ── public ────────────────────────────────────────────────────────────────

    def get(self, pair: str, timeframe: str, limit: int = 500,
            with_indicators: bool = True) -> pd.DataFrame:
        """
        Fetch latest candles, store in DB, return DataFrame.
        Always fetches from the live adapter; DB is secondary storage.
        """
        df = self._adapter.fetch_ohlcv(pair, timeframe, limit)
        if df.empty:
            log.warning(f"No data returned for {pair} {timeframe}")
            return df

        if with_indicators:
            df = add_indicators(df)

        self._upsert_candles(pair, timeframe, df)
        self._cache[(pair, timeframe)] = df
        log.debug(f"Fetched {len(df)} candles | {pair} {timeframe} | last={df.index[-1]}")
        return df

    def get_from_db(self, pair: str, timeframe: str, limit: int = 500) -> pd.DataFrame:
        """
        Load candles from database only (no API call).
        Useful when API is unavailable or for backtesting.
        """
        rows = db.fetchall(
            "SELECT timestamp, open, high, low, close, volume "
            "FROM candles WHERE pair=? AND timeframe=? "
            "ORDER BY timestamp DESC LIMIT ?",
            (pair, timeframe, limit),
        )
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame([dict(r) for r in rows])
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        df.set_index("timestamp", inplace=True)
        return df.sort_index()

    def latest_close(self, pair: str, timeframe: str) -> Optional[float]:
        """Return the most recent closing price for a pair/timeframe."""
        cached = self._cache.get((pair, timeframe))
        if cached is not None and not cached.empty:
            return float(cached["close"].iloc[-1])
        row = db.fetchone(
            "SELECT close FROM candles WHERE pair=? AND timeframe=? "
            "ORDER BY timestamp DESC LIMIT 1",
            (pair, timeframe),
        )
        return float(row["close"]) if row else None

    # ── private ───────────────────────────────────────────────────────────────

    def _init_adapter(self):
        source = self._source.lower()
        if source == "ccxt":
            self._adapter = CcxtAdapter(self._cfg.get("ccxt", {}))
        elif source == "mt5":
            self._adapter = MT5Adapter(self._cfg.get("mt5", {}))
        elif source == "yfinance":
            self._adapter = YFinanceAdapter(self._cfg.get("yfinance", {}))
        else:
            raise ValueError(f"Unknown data_source: {self._source}")
        log.info(f"DataFeed using adapter: {source.upper()}")

    def _upsert_candles(self, pair: str, timeframe: str, df: pd.DataFrame):
        """
        Insert-or-replace candles into the database.
        Uses INSERT OR REPLACE for SQLite; ON CONFLICT for PostgreSQL.
        """
        tf_norm = _normalise_tf(timeframe)
        rows = [
            (
                pair, tf_norm,
                row.Index.isoformat(),
                float(row.open), float(row.high),
                float(row.low),  float(row.close),
                float(row.volume) if hasattr(row, "volume") else 0.0,
            )
            for row in df.itertuples()
        ]
        sql = (
            "INSERT OR REPLACE INTO candles "
            "(pair, timeframe, timestamp, open, high, low, close, volume) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
        )
        with db.get() as conn:
            conn.executemany(sql, rows)
        log.debug(f"Upserted {len(rows)} candles -> DB ({pair} {tf_norm})")
