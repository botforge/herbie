"""
Postgres connection helpers.

How to read this file:

  1. _pool is a process-wide psycopg ConnectionPool. Initialized
     lazily on first use so unit tests that never touch the DB do
     not require a running Postgres.
  2. _ensure_pool() guards construction with a double-checked lock so
     two threads racing on first use never leak a duplicate pool.
  3. connect() yields a connection from the pool inside a context
     manager. Callers use it as: `with connect() as conn: ...`.
  4. fetch_one / fetch_all are thin helpers that hand back dict rows
     (psycopg's dict_row). Most callers use them directly; complex
     transactions use connect() and run multiple statements.
"""

import os
import threading
from contextlib import contextmanager

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

_pool: ConnectionPool | None = None
_pool_lock = threading.Lock()


def _ensure_pool() -> ConnectionPool:
    """
    1. Fast path: return the pool if it's already built (no lock needed).
    2. Slow path: acquire the lock, then re-check so only one thread
       builds the pool even when two race through the first check.
    3. Open the pool immediately (open=True) to silence the psycopg_pool
       DeprecationWarning about the default changing in 3.3+.
    """
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                dsn = os.environ.get("DATABASE_URL")
                if not dsn:
                    raise RuntimeError("DATABASE_URL is not set")
                _pool = ConnectionPool(
                    dsn, min_size=1, max_size=8, open=True,
                    kwargs={"row_factory": dict_row},
                )
    return _pool


@contextmanager
def connect():
    pool = _ensure_pool()
    with pool.connection() as conn:
        yield conn


def fetch_one(sql: str, params: tuple = ()) -> dict | None:
    with connect() as conn:
        cur = conn.execute(sql, params)
        return cur.fetchone()


def fetch_all(sql: str, params: tuple = ()) -> list[dict]:
    with connect() as conn:
        cur = conn.execute(sql, params)
        return list(cur.fetchall())


def execute(sql: str, params: tuple = ()) -> None:
    # pool.connection() commits automatically on clean exit; no explicit commit needed.
    with connect() as conn:
        conn.execute(sql, params)


def reset_pool_for_tests() -> None:
    """Tests reset the pool when they switch DSNs between files."""
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None
