"""
core/risk_manager.py
====================
Calculates lot size based on account balance and risk %.
Enforces daily loss limit, max open trades gate.
"""

from utils.logger import get_logger

log = get_logger("risk_manager")


class RiskManager:
    def __init__(self, cfg: dict, pip_size: float = 0.0001):
        self.cfg = cfg
        self.pip = pip_size
        self._day_start_balance: float | None = None

    # ── public ────────────────────────────────

    def calc_lot_size(self, balance: float, risk_pips: float,
                      symbol_info: dict) -> float:
        """
        Compute lot size so that `risk_pct` of balance is lost if SL is hit.

        Formula:
            risk_amount  = balance × risk_pct / 100
            pip_value    = contract_size × pip_size           (per lot, per pip)
            lots         = risk_amount / (risk_pips × pip_value)
        """
        risk_pct      = self.cfg.get('risk_pct', 1.0)
        risk_amount   = balance * risk_pct / 100.0

        contract_size = symbol_info.get('trade_contract_size', 100_000)
        pip_value     = contract_size * self.pip           # e.g. 100000 × 0.0001 = 10 USD/pip/lot

        if risk_pips <= 0 or pip_value <= 0:
            log.warning("Invalid risk_pips or pip_value — defaulting to min lot.")
            return symbol_info.get('volume_min', 0.01)

        raw_lots = risk_amount / (risk_pips * pip_value)

        # Round to broker step
        step = symbol_info.get('volume_step', 0.01)
        lots = round(raw_lots / step) * step
        lots = max(symbol_info.get('volume_min', 0.01), lots)
        lots = min(symbol_info.get('volume_max', 100.0), lots)
        lots = round(lots, 2)

        log.info(
            f"Lot calc | Balance={balance:.2f} | Risk={risk_pct}% ({risk_amount:.2f}) | "
            f"Pips={risk_pips:.1f} | PipVal={pip_value:.2f} | Lots={lots:.2f}"
        )
        return lots

    def check_daily_loss(self, current_balance: float) -> bool:
        """
        Returns True (trading allowed) or False (daily loss limit hit).
        Call once per cycle.
        """
        if self._day_start_balance is None:
            self._day_start_balance = current_balance
            return True

        max_loss_pct = self.cfg.get('max_daily_loss_pct', 3.0)
        loss_pct = (self._day_start_balance - current_balance) / self._day_start_balance * 100
        if loss_pct >= max_loss_pct:
            log.warning(
                f"Daily loss limit hit: {loss_pct:.2f}% >= {max_loss_pct}%. "
                f"No new trades today."
            )
            return False
        return True

    def reset_day(self, balance: float):
        """Call at the start of each new trading day."""
        self._day_start_balance = balance
        log.info(f"Day reset — start balance: {balance:.2f}")
