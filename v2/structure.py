"""
v2/structure.py
===============
Module 2 — Market Structure Analysis

Responsibilities:
  - Detect swing highs and swing lows on 5M and 15M timeframes
  - Identify Break of Structure (BOS):
      Bullish BOS : price closes ABOVE a prior confirmed swing high
      Bearish BOS : price closes BELOW a prior confirmed swing low
  - Identify Change of Character (CHoCH):
      First BOS that opposes the prevailing trend = market shift
  - Tag each event with timestamp, price level, and type
  - Persist results to the structure_events table
  - Expose get_latest_bos(pair, tf) for ob_detector.py

Algorithm notes:
  Swing High : bar[i].high == max(bar[i-n : i+n+1].high)  → n = swing_lookback
  Swing Low  : bar[i].low  == min(bar[i-n : i+n+1].low)
  BOS        : current bar close crosses a confirmed swing level
  CHoCH      : BOS direction != last known BOS direction
"""

import logging
from datetime import timezone
from typing import Optional

import pandas as pd
import numpy as np

from v2.db import connection as db

log = logging.getLogger("structure")


# ══════════════════════════════════════════════════════════════════════════════
# DATA CLASSES
# ══════════════════════════════════════════════════════════════════════════════

class SwingPoint:
    __slots__ = ("kind", "price", "timestamp", "bar_index")

    def __init__(self, kind: str, price: float, timestamp, bar_index: int):
        self.kind      = kind        # 'HIGH' | 'LOW'
        self.price     = price
        self.timestamp = timestamp
        self.bar_index = bar_index

    def __repr__(self):
        return f"<Swing {self.kind} {self.price:.5f} @ {self.timestamp}>"


class StructureEvent:
    __slots__ = ("event_type", "price", "timestamp", "bar_index")

    def __init__(self, event_type: str, price: float, timestamp, bar_index: int):
        self.event_type = event_type  # 'BOS_BULL'|'BOS_BEAR'|'CHOCH_BULL'|'CHOCH_BEAR'
        self.price      = price
        self.timestamp  = timestamp
        self.bar_index  = bar_index

    def __repr__(self):
        return f"<StructureEvent {self.event_type} {self.price:.5f} @ {self.timestamp}>"


# ══════════════════════════════════════════════════════════════════════════════
# STRUCTURE ANALYSER
# ══════════════════════════════════════════════════════════════════════════════

