"""
mt5/order_executor.py
=====================
Sends buy/sell orders to MT5 and handles retries.
All order requests go through execute_market_order().
"""

import time
from utils.logger import get_logger, log_trade
from utils import notifier

log = get_logger("order_executor")


class OrderExecutor:
    def __init__(self, cfg_exec: dict):
        self.cfg = cfg_exec

    # ── public ────────────────────────────────

    def execute_market_order(
        self,
        symbol: str,
        direction: str,   # 'BUY' | 'SELL'
        volume: float,
        sl: float,
        tp: float,        # This will be TP2 — full target
        comment: str = "",
    ) -> dict | None:
        """
        Place a market order. Returns order result dict or None on failure.
        TP here is the final TP2 level; TP1 management is done in position_manager.
        """
        mt5      = self._get_mt5()
        order_type = mt5.ORDER_TYPE_BUY if direction == 'BUY' else mt5.ORDER_TYPE_SELL
        tick     = mt5.symbol_info_tick(symbol)
        if tick is None:
            log.error(f"No tick for {symbol}")
            return None

        price = tick.ask if direction == 'BUY' else tick.bid
        magic = self.cfg.get('magic_number', 202501)
        slip  = self.cfg.get('slippage', 3)
        cmt   = comment or self.cfg.get('comment', 'OB_Bot')

        request = {
            "action":      mt5.TRADE_ACTION_DEAL,
            "symbol":      symbol,
            "volume":      float(volume),
            "type":        order_type,
            "price":       price,
            "sl":          sl,
            "tp":          tp,
            "deviation":   slip,
            "magic":       magic,
            "comment":     cmt,
            "type_time":   mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        result = self._send_with_retry(mt5, request)
        if result is None:
            return None

        log_trade(
            action="OPEN", symbol=symbol, direction=direction,
            volume=volume, entry=price, sl=sl, tp1=0, tp2=tp,
            ticket=result.order,
            extra={"retcode": result.retcode, "comment": result.comment}
        )
        notifier.alert_fill(symbol, direction, result.order, price, volume)
        return {
            "ticket":    result.order,
            "direction": direction,
            "symbol":    symbol,
            "volume":    volume,
            "entry":     price,
            "sl":        sl,
            "tp2":       tp,
            "retcode":   result.retcode,
        }

    def close_position(self, position) -> bool:
        """Close an open MT5 position object."""
        mt5     = self._get_mt5()
        tick    = mt5.symbol_info_tick(position.symbol)
        if tick is None:
            log.error(f"No tick for {position.symbol}")
            return False

        if position.type == mt5.POSITION_TYPE_BUY:
            order_type = mt5.ORDER_TYPE_SELL
            price      = tick.bid
        else:
            order_type = mt5.ORDER_TYPE_BUY
            price      = tick.ask

        request = {
            "action":      mt5.TRADE_ACTION_DEAL,
            "symbol":      position.symbol,
            "volume":      position.volume,
            "type":        order_type,
            "position":    position.ticket,
            "price":       price,
            "deviation":   self.cfg.get('slippage', 3),
            "magic":       self.cfg.get('magic_number', 202501),
            "comment":     "OB_Bot_Close",
            "type_time":   mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        result = self._send_with_retry(mt5, request)
        if result is None:
            return False

        log.info(
            f"Closed position {position.ticket} | "
            f"{position.symbol} | profit={position.profit:.2f}"
        )
        return True

    def modify_sl(self, position, new_sl: float) -> bool:
        """Modify the stop loss of an open position."""
        mt5 = self._get_mt5()
        request = {
            "action":   mt5.TRADE_ACTION_SLTP,
            "symbol":   position.symbol,
            "position": position.ticket,
            "sl":       new_sl,
            "tp":       position.tp,
        }
        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            err = mt5.last_error() if result is None else result.comment
            log.warning(f"Modify SL failed for #{position.ticket}: {err}")
            return False

        log.info(f"SL moved → {new_sl:.5f} for ticket #{position.ticket}")
        return True

    # ── private ───────────────────────────────

    def _send_with_retry(self, mt5, request: dict):
        retries = self.cfg.get('retry_attempts', 3)
        delay   = self.cfg.get('retry_delay_s', 1.0)

        for attempt in range(1, retries + 1):
            result = mt5.order_send(request)
            if result is None:
                log.warning(f"order_send returned None (attempt {attempt})")
                time.sleep(delay)
                continue

            if result.retcode == mt5.TRADE_RETCODE_DONE:
                log.info(
                    f"Order sent OK | ticket={result.order} | "
                    f"price={result.price:.5f} | retcode={result.retcode}"
                )
                return result

            # Requote — refresh price and retry
            if result.retcode == mt5.TRADE_RETCODE_REQUOTE:
                tick = mt5.symbol_info_tick(request['symbol'])
                if tick:
                    request['price'] = (
                        tick.ask if request['type'] in
                        (mt5.ORDER_TYPE_BUY,) else tick.bid
                    )
                log.warning(f"Requote on attempt {attempt} — retrying...")
                time.sleep(0.3)
                continue

            log.error(
                f"Order failed | retcode={result.retcode} | "
                f"comment={result.comment} (attempt {attempt})"
            )
            if attempt < retries:
                time.sleep(delay)

        log.error("All order attempts exhausted.")
        notifier.alert_error("order_executor", f"Order failed after {retries} attempts")
        return None

    def _get_mt5(self):
        from mt5.connector import get_connector
        return get_connector().get_mt5()
