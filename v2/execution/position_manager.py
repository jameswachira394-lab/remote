"""
v2/execution/position_manager.py
================================
Monitors all open positions placed by this bot.
"""

import logging
from v2.utils.logger import log_trade
from v2.execution.connector import get_connector

log = logging.getLogger("position_manager")


class PositionManager:
    def __init__(self, cfg_exec: dict, cfg_risk: dict, pip_size: float = 0.0001):
        self.magic    = cfg_exec.get('magic_number', 202501)
        self.cfg_risk = cfg_risk
        self.pip      = pip_size
        self._tp1_map: dict[int, float] = {}
        self._highest_price: dict[int, float] = {}

    def register_trade(self, ticket: int, tp1: float):
        self._tp1_map[ticket] = tp1
        log.debug(f"Registered TP1={tp1:.5f} for ticket #{ticket}")

    def monitor(self, executor) -> list[dict]:
        positions = self._get_positions()
        if not positions:
            self._cleanup_closed_positions()
        summaries = []

        for pos in positions:
            tick = self._get_tick(pos.symbol)
            if tick is None:
                continue

            current_price = tick.bid if pos.type == 0 else tick.ask
            entry         = pos.price_open
            sl            = pos.sl
            tp1           = self._tp1_map.get(pos.ticket)

            pips = ((current_price - entry) / self.pip if pos.type == 0 else (entry - current_price) / self.pip)

            summary = {
                'ticket':    pos.ticket,
                'symbol':    pos.symbol,
                'type':      'BUY' if pos.type == 0 else 'SELL',
                'volume':    pos.volume,
                'entry':     entry,
                'sl':        sl,
                'current':   current_price,
                'pips':      round(pips, 1),
                'profit':    pos.profit,
            }
            summaries.append(summary)

            # Move SL to breakeven after TP1
            if (tp1 is not None and self.cfg_risk.get('move_be_at_tp1', True) and not self._is_at_be(sl, entry)):
                tp1_hit = ((pos.type == 0 and current_price >= tp1) or (pos.type == 1 and current_price <= tp1))
                if tp1_hit:
                    log.info(f"TP1 hit for #{pos.ticket} | Moving SL to breakeven {entry:.5f}")
                    success = executor.modify_sl(pos, entry)
                    if success:
                        del self._tp1_map[pos.ticket]
                        log_trade(
                            action="MODIFY_BE", symbol=pos.symbol,
                            direction=summary['type'], volume=pos.volume,
                            entry=entry, sl=entry, tp1=tp1, tp2=pos.tp,
                            ticket=pos.ticket
                        )

            # Trailing stop handling
            if self.cfg_risk.get('trailing_stop_enabled', False):
                ts_pips = self.cfg_risk.get('trailing_stop_pips', 15)
                min_profit_pips = 10
                
                if pos.type == 0:
                    if pips >= min_profit_pips:
                        if pos.ticket not in self._highest_price:
                            self._highest_price[pos.ticket] = current_price
                        elif current_price > self._highest_price[pos.ticket]:
                            self._highest_price[pos.ticket] = current_price
                        
                        trail_sl = self._highest_price[pos.ticket] - ts_pips * self.pip
                        if trail_sl > sl:
                            if executor.modify_sl(pos, trail_sl):
                                log_trade(
                                    action="MODIFY_TRAIL", symbol=pos.symbol,
                                    direction=summary['type'], volume=pos.volume,
                                    entry=entry, sl=trail_sl, tp1=tp1, tp2=pos.tp,
                                    ticket=pos.ticket
                                )

                if pos.type == 1:
                    if pips >= min_profit_pips:
                        if pos.ticket not in self._highest_price:
                            self._highest_price[pos.ticket] = current_price
                        elif current_price < self._highest_price[pos.ticket]:
                            self._highest_price[pos.ticket] = current_price
                        
                        trail_sl = self._highest_price[pos.ticket] + ts_pips * self.pip
                        trail_sl = min(trail_sl, entry - 0.0001)
                        if trail_sl < sl:
                            if executor.modify_sl(pos, trail_sl):
                                log_trade(
                                    action="MODIFY_TRAIL", symbol=pos.symbol,
                                    direction=summary['type'], volume=pos.volume,
                                    entry=entry, sl=trail_sl, tp1=tp1, tp2=pos.tp,
                                    ticket=pos.ticket
                                )

        self._cleanup_closed_positions(positions)
        return summaries

    def open_count(self) -> int:
        return len(self._get_positions())

    def close_all(self, executor) -> int:
        positions = self._get_positions()
        closed = 0
        for pos in positions:
            if executor.close_position(pos):
                closed += 1
        return closed

    def _get_positions(self) -> list:
        mt5 = get_connector().get_mt5()
        if mt5 is None: return []
        all_pos = mt5.positions_get()
        if all_pos is None: return []
        return [p for p in all_pos if p.magic == self.magic]

    def _get_tick(self, symbol: str):
        mt5 = get_connector().get_mt5()
        if mt5 is None: return None
        return mt5.symbol_info_tick(symbol)

    def _is_at_be(self, sl: float, entry: float, tol: float = 0.00003) -> bool:
        return abs(sl - entry) < tol

    def _cleanup_closed_positions(self, current_positions: list | None = None):
        if current_positions is None:
            current_positions = []
        open_tickets = {p.ticket for p in current_positions}
        closed_tickets = [t for t in self._tp1_map if t not in open_tickets]
        for ticket in closed_tickets:
            del self._tp1_map[ticket]
            self._highest_price.pop(ticket, None)