class StructureAnalyser:
    """
    Scans a DataFrame of OHLCV candles for swing points and structure breaks.

    Usage:
        analyser = StructureAnalyser(cfg["structure"])
        swings, events = analyser.analyse(df, pair="XAUUSD", timeframe="15m")
    """

    def __init__(self, cfg: dict):
        self._lookback  = cfg.get("swing_lookback", 3)     # bars each side
        self._bos_confirm = cfg.get("bos_confirmation", True)  # require close
        # Track last BOS direction per (pair, tf) to detect CHoCH
        self._last_bos: dict[tuple, str] = {}

    # ── public ────────────────────────────────────────────────────────────────

    def analyse(
        self, df: pd.DataFrame, pair: str, timeframe: str
    ) -> tuple[list[SwingPoint], list[StructureEvent]]:
        """
        Full analysis on the supplied DataFrame.
        Returns (swing_points, structure_events).
        Both lists are in chronological order.
        """
        if len(df) < self._lookback * 2 + 3:
            log.debug(f"{pair} {timeframe}: not enough bars ({len(df)}) for structure analysis")
            return [], []

        swings = self._detect_swings(df)
        events = self._detect_structure(df, swings, pair, timeframe)

        self._persist_swings(pair, timeframe, swings)
        self._persist_events(pair, timeframe, events)

        log.info(
            f"Structure [{pair} {timeframe}] | "
            f"swings={len(swings)} | BOS/CHoCH={len(events)}"
        )
        return swings, events

    def get_latest_bos(self, pair: str, timeframe: str) -> Optional[StructureEvent]:
        """Return the most recent BOS event from the database."""
        row = db.fetchone(
            "SELECT event_type, price, timestamp, bar_index "
            "FROM structure_events "
            "WHERE pair=? AND timeframe=? "
            "  AND event_type IN ('BOS_BULL','BOS_BEAR','CHOCH_BULL','CHOCH_BEAR') "
            "ORDER BY timestamp DESC LIMIT 1",
            (pair, timeframe),
        )
        if row is None:
            return None
        return StructureEvent(row["event_type"], row["price"], row["timestamp"], row["bar_index"] or 0)

    def get_swing_highs(self, pair: str, timeframe: str, limit: int = 20) -> list[SwingPoint]:
        rows = db.fetchall(
            "SELECT price, timestamp, bar_index FROM structure_events "
            "WHERE pair=? AND timeframe=? AND event_type='SWING_HIGH' "
            "ORDER BY timestamp DESC LIMIT ?",
            (pair, timeframe, limit),
        )
        return [SwingPoint("HIGH", r["price"], r["timestamp"], r["bar_index"] or 0) for r in rows]

    def get_swing_lows(self, pair: str, timeframe: str, limit: int = 20) -> list[SwingPoint]:
        rows = db.fetchall(
            "SELECT price, timestamp, bar_index FROM structure_events "
            "WHERE pair=? AND timeframe=? AND event_type='SWING_LOW' "
            "ORDER BY timestamp DESC LIMIT ?",
            (pair, timeframe, limit),
        )
        return [SwingPoint("LOW", r["price"], r["timestamp"], r["bar_index"] or 0) for r in rows]

    # ── swing detection ───────────────────────────────────────────────────────

    def _detect_swings(self, df: pd.DataFrame) -> list[SwingPoint]:
        """
        Identify swing highs and lows using n-bar pivot logic.
        A swing high at bar i: df.high[i] is the maximum over [i-n, i+n].
        A swing low  at bar i: df.low[i]  is the minimum over [i-n, i+n].
        Only bars [n : len-n-1] can be confirmed (need n bars each side).
        """
        n   = self._lookback
        highs = df["high"].values
        lows  = df["low"].values
        swings: list[SwingPoint] = []

        for i in range(n, len(df) - n):
            window_h = highs[i - n : i + n + 1]
            window_l = lows[i  - n : i + n + 1]

            if highs[i] == window_h.max():
                swings.append(SwingPoint(
                    "HIGH", float(highs[i]), df.index[i], i
                ))
            if lows[i] == window_l.min():
                swings.append(SwingPoint(
                    "LOW", float(lows[i]), df.index[i], i
                ))

        return sorted(swings, key=lambda s: s.bar_index)

    # ── structure break detection ─────────────────────────────────────────────

    def _detect_structure(
        self, df: pd.DataFrame, swings: list[SwingPoint],
        pair: str, timeframe: str
    ) -> list[StructureEvent]:
        """
        For each bar after the confirmed swings, check if price closes
        through a prior swing high (Bullish BOS) or swing low (Bearish BOS).
        A CHoCH is flagged when the BOS direction opposes the last known BOS.
        """
        events: list[StructureEvent] = []
        key = (pair, timeframe)

        swing_highs = [s for s in swings if s.kind == "HIGH"]
        swing_lows  = [s for s in swings if s.kind == "LOW"]

        closes = df["close"].values
        n = self._lookback

        # Only scan bars that are AFTER all swings (confirmed, no lookahead)
        scan_start = (swings[-1].bar_index + 1) if swings else n + 1

        for i in range(scan_start, len(df)):
            close = closes[i]
            ts    = df.index[i]

            # ── Bullish BOS: close above a prior swing high ───────────────
            for sh in reversed(swing_highs):
                if sh.bar_index >= i:
                    continue  # only use confirmed prior swings
                if close > sh.price:
                    bos_type = "BOS_BULL"
                    # CHoCH if last BOS was bearish
                    if self._last_bos.get(key) == "BOS_BEAR":
                        bos_type = "CHOCH_BULL"
                    self._last_bos[key] = "BOS_BULL"
                    events.append(StructureEvent(bos_type, sh.price, ts, i))
                    log.debug(f"{bos_type} @ {sh.price:.5f} bar={i} ts={ts}")
                    break  # only fire on the most recent swing high

            # ── Bearish BOS: close below a prior swing low ────────────────
            for sl in reversed(swing_lows):
                if sl.bar_index >= i:
                    continue
                if close < sl.price:
                    bos_type = "BOS_BEAR"
                    if self._last_bos.get(key) == "BOS_BULL":
                        bos_type = "CHOCH_BEAR"
                    self._last_bos[key] = "BOS_BEAR"
                    events.append(StructureEvent(bos_type, sl.price, ts, i))
                    log.debug(f"{bos_type} @ {sl.price:.5f} bar={i} ts={ts}")
                    break

        return events

    # ── persistence ──────────────────────────────────────────────────────────

    def _persist_swings(self, pair: str, timeframe: str, swings: list[SwingPoint]):
        sql = (
            "INSERT OR IGNORE INTO structure_events "
            "(pair, timeframe, event_type, price, timestamp, bar_index) "
            "VALUES (?, ?, ?, ?, ?, ?)"
        )
        rows = [
            (pair, timeframe, f"SWING_{s.kind}", s.price,
             _ts_to_str(s.timestamp), s.bar_index)
            for s in swings
        ]
        with db.get() as conn:
            conn.executemany(sql, rows)

    def _persist_events(self, pair: str, timeframe: str, events: list[StructureEvent]):
        sql = (
            "INSERT OR IGNORE INTO structure_events "
            "(pair, timeframe, event_type, price, timestamp, bar_index) "
            "VALUES (?, ?, ?, ?, ?, ?)"
        )
        rows = [
            (pair, timeframe, e.event_type, e.price,
             _ts_to_str(e.timestamp), e.bar_index)
            for e in events
        ]
        with db.get() as conn:
            conn.executemany(sql, rows)


# ── helpers ──────────────────────────────────────────────────────────────────

def _ts_to_str(ts) -> str:
    """Convert pandas Timestamp or datetime to UTC ISO string."""
    if hasattr(ts, "isoformat"):
        return ts.isoformat()
    return str(ts)
