"""
v2/bias.py
==========
Module 4 — HTF Bias Engine

Determines the Higher-Timeframe (1H) directional bias for each pair.

Algorithm:
  BULLISH  — price is making Higher Highs (HH) AND Higher Lows (HL)
             over the last N confirmed swing pairs
  BEARISH  — price is making Lower Highs (LH) AND Lower Lows (LL)
  RANGING  — neither condition met (mixed structure)

Bias is updated every 1H candle close (via scheduler).
Result is stored in the 'bias' table and cached in-memory.

Exposes:
  BiasEngine.get(pair)          → "BULLISH" | "BEARISH" | "RANGING"
  BiasEngine.update(df, pair)   → recalculates and persists bias
"""

import logging
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
import numpy as np

from v2.db import connection as db

log = logging.getLogger("bias")

# ── Constants ────────────────────────────────────────────────────────────────
BULLISH = "BULLISH"
BEARISH = "BEARISH"
RANGING = "RANGING"


class BiasEngine:
    """
    HTF bias calculator.

    Usage:
        engine = BiasEngine(cfg["bias"])
        bias   = engine.update(df_1h, pair="XAUUSD")
        # Later (after DB update):
        bias   = engine.get("XAUUSD")
    """

    def __init__(self, cfg: dict):
        self._hh_hl_count  = cfg.get("hh_hl_count", 2)    # consecutive HH+HL pairs
        self._lh_ll_count  = cfg.get("lh_ll_count", 2)    # consecutive LH+LL pairs
        self._swing_n      = cfg.get("swing_lookback", 3)  # pivot lookback bars
        self._cache: dict[str, str] = {}                   # {pair: bias}

    # ── public ────────────────────────────────────────────────────────────────

    def update(self, df: pd.DataFrame, pair: str, timeframe: str = "1h") -> str:
        """
        Recalculate and persist bias for a pair using the supplied 1H DataFrame.
        Returns the new bias string.
        """
        if len(df) < self._swing_n * 2 + 4:
            log.warning(f"Bias [{pair}]: not enough bars ({len(df)}) — defaulting to RANGING")
            bias = RANGING
        else:
            bias = self._calculate(df)

        self._cache[pair] = bias
        self._persist(pair, timeframe, bias, df.index[-1])

        log.info(f"Bias [{pair} {timeframe}] → {bias}")
        return bias

    def get(self, pair: str) -> str:
        """
        Return the current bias for a pair.
        Checks memory cache first, then falls back to database.
        Returns RANGING if no data found.
        """
        if pair in self._cache:
            return self._cache[pair]

        row = db.fetchone(
            "SELECT bias FROM bias WHERE pair=? ORDER BY timestamp DESC LIMIT 1",
            (pair,),
        )
        if row:
            self._cache[pair] = row["bias"]
            return row["bias"]

        log.debug(f"Bias [{pair}]: no DB record — returning RANGING")
        return RANGING

    def get_all(self) -> dict[str, str]:
        """Return {pair: bias} for all pairs in the database."""
        rows = db.fetchall(
            "SELECT pair, bias FROM ("
            "  SELECT pair, bias, ROW_NUMBER() OVER (PARTITION BY pair ORDER BY timestamp DESC) rn "
            "  FROM bias"
            ") WHERE rn=1"
        )
        result = {r["pair"]: r["bias"] for r in rows}
        self._cache.update(result)
        return result

    # ── calculation ──────────────────────────────────────────────────────────

    def _calculate(self, df: pd.DataFrame) -> str:
        """
        Detect HH/HL or LH/LL patterns from confirmed pivot points.
        Uses a simple but robust n-bar pivot algorithm.
        """
        swings = self._extract_pivots(df)
        if len(swings) < 4:
            return RANGING

        # Split into highs and lows
        highs = [s["price"] for s in swings if s["kind"] == "H"]
        lows  = [s["price"] for s in swings if s["kind"] == "L"]

        if len(highs) < 2 or len(lows) < 2:
            return RANGING

        # Check last N consecutive pairs
        n = min(self._hh_hl_count, len(highs) - 1, len(lows) - 1)

        # BULLISH: each recent high > prior high, each recent low > prior low
        hh = all(highs[-(i + 1)] > highs[-(i + 2)] for i in range(n))
        hl = all(lows[-(i + 1)]  > lows[-(i + 2)]  for i in range(n))

        # BEARISH: each recent high < prior high, each recent low < prior low
        lh = all(highs[-(i + 1)] < highs[-(i + 2)] for i in range(n))
        ll = all(lows[-(i + 1)]  < lows[-(i + 2)]  for i in range(n))

        if hh and hl:
            return BULLISH
        if lh and ll:
            return BEARISH
        return RANGING

    def _extract_pivots(self, df: pd.DataFrame) -> list[dict]:
        """
        Extract alternating swing highs and lows.
        Returns list of {"kind": "H"|"L", "price": float, "idx": int}
        sorted by bar index (chronological).
        """
        n    = self._swing_n
        highs_arr = df["high"].values
        lows_arr  = df["low"].values
        pivots: list[dict] = []

        for i in range(n, len(df) - n):
            window_h = highs_arr[i - n : i + n + 1]
            window_l = lows_arr[i  - n : i + n + 1]

            is_sh = highs_arr[i] == window_h.max()
            is_sl = lows_arr[i]  == window_l.min()

            if is_sh:
                pivots.append({"kind": "H", "price": float(highs_arr[i]), "idx": i})
            if is_sl:
                pivots.append({"kind": "L", "price": float(lows_arr[i]),  "idx": i})

        # Sort chronologically and de-duplicate consecutive same-kind pivots
        # (keep the more extreme one)
        pivots.sort(key=lambda p: p["idx"])
        return self._deduplicate_pivots(pivots)

    @staticmethod
    def _deduplicate_pivots(pivots: list[dict]) -> list[dict]:
        """
        Ensure alternating H/L sequence.
        When two consecutive same-kind pivots appear, keep the extreme one.
        """
        result: list[dict] = []
        for p in pivots:
            if not result:
                result.append(p)
                continue
            last = result[-1]
            if last["kind"] == p["kind"]:
                # Keep higher high or lower low
                if p["kind"] == "H" and p["price"] > last["price"]:
                    result[-1] = p
                elif p["kind"] == "L" and p["price"] < last["price"]:
                    result[-1] = p
            else:
                result.append(p)
        return result

    # ── persistence ──────────────────────────────────────────────────────────

    def _persist(self, pair: str, timeframe: str, bias: str, ts) -> None:
        ts_str = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
        db.execute(
            "INSERT OR REPLACE INTO bias (pair, timeframe, bias, timestamp) "
            "VALUES (?, ?, ?, ?)",
            (pair, timeframe, bias, ts_str),
        )

    # ── CLI summary ──────────────────────────────────────────────────────────

    def print_summary(self):
        """Print a formatted bias table to stdout."""
        all_bias = self.get_all()
        if not all_bias:
            print("  No bias data available.")
            return
        print(f"\n{'Pair':<12} {'Bias':<12}")
        print("-" * 24)
        for pair, bias in sorted(all_bias.items()):
            icon = "▲" if bias == BULLISH else "▼" if bias == BEARISH else "─"
            print(f"  {pair:<10} {icon} {bias}")
        print()
