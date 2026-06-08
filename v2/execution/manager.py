"""
v2/execution/manager.py
=======================
Interface for live execution. If the configured data source is MT5, 
this handles translating a Signal object into a live MT5 order, and 
periodically checks positions to manage SL/TP via PositionManager.
"""

import logging
from typing import Optional
from v2.signals import Signal
from v2.execution.connector import get_connector
from v2.execution.order_executor import OrderExecutor
from v2.execution.position_manager import PositionManager

log = logging.getLogger("execution.manager")


class ExecutionManager:
    """Facade for all live trading execution operations."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.source = cfg.get("data_source", "ccxt").lower()
        
        # MT5 is the only execution backend supported right now
        self.use_mt5 = (self.source == "mt5")
        
        if self.use_mt5:
            # We assume mt5 dict contains execution keys (magic_number, slippage)
            self.mt5_cfg = cfg.get("mt5", {})
            self.risk_cfg = cfg.get("risk", {})
            self.executor = OrderExecutor(self.mt5_cfg)
            
            # Since PositionManager needs pip_size, we'll store a mapping per symbol
            # For simplicity, we create one PositionManager per symbol or pass the pip size.
            # Here we just use a generic PositionManager and rely on the config to set pip.
            # If multiple pairs have vastly different pips (e.g. JPY vs USD), this needs mapping.
            # To keep it robust, we'll manage multiple position managers mapped by symbol.
            self.pos_managers = {}
            for pair in cfg.get("pairs", []):
                sym = pair["symbol"]
                pip = pair.get("pip_size", 0.0001)
                self.pos_managers[sym] = PositionManager(self.mt5_cfg, self.risk_cfg, pip)
                
            self.connector = get_connector(self.mt5_cfg)
        else:
            log.info(f"ExecutionManager: Data source is '{self.source}'. Live execution will be disabled.")

    def connect(self):
        if self.use_mt5:
            self.connector.connect()

    def disconnect(self):
        if self.use_mt5:
            self.connector.disconnect()

    def execute_signal(self, sig: Signal, volume: float) -> Optional[dict]:
        """Send a live trade based on the provided Signal and volume."""
        if not self.use_mt5:
            log.warning(f"Live execution bypassed (source={self.source}). Would have executed {sig.signal_type} {sig.pair}.")
            return None
            
        if not self.connector.is_alive():
            log.error("Cannot execute signal: MT5 disconnected")
            return None

        result = self.executor.execute_market_order(
            symbol=sig.pair,
            direction=sig.signal_type,
            volume=volume,
            sl=sig.sl,
            tp=sig.tp2,
            risk_pips=sig.risk_pips
        )
        
        if result and result.get("ticket"):
            ticket = result["ticket"]
            # Register TP1 for breakeven logic
            pm = self.pos_managers.get(sig.pair)
            if pm:
                pm.register_trade(ticket, sig.tp1)
            
        return result

    def get_open_count(self) -> int:
        """Get total open trades for this bot."""
        if not self.use_mt5:
            return 0
        total = 0
        for pm in self.pos_managers.values():
            total += pm.open_count()
        return total

    def manage_positions(self):
        """Monitor open positions, move SL to BE, etc."""
        if not self.use_mt5 or not self.connector.is_alive():
            return
            
        for sym, pm in self.pos_managers.items():
            pm.monitor(self.executor)
