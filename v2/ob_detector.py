"""
v2/ob_detector.py
=================
Module 3 — Order Block Detection

Logic (mirrors + extends core/ob_detector.py):
  After every confirmed BOS, look back into the impulse and find:
    Bullish OB : last BEARISH candle before the bullish impulse
    Bearish OB : last BULLISH candle before the bearish impulse

Validation rules:
  1. Impulse must be >= impulse_candles (default 2) consecutive same-dir closes
  2. Impulse must have caused a confirmed BOS event
  3. OB candle must have a body (not a doji)
  4. OB body ratio (body / range) >= body_ratio_min  (default 0.5)
  5. OB must not be older than max_ob_age_bars

Lifecycle (status field):
  fresh   → OB just formed, price has not retested it yet
  tested  → price re-entered the zone (tap_count incremented)
  invalid → price CLOSED fully through OB (fully mitigated)
            OR tap_count > max_taps

DB table: order_blocks
  pair, timeframe, ob_type, high, low, open, close,
  timestamp, formation_ts, status, tap_count, bos_event_id
"""

import logging
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
import numpy as np

from v2.db import connection as db
from v2.structure import StructureEvent

log = logging.getLogger("ob_detector")


# ══════════════════════════════════════════════════════════════════════════════
# DATA CLASS
# ══════════════════════════════════════════════════════════════════════════════

class OrderBlock:
    """Represents a detected Order Block zone."""

    __slots__ = (
        "id", "pair", "timeframe", "ob_type",
        "high", "low", "open", "close",
        "timestamp", "formation_ts", "status", "tap_count", "bos_event_id"
    )

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)
        if not hasattr(self, "id"):          self.id          = None
        if not hasattr(self, "tap_count"):   self.tap_count   = 0
        if not hasattr(self, "status"):      self.status      = "fresh"
        if not hasattr(self, "bos_event_id"):self.bos_event_id= None

    @property
    def midpoint(self) -> float:
        return (self.high + self.low) / 2.0

    @property
    def size(self) -> float:
        return self.high - self.low

    def __repr__(self):
        return (
            f"<OB {self.ob_type} {self.pair}/{self.timeframe} "
            f"H={self.high:.5f} L={self.low:.5f} [{self.status}]>"
        )


# ══════════════════════════════════════════════════════════════════════════════
# DETECTOR
# ══════════════════════════════════════════════════════════════════════════════

