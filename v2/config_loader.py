"""
v2/config_loader.py
===================
YAML configuration loader with environment variable overrides.
"""

import os
import yaml
import logging

log = logging.getLogger("config")


def load_config(path: str) -> dict:
    """Load YAML config and apply environment variable overrides."""
    if not os.path.exists(path):
        raise FileNotFoundError(f"Config file not found: {path}")

    with open(path, "r", encoding="utf-8") as fh:
        try:
            cfg = yaml.safe_load(fh)
        except yaml.YAMLError as e:
            raise RuntimeError(f"Failed to parse config YAML: {e}")

    _apply_env_overrides(cfg)
    return cfg


def _apply_env_overrides(cfg: dict):
    """
    Overrides config values with environment variables if they exist.
    Format: OB_<SECTION>_<KEY>  (e.g., OB_CCXT_API_KEY)
    """
    prefix = "OB_"

    # ccxt overrides
    if "ccxt" in cfg:
        if os.environ.get(f"{prefix}CCXT_API_KEY"):
            cfg["ccxt"]["api_key"] = os.environ[f"{prefix}CCXT_API_KEY"]
        if os.environ.get(f"{prefix}CCXT_API_SECRET"):
            cfg["ccxt"]["api_secret"] = os.environ[f"{prefix}CCXT_API_SECRET"]

    # db overrides
    if "database" in cfg and "postgresql" in cfg["database"]:
        pg = cfg["database"]["postgresql"]
        if os.environ.get(f"{prefix}DB_HOST"):
            pg["host"] = os.environ[f"{prefix}DB_HOST"]
        if os.environ.get(f"{prefix}DB_USER"):
            pg["user"] = os.environ[f"{prefix}DB_USER"]
        if os.environ.get(f"{prefix}DB_PASSWORD"):
            pg["password"] = os.environ[f"{prefix}DB_PASSWORD"]
        if os.environ.get(f"{prefix}DB_NAME"):
            pg["dbname"] = os.environ[f"{prefix}DB_NAME"]

    # alerts overrides
    if "alerts" in cfg:
        if "telegram" in cfg["alerts"]:
            if os.environ.get(f"{prefix}TG_TOKEN"):
                cfg["alerts"]["telegram"]["token"] = os.environ[f"{prefix}TG_TOKEN"]
            if os.environ.get(f"{prefix}TG_CHAT_ID"):
                cfg["alerts"]["telegram"]["chat_id"] = os.environ[f"{prefix}TG_CHAT_ID"]

        if "aws_sns" in cfg["alerts"]:
            if os.environ.get(f"{prefix}SNS_TOPIC_ARN"):
                cfg["alerts"]["aws_sns"]["topic_arn"] = os.environ[f"{prefix}SNS_TOPIC_ARN"]
