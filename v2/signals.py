"""
v2/signals.py
=============
Module 5 — Signal Generator

Run every 5M candle close. For each pair:
  1. Get HTF bias from BiasEngine
  2. Check if price has returned to a fresh OB aligned with bias
  3. Check confirmation inside the OB zone:
       a. Rejection wick  (wick >= 60% of candle range)
       b. Engulfing candle (body engulfs prior candle's body)
       c. Internal 5M BOS  (close crosses an intra-OB swing)
  4. Calculate RR:
       Entry : current close (or OB midpoint if already inside)
       SL    : 2 pips/points beyond OB extreme
       TP1   : 1:1 RR (move BE after hit)
       TP2   : next swing high/low from structure_events table
       Skip  : if RR < min_rr (default 2.0)
  5. Session filter: London 08-12 UTC | NY 13-17 UTC
  6. Generate Signal object and persist to signals table

A signal is only generated ONCE per OB (deduplication via ob_id).
"""

import logging
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
import numpy as np

from v2.db import connection as db
from v2.ob_detector import OrderBlock
from v2.bias import BiasEngine, BULLISH, BEARISH, RANGING

log = logging.getLogger("signals")


# ══════════════════════════════════════════════════════════════════════════════
# DATA CLASS
# ══════════════════════════════════════════════════════════════════════════════

class Signal:
    """A trade signal ready for output / execution."""

    __slots__ = (
        "id", "pair", "timeframe", "signal_type",
        "entry", "sl", "tp1", "tp2",
        "risk_pips", "rr", "ob_id", "bias",
        "session", "confirmation", "status", "timestamp",
    )

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)
        if not hasattr(self, "id"):     self.id     = None
        if not hasattr(self, "status"): self.status = "new"

    def to_dict(self) -> dict:
        return {s: getattr(self, s) for s in self.__slots__}

    def __repr__(self):
        return (
            f"<Signal {self.signal_type} {self.pair} "
            f"E={self.entry:.5f} SL={self.sl:.5f} "
            f"TP2={self.tp2:.5f} RR={self.rr:.2f} [{self.session}]>"
        )


# ══════════════════════════════════════════════════════════════════════════════
# SIGNAL GENERATOR
# ══════════════════════════════════════════════════════════════════════════════

