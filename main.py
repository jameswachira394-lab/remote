"""
main.py
=======
Institutional Order Block Live Trading System
=============================================
Entry point. Run this file to start the bot.

Usage:
    python main.py              # live trading
    python main.py --dry-run    # signal detection only, no orders placed

Architecture per cycle (every LOOP.cycle_seconds):
    1. Health-check MT5 connection
    2. Fetch latest OHLCV bars + indicators
    3. Detect Order Blocks (full scan, no lookahead)
    4. Evaluate signals on last completed bar
    5. Risk gates (max open trades, daily loss limit)
    6. Execute order if signal passes all gates
    7. Monitor open positions (TP1 → breakeven move)
    8. Log status summary
    9. Sleep until next cycle
"""

import sys
import time
import signal as _signal
import argparse
from datetime import datetime, date

# ── stdlib path bootstrap (ensures imports work from project root) ─────────
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.settings import (
    MT5, SYMBOL, TIMEFRAME, BARS,
    OB, SIGNAL, RISK, FILTERS, EXEC, LOOP,
)
from core.data_feed     import DataFeed
from core.ob_detector   import OrderBlockDetector
from core.signal_engine import SignalEngine
from core.risk_manager  import RiskManager
from mt5.connector      import get_connector
from mt5.order_executor import OrderExecutor
from mt5.position_manager import PositionManager
from utils.logger       import get_logger
from utils.chart_exporter import save_signal_chart
from utils import notifier

log = get_logger("main")

# ── graceful shutdown ──────────────────────────────────────────────────────
_running = True

def _on_signal(signum, frame):
    global _running
    log.info(f"Shutdown signal received ({signum}). Stopping after current cycle...")
    _running = False

_signal.signal(_signal.SIGINT,  _on_signal)
_signal.signal(_signal.SIGTERM, _on_signal)


# ══════════════════════════════════════════════════════════════════════════
# MAIN BOT CLASS
# ══════════════════════════════════════════════════════════════════════════

