"""
v2/scheduler.py
===============
APScheduler-based scheduler for AWS EC2 deployment.

Jobs:
  data_and_signals   every 60s  (aligns to 5M candle boundary)
  bias_update        every 1H   (on the hour)
  position_monitor   every 10s  (tracks open trades for BE move)
  daily_reset        00:01 UTC  (resets risk counters)

Run:
    python -m v2.scheduler          # starts all jobs
    python -m v2.scheduler --once   # run one cycle and exit (testing)

Graceful shutdown: CTRL-C or SIGTERM → scheduler stops cleanly.
"""

import argparse
import logging
import signal
import sys
import os

# Ensure project root is on path when run as __main__
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from v2.config_loader import load_config
from v2.utils.logger  import init as init_logger, get_logger
from v2.db.schema     import create_all
from v2.db            import connection as db
from v2.engine        import TradingEngine

log = get_logger("scheduler")

_running = True


def _on_signal(signum, frame):
    global _running
    log.info(f"Shutdown signal received ({signum}) — stopping scheduler...")
    _running = False


def build_scheduler(engine: TradingEngine, cfg: dict):
    """Create and configure APScheduler with all jobs."""
    try:
        from apscheduler.schedulers.blocking import BlockingScheduler
        from apscheduler.triggers.cron       import CronTrigger
        from apscheduler.triggers.interval   import IntervalTrigger
    except ImportError:
        log.critical("APScheduler not installed. Run: pip install apscheduler")
        sys.exit(1)

    sched_cfg = cfg.get("scheduler", {})
    data_interval  = sched_cfg.get("data_fetch_interval_s",  60)
    bias_interval  = sched_cfg.get("bias_update_interval_s", 3600)
    pos_interval   = sched_cfg.get("position_check_s",       10)

    scheduler = BlockingScheduler(timezone="UTC")

    # ── Job 1: Data fetch + signal generation (every 60s) ─────────────────
    scheduler.add_job(
        engine.run_cycle,
        trigger=IntervalTrigger(seconds=data_interval),
        id="data_signals",
        name="Data Fetch + Signal Scan",
        max_instances=1,
        coalesce=True,        # skip if previous run still executing
        misfire_grace_time=30,
    )
    log.info(f"Job scheduled: data_signals every {data_interval}s")

    # ── Job 2: Position Management (every 10s) ───────────────────────────
    scheduler.add_job(
        engine._exec_mgr.manage_positions,
        trigger=IntervalTrigger(seconds=pos_interval),
        id="position_monitor",
        name="Position Monitor",
        max_instances=1,
        coalesce=True,
    )
    log.info(f"Job scheduled: position_monitor every {pos_interval}s")

    # ── Job 3: HTF bias update (every 1H on the hour) ─────────────────────
    scheduler.add_job(
        engine.run_bias_update,
        trigger=CronTrigger(minute=2),   # 2 minutes past each hour
        id="bias_update",
        name="HTF Bias Update",
        max_instances=1,
        coalesce=True,
    )
    log.info("Job scheduled: bias_update at HH:02 UTC")

    # ── Job 3: Daily reset at 00:01 UTC ───────────────────────────────────
    scheduler.add_job(
        _daily_reset,
        trigger=CronTrigger(hour=0, minute=1),
        id="daily_reset",
        name="Daily Risk Reset",
        args=[engine],
    )
    log.info("Job scheduled: daily_reset at 00:01 UTC")

    return scheduler


def _daily_reset(engine: TradingEngine):
    """Reset daily risk counters. Called at 00:01 UTC."""
    log.info("Daily reset triggered")
    # engine._risk_mgr.on_new_day(balance)  ← fill with live balance
    engine._risk_mgr.on_new_day(0.0)


def main():
    global _running

    parser = argparse.ArgumentParser(description="OB Trading System v2 — APScheduler")
    parser.add_argument("--once",    action="store_true", help="Run one cycle then exit")
    parser.add_argument("--config",  default="v2/config.yaml", help="Path to config.yaml")
    parser.add_argument("--dry-run", action="store_true", help="Override dry_run=true")
    parser.add_argument("--close-all", action="store_true", help="Emergency: close all open positions and exit")
    args = parser.parse_args()

    # ── Load config ───────────────────────────────────────────────────────
    cfg = load_config(args.config)
    if args.dry_run:
        cfg["system"]["dry_run"] = True

    # ── Initialise logger ─────────────────────────────────────────────────
    init_logger(cfg.get("logging", {}))
    log = get_logger("scheduler")

    # ── Initialise database ───────────────────────────────────────────────
    db.init(cfg["database"])
    with db.get() as conn:
        create_all(conn)

    # ── Build engine ──────────────────────────────────────────────────────
    engine = TradingEngine(cfg)
    engine.startup_report()

    # ── Signal handlers ───────────────────────────────────────────────────
    signal.signal(signal.SIGINT,  _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    # ── Handle --close-all ────────────────────────────────────────────────
    if args.close_all:
        log.info("--close-all flag set. Connecting and closing all positions...")
        engine._exec_mgr.connect()
        # Ensure we have active MT5 connection
        if engine._exec_mgr.use_mt5 and engine._exec_mgr.connector.is_alive():
            closed = 0
            for pm in engine._exec_mgr.pos_managers.values():
                closed += pm.close_all(engine._exec_mgr.executor)
            log.info(f"Closed {closed} positions. Exiting.")
        else:
            log.warning("MT5 not connected or not configured as data source. Nothing to close.")
        engine.shutdown()
        sys.exit(0)

    # ── Run one cycle and exit (for testing / Lambda simulation) ──────────
    if args.once:
        log.info("--once flag: running single cycle")
        engine.run_bias_update()
        engine.run_cycle()
        engine.shutdown()
        sys.exit(0)

    # ── Start APScheduler ─────────────────────────────────────────────────
    scheduler = build_scheduler(engine, cfg)
    log.info("Starting APScheduler (blocking) — press CTRL-C to stop")

    try:
        # Run an immediate bias update before scheduler starts
        engine.run_bias_update()
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)
        engine.shutdown()
        log.info("Scheduler stopped.")


if __name__ == "__main__":
    main()
