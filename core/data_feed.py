"""
core/data_feed.py
=================
Fetches OHLCV bars from MT5 and computes indicators.
"""

import pandas as pd
import numpy as np
from datetime import datetime
from utils.logger import get_logger

log = get_logger("data_feed")

# Map string timeframe names → MT5 TIMEFRAME constants (resolved at runtime)
_TF_MAP = {
    "M1":  "TIMEFRAME_M1",
    "M5":  "TIMEFRAME_M5",
    "M15": "TIMEFRAME_M15",
    "M30": "TIMEFRAME_M30",
    "H1":  "TIMEFRAME_H1",
    "H4":  "TIMEFRAME_H4",
    "D1":  "TIMEFRAME_D1",
}


class DataFeed:
    def __init__(self, symbol: str, timeframe: str, bars: int, cfg_filters: dict):
        self.symbol    = symbol
        self.timeframe = timeframe
        self.bars      = bars
        self.cfg       = cfg_filters
        self._tf_const = None

    # ── public ────────────────────────────────

    def fetch(self) -> pd.DataFrame:
        """Pull latest bars from MT5, compute indicators, return DataFrame."""
        mt5 = self._get_mt5()
        tf  = self._resolve_tf(mt5)

        rates = mt5.copy_rates_from_pos(self.symbol, tf, 0, self.bars)
        if rates is None or len(rates) == 0:
            err = mt5.last_error()
            raise RuntimeError(f"copy_rates_from_pos failed: {err}")

        df = pd.DataFrame(rates)
        df['time'] = pd.to_datetime(df['time'], unit='s')
        df.set_index('time', inplace=True)
        df.rename(columns={
            'open': 'Open', 'high': 'High',
            'low':  'Low',  'close': 'Close',
            'tick_volume': 'Volume'
        }, inplace=True)
        df = df[['Open', 'High', 'Low', 'Close', 'Volume']].copy()

        df = self._add_indicators(df)
        log.debug(f"Fetched {len(df)} bars | last: {df.index[-1]}")
        return df

    def get_current_price(self) -> tuple[float, float]:
        """Return (bid, ask) for the symbol."""
        mt5  = self._get_mt5()
        tick = mt5.symbol_info_tick(self.symbol)
        if tick is None:
            raise RuntimeError(f"No tick data for {self.symbol}")
        return tick.bid, tick.ask

    def get_symbol_info(self) -> dict:
        mt5  = self._get_mt5()
        info = mt5.symbol_info(self.symbol)
        if info is None:
            raise RuntimeError(f"Symbol {self.symbol} not found in MT5")
        return {
            "digits":        info.digits,
            "trade_contract_size": info.trade_contract_size,
            "volume_min":    info.volume_min,
            "volume_max":    info.volume_max,
            "volume_step":   info.volume_step,
            "point":         info.point,
            "spread":        info.spread,
        }

    # ── private ───────────────────────────────

    def _add_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        ema_span = self.cfg.get("ema_1h_period", 600)
        atr_per  = self.cfg.get("atr_period", 14)

        # 1H trend EMA (approx 50 × 12 bars on M5)
        df['EMA_1H'] = df['Close'].ewm(span=ema_span, adjust=False).mean()

        # ATR
        hl  = df['High'] - df['Low']
        hc  = (df['High'] - df['Close'].shift(1)).abs()
        lc  = (df['Low']  - df['Close'].shift(1)).abs()
        tr  = pd.concat([hl, hc, lc], axis=1).max(axis=1)
        df['ATR'] = tr.ewm(span=atr_per, adjust=False).mean()

        return df

    def _get_mt5(self):
        from mt5.connector import get_connector
        conn = get_connector()
        if not conn.is_alive():
            log.warning("MT5 connection lost — attempting reconnect...")
            if not conn.reconnect():
                raise RuntimeError("Could not reconnect to MT5.")
        return conn.get_mt5()

    def _resolve_tf(self, mt5):
        if self._tf_const is not None:
            return self._tf_const
        key = _TF_MAP.get(self.timeframe.upper())
        if key is None:
            raise ValueError(f"Unknown timeframe: {self.timeframe}")
        self._tf_const = getattr(mt5, key)
        return self._tf_const
