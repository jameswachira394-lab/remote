"""
v2/db/schema.py
===============
Database schema manager.
Creates all tables for SQLite or PostgreSQL.
All timestamps are stored as UTC ISO-8601 strings.

Tables:
  candles           — OHLCV data per pair/timeframe
  structure_events  — BOS / CHoCH detected events
  order_blocks      — Detected and tracked OBs
  signals           — Generated trade signals
  bias              — HTF bias state per pair
"""

import os
import logging

log = logging.getLogger("db.schema")

# ── DDL ───────────────────────────────────────────────────────────

CANDLES_DDL = """
CREATE TABLE IF NOT EXISTS candles (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    pair        TEXT    NOT NULL,
    timeframe   TEXT    NOT NULL,
    timestamp   TEXT    NOT NULL,
    open        REAL    NOT NULL,
    high        REAL    NOT NULL,
    low         REAL    NOT NULL,
    close       REAL    NOT NULL,
    volume      REAL    NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(pair, timeframe, timestamp)
);
CREATE INDEX IF NOT EXISTS idx_candles_pair_tf
    ON candles(pair, timeframe, timestamp DESC);
"""

STRUCTURE_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS structure_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    pair        TEXT    NOT NULL,
    timeframe   TEXT    NOT NULL,
    event_type  TEXT    NOT NULL,  -- 'SWING_HIGH'|'SWING_LOW'|'BOS_BULL'|'BOS_BEAR'|'CHOCH_BULL'|'CHOCH_BEAR'
    price       REAL    NOT NULL,
    timestamp   TEXT    NOT NULL,
    bar_index   INTEGER,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(pair, timeframe, event_type, timestamp)
);
CREATE INDEX IF NOT EXISTS idx_struct_pair_tf
    ON structure_events(pair, timeframe, timestamp DESC);
"""

ORDER_BLOCKS_DDL = """
CREATE TABLE IF NOT EXISTS order_blocks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    pair            TEXT    NOT NULL,
    timeframe       TEXT    NOT NULL,
    ob_type         TEXT    NOT NULL,  -- 'BULL' | 'BEAR'
    high            REAL    NOT NULL,
    low             REAL    NOT NULL,
    open            REAL    NOT NULL,
    close           REAL    NOT NULL,
    timestamp       TEXT    NOT NULL,  -- candle timestamp of the OB candle
    formation_ts    TEXT    NOT NULL,  -- timestamp when BOS confirmed this OB
    status          TEXT    NOT NULL DEFAULT 'fresh',  -- fresh|tested|invalid
    tap_count       INTEGER NOT NULL DEFAULT 0,
    bos_event_id    INTEGER,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(pair, timeframe, ob_type, timestamp)
);
CREATE INDEX IF NOT EXISTS idx_ob_pair_status
    ON order_blocks(pair, status, timeframe);
"""

SIGNALS_DDL = """
CREATE TABLE IF NOT EXISTS signals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    pair        TEXT    NOT NULL,
    timeframe   TEXT    NOT NULL,
    signal_type TEXT    NOT NULL,  -- 'BUY' | 'SELL'
    entry       REAL    NOT NULL,
    sl          REAL    NOT NULL,
    tp1         REAL    NOT NULL,
    tp2         REAL    NOT NULL,
    risk_pips   REAL    NOT NULL,
    rr          REAL    NOT NULL,
    ob_id       INTEGER,
    bias        TEXT    NOT NULL,  -- 'BULLISH'|'BEARISH'
    session     TEXT    NOT NULL,  -- 'LONDON'|'NEW_YORK'|'OTHER'
    status      TEXT    NOT NULL DEFAULT 'new',  -- new|sent|executed|expired
    timestamp   TEXT    NOT NULL,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_signals_pair
    ON signals(pair, status, timestamp DESC);
"""

BIAS_DDL = """
CREATE TABLE IF NOT EXISTS bias (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    pair        TEXT    NOT NULL,
    timeframe   TEXT    NOT NULL DEFAULT '1h',
    bias        TEXT    NOT NULL,  -- 'BULLISH'|'BEARISH'|'RANGING'
    timestamp   TEXT    NOT NULL,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(pair, timestamp)
);
CREATE INDEX IF NOT EXISTS idx_bias_pair
    ON bias(pair, timestamp DESC);
"""

ALL_DDL = [CANDLES_DDL, STRUCTURE_EVENTS_DDL, ORDER_BLOCKS_DDL, SIGNALS_DDL, BIAS_DDL]


def create_all(conn):
    """Create all tables. Works for both SQLite and PostgreSQL connections."""
    cur = conn.cursor()
    for ddl in ALL_DDL:
        # PostgreSQL uses SERIAL PRIMARY KEY and doesn't support AUTOINCREMENT
        ddl_exec = ddl
        try:
            db_type = conn.__class__.__module__
            if "psycopg2" in db_type:
                ddl_exec = (
                    ddl
                    .replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
                    .replace("datetime('now')", "NOW()")
                )
        except Exception:
            pass
        cur.executescript(ddl_exec) if hasattr(cur, "executescript") else [
            cur.execute(stmt.strip()) for stmt in ddl_exec.split(";") if stmt.strip()
        ]
    conn.commit()
    log.info("All database tables created/verified.")