class OBTradingBot:

    def __init__(self, dry_run: bool = False):
        self.dry_run  = dry_run
        self.symbol   = SYMBOL
        self.pip_size = EXEC.get('pip_size', 0.0001)

        # Components
        self.connector   = get_connector()
        self.data_feed   = DataFeed(SYMBOL, TIMEFRAME, BARS, FILTERS)
        self.ob_detector = OrderBlockDetector(OB)
        self.sig_engine  = SignalEngine(SIGNAL, FILTERS, RISK, self.pip_size)
        self.risk_mgr    = RiskManager(RISK, self.pip_size)
        self.executor    = OrderExecutor(EXEC)
        self.pos_mgr     = PositionManager(EXEC, RISK, self.pip_size)

        # State
        self._last_signal_bar: int | None = None   # avoid duplicate signals
        self._current_day: date | None    = None

    # ── public ────────────────────────────────

    def start(self):
        log.info("=" * 60)
        log.info(f"  OB Trading Bot starting | symbol={self.symbol}")
        log.info(f"  Timeframe={TIMEFRAME} | DryRun={self.dry_run}")
        log.info("=" * 60)

        # Connect to MT5
        if not self.connector.connect():
            log.critical("Cannot connect to MT5. Exiting.")
            sys.exit(1)

        notifier.send(
            f"🟢 <b>OB Bot started</b>\n"
            f"Symbol: {self.symbol} | TF: {TIMEFRAME} | "
            f"{'DRY RUN' if self.dry_run else 'LIVE'}"
        )

        # Fetch symbol info once (contract size, pip value, etc.)
        try:
            self._sym_info = self.data_feed.get_symbol_info()
            log.info(
                f"Symbol info | digits={self._sym_info['digits']} | "
                f"contract={self._sym_info['trade_contract_size']} | "
                f"spread={self._sym_info['spread']} pts"
            )
        except Exception as e:
            log.critical(f"Cannot fetch symbol info: {e}")
            self.connector.disconnect()
            sys.exit(1)

        self._run_loop()

    def stop(self):
        log.info("Stopping bot...")
        notifier.send("🔴 <b>OB Bot stopped.</b>")
        self.connector.disconnect()

    # ── private: main loop ────────────────────

    def _run_loop(self):
        cycle          = 0
        last_pos_check = 0.0

        while _running:
            cycle += 1
            t_start = time.monotonic()

            try:
                self._cycle(cycle)
            except Exception as e:
                log.exception(f"Unhandled error in cycle {cycle}: {e}")
                notifier.alert_error("main_loop", str(e))
                # brief back-off on error
                time.sleep(5)

            # Position monitoring (faster than main cycle)
            now = time.monotonic()
            if now - last_pos_check >= LOOP.get('position_check_s', 10):
                try:
                    self._monitor_positions()
                except Exception as e:
                    log.warning(f"Position monitor error: {e}")
                last_pos_check = time.monotonic()

            elapsed = time.monotonic() - t_start
            sleep_s = max(0.0, LOOP['cycle_seconds'] - elapsed)
            log.debug(f"Cycle {cycle} done in {elapsed:.1f}s — sleeping {sleep_s:.1f}s")

            # Interruptible sleep
            deadline = time.monotonic() + sleep_s
            while _running and time.monotonic() < deadline:
                time.sleep(0.5)

        self.stop()

    def _cycle(self, cycle: int):
        # ── 0. Day reset ──────────────────────────────────────
        today = date.today()
        if today != self._current_day:
            acct = self.connector.account_info()
            self.risk_mgr.reset_day(acct.get('balance', 0.0))
            self._current_day = today

        # ── 1. Health check ───────────────────────────────────
        if not self.connector.is_alive():
            log.warning("MT5 connection lost — reconnecting...")
            if not self.connector.reconnect():
                log.error("Reconnect failed. Skipping cycle.")
                return

        # ── 2. Fetch data ─────────────────────────────────────
        df = self.data_feed.fetch()

        # ── 3. Detect Order Blocks ────────────────────────────
        obs = self.ob_detector.detect(df)
        active_obs = [o for o in obs if o['active']]

        # ── 4. Evaluate signal ────────────────────────────────
        signal = self.sig_engine.evaluate(df, active_obs, self.symbol)

        if signal:
            # De-duplicate: skip if same bar fired last cycle
            if signal['bar_index'] == self._last_signal_bar:
                log.debug("Signal already processed for this bar — skipping.")
                signal = None

        # ── 5. Risk gates ─────────────────────────────────────
        if signal:
            acct = self.connector.account_info()
            balance = acct.get('balance', 0.0)

            # Gate 1: daily loss limit
            if not self.risk_mgr.check_daily_loss(balance):
                log.warning("Daily loss limit — signal blocked.")
                signal = None

            # Gate 2: max concurrent trades
            if signal and self.pos_mgr.open_count() >= RISK.get('max_open_trades', 2):
                log.info(
                    f"Max open trades ({RISK['max_open_trades']}) reached — signal blocked."
                )
                signal = None

        # ── 6. Execute ────────────────────────────────────────
        if signal:
            self._last_signal_bar = signal['bar_index']

            # Lot size
            acct    = self.connector.account_info()
            balance = acct.get('balance', 0.0)
            lots    = self.risk_mgr.calc_lot_size(
                balance, signal['risk_pips'], self._sym_info
            )

            log.info(
                f"EXECUTING | {signal['type']} {self.symbol} | "
                f"Entry≈{signal['entry']:.5f} | SL={signal['sl']:.5f} | "
                f"TP1={signal['tp1']:.5f} | TP2={signal['tp2']:.5f} | "
                f"Lots={lots:.2f} | Risk={signal['risk_pips']:.1f}pips"
            )

            # Save chart snapshot
            try:
                ob_for_chart = {
                    'type':      signal['ob_type'],
                    'top':       signal['ob_top'],
                    'bottom':    signal['ob_bottom'],
                    'bar_index': signal['ob_bar'],
                }
                save_signal_chart(df, ob_for_chart, signal)
            except Exception as e:
                log.warning(f"Chart export failed: {e}")

            # Telegram alert (pre-execution)
            notifier.alert_signal(
                self.symbol, signal['type'],
                signal['entry'], signal['sl'],
                signal['tp1'], signal['tp2'],
                signal['risk_pips']
            )

            if self.dry_run:
                log.info("[DRY RUN] Order NOT sent to MT5.")
                # Still mark OB as fired to prevent duplicate signals in dry run
                self.ob_detector.mark_signal_fired(signal['ob_bar'], signal['ob_type'])
            else:
                result = self.executor.execute_market_order(
                    symbol     = self.symbol,
                    direction  = signal['type'],
                    volume     = lots,
                    sl         = signal['sl'],
                    tp         = signal['tp2'],   # MT5 TP = our TP2
                    risk_pips  = signal['risk_pips'],
                    comment    = f"OB_{signal['ob_type'][:4]}",
                )
                if result:
                    # Mark OB as having fired a signal (prevents duplicates)
                    self.ob_detector.mark_signal_fired(signal['ob_bar'], signal['ob_type'])
                    
                    # Register TP1 with position manager for BE move
                    self.pos_mgr.register_trade(result['ticket'], signal['tp1'])
                    log.info(
                        f"Order filled | ticket=#{result['ticket']} | "
                        f"entry={result['entry']:.5f}"
                    )
                    # Initialize trailing stop tracking
                    self.pos_mgr._register_trailing_stop(result['ticket'], result['entry'], signal['type'])
                else:
                    log.error("Order execution failed — see logs above.")
                    notifier.alert_error("order_execution", "Market order failed")

        # ── 7. Status summary (every 10 cycles) ──────────────
        if cycle % 10 == 0:
            self._log_status(df, active_obs)

    def _monitor_positions(self):
        """Called more frequently than the main cycle."""
        try:
            summaries = self.pos_mgr.monitor(self.executor)
        except Exception as e:
            log.error(f"Position monitor exception: {e}", exc_info=True)
            return
        
        if summaries:
            for s in summaries:
                log.debug(
                    f"POS #{s['ticket']} | {s['type']} {s['symbol']} | "
                    f"Entry={s['entry']:.5f} | Current={s['current']:.5f} | "
                    f"Pips={s['pips']:+.1f} | P&L={s['profit']:.2f}"
                )

    def _log_status(self, df, active_obs):
        acct = self.connector.account_info()
        log.info(
            f"STATUS | Balance={acct.get('balance',0):.2f} | "
            f"Equity={acct.get('equity',0):.2f} | "
            f"OpenTrades={self.pos_mgr.open_count()} | "
            f"ActiveOBs={len(active_obs)} | "
            f"LastBar={df.index[-1]}"
        )


# ══════════════════════════════════════════════════════════════════════════
# CLI ENTRY
# ══════════════════════════════════════════════════════════════════════════

def parse_args():
    parser = argparse.ArgumentParser(
        description="Institutional Order Block MT5 Trading Bot"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Run signal detection only — no orders sent to MT5"
    )
    parser.add_argument(
        "--close-all", action="store_true",
        help="Emergency: close all open positions and exit"
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    bot = OBTradingBot(dry_run=args.dry_run)

    if args.close_all:
        log.info("--close-all flag set. Connecting and closing all positions...")
        if bot.connector.connect():
            executor = OrderExecutor(EXEC)
            pos_mgr  = PositionManager(EXEC, RISK)
            closed   = pos_mgr.close_all(executor)
            log.info(f"Closed {closed} positions. Exiting.")
            bot.connector.disconnect()
        sys.exit(0)

    bot.start()