class SignalGenerator:
    """
    Evaluates active OBs against current price and generates trade signals.

    Usage:
        gen     = SignalGenerator(cfg["signal"], cfg["risk"], pair_cfg)
        signals = gen.evaluate(
            df5m, df15m, pair="XAUUSD", timeframe="5m",
            fresh_obs=ob_detector.get_fresh_aligned("XAUUSD","5m", bias),
            bias=bias_engine.get("XAUUSD"),
        )
    """

    def __init__(self, signal_cfg: dict, risk_cfg: dict, pairs_cfg: list[dict]):
        self._sc       = signal_cfg
        self._rc       = risk_cfg
        self._pairs    = {p["symbol"]: p for p in pairs_cfg}

        # Pre-computed from config
        self._min_rr          = signal_cfg.get("min_rr", 2.0)
        self._wick_ratio      = signal_cfg.get("wick_ratio", 0.60)
        self._engulf          = signal_cfg.get("engulf", True)
        self._internal_bos    = signal_cfg.get("internal_bos", True)
        self._require_confirm = signal_cfg.get("require_confirmation", True)
        self._sl_buf          = signal_cfg.get("sl_buffer_pips", 2)
        self._tp_target       = signal_cfg.get("tp_target", "next_swing")
        self._fixed_rr        = signal_cfg.get("fixed_rr", 3.0)
        self._sessions        = signal_cfg.get("sessions", {})
        self._session_enabled = self._sessions.get("enabled", True)

    # ── public ────────────────────────────────────────────────────────────────

    def evaluate(
        self,
        df5m:      pd.DataFrame,
        df15m:     pd.DataFrame,
        pair:      str,
        timeframe: str,
        fresh_obs: list[OrderBlock],
        bias:      str,
    ) -> list[Signal]:
        """
        Main evaluation loop. Returns list of generated signals (may be empty).
        Only evaluates the LAST COMPLETED bar (index -2) to avoid lookahead.
        """
        signals: list[Signal] = []

        if len(df5m) < 10:
            return signals

        # Session gate
        session = self._active_session()
        if self._session_enabled and session == "NONE":
            log.debug(f"[{pair}] Outside trading sessions — skipping signal check")
            return signals

        # Use last completed bar (index -2 avoids the live forming candle)
        bar_idx = len(df5m) - 2
        bar     = df5m.iloc[bar_idx]

        for ob in fresh_obs:
            # Deduplication: skip if this OB already fired a signal
            if self._ob_already_fired(ob.id):
                continue

            # Check OB retest gap
            try:
                ob_bar = df5m.index.get_loc(
                    pd.Timestamp(ob.timestamp).tz_localize("UTC")
                    if pd.Timestamp(ob.timestamp).tzinfo is None
                    else pd.Timestamp(ob.timestamp)
                )
            except (KeyError, Exception):
                ob_bar = 0

            min_bar = ob_bar + self._sc.get("min_retest_gap", 5)
            if bar_idx < min_bar:
                continue

            # Is price retesting the OB zone?
            in_zone = self._price_in_zone(bar, ob)
            if not in_zone:
                continue

            # Confirmation check
            confirmed, conf_type = self._check_confirmation(df5m, bar_idx, ob)
            if self._require_confirm and not confirmed:
                log.debug(f"[{pair}] OB #{ob.id}: no confirmation — skipped")
                continue

            # Build levels
            sig = self._build_signal(
                df5m, df15m, bar_idx, bar, ob,
                pair, timeframe, bias, session,
                conf_type or "zone_touch",
            )
            if sig is None:
                continue

            signals.append(sig)
            self._persist(sig)
            log.info(
                f"SIGNAL | {sig.signal_type} {pair} | "
                f"E={sig.entry:.5f} SL={sig.sl:.5f} "
                f"TP1={sig.tp1:.5f} TP2={sig.tp2:.5f} "
                f"RR={sig.rr:.2f} | confirm={conf_type} | session={session}"
            )

        return signals

    # ── zone check ───────────────────────────────────────────────────────────

    def _price_in_zone(self, bar: pd.Series, ob: OrderBlock) -> bool:
        """True if the bar's wick entered the OB zone and close respects it."""
        lo = float(bar["low"])
        hi = float(bar["high"])
        cl = float(bar["close"])
        if ob.ob_type == "BULL":
            return lo <= ob.high and cl >= ob.low
        else:  # BEAR
            return hi >= ob.low and cl <= ob.high

    # ── confirmation ─────────────────────────────────────────────────────────

    def _check_confirmation(
        self, df: pd.DataFrame, i: int, ob: OrderBlock
    ) -> tuple[bool, Optional[str]]:
        """
        Returns (confirmed: bool, confirmation_type: str | None).
        Checks three patterns in order: wick, engulf, internal BOS.
        """
        bar   = df.iloc[i]
        o, h, l, c = float(bar["open"]), float(bar["high"]), float(bar["low"]), float(bar["close"])
        body  = abs(c - o)
        rng   = h - l if (h - l) > 0 else 1e-9

        # ── 1. Rejection wick ─────────────────────────────────────────────
        if ob.ob_type == "BULL":
            lower_wick = min(o, c) - l
            if (lower_wick / rng) >= self._wick_ratio:
                return True, "rejection_wick"
        else:
            upper_wick = h - max(o, c)
            if (upper_wick / rng) >= self._wick_ratio:
                return True, "rejection_wick"

        # ── 2. Engulfing candle ───────────────────────────────────────────
        if self._engulf and i > 0:
            prev = df.iloc[i - 1]
            po, pc = float(prev["open"]), float(prev["close"])
            if ob.ob_type == "BULL":
                # Current bullish engulfs prior bearish
                if pc < po and c > o and c > po and o < pc:
                    return True, "engulfing"
            else:
                # Current bearish engulfs prior bullish
                if pc > po and c < o and c < po and o > pc:
                    return True, "engulfing"

        # ── 3. Internal 5M BOS ────────────────────────────────────────────
        if self._internal_bos and i >= 3:
            # Simple: current close breaks the highest high / lowest low
            # of the last 3 bars inside the zone
            recent_bars = df.iloc[i - 3 : i]
            if ob.ob_type == "BULL":
                mini_high = float(recent_bars["high"].max())
                if c > mini_high:
                    return True, "internal_bos"
            else:
                mini_low = float(recent_bars["low"].min())
                if c < mini_low:
                    return True, "internal_bos"

        return False, None

    # ── signal builder ────────────────────────────────────────────────────────

    def _build_signal(
        self,
        df5m:     pd.DataFrame,
        df15m:    pd.DataFrame,
        bar_idx:  int,
        bar:      pd.Series,
        ob:       OrderBlock,
        pair:     str,
        timeframe: str,
        bias:     str,
        session:  str,
        conf_type: str,
    ) -> Optional[Signal]:

        pair_cfg  = self._pairs.get(pair, {})
        pip_size  = pair_cfg.get("pip_size", 0.0001)
        sl_buf    = pair_cfg.get("sl_buffer_pips", self._sl_buf) * pip_size

        close = float(bar["close"])
        spread = pip_size  # 1 pip spread assumption

        if ob.ob_type == "BULL":
            sig_type = "BUY"
            entry    = close + spread
            sl       = ob.low - sl_buf
            risk     = entry - sl
        else:
            sig_type = "SELL"
            entry    = close - spread
            sl       = ob.high + sl_buf
            risk     = sl - entry

        if risk <= 0:
            log.debug(f"[{pair}] OB #{ob.id}: risk <= 0 — skipped")
            return None

        # TP levels
        tp1 = (entry + risk * self._rc.get("tp1_rr", 1.0)
               if sig_type == "BUY"
               else entry - risk * self._rc.get("tp1_rr", 1.0))

        tp2 = self._get_tp2(df15m, ob, entry, risk, sig_type, pip_size)
        if tp2 is None:
            return None

        # RR check
        reward = abs(tp2 - entry)
        rr     = reward / risk if risk > 0 else 0
        if rr < self._min_rr:
            log.debug(f"[{pair}] OB #{ob.id}: RR={rr:.2f} < {self._min_rr} — skipped")
            return None

        risk_pips = round(risk / pip_size, 1)

        return Signal(
            pair        = pair,
            timeframe   = timeframe,
            signal_type = sig_type,
            entry       = round(entry, 5),
            sl          = round(sl,    5),
            tp1         = round(tp1,   5),
            tp2         = round(tp2,   5),
            risk_pips   = risk_pips,
            rr          = round(rr, 2),
            ob_id       = ob.id,
            bias        = bias,
            session     = session,
            confirmation= conf_type,
            timestamp   = df5m.index[bar_idx].isoformat(),
        )

    def _get_tp2(
        self,
        df15m:    pd.DataFrame,
        ob:       OrderBlock,
        entry:    float,
        risk:     float,
        sig_type: str,
        pip_size: float,
    ) -> Optional[float]:
        """
        Determine TP2.
        Mode 'next_swing': use the nearest 15M swing high (BUY) or low (SELL)
        Mode 'fixed_rr':   use entry ± risk × fixed_rr
        """
        if self._tp_target == "fixed_rr":
            if sig_type == "BUY":
                return entry + risk * self._fixed_rr
            return entry - risk * self._fixed_rr

        # next_swing: scan df15m for the nearest opposing swing
        if df15m.empty:
            # fallback to 2:1
            return entry + risk * 2.0 if sig_type == "BUY" else entry - risk * 2.0

        n = 3  # pivot lookback on 15M
        highs = df15m["high"].values
        lows  = df15m["low"].values

        # Collect all swing highs / lows after the OB formed
        swings_h = []
        swings_l = []
        for i in range(n, len(df15m) - n):
            if highs[i] == highs[i - n : i + n + 1].max():
                swings_h.append(float(highs[i]))
            if lows[i] == lows[i - n : i + n + 1].min():
                swings_l.append(float(lows[i]))

        if sig_type == "BUY":
            # Next swing high ABOVE entry
            targets = [h for h in swings_h if h > entry + risk]
            if targets:
                return min(targets)          # nearest high above
            return entry + risk * 2.0        # fallback
        else:
            # Next swing low BELOW entry
            targets = [l for l in swings_l if l < entry - risk]
            if targets:
                return max(targets)          # nearest low below
            return entry - risk * 2.0

    # ── session filter ────────────────────────────────────────────────────────

    def _active_session(self) -> str:
        """Return 'LONDON', 'NEW_YORK', or 'NONE' based on current UTC hour."""
        utc_hour = datetime.now(timezone.utc).hour

        lon = self._sessions.get("london", {})
        ny  = self._sessions.get("new_york", {})

        if lon.get("start", 8) <= utc_hour < lon.get("end", 12):
            return "LONDON"
        if ny.get("start", 13) <= utc_hour < ny.get("end", 17):
            return "NEW_YORK"
        return "NONE"

    # ── deduplication ─────────────────────────────────────────────────────────

    def _ob_already_fired(self, ob_id: Optional[int]) -> bool:
        if ob_id is None:
            return False
        row = db.fetchone(
            "SELECT id FROM signals WHERE ob_id=? AND status != 'expired' LIMIT 1",
            (ob_id,),
        )
        return row is not None

    # ── persistence ──────────────────────────────────────────────────────────

    def _persist(self, sig: Signal):
        sql = (
            "INSERT INTO signals "
            "(pair, timeframe, signal_type, entry, sl, tp1, tp2, "
            " risk_pips, rr, ob_id, bias, session, status, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )
        with db.get() as conn:
            cur = conn.execute(sql, (
                sig.pair, sig.timeframe, sig.signal_type,
                sig.entry, sig.sl, sig.tp1, sig.tp2,
                sig.risk_pips, sig.rr, sig.ob_id,
                sig.bias, sig.session, sig.status, sig.timestamp,
            ))
            sig.id = cur.lastrowid
