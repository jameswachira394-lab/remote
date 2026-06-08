"""
v2/lambda_handler.py
====================
AWS Lambda entry point.

Designed to be triggered by AWS EventBridge (CloudWatch Events).
- Trigger 1: rate(5 minutes)   → event["action"] = "cycle"
- Trigger 2: rate(1 hour)      → event["action"] = "bias_update"
- Trigger 3: cron(0 0 * * ? *) → event["action"] = "daily_reset"

Deployment:
  1. Package code + dependencies (pandas, ccxt, pyyaml, psycopg2-binary) into a zip.
  2. Upload to AWS Lambda.
  3. Set Handler to `v2.lambda_handler.lambda_handler`.
  4. Configure EventBridge triggers passing the JSON payloads below.

Event payload examples:
  {"action": "cycle"}
  {"action": "bias_update"}
  {"action": "daily_reset"}
"""

import os
import json
import logging
from typing import Any, Dict

from v2.config_loader import load_config
from v2.utils.logger  import init as init_logger, get_logger
from v2.db.schema     import create_all
from v2.db            import connection as db
from v2.engine        import TradingEngine

# Initialisation code runs on Lambda cold start.
# Global variables are retained between invocations (warm start).
_engine = None
_initialised = False


def _init_system():
    """Load config, init DB, and instantiate TradingEngine once."""
    global _engine, _initialised
    
    if _initialised:
        return

    # Lambda environment: write access is restricted to /tmp
    # Override config paths if running in Lambda
    config_path = os.environ.get("OB_CONFIG_PATH", "v2/config.yaml")
    cfg = load_config(config_path)

    if os.environ.get("AWS_EXECUTION_ENV"):
        cfg["database"]["sqlite"]["path"] = "/tmp/ob_trading.db"
        cfg["logging"]["dir"] = "/tmp/logs"
        cfg["paths"]["signal_dir"] = "/tmp/signals"
        # Force SQLite in Lambda if PostgreSQL is not used, since /tmp is ephemeral
        if cfg["database"]["engine"] == "sqlite":
            logging.getLogger().warning("Using SQLite in AWS Lambda /tmp. Data will be lost on cold start! Use PostgreSQL for persistence.")

    init_logger(cfg.get("logging", {}))
    log = get_logger("lambda")

    db.init(cfg["database"])
    with db.get() as conn:
        create_all(conn)

    _engine = TradingEngine(cfg)
    _initialised = True
    log.info("Lambda cold start initialisation complete.")


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    AWS Lambda handler function.
    """
    _init_system()
    log = get_logger("lambda")
    
    action = event.get("action", "cycle")
    log.info(f"Lambda invoked with action: {action}")

    try:
        if action == "cycle":
            _engine.run_cycle()
            return {"statusCode": 200, "body": "Cycle completed successfully"}
            
        elif action == "bias_update":
            _engine.run_bias_update()
            return {"statusCode": 200, "body": "Bias update completed successfully"}
            
        elif action == "daily_reset":
            # Pass 0.0 as balance for now, unless balance fetch is implemented
            _engine._risk_mgr.on_new_day(0.0)
            return {"statusCode": 200, "body": "Daily reset completed successfully"}
            
        else:
            log.warning(f"Unknown action received: {action}")
            return {"statusCode": 400, "body": f"Unknown action: {action}"}

    except Exception as e:
        log.exception(f"Error during lambda execution: {e}")
        return {"statusCode": 500, "body": f"Error: {str(e)}"}
