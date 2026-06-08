"""
v2/db/connection.py
===================
Database connection manager.
Supports SQLite (local/EC2) and PostgreSQL (AWS RDS).
Returns a context-managed connection from a thread-safe pool.
"""

import sqlite3
import threading
import logging
from contextlib import contextmanager

log = logging.getLogger("db.connection")

# Module-level singletons
_sqlite_local = threading.local()   # thread-local for SQLite
_pg_pool       = None               # psycopg2 SimpleConnectionPool
_cfg           = None               # loaded config dict


def init(cfg: dict):
    """
    Call once at startup with the 'database' section of config.yaml.
    Creates directories and verifies connectivity.
    """
    global _cfg
    _cfg = cfg
    engine = cfg.get("engine", "sqlite")

    if engine == "sqlite":
        import os
        db_path = cfg["sqlite"]["path"]
        os.makedirs(os.path.dirname(db_path) if os.path.dirname(db_path) else ".", exist_ok=True)
        # Verify by opening once
        with get() as conn:
            conn.execute("SELECT 1")
        log.info(f"SQLite database initialised -> {db_path}")

    elif engine == "postgresql":
        _init_pg(cfg["postgresql"])
    else:
        raise ValueError(f"Unknown database engine: {engine}")


def _init_pg(pg_cfg: dict):
    """Initialise a psycopg2 connection pool (min 2, max 10)."""
    global _pg_pool
    try:
        from psycopg2 import pool as pg_pool
        _pg_pool = pg_pool.SimpleConnectionPool(
            minconn=2, maxconn=10,
            host=pg_cfg["host"],
            port=pg_cfg.get("port", 5432),
            dbname=pg_cfg["dbname"],
            user=pg_cfg["user"],
            password=pg_cfg["password"],
            sslmode=pg_cfg.get("sslmode", "require"),
        )
        log.info(f"PostgreSQL pool initialised → {pg_cfg['host']}:{pg_cfg.get('port', 5432)}/{pg_cfg['dbname']}")
    except ImportError:
        log.critical("psycopg2 not installed. Run: pip install psycopg2-binary")
        raise


@contextmanager
def get():
    """
    Context manager yielding a database connection.
    Commits on exit, rolls back on exception, always closes/returns.

    Usage:
        with db.get() as conn:
            conn.execute("INSERT INTO ...")
    """
    if _cfg is None:
        raise RuntimeError("db.init() must be called before db.get()")

    engine = _cfg.get("engine", "sqlite")

    if engine == "sqlite":
        conn = _get_sqlite()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        # SQLite thread-local connections stay open — do NOT close here

    elif engine == "postgresql":
        if _pg_pool is None:
            raise RuntimeError("PostgreSQL pool not initialised.")
        conn = _pg_pool.getconn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            _pg_pool.putconn(conn)


def _get_sqlite() -> sqlite3.Connection:
    """Get or create a thread-local SQLite connection."""
    db_path = _cfg["sqlite"]["path"]
    if not hasattr(_sqlite_local, "conn") or _sqlite_local.conn is None:
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        _sqlite_local.conn = conn
    return _sqlite_local.conn


def fetchall(sql: str, params=()) -> list:
    """Convenience: run a SELECT and return list of Row objects."""
    with get() as conn:
        cur = conn.execute(sql, params)
        return cur.fetchall()


def fetchone(sql: str, params=()):
    """Convenience: run a SELECT and return first Row or None."""
    with get() as conn:
        cur = conn.execute(sql, params)
        return cur.fetchone()


def execute(sql: str, params=()):
    """Convenience: run INSERT/UPDATE/DELETE with auto-commit."""
    with get() as conn:
        conn.execute(sql, params)
