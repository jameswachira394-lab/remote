"""
v2/risk_manager.py
==================
Risk Management Engine

Responsibilities:
  - Calculate position size (lots) from account balance + risk %
  - Enforce daily loss limit gate
  - Enforce max concurrent open trades
  - Track day-start balance and reset at midnight UTC
  - Provide TP1 / TP2 pip values for reference

Formula (same as core/risk_manager.py, extended for multi-pair):
    risk_amount = balance × risk_pct / 100
    pip_value   = contract_size × pip_size        (USD per pip per lot)
    lots        = risk_amount / (risk_pips × pip_value)
    lots        = clamp(lots, volume_min, volume_max), rounded to volume_step
"""

import logging
from datetime import datetime, timezone, date
from typing import Optional

log = logging.getLogger("risk_manager")


class RiskManager:
    """
    Stateful risk controller. One instance shared across all pairs.

    Usage:
        rm = RiskManager(cfg["risk"], cfg["pairs"])
        lots = rm.calc_lot_size("XAUUSD", balance=10000, risk_pips=30)
        ok   = rm.check_daily_loss(current_balance=9850)
        ok   = rm.check_max_trades(open_count=2)
    """

    def __init__(self, risk_cfg: dict, pairs_cfg: list[dict]):
        self._rc         = risk_cfg
        self._pairs      = {p["symbol"]: p for p in pairs_cfg}

        # State
        self._day_start_balance: Optional[float] = None
        self._current_day: Optional[date]        = None

    # ── public ────────────────────────────────────────────────────────────────

    def on_new_day(self, balance: float):
        """Call at start of each new UTC trading day to reset day tracking."""
        today = datetime.now(timezone.utc).date()
        if today != self._current_day:
            self._day_start_balance = balance
            self._current_day       = today
            log.info(f"New day reset | date={today} | start_balance=${balance:,.2f}")

    def calc_lot_size(
        self,
        symbol:     str,
        balance:    float,
        risk_pips:  float,
        volume_min: float = 0.01,
        volume_max: float = 100.0,
        volume_step: float = 0.01,
    ) -> float:
        """
        Compute lot size so risk_pct% of balance is lost if SL hits.

        Returns lot size rounded to broker volume_step,
        clamped between volume_min and volume_max.
        """
        risk_pct    = self._rc.get("risk_pct", 0.5)
        risk_amount = balance * risk_pct / 100.0

        pair_cfg      = self._pairs.get(symbol, {})
        pip_size      = pair_cfg.get("pip_size", 0.0001)
        contract_size = pair_cfg.get("contract_size", 100_000)
        pip_value     = contract_size * pip_size      # USD per pip per standard lot

        if risk_pips <= 0 or pip_value <= 0:
            log.warning(f"[{symbol}] Invalid risk_pips={risk_pips} or pip_value={pip_value} → min lot")
            return volume_min

        raw_lots = risk_amount / (risk_pips * pip_value)
        lots     = round(raw_lots / volume_step) * volume_step
        lots     = max(volume_min, min(volume_max, lots))
        lots     = round(lots, 2)

        log.info(
            f"Lot calc [{symbol}] | "
            f"Balance=${balance:,.2f} Risk={risk_pct}% (${risk_amount:.2f}) | "
            f"Pips={risk_pips:.1f} PipVal=${pip_value:.4f}/pip/lot | "
            f"Lots={lots:.2f}"
        )
        return lots

    def check_daily_loss(self, current_balance: float) -> bool:
        """
        Returns True (trading allowed) / False (daily loss limit hit).
        Initialises day tracking on first call.
        """
        if self._day_start_balance is None:
            self._day_start_balance = current_balance
            return True

        max_loss_pct = self._rc.get("max_daily_loss_pct", 3.0)
        loss_pct = (
            (self._day_start_balance - current_balance)
            / self._day_start_balance * 100
        )

        if loss_pct >= max_loss_pct:
            log.warning(
                f"Daily loss limit hit: {loss_pct:.2f}% ≥ {max_loss_pct}% | "
                f"Start=${self._day_start_balance:.2f} Current=${current_balance:.2f}"
            )
            return False

        log.debug(f"Daily drawdown: {loss_pct:.2f}% (limit {max_loss_pct}%)")
        return True

    def check_max_trades(self, open_count: int) -> bool:
        """Returns True if adding another trade is within max_open_trades limit."""
        max_trades = self._rc.get("max_open_trades", 3)
        if open_count >= max_trades:
            log.warning(f"Max open trades reached: {open_count}/{max_trades}")
            return False
        return True

    def tp1_price(self, entry: float, sl: float, signal_type: str) -> float:
        """Calculate TP1 price using configured tp1_rr."""
        risk   = abs(entry - sl)
        ratio  = self._rc.get("tp1_rr", 1.0)
        return entry + risk * ratio if signal_type == "BUY" else entry - risk * ratio

    def tp2_price(self, entry: float, sl: float, signal_type: str) -> float:
        """Calculate TP2 price using configured tp2_rr."""
        risk   = abs(entry - sl)
        ratio  = self._rc.get("tp2_rr", 2.0)
        return entry + risk * ratio if signal_type == "BUY" else entry - risk * ratio

    def risk_summary(self, balance: float) -> dict:
        """Return a summary dict for CLI display."""
        return {
            "balance":          round(balance, 2),
            "risk_pct":         self._rc.get("risk_pct", 0.5),
            "risk_amount":      round(balance * self._rc.get("risk_pct", 0.5) / 100, 2),
            "max_open_trades":  self._rc.get("max_open_trades", 3),
            "max_daily_loss_pct": self._rc.get("max_daily_loss_pct", 3.0),
            "day_start_balance": self._day_start_balance,
        }