class OrderBlockDetector:
    """
    Detects, validates, and manages Order Blocks.

    Usage:
        detector = OrderBlockDetector(cfg["ob"])
        obs = detector.detect(df, pair="XAUUSD", timeframe="5m",
                              bos_events=events)
        fresh_obs = detector.get_fresh(pair="XAUUSD", timeframe="5m")
    """

    def __init__(self, cfg: dict):
        self._n_imp      = cfg.get("impulse_candles",  2)
        self._lb         = cfg.get("bos_lookback",     5)
        self._max_age    = cfg.get("max_ob_age_bars",  300)
        self._max_taps   = cfg.get("max_taps",         1)
        self._body_ratio = cfg.get("body_ratio_min",   0.5)
        self._min_gap    = cfg.get("min_retest_gap",   5)

    # ── public ────────────────────────────────────────────────────────────────

    def detect(
        self,
        df: pd.DataFrame,
        pair: str,
        timeframe: str,
        bos_events: list[StructureEvent] | None = None,
    ) -> list[OrderBlock]:
        """
        Full scan of df for Order Blocks triggered by BOS events.
        Returns list of ALL OBs (fresh + tested).
        Also calls update_status() to keep DB in sync with current price.
        """
        obs: list[OrderBlock] = []
        seen_bars: set[int]   = set()
        last_bar = len(df) - 1
        cutoff   = last_bar - self._max_age

        for i in range(self._lb + 1, last_bar - self._n_imp - 1):

            # ── Bullish impulse → Bullish OB ─────────────────────────────
            if self._is_impulse(df, i, "bullish"):
                ob_bar = self._find_ob_candle(df, i, "bearish")
                if ob_bar is not None and ob_bar not in seen_bars and ob_bar >= cutoff:
                    ob = self._build_ob(df, ob_bar, i, "BULL", pair, timeframe)
                    if ob:
                        obs.append(ob)
                        seen_bars.add(ob_bar)

            # ── Bearish impulse → Bearish OB ─────────────────────────────
            if self._is_impulse(df, i, "bearish"):
                ob_bar = self._find_ob_candle(df, i, "bullish")
                if ob_bar is not None and ob_bar not in seen_bars and ob_bar >= cutoff:
                    ob = self._build_ob(df, ob_bar, i, "BEAR", pair, timeframe)
                    if ob:
                        obs.append(ob)
                        seen_bars.add(ob_bar)

        self._upsert_obs(obs)
        self.update_status(df, pair, timeframe)

        fresh = [o for o in obs if o.status == "fresh"]
        log.info(
            f"OB Scan [{pair} {timeframe}] | "
            f"detected={len(obs)} | fresh={len(fresh)}"
        )
        return obs

    def update_status(self, df: pd.DataFrame, pair: str, timeframe: str):
        """
        Check every active OB against the latest bar and update status:
          - tested  : wick touched the OB zone
          - invalid : close moved fully through the OB zone
        """
        if df.empty:
            return

        last_close = float(df["close"].iloc[-1])
        last_low   = float(df["low"].iloc[-1])
        last_high  = float(df["high"].iloc[-1])

        active_obs = self.get_active(pair, timeframe)
        for ob in active_obs:
            new_status = ob.status
            tap_delta  = 0

            if ob.ob_type == "BULL":
                # Price wicked into zone
                if last_low <= ob.high and last_close >= ob.low:
                    new_status = "tested"
                    tap_delta  = 1
                # Price closed BELOW OB — fully mitigated
                elif last_close < ob.low:
                    new_status = "invalid"

            elif ob.ob_type == "BEAR":
                # Price wicked into zone
                if last_high >= ob.low and last_close <= ob.high:
                    new_status = "tested"
                    tap_delta  = 1
                # Price closed ABOVE OB — fully mitigated
                elif last_close > ob.high:
                    new_status = "invalid"

            new_taps = ob.tap_count + tap_delta
            if new_taps > self._max_taps:
                new_status = "invalid"

            if new_status != ob.status or tap_delta > 0:
                db.execute(
                    "UPDATE order_blocks SET status=?, tap_count=?, updated_at=? "
                    "WHERE id=?",
                    (new_status, new_taps, _now_utc(), ob.id),
                )
                log.debug(f"OB #{ob.id} status: {ob.status} → {new_status} (taps={new_taps})")

    def get_fresh(self, pair: str, timeframe: str) -> list[OrderBlock]:
        """Return all 'fresh' OBs for a given pair/timeframe from DB."""
        return self._load_obs(pair, timeframe, status="fresh")

    def get_active(self, pair: str, timeframe: str) -> list[OrderBlock]:
        """Return all non-invalid OBs (fresh + tested)."""
        rows = db.fetchall(
            "SELECT * FROM order_blocks "
            "WHERE pair=? AND timeframe=? AND status != 'invalid' "
            "ORDER BY timestamp DESC",
            (pair, timeframe),
        )
        return [self._row_to_ob(r) for r in rows]

    def get_fresh_aligned(
        self, pair: str, timeframe: str, bias: str
    ) -> list[OrderBlock]:
        """
        Return fresh OBs that align with the given HTF bias.
        bias='BULLISH' → return BULL OBs
        bias='BEARISH' → return BEAR OBs
        bias='RANGING' → return all fresh OBs
        """
        obs = self.get_fresh(pair, timeframe)
        if bias == "BULLISH":
            return [o for o in obs if o.ob_type == "BULL"]
        if bias == "BEARISH":
            return [o for o in obs if o.ob_type == "BEAR"]
        return obs  # RANGING — return all

    # ── impulse detection ─────────────────────────────────────────────────────

    def _is_impulse(self, df: pd.DataFrame, i: int, direction: str) -> bool:
        """
        True if bars i+1 … i+n_imp are all same-direction closes
        AND the final close breaks a prior swing (BOS).
        """
        n = self._n_imp
        if i + n >= len(df):
            return False

        closes = df["close"].values
        opens  = df["open"].values

        if direction == "bullish":
            all_up = all(closes[i + k] > opens[i + k] for k in range(1, n + 1))
            if not all_up:
                return False
            # Verify body quality
            if not all(self._body_ok(df, i + k) for k in range(1, n + 1)):
                return False
            # BOS: close above prior swing high
            prior_high = df["high"].iloc[max(0, i - self._lb) : i].max()
            return float(closes[i + n]) > float(prior_high)

        else:  # bearish
            all_dn = all(closes[i + k] < opens[i + k] for k in range(1, n + 1))
            if not all_dn:
                return False
            if not all(self._body_ok(df, i + k) for k in range(1, n + 1)):
                return False
            prior_low = df["low"].iloc[max(0, i - self._lb) : i].min()
            return float(closes[i + n]) < float(prior_low)

    def _body_ok(self, df: pd.DataFrame, i: int) -> bool:
        """True if candle has a body ratio >= body_ratio_min (not a doji)."""
        body  = abs(float(df["close"].iloc[i]) - float(df["open"].iloc[i]))
        rng   = float(df["high"].iloc[i]) - float(df["low"].iloc[i])
        if rng == 0:
            return False
        return (body / rng) >= self._body_ratio

    # ── OB candle finder ──────────────────────────────────────────────────────

    def _find_ob_candle(
        self, df: pd.DataFrame, impulse_start: int, direction: str
    ) -> Optional[int]:
        """
        Walk backward from impulse_start and return the bar index of
        the LAST candle in the given direction before the impulse.
        direction='bearish' → find last bearish bar (for Bullish OB)
        direction='bullish' → find last bullish bar (for Bearish OB)
        """
        closes = df["close"].values
        opens  = df["open"].values
        for j in range(impulse_start, max(impulse_start - self._lb - 1, -1), -1):
            if direction == "bearish" and closes[j] < opens[j]:
                return j
            if direction == "bullish" and closes[j] > opens[j]:
                return j
        return None

    # ── OB builder ───────────────────────────────────────────────────────────

    def _build_ob(
        self,
        df: pd.DataFrame,
        ob_bar: int,
        impulse_start: int,
        ob_type: str,
        pair: str,
        timeframe: str,
    ) -> Optional[OrderBlock]:
        row = df.iloc[ob_bar]
        formation_bar = impulse_start + self._n_imp
        if formation_bar >= len(df):
            return None

        return OrderBlock(
            pair        = pair,
            timeframe   = timeframe,
            ob_type     = ob_type,
            high        = float(row["high"]),
            low         = float(row["low"]),
            open        = float(row["open"]),
            close       = float(row["close"]),
            timestamp   = _ts_to_str(df.index[ob_bar]),
            formation_ts= _ts_to_str(df.index[formation_bar]),
            status      = "fresh",
            tap_count   = 0,
        )

    # ── DB helpers ────────────────────────────────────────────────────────────

    def _upsert_obs(self, obs: list[OrderBlock]):
        sql = (
            "INSERT OR IGNORE INTO order_blocks "
            "(pair, timeframe, ob_type, high, low, open, close, "
            " timestamp, formation_ts, status, tap_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )
        rows = [
            (o.pair, o.timeframe, o.ob_type, o.high, o.low, o.open,
             o.close, o.timestamp, o.formation_ts, o.status, o.tap_count)
            for o in obs
        ]
        with db.get() as conn:
            conn.executemany(sql, rows)
            # Re-load IDs
            for o in obs:
                cur = conn.execute(
                    "SELECT id FROM order_blocks "
                    "WHERE pair=? AND timeframe=? AND ob_type=? AND timestamp=?",
                    (o.pair, o.timeframe, o.ob_type, o.timestamp),
                )
                row = cur.fetchone()
                if row:
                    o.id = row["id"]

    def _load_obs(self, pair: str, timeframe: str, status: str = "fresh") -> list[OrderBlock]:
        rows = db.fetchall(
            "SELECT * FROM order_blocks WHERE pair=? AND timeframe=? AND status=? "
            "ORDER BY timestamp DESC",
            (pair, timeframe, status),
        )
        return [self._row_to_ob(r) for r in rows]

    @staticmethod
    def _row_to_ob(row) -> OrderBlock:
        d = dict(row)
        return OrderBlock(**d)


# ── helpers ──────────────────────────────────────────────────────────────────

def _ts_to_str(ts) -> str:
    if hasattr(ts, "isoformat"):
        return ts.isoformat()
    return str(ts)

def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()
