"""
Postgres test fixtures — one fresh schema per test session.

How to read this file:

  1. The session-scoped fixture provisions a temp database name,
     creates it from the DATABASE_URL host/port/user, runs the
     migration runner (via sys.executable so the venv python is always
     used) against it, and teardown drops the database.
  2. The function-scoped fixture truncates every table before each
     test so cross-test state cannot leak.
  3. tests/conftest.py imports * from this module so fixtures are
     auto-discovered.

To run DB tests locally:
  export DATABASE_URL='postgresql://Dhruv@localhost:5432/lila_dev'
  pytest tests/test_archive_pg.py
"""

import os
import secrets
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import psycopg
import pytest

from services import db as _db


__all__ = ["_test_database", "db", "seed_user"]

_BASE_DSN = os.environ.get("DATABASE_URL")


def _admin_dsn(dsn: str) -> str:
    parsed = urlparse(dsn)
    return urlunparse(parsed._replace(path="/postgres"))


@pytest.fixture(scope="session")
def _test_database():
    if not _BASE_DSN:
        pytest.skip("DATABASE_URL not set; skipping DB tests")
    db_name = f"lila_test_{secrets.token_hex(4)}"
    admin = _admin_dsn(_BASE_DSN)
    with psycopg.connect(admin, autocommit=True) as c:
        # CREATE DATABASE cannot be parameterized (identifier, not value).
        # db_name is secrets.token_hex(4)-derived so injection risk is zero.
        c.execute(f'CREATE DATABASE "{db_name}"')

    parsed = urlparse(_BASE_DSN)
    test_dsn = urlunparse(parsed._replace(path=f"/{db_name}"))
    os.environ["DATABASE_URL"] = test_dsn
    _db.reset_pool_for_tests()

    here = Path(__file__).resolve().parent.parent / "migrations"
    subprocess.run([sys.executable, "run.py"], cwd=here, check=True,
                   env={**os.environ, "DATABASE_URL": test_dsn})

    yield test_dsn

    _db.reset_pool_for_tests()
    os.environ["DATABASE_URL"] = _BASE_DSN
    with psycopg.connect(admin, autocommit=True) as c:
        c.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s",
            (db_name,),
        )
        # DROP DATABASE cannot be parameterized (identifier, not value).
        # db_name is secrets.token_hex(4)-derived so injection risk is zero.
        c.execute(f'DROP DATABASE "{db_name}"')


@pytest.fixture
def db(_test_database):
    """
    1. Open a pooled connection.
    2. TRUNCATE every domain table with RESTART IDENTITY CASCADE so
       sequences reset and FK chains clear in one statement.
    3. Yield to the test; the connection commits on exit.
    """
    with _db.connect() as conn:
        conn.execute("""TRUNCATE
            last_action, conversation_turns, jobs, events, users
            RESTART IDENTITY CASCADE""")
    yield


@pytest.fixture
def seed_user(db):
    """
    1. Build a closure so callers can pass an optional user_id.
    2. INSERT a row with a placeholder password hash (no real hashing needed in tests).
    3. Return the user_id so the caller can use it in subsequent queries.
    """
    def _make(user_id: str = "u_test") -> str:
        with _db.connect() as conn:
            conn.execute(
                """INSERT INTO users (user_id, username, password_hash)
                   VALUES (%s, %s, %s)""",
                (user_id, user_id, "argon2id$placeholder"),
            )
        return user_id
    return _make
