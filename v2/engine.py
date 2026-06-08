"""
v2/engine.py
============
Core Trading Engine

Orchestrates one full analysis cycle for all configured pairs:
  Step 1  Fetch OHLCV candles (5M, 15M, 1H) via DataFeed
  Step 2  Update HTF bias (1H) via BiasEngine
  Step 3  Run structure analysis (15M) via StructureAnalyser
  Step 4  Detect/update Order Blocks (5M) via OrderBlockDetector
  Step 5  Generate signals via SignalGenerator
  Step 6  Route signals to AlertManager
  Step 7  Apply risk gates before each signal is logged as actionable
  Step 8  Log cycle summary

Used by both:
  - scheduler.py  (APScheduler on EC2 — calls engine.run_cycle())
  - lambda_handler.py  (AWS Lambda — calls engine.run_cycle() once per invocation)
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from v2.data_feed     import DataFeed
from v2.structure     import StructureAnalyser
from v2.ob_detector   import OrderBlockDetector
from v2.bias          import BiasEngine
from v2.signals       import SignalGenerator
from v2.alerts        import AlertManager
from v2.risk_manager  import RiskManager
from v2.execution.manager import ExecutionManager
from v2.db import connection as db
from v2.db import schema as db_schema

log = logging.getLogger("engine")


class TradingEngine:
    """
    Stateful engine shared across scheduler cycles.
    All heavy objects (adapters, DB connections) are created once.

    Usage:
        engine = TradingEngine(cfg)
        engine.run_cycle()          # called every 5M by scheduler
        engine.run_bias_update()    # called every 1H by scheduler
    """

    def __init__(self, cfg: dict):
        self._cfg      = cfg
        self._dry_run  = cfg.get("system", {}).get("dry_run", True)
        self._pairs    = cfg.get("pairs", [])
        self._pair_syms= [p["symbol"] for p in self._pairs]
        self._bars_cfg = cfg.get("bars", {})

        # Initialise database first (must happen before any component uses DB)
        db.init(cfg.get("database", {}))
        with db.get() as conn:
            db_schema.create_all(conn)

        # Component initialisation
        self._feed      = DataFeed(cfg)
        self._structure = StructureAnalyser(cfg.get("structure", {}))
        self._ob_det    = OrderBlockDetector(cfg.get("ob", {}))
        self._bias_eng  = BiasEngine(cfg.get("bias", {}))
        self._sig_gen   = SignalGenerator(
            cfg.get("signal", {}),
            cfg.get("risk",   {}),
            self._pairs,
        )
        self._alerter   = AlertManager(
            cfg.get("alerts", {}),
            cfg.get("paths",  {}),
        )
        self._risk_mgr  = RiskManager(cfg.get("risk", {}), self._pairs)
        self._exec_mgr  = ExecutionManager(cfg)

        self._cycle_count = 0
        log.info(
            f"TradingEngine initialised | "
            f"pairs={self._pair_syms} | dry_run={self._dry_run}"
        )

    # ── public ────────────────────────────────────────────────────────────────

    def run_cycle(self):
        """
        Full 5M analysis cycle.
        Safe to call on Lambda cold-start or APScheduler tick.
        """
        self._cycle_count += 1
        t_start = datetime.now(timezone.utc)
        log.info(f"──── Cycle #{self._cycle_count} start @ {t_start.strftime('%H:%M:%S UTC')} ────")

        total_signals = 0

        for pair_cfg in self._pairs:
            pair = pair_cfg["symbol"]
            try:
                signals = self._process_pair(pair)
                total_signals += len(signals)
            except Exception as e:
                log.exception(f"[{pair}] Unhandled error in cycle: {e}")
                self._alerter.send_error(f"cycle/{pair}", str(e))

        elapsed = (datetime.now(timezone.utc) - t_start).total_seconds()
        log.info(
            f"---- Cycle #{self._cycle_count} done | "
            f"signals={total_signals} | elapsed={elapsed:.1f}s ----"
        )

    def run_bias_update(self):
        """
        Hourly bias refresh cycle.
        Fetches 1H candles and recalculates HTF bias for all pairs.
        """
        log.info("Bias update cycle starting...")
        for pair_cfg in self._pairs:
            pair = pair_cfg["symbol"]
            try:
                df1h = self._feed.get(
                    pair, "1h",
                    limit=self._bars_cfg.get("htf_bias", 500),
                    with_indicators=False,
                )
                if df1h.empty:
                    log.warning(f"[{pair}] No 1H data for bias update")
                    continue
                self._bias_eng.update(df1h, pair, "1h")
            except Exception as e:
                log.exception(f"[{pair}] Bias update error: {e}")

    def startup_report(self):
        """Print system status and send startup alert."""
        log.info("=" * 60)
        log.info("  OB Trading System v2 — Startup")
        log.info(f"  Pairs    : {', '.join(self._pair_syms)}")
        log.info(f"  Source   : {self._cfg.get('data_source','').upper()}")
        log.info(f"  Dry Run  : {self._dry_run}")
        log.info(f"  Mode     : {self._cfg.get('scheduler',{}).get('mode','apscheduler').upper()}")
        log.info("=" * 60)
        self._alerter.send_startup(
            pairs   = self._pair_syms,
            dry_run = self._dry_run,
            source  = self._cfg.get("data_source", "ccxt"),
        )
        self._exec_mgr.connect()

    def shutdown(self):
        """Graceful shutdown — called on SIGINT/SIGTERM."""
        log.info("TradingEngine shutting down...")
        self._alerter.send_shutdown()
        self._exec_mgr.disconnect()

    # ── private: per-pair cycle ───────────────────────────────────────────────

    def _process_pair(self, pair: str) -> list:
        """Run the full pipeline for one pair. Returns generated signals."""

        # ── Step 1: Fetch candles ─────────────────────────────────────────
        df5m = self._feed.get(
            pair, "5m",
            limit=self._bars_cfg.get("entry", 1500),
        )
        df15m = self._feed.get(
            pair, "15m",
            limit=self._bars_cfg.get("structure", 800),
        )
        if df5m.empty:
            log.warning(f"[{pair}] No 5M data — skipping")
            return []

        # ── Step 2: HTF bias (from cache — updated separately every 1H) ──
        bias = self._bias_eng.get(pair)
        log.debug(f"[{pair}] HTF bias = {bias}")

        # ── Step 3: Structure analysis (15M) ──────────────────────────────
        if not df15m.empty:
            self._structure.analyse(df15m, pair, "15m")

        # ── Step 4: Order block detection (5M) ───────────────────────────
        self._ob_det.detect(df5m, pair, "5m")
        fresh_obs = self._ob_det.get_fresh_aligned(pair, "5m", bias)

        log.debug(f"[{pair}] Fresh OBs aligned with {bias}: {len(fresh_obs)}")

        if not fresh_obs:
            return []

        # ── Step 5: Generate signals ──────────────────────────────────────
        raw_signals = self._sig_gen.evaluate(
            df5m   = df5m,
            df15m  = df15m,
            pair   = pair,
            timeframe = "5m",
            fresh_obs = fresh_obs,
            bias   = bias,
        )

        # ── Step 6 & 7: Risk gates + alert ───────────────────────────────
        actionable = []
        for sig in raw_signals:
            if not self._risk_gates_ok():
                log.warning(f"[{pair}] Signal blocked by risk gates")
                continue
            actionable.append(sig)
            self._alerter.send_signal(sig)

            if self._dry_run:
                log.info(f"[{pair}] [DRY RUN] Signal logged — no order sent")
            else:
                # Live execution
                balance = 0.0 # Fallback
                if self._exec_mgr.use_mt5 and self._exec_mgr.connector.is_alive():
                    acct = self._exec_mgr.connector.account_info()
                    if acct:
                        balance = acct.get("balance", 0.0)

                # Calculate lot size
                lots = self._risk_mgr.calc_lot_size(pair, balance, sig.risk_pips)
                self._exec_mgr.execute_signal(sig, volume=lots)

        return actionable

    def _risk_gates_ok(self) -> bool:
        """Check shared risk gates (daily loss, max trades)."""
        current_balance = 0.0
        if self._exec_mgr.use_mt5 and self._exec_mgr.connector.is_alive():
            acct = self._exec_mgr.connector.account_info()
            if acct:
                current_balance = acct.get("balance", 0.0)
                
        open_count = self._exec_mgr.get_open_count()
        
        daily_ok = self._risk_mgr.check_daily_loss(current_balance)
        trades_ok = self._risk_mgr.check_max_trades(open_count)
        return daily_ok and trades_ok
