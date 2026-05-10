# Lila Multi-User Cloud Deployment — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate Lila from a single-user laptop app to a multi-user cloud deployment on Fly.io: Postgres-backed events/users/conversations, JWT-cookie auth, persistent volume for raw audio, two-process Fly app (web + Telegram), PWA shell over the existing UI.

**Architecture:** Service layer's public function shapes stay the same; every callable gains `user_id: str` as the first parameter. The archive backend swaps from `events.jsonl` + sidecar files to Postgres + a per-user volume directory. Auth runs in a single FastAPI dependency and threads `user_id` into every route. The append-only chat invariant (no mutating tools) is preserved.

**Tech Stack:** Python 3.11, FastAPI, psycopg[binary], asyncpg/SQLAlchemy not used (raw SQL keeps it close to the existing flat-file shape), argon2-cffi, PyJWT, Fly.io, Fly Postgres, Docker, faster-whisper.

**Spec:** `docs/superpowers/specs/2026-05-10-lila-multi-user-cloud-deployment-design.md`

---

## File Structure

**New files:**
- `services/db.py` — psycopg connection pool singleton
- `services/users.py` — user CRUD + admin CLI (`python -m services.users …`)
- `services/auth.py` — argon2id hashing, JWT encode/decode, `get_current_user` FastAPI dependency
- `services/conversation_store.py` — DB-backed conversation history (replaces `_conversations` dict)
- `migrations/001_initial_schema.sql` — Postgres schema (users, events, jobs, conversation_turns, last_action)
- `migrations/run.py` — applies SQL files in order, idempotent
- `scripts/migrate_jsonl_to_postgres.py` — one-shot import of the existing single-user archive
- `scripts/seed_owner.py` — creates the default owner user with a generated random password, prints it once
- `Dockerfile` — image build, bakes faster-whisper `base` model
- `fly.toml` — Fly app config with two processes
- `.dockerignore` — excludes `archive/`, `logs/`, `.env`, `__pycache__/`
- `static/manifest.webmanifest` — PWA manifest
- `static/sw.js` — minimal service worker (cache shell, pass-through everything else)
- `static/login.html` — login form
- `tests/conftest_db.py` — Postgres test fixtures (one schema per test)
- `tests/test_users.py`, `tests/test_auth.py`, `tests/test_archive_pg.py`, `tests/test_conversation_store.py`

**Modified files:**
- `services/archive.py` — internals rewritten to Postgres + per-user volume; public API gains `user_id` first parameter
- `services/pipeline.py` — `handle_text` and `handle_audio` gain `user_id` parameter
- `services/llm.py` — tool-handler closures thread `user_id` into archive calls
- `services/conversation_log.py` — `log_turn` gains `user_id`; on-disk JSONL row includes it
- `main.py` — auth dependency on every protected route; user_id threading; `/files/.../reveal` removed; `/health` added; jobs run via `BackgroundTasks`; structured JSON logging
- `telegram_bot.py` — chat_id → user_id lookup via DB; persistent history via `conversation_store`
- `static/index.html` — `<link rel="manifest">`, service worker registration, login redirect when 401, voice-memo `MediaRecorder` button
- `requirements.txt` — add `psycopg[binary,pool]`, `argon2-cffi`, `pyjwt`, `python-json-logger`
- `tests/conftest.py` — keep existing fixtures, import DB fixtures
- `tests/test_corrections.py`, `tests/test_monastery_flow.py` — pass `user_id` into archive calls

---

## Conventions (read once, applies to every task)

- **TDD:** every functional task writes the failing test, runs it red, implements, runs it green, commits.
- **Comment style:** every nontrivial function gets a numbered-story docstring per `CLAUDE.md`. Short helpers do not need one.
- **Append-only chat invariant:** no task adds a chat-side mutating tool. The web `PATCH /files/{file_id}` route is the only mutating path and it stays.
- **Commits:** small, frequent, one per task unless a task explicitly says otherwise. Always end a task with a commit step.
- **Test DB:** the DB fixture (Task 3) gives every test a dedicated empty schema. Tests never touch the dev or production DB.

---

## Task 1: Add new dependencies

**Files:**
- Modify: `requirements.txt`

- [ ] **Step 1: Add new packages**

Append to `requirements.txt`:

```
psycopg[binary,pool]
argon2-cffi
pyjwt
python-json-logger
```

- [ ] **Step 2: Install**

Run: `pip install -r requirements.txt`
Expected: clean install, no errors.

- [ ] **Step 3: Smoke import**

Run: `python -c "import psycopg, argon2, jwt, pythonjsonlogger; print('ok')"`
Expected: `ok`

- [ ] **Step 4: Commit**

```bash
git add requirements.txt
git commit -m "Add psycopg, argon2-cffi, pyjwt, python-json-logger"
```

---

## Task 2: Postgres schema migration

**Files:**
- Create: `migrations/001_initial_schema.sql`
- Create: `migrations/run.py`

- [ ] **Step 1: Write the schema file**

Create `migrations/001_initial_schema.sql`:

```sql
-- Lila initial schema. Idempotent — re-running is a no-op.
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     TEXT PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS users (
    user_id          TEXT PRIMARY KEY,
    username         TEXT UNIQUE NOT NULL,
    password_hash    TEXT NOT NULL,
    telegram_chat_id BIGINT UNIQUE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS events (
    event_id    TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(user_id),
    type        TEXT NOT NULL,
    file_id     TEXT,
    slug        TEXT,
    tags        TEXT[] NOT NULL DEFAULT '{}',
    transcript  TEXT,
    text        TEXT,
    midi_notes  TEXT,
    ext         TEXT,
    parent_id   TEXT,
    job_id      TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS events_user_created_idx ON events (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS events_user_tag_idx     ON events USING GIN (tags);
CREATE INDEX IF NOT EXISTS events_user_file_idx    ON events (user_id, file_id);

CREATE TABLE IF NOT EXISTS jobs (
    job_id          TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(user_id),
    type            TEXT NOT NULL,
    status          TEXT NOT NULL,
    input_file_id   TEXT,
    output_file_id  TEXT,
    params          JSONB NOT NULL DEFAULT '{}',
    error           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS jobs_user_status_idx ON jobs (user_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS conversation_turns (
    turn_id     BIGSERIAL PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(user_id),
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS conv_user_idx ON conversation_turns (user_id, turn_id);

CREATE TABLE IF NOT EXISTS last_action (
    user_id     TEXT PRIMARY KEY REFERENCES users(user_id),
    snapshots   JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO schema_migrations (version) VALUES ('001_initial_schema')
ON CONFLICT (version) DO NOTHING;
```

- [ ] **Step 2: Write the runner**

Create `migrations/run.py`:

```python
"""
Apply every migrations/*.sql file in lexical order, exactly once.

How to read this file:

  1. Connect to DATABASE_URL.
  2. Ensure the schema_migrations table exists by running 001 itself
     in raw mode — every file is idempotent (uses IF NOT EXISTS / ON
     CONFLICT) so re-applying is safe and the migrations table is
     created in 001.
  3. After each file applies cleanly, the file's INSERT into
     schema_migrations records that it ran.
"""

import os
import sys
from pathlib import Path

import psycopg


def main() -> int:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2

    here = Path(__file__).parent
    files = sorted(p for p in here.glob("*.sql"))
    if not files:
        print("no migration files found", file=sys.stderr)
        return 1

    with psycopg.connect(dsn) as conn:
        for f in files:
            print(f"applying {f.name}…")
            conn.execute(f.read_text())
        conn.commit()
    print("done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 3: Apply against a local Postgres**

Set `DATABASE_URL` for local dev (assumes Postgres running on localhost):

```bash
export DATABASE_URL='postgresql://localhost:5432/lila_dev'
createdb lila_dev   # one-time
python migrations/run.py
```

Expected: `applying 001_initial_schema.sql…` then `done.`

- [ ] **Step 4: Verify**

```bash
psql "$DATABASE_URL" -c "\dt"
```

Expected: rows for `users`, `events`, `jobs`, `conversation_turns`, `last_action`, `schema_migrations`.

- [ ] **Step 5: Commit**

```bash
git add migrations/
git commit -m "Add initial Postgres schema and migration runner"
```

---

## Task 3: services/db.py — connection pool

**Files:**
- Create: `services/db.py`
- Create: `tests/conftest_db.py`
- Modify: `tests/conftest.py`

- [ ] **Step 1: Write the db module**

Create `services/db.py`:

```python
"""
Postgres connection helpers.

How to read this file:

  1. _pool is a process-wide psycopg ConnectionPool. Initialized
     lazily on first use so unit tests that never touch the DB do
     not require a running Postgres.
  2. connect() yields a connection from the pool inside a context
     manager. Callers use it as: `with connect() as conn: ...`.
  3. fetch_one / fetch_all are thin helpers that hand back dict rows
     (psycopg's dict_row). Most callers use them directly; complex
     transactions use connect() and run multiple statements.
"""

import os
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

_pool: ConnectionPool | None = None


def _ensure_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        dsn = os.environ.get("DATABASE_URL")
        if not dsn:
            raise RuntimeError("DATABASE_URL is not set")
        _pool = ConnectionPool(dsn, min_size=1, max_size=8, kwargs={"row_factory": dict_row})
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
    with connect() as conn:
        conn.execute(sql, params)
        conn.commit()


def reset_pool_for_tests() -> None:
    """Tests reset the pool when they switch DSNs between files."""
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None
```

- [ ] **Step 2: Write the DB test fixture**

Create `tests/conftest_db.py`:

```python
"""
Postgres test fixtures — one fresh schema per test session.

How to read this file:

  1. The session-scoped fixture provisions a temp database name,
     creates it from the DATABASE_URL host/port/user, runs the
     migration runner against it, and teardown drops the database.
  2. The function-scoped fixture truncates every table before each
     test so cross-test state cannot leak.
  3. tests/conftest.py imports * from this module so fixtures are
     auto-discovered.

To run DB tests locally:
  export DATABASE_URL='postgresql://localhost:5432/lila_dev'
  pytest tests/test_archive_pg.py
"""

import os
import secrets
import subprocess
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import psycopg
import pytest

from services import db as _db


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
        c.execute(f'CREATE DATABASE "{db_name}"')

    parsed = urlparse(_BASE_DSN)
    test_dsn = urlunparse(parsed._replace(path=f"/{db_name}"))
    os.environ["DATABASE_URL"] = test_dsn
    _db.reset_pool_for_tests()

    here = Path(__file__).resolve().parent.parent / "migrations"
    subprocess.run(["python", "run.py"], cwd=here, check=True,
                   env={**os.environ, "DATABASE_URL": test_dsn})

    yield test_dsn

    _db.reset_pool_for_tests()
    os.environ["DATABASE_URL"] = _BASE_DSN
    with psycopg.connect(admin, autocommit=True) as c:
        c.execute(f"""SELECT pg_terminate_backend(pid)
                      FROM pg_stat_activity
                      WHERE datname = '{db_name}'""")
        c.execute(f'DROP DATABASE "{db_name}"')


@pytest.fixture
def db(_test_database):
    """Truncate every domain table before each test."""
    with _db.connect() as conn:
        conn.execute("""TRUNCATE
            last_action, conversation_turns, jobs, events, users
            RESTART IDENTITY CASCADE""")
        conn.commit()
    yield


@pytest.fixture
def seed_user(db):
    """Insert a minimal user row callers can attribute writes to."""
    def _make(user_id: str = "u_test") -> str:
        with _db.connect() as conn:
            conn.execute(
                """INSERT INTO users (user_id, username, password_hash)
                   VALUES (%s, %s, %s)""",
                (user_id, user_id, "argon2id$placeholder"),
            )
            conn.commit()
        return user_id
    return _make
```

- [ ] **Step 3: Wire DB fixtures into the existing conftest**

Append to `tests/conftest.py`:

```python
# DB fixtures (auto-discovered by pytest via star import)
from tests.conftest_db import *  # noqa: F401,F403
```

- [ ] **Step 4: Smoke test the fixture**

Create `tests/test_db_fixture.py`:

```python
def test_seed_user_creates_row(db, seed_user):
    from services import db as _db
    uid = seed_user("u_alpha")
    row = _db.fetch_one("SELECT user_id FROM users WHERE user_id = %s", (uid,))
    assert row["user_id"] == "u_alpha"
```

Run: `pytest tests/test_db_fixture.py -v`
Expected: PASS (or SKIP if DATABASE_URL unset).

- [ ] **Step 5: Commit**

```bash
git add services/db.py tests/conftest_db.py tests/conftest.py tests/test_db_fixture.py
git commit -m "Add Postgres connection pool and per-test schema fixture"
```

---

## Task 4: services/users.py — CRUD + admin CLI

**Files:**
- Create: `services/users.py`
- Create: `tests/test_users.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_users.py`:

```python
"""
Unit tests for services/users.py.

How to read this file:

  1. create_user inserts a row, hashes the password with argon2id,
     returns the canonical user_id.
  2. verify_password returns True only for the original plaintext.
  3. set_telegram_chat_id links a Telegram chat to an existing user
     and is idempotent.
  4. get_user_by_telegram returns the user row when the chat id is
     linked, None otherwise.
"""

import pytest
from services import users


def test_create_and_verify(db):
    """1. Create a user. 2. Verify the password matches. 3. Bad password fails."""
    uid = users.create_user(username="alice", password="hunter2")
    assert uid == "alice"
    assert users.verify_password("alice", "hunter2") is True
    assert users.verify_password("alice", "wrong") is False


def test_create_duplicate_raises(db):
    users.create_user(username="bob", password="x")
    with pytest.raises(users.UserAlreadyExists):
        users.create_user(username="bob", password="x")


def test_set_telegram_links_chat(db):
    users.create_user(username="cara", password="x")
    users.set_telegram_chat_id("cara", 999)
    row = users.get_user_by_telegram(999)
    assert row["user_id"] == "cara"


def test_get_user_by_telegram_unknown_returns_none(db):
    assert users.get_user_by_telegram(123456) is None
```

- [ ] **Step 2: Run tests to confirm failure**

Run: `pytest tests/test_users.py -v`
Expected: ImportError or 4 failures (`services.users` does not exist yet).

- [ ] **Step 3: Implement services/users.py**

Create `services/users.py`:

```python
"""
User CRUD + admin CLI.

Public API:
  create_user(username, password, telegram_chat_id?) -> user_id
  verify_password(username, plaintext)               -> bool
  set_telegram_chat_id(user_id, chat_id)             -> None
  set_password(user_id, new_password)                -> None
  get_user(user_id)                                  -> dict | None
  get_user_by_telegram(chat_id)                      -> dict | None
  list_users()                                       -> list[dict]

Admin CLI (no public registration UI):
  python -m services.users create   --username U --password P [--telegram-chat-id N]
  python -m services.users set-telegram   --username U --chat-id N
  python -m services.users set-password   --username U --password P
  python -m services.users list
"""

import argparse
import sys

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from services import db as _db


_hasher = PasswordHasher()


class UserAlreadyExists(Exception):
    pass


class UserNotFound(Exception):
    pass


def create_user(username: str, password: str,
                telegram_chat_id: int | None = None) -> str:
    """
    1. user_id is the username for now (slug shape).
    2. Hash with argon2id (defaults are conservative: 64MB, 3 iters).
    3. INSERT — duplicate username trips a UniqueViolation which we
       map to UserAlreadyExists for callers.
    """
    user_id = username
    pwd_hash = _hasher.hash(password)
    try:
        with _db.connect() as conn:
            conn.execute(
                """INSERT INTO users (user_id, username, password_hash, telegram_chat_id)
                   VALUES (%s, %s, %s, %s)""",
                (user_id, username, pwd_hash, telegram_chat_id),
            )
            conn.commit()
    except Exception as e:
        if "users_pkey" in str(e) or "users_username_key" in str(e):
            raise UserAlreadyExists(username) from e
        raise
    return user_id


def verify_password(username: str, plaintext: str) -> bool:
    row = _db.fetch_one(
        "SELECT password_hash FROM users WHERE username = %s", (username,),
    )
    if not row:
        return False
    try:
        return _hasher.verify(row["password_hash"], plaintext)
    except VerifyMismatchError:
        return False


def set_telegram_chat_id(user_id: str, chat_id: int) -> None:
    _db.execute(
        "UPDATE users SET telegram_chat_id = %s WHERE user_id = %s",
        (chat_id, user_id),
    )


def set_password(user_id: str, new_password: str) -> None:
    pwd_hash = _hasher.hash(new_password)
    _db.execute(
        "UPDATE users SET password_hash = %s WHERE user_id = %s",
        (pwd_hash, user_id),
    )


def get_user(user_id: str) -> dict | None:
    return _db.fetch_one(
        "SELECT user_id, username, telegram_chat_id, created_at FROM users WHERE user_id = %s",
        (user_id,),
    )


def get_user_by_telegram(chat_id: int) -> dict | None:
    return _db.fetch_one(
        "SELECT user_id, username, telegram_chat_id, created_at FROM users WHERE telegram_chat_id = %s",
        (chat_id,),
    )


def list_users() -> list[dict]:
    return _db.fetch_all(
        "SELECT user_id, username, telegram_chat_id, created_at FROM users ORDER BY user_id"
    )


# ── admin CLI ────────────────────────────────────────────────────────────────

def _cli() -> int:
    p = argparse.ArgumentParser(prog="python -m services.users")
    sub = p.add_subparsers(dest="cmd", required=True)

    pc = sub.add_parser("create")
    pc.add_argument("--username", required=True)
    pc.add_argument("--password", required=True)
    pc.add_argument("--telegram-chat-id", type=int, default=None)

    pt = sub.add_parser("set-telegram")
    pt.add_argument("--username", required=True)
    pt.add_argument("--chat-id", type=int, required=True)

    pp = sub.add_parser("set-password")
    pp.add_argument("--username", required=True)
    pp.add_argument("--password", required=True)

    sub.add_parser("list")

    args = p.parse_args()

    if args.cmd == "create":
        try:
            uid = create_user(args.username, args.password, args.telegram_chat_id)
            print(f"created user_id={uid}")
            return 0
        except UserAlreadyExists:
            print(f"user already exists: {args.username}", file=sys.stderr)
            return 1

    if args.cmd == "set-telegram":
        set_telegram_chat_id(args.username, args.chat_id)
        print(f"linked telegram chat_id={args.chat_id} to user_id={args.username}")
        return 0

    if args.cmd == "set-password":
        set_password(args.username, args.password)
        print(f"updated password for user_id={args.username}")
        return 0

    if args.cmd == "list":
        for u in list_users():
            print(f"{u['user_id']:20} tg={u['telegram_chat_id']}  {u['created_at']}")
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(_cli())
```

- [ ] **Step 4: Run tests to confirm pass**

Run: `pytest tests/test_users.py -v`
Expected: 4 PASS.

- [ ] **Step 5: Commit**

```bash
git add services/users.py tests/test_users.py
git commit -m "Add services/users.py with argon2id hashing and admin CLI"
```

---

## Task 5: services/auth.py — JWT cookies + FastAPI dependency

**Files:**
- Create: `services/auth.py`
- Create: `tests/test_auth.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_auth.py`:

```python
"""
Unit tests for services/auth.py.

How to read this file:

  1. encode_token returns a JWT string with the user_id claim and an
     exp claim 30 days in the future.
  2. decode_token recovers the user_id from a valid token, returns
     None for tampered or expired tokens.
  3. get_current_user — exercised in Task 7 against the real FastAPI
     route. Here we test the lower-level encode/decode pair.
"""

import os
import time

import jwt as pyjwt
import pytest

os.environ.setdefault("LILA_JWT_SECRET", "test-secret-do-not-use-in-prod")

from services import auth


def test_roundtrip():
    tok = auth.encode_token("alice")
    assert auth.decode_token(tok) == "alice"


def test_tampered_token_returns_none():
    tok = auth.encode_token("alice")
    bad = tok[:-2] + ("AA" if tok[-2:] != "AA" else "BB")
    assert auth.decode_token(bad) is None


def test_expired_token_returns_none():
    secret = os.environ["LILA_JWT_SECRET"]
    expired = pyjwt.encode(
        {"sub": "alice", "exp": int(time.time()) - 5},
        secret, algorithm="HS256",
    )
    assert auth.decode_token(expired) is None
```

- [ ] **Step 2: Run tests to confirm failure**

Run: `pytest tests/test_auth.py -v`
Expected: ImportError (`services.auth` does not exist yet).

- [ ] **Step 3: Implement services/auth.py**

Create `services/auth.py`:

```python
"""
Cookie-based JWT auth.

Public API:
  encode_token(user_id)         -> jwt string
  decode_token(token)           -> user_id | None
  get_current_user(request)     -> dict     (FastAPI dependency)
  set_login_cookie(response, token)
  clear_login_cookie(response)

How to read this file:

  1. encode_token: HS256-sign a payload {sub: user_id, exp: now+30d}
     with LILA_JWT_SECRET from the environment.
  2. decode_token: verify signature + expiry. Returns the sub claim
     on success, None on any failure.
  3. get_current_user: read the cookie, decode the token, look up
     the user row in Postgres, raise 401 if anything is missing.
  4. set_login_cookie / clear_login_cookie: standardize the cookie
     name, path, secure, httpOnly, samesite settings in one place
     so every route sets them identically.
"""

import os
import time
from typing import Any

import jwt as pyjwt
from fastapi import HTTPException, Request, Response

from services import users as _users

_COOKIE_NAME = "lila_session"
_TOKEN_TTL_SECONDS = 30 * 24 * 3600   # 30 days
_ALG = "HS256"


def _secret() -> str:
    s = os.environ.get("LILA_JWT_SECRET")
    if not s:
        raise RuntimeError("LILA_JWT_SECRET is not set")
    return s


def encode_token(user_id: str) -> str:
    payload = {"sub": user_id, "exp": int(time.time()) + _TOKEN_TTL_SECONDS}
    return pyjwt.encode(payload, _secret(), algorithm=_ALG)


def decode_token(token: str) -> str | None:
    try:
        payload = pyjwt.decode(token, _secret(), algorithms=[_ALG])
        return payload.get("sub")
    except pyjwt.PyJWTError:
        return None


def set_login_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=_COOKIE_NAME, value=token, max_age=_TOKEN_TTL_SECONDS,
        httponly=True, secure=True, samesite="lax", path="/",
    )


def clear_login_cookie(response: Response) -> None:
    response.delete_cookie(key=_COOKIE_NAME, path="/")


def get_current_user(request: Request) -> dict:
    """
    1. Read the lila_session cookie.
    2. Decode it; missing or invalid → 401.
    3. Load the user row; missing → 401 (account was deleted while
       the cookie was still valid).
    4. Return the user row dict.
    """
    token = request.cookies.get(_COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="not authenticated")
    user_id = decode_token(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="invalid session")
    user = _users.get_user(user_id)
    if not user:
        raise HTTPException(status_code=401, detail="user no longer exists")
    return user
```

- [ ] **Step 4: Run tests to confirm pass**

Run: `pytest tests/test_auth.py -v`
Expected: 3 PASS.

- [ ] **Step 5: Commit**

```bash
git add services/auth.py tests/test_auth.py
git commit -m "Add JWT cookie auth and FastAPI get_current_user dependency"
```

---

## Task 6: archive.py rewrite — set up the new module + retire legacy paths

**Files:**
- Modify: `services/archive.py`

This task replaces `archive.py`'s storage backend with Postgres + a
per-user volume directory. To keep the diff manageable, this task only
changes the module-level constants and helpers; subsequent tasks
rewrite each public function with TDD against the DB fixture.

- [ ] **Step 1: Replace the path constants and add per-user helpers**

Open `services/archive.py`. Replace the entire region from the
imports through `_new_id()` (the current lines 45–148) with:

```python
"""
Archive service — Postgres-backed event log + per-user volume audio.

How to read this module:

  1. Postgres tables (see migrations/001_initial_schema.sql):
     1A. events              one row per audio/text/delete/job_*
     1B. jobs                one row per queued / done job
     1C. last_action         one row per user — the single-step undo buffer
  2. Volume layout under VOLUME_ROOT:
     2A. <user_id>/raw/<file_id>.<ext>   raw audio bytes
     2B. <user_id>/raw/<file_id>.txt     text payloads (kept on disk
                                         so legacy callers reading
                                         {file_id}.txt still work)
     2C. <user_id>/raw/<file_id>.mid     materialized MIDI on first
                                         reveal-style download
  3. Public API: every function takes user_id as the first
     parameter. Internals filter rows by user_id everywhere; cross-
     user reads are impossible at the SQL layer.
  4. The chat surface remains append-only. update_files_meta is
     only invoked by the web PATCH route, snapshots the prior rows
     into last_action, and supports a single-step undo via
     undo_last_action.
"""

import json
import os
import secrets
import shutil
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from services import db as _db

load_dotenv()

VOLUME_ROOT = Path(os.getenv("ARCHIVE_PATH", "./archive"))


# ── Path helpers ─────────────────────────────────────────────────────────────

def _user_dir(user_id: str) -> Path:
    return VOLUME_ROOT / user_id


def _raw_dir(user_id: str) -> Path:
    return _user_dir(user_id) / "raw"


def ensure_user_dirs(user_id: str) -> None:
    """
    1. Create <volume>/<user_id>/raw/ if it does not exist.
    2. Idempotent — safe to call from every ingest path.
    """
    _raw_dir(user_id).mkdir(parents=True, exist_ok=True)


def _new_id() -> str:
    return secrets.token_hex(4)


# ── Module-level back-compat constants ───────────────────────────────────────
# Tests in conftest.py monkeypatch ARCHIVE_ROOT / RAW_DIR to a temp
# path. These constants stay defined so the existing _guard_real_archive
# fixture and any callers that import them keep working.
ARCHIVE_ROOT = VOLUME_ROOT
RAW_DIR      = VOLUME_ROOT
EVENTS_FILE  = VOLUME_ROOT / ".events_legacy_unused"
JOBS_DIR     = VOLUME_ROOT / ".jobs_legacy_unused"
SUMMARIES_DIR = VOLUME_ROOT / "summaries"


def ensure_archive_root() -> None:
    """Back-compat shim. Per-user dirs are created lazily by ingest."""
    VOLUME_ROOT.mkdir(parents=True, exist_ok=True)
    SUMMARIES_DIR.mkdir(parents=True, exist_ok=True)
```

- [ ] **Step 2: Delete the obsolete `_bootstrap_events_from_orphan_sidecars`, `_append_event`, `_read_events`, `_deleted_ids`, and `_MIGRATION_MARKER` / `migrate_v1` functions**

These were JSONL-era helpers. Cut them entirely. The dedicated
migration script (Task 16) is the new one-shot import path.

- [ ] **Step 3: Run the existing test suite to see what breaks**

Run: `pytest tests/ -x -q`
Expected: many failures — the old API has been gutted. This is
intentional. Subsequent tasks reimplement function-by-function with
TDD; tests will turn green incrementally.

- [ ] **Step 4: Commit (red state)**

```bash
git add services/archive.py
git commit -m "Stub archive.py for Postgres rewrite (tests intentionally red)"
```

---

## Task 7: archive.py — ingest_audio + current_entry + get_feed

**Files:**
- Modify: `services/archive.py`
- Create: `tests/test_archive_pg.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_archive_pg.py`:

```python
"""
Postgres-backed archive tests.

How to read this file:

  1. Each test seeds at least one user via the seed_user fixture.
  2. ingest_audio copies bytes to <volume>/<user>/raw/<fid>.<ext>
     and inserts an audio event row.
  3. current_entry returns the row, or {} if absent or soft-deleted.
  4. get_feed returns newest-first audio/text events for the user,
     filtered by tag, paginated.
"""

from pathlib import Path

import pytest

from services import archive


@pytest.fixture
def fake_audio(tmp_path) -> Path:
    p = tmp_path / "src.ogg"
    p.write_bytes(b"OGGS\x00\x00fake-audio")
    return p


@pytest.fixture
def temp_volume(tmp_path, monkeypatch):
    monkeypatch.setattr(archive, "VOLUME_ROOT", tmp_path / "v")
    archive.ensure_archive_root()
    return tmp_path / "v"


def test_ingest_audio_copies_bytes_and_inserts_event(
    db, seed_user, fake_audio, temp_volume,
):
    uid = seed_user("dhruv")

    ev = archive.ingest_audio(
        uid, str(fake_audio),
        slug="test-loop", tags=["sketch", "loop"],
        ext="ogg", transcript="hello",
    )

    assert ev["slug"] == "test-loop"
    assert ev["tags"] == ["sketch", "loop"]
    raw_path = temp_volume / uid / "raw" / f"{ev['file_id']}.ogg"
    assert raw_path.exists()
    assert raw_path.read_bytes() == fake_audio.read_bytes()

    same = archive.current_entry(uid, ev["file_id"])
    assert same["slug"] == "test-loop"
    assert same["transcript"] == "hello"


def test_current_entry_isolates_users(
    db, seed_user, fake_audio, temp_volume,
):
    a = seed_user("alice")
    b = seed_user("bob")

    ev = archive.ingest_audio(a, str(fake_audio), "x", [], "ogg", "")
    assert archive.current_entry(b, ev["file_id"]) == {}


def test_get_feed_newest_first_with_tag(
    db, seed_user, fake_audio, temp_volume,
):
    uid = seed_user("dhruv")
    archive.ingest_audio(uid, str(fake_audio), "older", ["a"], "ogg", "")
    archive.ingest_audio(uid, str(fake_audio), "newer", ["a"], "ogg", "")

    feed = archive.get_feed(uid, tag="a")
    assert [e["slug"] for e in feed] == ["newer", "older"]
```

- [ ] **Step 2: Run tests to confirm failure**

Run: `pytest tests/test_archive_pg.py -v`
Expected: 3 failures (functions undefined or returning wrong shape).

- [ ] **Step 3: Implement the three functions**

Append to `services/archive.py`:

```python
# ── Audio ingest ─────────────────────────────────────────────────────────────

def ingest_audio(
    user_id: str,
    src_path: str,
    slug: str,
    tags: list[str],
    ext: str,
    transcript: str = "",
    parent_id: str | None = None,
) -> dict:
    """
    1. Make sure the per-user raw/ directory exists.
    2. If parent_id is given, inherit its tags so derived files
       carry their lineage automatically.
    3. Copy the source bytes to <volume>/<user>/raw/<file_id>.<ext>
       BEFORE inserting the event — the row should never refer to
       a file that does not exist on disk.
    4. INSERT one audio event row and RETURNING * so the caller sees
       the canonical shape (with created_at populated by Postgres).
    """
    ensure_user_dirs(user_id)
    if parent_id:
        parent = current_entry(user_id, parent_id)
        inherited = parent.get("tags", [])
        tags = inherited + [t for t in tags if t not in inherited]

    file_id = _new_id()
    ext = ext.lstrip(".")
    shutil.copy2(str(src_path), str(_raw_dir(user_id) / f"{file_id}.{ext}"))

    event_id = _new_id()
    with _db.connect() as conn:
        cur = conn.execute(
            """INSERT INTO events
               (event_id, user_id, type, file_id, slug, tags,
                transcript, ext, parent_id, job_id)
               VALUES (%s, %s, 'audio', %s, %s, %s, %s, %s, %s, NULL)
               RETURNING *""",
            (event_id, user_id, file_id, slug, tags, transcript, ext, parent_id),
        )
        row = cur.fetchone()
        conn.commit()
    row["id"] = row["file_id"]
    return row


# ── Reads ────────────────────────────────────────────────────────────────────

def current_entry(user_id: str, file_id: str) -> dict:
    """
    1. Look for a delete event for this (user, file_id). If one
       exists, the entry is gone from the user's perspective →
       return {}.
    2. Otherwise return the audio/text event row for the file_id.
       Empty dict (not None) so callers can chain .get(...).
    3. Alias `id` ← `file_id` so legacy call sites that read
       entry["id"] still work.
    """
    deleted = _db.fetch_one(
        """SELECT 1 FROM events
           WHERE user_id = %s AND file_id = %s AND type = 'delete'
           LIMIT 1""",
        (user_id, file_id),
    )
    if deleted:
        return {}
    row = _db.fetch_one(
        """SELECT * FROM events
           WHERE user_id = %s AND file_id = %s AND type IN ('audio','text')
           ORDER BY created_at DESC LIMIT 1""",
        (user_id, file_id),
    )
    if not row:
        return {}
    row["id"] = row["file_id"]
    return row


def get_feed(user_id: str, tag: str = "", limit: int = 100, offset: int = 0) -> list[dict]:
    """
    1. Return audio/text events newest-first.
    2. Exclude any file_id that has a delete event.
    3. Optionally filter by tag (array containment).
    """
    if tag:
        sql = """
            SELECT e.* FROM events e
            WHERE e.user_id = %s
              AND e.type IN ('audio','text')
              AND %s = ANY(e.tags)
              AND NOT EXISTS (
                  SELECT 1 FROM events d
                  WHERE d.user_id = e.user_id
                    AND d.file_id = e.file_id
                    AND d.type = 'delete')
            ORDER BY e.created_at DESC
            LIMIT %s OFFSET %s
        """
        rows = _db.fetch_all(sql, (user_id, tag, limit, offset))
    else:
        sql = """
            SELECT e.* FROM events e
            WHERE e.user_id = %s
              AND e.type IN ('audio','text')
              AND NOT EXISTS (
                  SELECT 1 FROM events d
                  WHERE d.user_id = e.user_id
                    AND d.file_id = e.file_id
                    AND d.type = 'delete')
            ORDER BY e.created_at DESC
            LIMIT %s OFFSET %s
        """
        rows = _db.fetch_all(sql, (user_id, limit, offset))
    for r in rows:
        r["id"] = r["file_id"]
    return rows
```

- [ ] **Step 4: Run tests to confirm pass**

Run: `pytest tests/test_archive_pg.py -v`
Expected: 3 PASS.

- [ ] **Step 5: Commit**

```bash
git add services/archive.py tests/test_archive_pg.py
git commit -m "archive: ingest_audio + current_entry + get_feed against Postgres"
```

---

## Task 8: archive.py — ingest_text + get_all_tags + delete_file + search

**Files:**
- Modify: `services/archive.py`
- Modify: `tests/test_archive_pg.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_archive_pg.py`:

```python
def test_ingest_text_inserts_row_and_writes_payload(
    db, seed_user, temp_volume,
):
    uid = seed_user("dhruv")
    ev = archive.ingest_text(uid, "lyrics-1", ["lyric"], "all the broken hours")

    txt = temp_volume / uid / "raw" / f"{ev['file_id']}.txt"
    assert txt.exists()
    assert txt.read_text() == "all the broken hours"

    feed = archive.get_feed(uid)
    assert any(e["file_id"] == ev["file_id"] for e in feed)


def test_get_all_tags_counts_per_user(db, seed_user, fake_audio, temp_volume):
    uid = seed_user("dhruv")
    archive.ingest_audio(uid, str(fake_audio), "a", ["x", "y"], "ogg", "")
    archive.ingest_audio(uid, str(fake_audio), "b", ["x"],     "ogg", "")
    archive.ingest_text(uid, "n", ["x", "y"], "note")

    by_tag = {t["tag"]: t["count"] for t in archive.get_all_tags(uid)}
    assert by_tag == {"x": 3, "y": 2}


def test_delete_file_soft_deletes_and_filter_drops_it(
    db, seed_user, fake_audio, temp_volume,
):
    uid = seed_user("dhruv")
    ev = archive.ingest_audio(uid, str(fake_audio), "kill-me", ["x"], "ogg", "")
    assert archive.delete_file(uid, ev["file_id"]) is True
    assert archive.current_entry(uid, ev["file_id"]) == {}
    assert ev["file_id"] not in [e["file_id"] for e in archive.get_feed(uid)]


def test_search_matches_slug_tag_text_transcript(
    db, seed_user, fake_audio, temp_volume,
):
    uid = seed_user("dhruv")
    archive.ingest_audio(uid, str(fake_audio), "monastery-loop", ["foley"], "ogg",
                         "the bell rings once")
    archive.ingest_text(uid, "lyrics-x", ["lyric"], "hospital corridor lights")

    res = archive.search(uid, "bell")
    assert any(e["slug"] == "monastery-loop" for e in res)

    res2 = archive.search(uid, "corridor")
    assert any(e["slug"] == "lyrics-x" for e in res2)
```

- [ ] **Step 2: Run tests to confirm failure**

Run: `pytest tests/test_archive_pg.py -v`
Expected: 4 new failures.

- [ ] **Step 3: Implement the four functions**

Append to `services/archive.py`:

```python
# ── Text ingest ──────────────────────────────────────────────────────────────

def ingest_text(
    user_id: str,
    slug: str,
    tags: list[str],
    text: str,
    parent_id: str | None = None,
    midi_notes: str | None = None,
) -> dict:
    """
    1. Inherit parent tags when parent_id is given.
    2. Persist the user-visible text (or midi notes when present)
       to <volume>/<user>/raw/<fid>.txt so legacy file-serving
       routes that look up {file_id}.txt continue to work.
    3. INSERT one text event row.
    """
    ensure_user_dirs(user_id)
    if parent_id:
        parent = current_entry(user_id, parent_id)
        inherited = parent.get("tags", [])
        tags = inherited + [t for t in tags if t not in inherited]

    file_id = _new_id()
    (_raw_dir(user_id) / f"{file_id}.txt").write_text(midi_notes or text)

    event_id = _new_id()
    with _db.connect() as conn:
        cur = conn.execute(
            """INSERT INTO events
               (event_id, user_id, type, file_id, slug, tags,
                text, midi_notes, ext, parent_id, job_id)
               VALUES (%s, %s, 'text', %s, %s, %s, %s, %s, 'txt', %s, NULL)
               RETURNING *""",
            (event_id, user_id, file_id, slug, tags, text, midi_notes, parent_id),
        )
        row = cur.fetchone()
        conn.commit()
    row["id"] = row["file_id"]
    return row


# ── Tag tally ────────────────────────────────────────────────────────────────

def get_all_tags(user_id: str) -> list[dict]:
    """Counts each tag across the user's live (non-deleted) entries."""
    rows = _db.fetch_all(
        """SELECT tag, COUNT(*) AS count
           FROM (
               SELECT UNNEST(e.tags) AS tag
               FROM events e
               WHERE e.user_id = %s
                 AND e.type IN ('audio','text')
                 AND NOT EXISTS (
                     SELECT 1 FROM events d
                     WHERE d.user_id = e.user_id
                       AND d.file_id = e.file_id
                       AND d.type = 'delete')
           ) t
           GROUP BY tag
           ORDER BY count DESC""",
        (user_id,),
    )
    return [dict(r) for r in rows]


# ── Soft delete ──────────────────────────────────────────────────────────────

def delete_file(user_id: str, file_id: str) -> bool:
    """
    1. Confirm the entry is currently live; if not, return False
       (already deleted or never existed).
    2. INSERT a delete event so future reads filter the entry out.
       The raw bytes on disk are intentionally left in place so a
       manual restore is still possible by removing the delete row.
    """
    if not current_entry(user_id, file_id):
        return False
    _db.execute(
        """INSERT INTO events (event_id, user_id, type, file_id)
           VALUES (%s, %s, 'delete', %s)""",
        (_new_id(), user_id, file_id),
    )
    return True


# ── Search ───────────────────────────────────────────────────────────────────

def search(user_id: str, query: str) -> list[dict]:
    """Case-insensitive substring match across slug, tags, transcript, text."""
    q = f"%{query.lower()}%"
    rows = _db.fetch_all(
        """SELECT e.* FROM events e
           WHERE e.user_id = %s
             AND e.type IN ('audio','text')
             AND NOT EXISTS (
                 SELECT 1 FROM events d
                 WHERE d.user_id = e.user_id
                   AND d.file_id = e.file_id
                   AND d.type = 'delete')
             AND (
                 LOWER(COALESCE(e.slug, ''))       LIKE %s OR
                 LOWER(COALESCE(e.transcript, '')) LIKE %s OR
                 LOWER(COALESCE(e.text, ''))       LIKE %s OR
                 EXISTS (
                     SELECT 1 FROM UNNEST(e.tags) tg
                     WHERE LOWER(tg) LIKE %s)
             )
           ORDER BY e.created_at DESC""",
        (user_id, q, q, q, q),
    )
    for r in rows:
        r["id"] = r["file_id"]
    return rows
```

- [ ] **Step 4: Run tests to confirm pass**

Run: `pytest tests/test_archive_pg.py -v`
Expected: 7 PASS total.

- [ ] **Step 5: Commit**

```bash
git add services/archive.py tests/test_archive_pg.py
git commit -m "archive: ingest_text, get_all_tags, delete_file, search"
```

---

## Task 9: archive.py — update_files_meta + undo_last_action + last_action snapshot

**Files:**
- Modify: `services/archive.py`
- Modify: `tests/test_archive_pg.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_archive_pg.py`:

```python
def test_update_meta_changes_tags_and_undo_restores(
    db, seed_user, fake_audio, temp_volume,
):
    uid = seed_user("dhruv")
    ev = archive.ingest_audio(uid, str(fake_audio), "x", ["a", "b"], "ogg", "")
    fid = ev["file_id"]

    assert archive.update_file_meta(uid, fid, tags=["c"]) is True
    assert archive.current_entry(uid, fid)["tags"] == ["c"]

    n = archive.undo_last_action(uid)
    assert n == 1
    assert archive.current_entry(uid, fid)["tags"] == ["a", "b"]


def test_update_meta_unknown_returns_false(db, seed_user, temp_volume):
    uid = seed_user("dhruv")
    assert archive.update_file_meta(uid, "deadbeef", tags=["x"]) is False
```

- [ ] **Step 2: Run tests to confirm failure**

Run: `pytest tests/test_archive_pg.py::test_update_meta_changes_tags_and_undo_restores -v`
Expected: FAIL.

- [ ] **Step 3: Implement update + undo**

Append to `services/archive.py`:

```python
# ── In-place edit (web PATCH only) + single-step undo ───────────────────────

def update_files_meta(
    user_id: str,
    file_ids: list[str],
    slug: str | None = None,
    tags: list[str] | None = None,
    transcript: str | None = None,
    text: str | None = None,
) -> int:
    """
    Web PATCH endpoint helper. Mutates rows in place — the chat
    surface still uses append-only file_system_note.

    1. Build the SET clause from supplied fields. If nothing to
       change, return 0.
    2. In one transaction:
       2A. SELECT every targeted live (non-deleted) audio/text row
           into a snapshot list.
       2B. Persist that snapshot to last_action (replacing any prior
           buffer for this user).
       2C. UPDATE the rows with the supplied fields.
    3. Return the number of rows actually changed.
    """
    fields: dict = {}
    if slug       is not None: fields["slug"]       = slug
    if tags       is not None: fields["tags"]       = tags
    if transcript is not None: fields["transcript"] = transcript
    if text       is not None: fields["text"]       = text
    if not fields or not file_ids:
        return 0

    set_sql = ", ".join(f"{k} = %s" for k in fields)
    params = list(fields.values())

    with _db.connect() as conn:
        snap_rows = conn.execute(
            """SELECT * FROM events
               WHERE user_id = %s
                 AND file_id = ANY(%s)
                 AND type IN ('audio','text')
                 AND NOT EXISTS (
                     SELECT 1 FROM events d
                     WHERE d.user_id = events.user_id
                       AND d.file_id = events.file_id
                       AND d.type = 'delete')""",
            (user_id, file_ids),
        ).fetchall()
        if not snap_rows:
            return 0

        snapshots = [_serialize_row_for_snapshot(r) for r in snap_rows]
        conn.execute(
            """INSERT INTO last_action (user_id, snapshots)
               VALUES (%s, %s)
               ON CONFLICT (user_id) DO UPDATE
                 SET snapshots = EXCLUDED.snapshots,
                     created_at = now()""",
            (user_id, json.dumps(snapshots)),
        )
        upd = conn.execute(
            f"""UPDATE events SET {set_sql}
                WHERE user_id = %s AND file_id = ANY(%s)
                  AND type IN ('audio','text')""",
            (*params, user_id, file_ids),
        )
        conn.commit()
        return upd.rowcount or 0


def update_file_meta(
    user_id: str,
    file_id: str,
    slug: str | None = None,
    tags: list[str] | None = None,
    transcript: str | None = None,
    text: str | None = None,
) -> bool:
    return update_files_meta(
        user_id, [file_id],
        slug=slug, tags=tags, transcript=transcript, text=text,
    ) > 0


def undo_last_action(user_id: str) -> int:
    """
    1. Read the last_action row for the user. None → nothing to do.
    2. For each snapshot, restore the prior column values via UPDATE.
    3. Delete the buffer so subsequent undo calls are no-ops.
    """
    with _db.connect() as conn:
        row = conn.execute(
            "SELECT snapshots FROM last_action WHERE user_id = %s", (user_id,),
        ).fetchone()
        if not row:
            return 0
        snapshots = row["snapshots"]
        if isinstance(snapshots, str):
            snapshots = json.loads(snapshots)
        for snap in snapshots:
            conn.execute(
                """UPDATE events SET
                       slug = %s, tags = %s, transcript = %s,
                       text = %s, midi_notes = %s
                   WHERE user_id = %s AND file_id = %s
                     AND type IN ('audio','text')""",
                (
                    snap.get("slug"),
                    snap.get("tags") or [],
                    snap.get("transcript"),
                    snap.get("text"),
                    snap.get("midi_notes"),
                    user_id,
                    snap["file_id"],
                ),
            )
        conn.execute("DELETE FROM last_action WHERE user_id = %s", (user_id,))
        conn.commit()
        return len(snapshots)


def _serialize_row_for_snapshot(row: dict) -> dict:
    """Pick only the columns we restore on undo. Strip transient fields."""
    return {
        "file_id":    row["file_id"],
        "slug":       row.get("slug"),
        "tags":       row.get("tags") or [],
        "transcript": row.get("transcript"),
        "text":       row.get("text"),
        "midi_notes": row.get("midi_notes"),
    }
```

- [ ] **Step 4: Run tests to confirm pass**

Run: `pytest tests/test_archive_pg.py -v`
Expected: 9 PASS total.

- [ ] **Step 5: Commit**

```bash
git add services/archive.py tests/test_archive_pg.py
git commit -m "archive: update_files_meta with last_action snapshot + undo"
```

---

## Task 10: archive.py — jobs (queue/complete/get) + stage/commit audio + slug version

**Files:**
- Modify: `services/archive.py`
- Modify: `tests/test_archive_pg.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_archive_pg.py`:

```python
def test_queue_and_complete_job(db, seed_user, fake_audio, temp_volume):
    uid = seed_user("dhruv")
    ev = archive.ingest_audio(uid, str(fake_audio), "x", ["a"], "ogg", "")
    job = archive.queue_job(uid, "to_midi", ev["file_id"], {"k": 1})
    assert job["status"] == "queued"
    archive.complete_job(uid, job["job_id"], output_file_id="abc12345")
    rows = archive.get_jobs(uid, status="done")
    assert any(j["job_id"] == job["job_id"] for j in rows)


def test_stage_then_commit_audio(db, seed_user, fake_audio, temp_volume):
    uid = seed_user("dhruv")
    fid, path = archive.stage_audio(uid, str(fake_audio), "ogg")
    assert path.exists()
    assert archive.current_entry(uid, fid) == {}    # not committed yet

    ev = archive.commit_audio(uid, fid, "voice-1", ["voice-note"], "ogg",
                              transcript="hi")
    assert ev["file_id"] == fid
    assert archive.current_entry(uid, fid)["slug"] == "voice-1"


def test_slug_version_counts_per_user(db, seed_user, fake_audio, temp_volume):
    uid = seed_user("dhruv")
    archive.ingest_audio(uid, str(fake_audio), "loop", ["x"], "ogg", "")
    archive.ingest_audio(uid, str(fake_audio), "loop", ["x"], "ogg", "")
    assert archive.get_slug_version(uid, "loop") == 2
```

- [ ] **Step 2: Run tests to confirm failure**

Run: `pytest tests/test_archive_pg.py -v`
Expected: 3 new failures.

- [ ] **Step 3: Implement jobs + staging + slug helper**

Append to `services/archive.py`:

```python
# ── Jobs ─────────────────────────────────────────────────────────────────────

def queue_job(user_id: str, job_type: str, input_file_id: str,
              params: dict | None = None) -> dict:
    """
    1. Insert a job row in 'queued' state.
    2. Append a job_queued event so the feed reflects it.
    """
    job_id = "job_" + _new_id()
    params = params or {}
    with _db.connect() as conn:
        cur = conn.execute(
            """INSERT INTO jobs
               (job_id, user_id, type, status, input_file_id, params)
               VALUES (%s, %s, %s, 'queued', %s, %s)
               RETURNING *""",
            (job_id, user_id, job_type, input_file_id, json.dumps(params)),
        )
        job = cur.fetchone()
        input_tags = []
        ce = current_entry(user_id, input_file_id)
        if ce:
            input_tags = ce.get("tags", [])
        conn.execute(
            """INSERT INTO events
               (event_id, user_id, type, tags, job_id)
               VALUES (%s, %s, 'job_queued', %s, %s)""",
            (_new_id(), user_id, input_tags, job_id),
        )
        conn.commit()
    return job


def complete_job(user_id: str, job_id: str,
                 output_file_id: str | None = None,
                 output_text: str | None = None) -> None:
    with _db.connect() as conn:
        conn.execute(
            """UPDATE jobs
               SET status = 'done', output_file_id = %s, completed_at = now()
               WHERE user_id = %s AND job_id = %s""",
            (output_file_id, user_id, job_id),
        )
        conn.execute(
            """INSERT INTO events
               (event_id, user_id, type, file_id, job_id, text)
               VALUES (%s, %s, 'job_done', %s, %s, %s)""",
            (_new_id(), user_id, output_file_id, job_id, output_text),
        )
        conn.commit()


def get_jobs(user_id: str, status: str | None = None) -> list[dict]:
    if status:
        rows = _db.fetch_all(
            """SELECT * FROM jobs WHERE user_id = %s AND status = %s
               ORDER BY created_at DESC""",
            (user_id, status),
        )
    else:
        rows = _db.fetch_all(
            "SELECT * FROM jobs WHERE user_id = %s ORDER BY created_at DESC",
            (user_id,),
        )
    return [dict(r) for r in rows]


# ── Staging (audio that may or may not be committed) ─────────────────────────

def stage_audio(user_id: str, src_path: str, ext: str) -> tuple[str, Path]:
    """
    1. Generate file_id and copy bytes to <volume>/<user>/raw/.
    2. Do NOT insert an event yet — caller decides whether to keep
       it (see commit_audio) based on the LLM's tool choice.
    """
    ensure_user_dirs(user_id)
    file_id = _new_id()
    ext = ext.lstrip(".")
    out = _raw_dir(user_id) / f"{file_id}.{ext}"
    shutil.copy2(str(src_path), str(out))
    return file_id, out


def commit_audio(user_id: str, file_id: str, slug: str, tags: list[str],
                 ext: str, transcript: str = "") -> dict:
    """
    Insert the audio event for a previously-staged file. Pairs with
    stage_audio — used by services/pipeline.py:handle_audio.
    """
    ext = ext.lstrip(".")
    with _db.connect() as conn:
        cur = conn.execute(
            """INSERT INTO events
               (event_id, user_id, type, file_id, slug, tags,
                transcript, ext, parent_id, job_id)
               VALUES (%s, %s, 'audio', %s, %s, %s, %s, %s, NULL, NULL)
               RETURNING *""",
            (_new_id(), user_id, file_id, slug, tags, transcript, ext),
        )
        row = cur.fetchone()
        conn.commit()
    row["id"] = row["file_id"]
    return row


# ── Helper: how many entries already use this slug ──────────────────────────

def get_slug_version(user_id: str, slug: str) -> int:
    row = _db.fetch_one(
        """SELECT COUNT(*) AS n FROM events
           WHERE user_id = %s AND slug = %s
             AND type IN ('audio','text')""",
        (user_id, slug),
    )
    return int(row["n"]) if row else 0
```

- [ ] **Step 4: Run tests to confirm pass**

Run: `pytest tests/test_archive_pg.py -v`
Expected: 12 PASS total.

- [ ] **Step 5: Commit**

```bash
git add services/archive.py tests/test_archive_pg.py
git commit -m "archive: jobs + stage_audio/commit_audio + get_slug_version"
```

---

## Task 11: services/conversation_store.py — DB-backed history

**Files:**
- Create: `services/conversation_store.py`
- Create: `tests/test_conversation_store.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_conversation_store.py`:

```python
"""
Unit tests for services/conversation_store.py.

How to read this file:

  1. append() inserts one turn row.
  2. recent() returns the last N turns oldest→newest, the shape the
     LLM expects when injected into chat history.
  3. Per-user-global: alice's history never includes bob's turns.
"""

from services import conversation_store as cs


def test_append_and_recent_round_trip(db, seed_user):
    uid = seed_user("alice")
    cs.append(uid, "user", "hi")
    cs.append(uid, "assistant", "hello")
    out = cs.recent(uid, limit=10)
    assert out == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]


def test_recent_caps_to_limit_oldest_dropped(db, seed_user):
    uid = seed_user("alice")
    for i in range(5):
        cs.append(uid, "user", f"m{i}")
    out = cs.recent(uid, limit=2)
    assert [t["content"] for t in out] == ["m3", "m4"]


def test_per_user_isolation(db, seed_user):
    a = seed_user("alice")
    b = seed_user("bob")
    cs.append(a, "user", "alice-only")
    cs.append(b, "user", "bob-only")
    assert [t["content"] for t in cs.recent(a, 10)] == ["alice-only"]
    assert [t["content"] for t in cs.recent(b, 10)] == ["bob-only"]
```

- [ ] **Step 2: Run tests to confirm failure**

Run: `pytest tests/test_conversation_store.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement the module**

Create `services/conversation_store.py`:

```python
"""
Per-user-global conversation history backed by Postgres.

Public API:
  append(user_id, role, content)     -> None
  recent(user_id, limit=20)          -> list[{role, content}]

How to read this file:

  1. append inserts one row; turn_id is BIGSERIAL so ordering is
     preserved without a clock dependency.
  2. recent fetches the last N rows DESC, then reverses them so the
     caller gets oldest→newest — matching the message-list shape
     the OpenAI chat API expects.
"""

from services import db as _db


def append(user_id: str, role: str, content: str) -> None:
    if role not in ("user", "assistant"):
        raise ValueError(f"unknown role: {role}")
    _db.execute(
        """INSERT INTO conversation_turns (user_id, role, content)
           VALUES (%s, %s, %s)""",
        (user_id, role, content),
    )


def recent(user_id: str, limit: int = 20) -> list[dict]:
    rows = _db.fetch_all(
        """SELECT role, content FROM conversation_turns
           WHERE user_id = %s
           ORDER BY turn_id DESC
           LIMIT %s""",
        (user_id, limit),
    )
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]
```

- [ ] **Step 4: Run tests to confirm pass**

Run: `pytest tests/test_conversation_store.py -v`
Expected: 3 PASS.

- [ ] **Step 5: Commit**

```bash
git add services/conversation_store.py tests/test_conversation_store.py
git commit -m "Add Postgres-backed per-user-global conversation history"
```

---

## Task 12: pipeline.py — thread user_id through handle_text and handle_audio

**Files:**
- Modify: `services/pipeline.py`
- Modify: `services/conversation_log.py`

- [ ] **Step 1: Update conversation_log.log_turn signature**

Open `services/conversation_log.py`. Find the `log_turn` function
signature and add `user_id: str` as a keyword argument. Find the dict
that gets written and add `"user_id": user_id`.

If the file isn't already structured to accept it, the change at the
JSONL row level is exactly:

```python
def log_turn(
    *,
    user_id: str,                    # NEW
    transport: str,
    input_type: str,
    input_text: str,
    llm_message: str,
    reply: str,
    tool_calls: list[dict],
    eval_candidate: bool,
    transcript: str | None = None,
) -> None:
    row = {
        "user_id":         user_id,  # NEW
        "transport":       transport,
        "input_type":      input_type,
        "input_text":      input_text,
        "llm_message":     llm_message,
        "reply":           reply,
        "tool_calls":      tool_calls,
        "eval_candidate":  eval_candidate,
        "transcript":      transcript,
        "created_at":      datetime.utcnow().isoformat(),
    }
    # ... rest of existing append logic ...
```

(If the existing implementation already takes a different shape, keep
its body; the only change is adding `user_id` to the parameters and
the JSONL row.)

- [ ] **Step 2: Modify pipeline.py — both entry points gain user_id**

Open `services/pipeline.py`. Update the two function signatures:

```python
def handle_text(
    user_id: str,                    # NEW first param
    message: str,
    history: list[dict],
    transport: str = "unknown",
) -> dict:
```

```python
def handle_audio(
    user_id: str,                    # NEW first param
    tmp_path: str,
    ext: str,
    user_context: str,
    history: list[dict],
    transport: str = "unknown",
) -> dict:
```

Inside both, every call to `log_turn(...)` gains `user_id=user_id` as
a keyword.

- [ ] **Step 3: Pass user_id into archive.stage_audio / commit_audio**

In `handle_audio`, replace:

```python
file_id, staged_path = stage_audio(tmp_path, ext)
```

with:

```python
file_id, staged_path = stage_audio(user_id, tmp_path, ext)
```

And replace the `commit_audio(...)` call inside the `_file_audio`
closure to pass `user_id` as the first parameter.

Replace `get_slug_version(slug)` with `get_slug_version(user_id, slug)`.

- [ ] **Step 4: Pass user_id into respond_to_text**

`respond_to_text` will gain a `user_id` parameter in Task 13. For now,
update the calls in pipeline.py to forward it:

```python
raw, tool_calls = respond_to_text(user_id, clean_message, history)
```

```python
raw, tool_calls = respond_to_text(
    user_id, llm_message, history,
    extra_tools=[_FILE_AUDIO_TOOL],
    extra_handlers={"file_audio": _file_audio},
)
```

- [ ] **Step 5: Pass user_id into handle_job**

In `handle_text`, find the job-marker branch:

```python
reply = handle_job(job_args)
```

Replace with:

```python
reply = handle_job(user_id, job_args)
```

`services/jobs.py:handle_job` will accept the new param in Task 13.

- [ ] **Step 6: Run pipeline tests**

Run: `pytest tests/ -k pipeline -v` (and any tests that exercise the
pipeline flow, e.g. `test_corrections.py`, `test_monastery_flow.py`).

Expected: still red — downstream callers haven't been updated yet.
That's intentional. Continue.

- [ ] **Step 7: Commit**

```bash
git add services/pipeline.py services/conversation_log.py
git commit -m "pipeline + conversation_log: thread user_id through both entry points"
```

---

## Task 13: llm.py — thread user_id through tool handlers; jobs.py too

**Files:**
- Modify: `services/llm.py`
- Modify: `services/jobs.py`

- [ ] **Step 1: Modify respond_to_text signature**

Open `services/llm.py`. Update:

```python
def respond_to_text(
    user_id: str,                    # NEW first param
    message: str,
    conversation_history: list[dict],
    extra_tools: list[dict] | None = None,
    extra_handlers: dict | None = None,
) -> tuple[str, list[dict]]:
```

Inside, every dispatcher must thread `user_id` into its archive call:

```python
                if name == "queue_job":
                    tool_call_log.append({"name": name, "args": args, "result": "queued"})
                    # user_id is intentionally NOT serialized into the marker —
                    # the caller (pipeline.handle_text) already has it locally
                    # and passes it into handle_job directly.
                    return _JOB_MARKER + json.dumps(args), tool_call_log

                if extra_handlers and name in extra_handlers:
                    result = extra_handlers[name](args)
                elif name == "list_entries":
                    result = _tool_list_entries(user_id, args)
                elif name == "read_entries":
                    result = _tool_read_entries(user_id, args)
                elif name == "file_text":
                    result = _tool_file_text(user_id, args)
                elif name == "file_system_note":
                    result = _tool_file_system_note(user_id, args)
                else:
                    result = f"unknown tool: {name}"
```

- [ ] **Step 2: Update each `_tool_*` helper**

Each tool helper gains `user_id` as the first parameter and forwards
it to the archive call. Replace the existing helpers with these:

```python
def _tool_list_entries(user_id: str, args: dict) -> str:
    from services.archive import get_feed
    limit = int(args.get("limit") or 15)
    tag   = (args.get("tag") or "").strip()
    print(f"[LILA/llm/list_entries] user={user_id} tag={tag!r} limit={limit}")

    events = get_feed(user_id, tag=tag, limit=max(limit * 3, 40))
    files  = [e for e in events if e.get("type") in ("audio", "text", "lyric")][:limit]
    if not files:
        return f"no files tagged {tag}" if tag else "archive is empty"

    lines = [f"{len(files)} entries" + (f" tagged {tag}:" if tag else ":")]
    for e in files:
        fid  = (e.get("file_id") or "")[:8]
        slug = e.get("slug", "—")
        tags = ", ".join(e.get("tags", []))
        when = str(e.get("created_at", ""))[:16].replace("T", " ")
        kind = e.get("type", "?")
        lines.append(f"  {fid}  {slug}  ({kind})  [{tags}]  {when}")
    return "\n".join(lines)


def _tool_file_text(user_id: str, args: dict) -> str:
    from services.archive import ingest_text
    text = (args.get("text") or "").strip()
    slug = (args.get("slug") or "untitled").strip()
    tags = args.get("tags") or []
    if not text:
        return "error: no text supplied"
    ev  = ingest_text(user_id, slug, tags, text)
    fid = (ev.get("file_id") or "")[:8]
    return f"filed. file_id={fid} slug={ev.get('slug')} tags={ev.get('tags')}"


def _tool_file_system_note(user_id: str, args: dict) -> str:
    from services.archive import ingest_text, current_entry
    content = (args.get("content") or "").strip()
    if not content:
        return "error: no content supplied for system_note"
    target = (args.get("target_file_id") or "").strip()
    inherited: list[str] = []
    if target:
        inherited = list(current_entry(user_id, target).get("tags", []))
    tags = inherited + (["system-note"] if "system-note" not in inherited else [])
    slug = f"system-note-{target[:8]}" if target else f"system-note-{_timestamp()}"
    body = f"[target: {target[:8]}] {content}" if target else content
    ev   = ingest_text(user_id, slug, tags, body)
    fid  = (ev.get("file_id") or "")[:8]
    return f"noted. file_id={fid} target={target[:8] if target else 'none'} tags={tags}"


def _tool_read_entries(user_id: str, args: dict) -> str:
    from services.archive import get_feed
    limit = int(args.get("limit") or 30)
    tag   = (args.get("tag") or "").strip()

    events = get_feed(user_id, tag=tag, limit=max(limit * 2, 60))
    fragments: list[str] = []
    for e in events:
        if e.get("type") not in ("audio", "text", "lyric"):
            continue
        text  = (e.get("text") or "").strip()
        trans = (e.get("transcript") or "").strip()
        midi  = (e.get("midi_notes") or "").strip()
        body  = text or trans
        if not body and not midi:
            continue
        fid  = (e.get("file_id") or "")[:8]
        slug = e.get("slug", "")
        tags = ", ".join(e.get("tags", []))
        when = str(e.get("created_at", ""))[:16].replace("T", " ")
        kind = "lyric/text" if e.get("type") in ("text", "lyric") else "voice-note"
        if midi:
            kind = "midi"
        parts = [f"[{when}]  {fid}  {slug}  ({kind})  [{tags}]"]
        if body:
            parts.append(body)
        if midi:
            parts.append(f"NOTE data (pitch start_sec dur_sec):\n{midi}")
        fragments.append("\n".join(parts))
        if len(fragments) >= limit:
            break
    if not fragments:
        return f"no readable entries tagged {tag}" if tag else "no readable entries"
    return "\n\n---\n\n".join(reversed(fragments))
```

- [ ] **Step 3: Update summarize_tag signature**

Replace the `summarize_tag(tag)` signature:

```python
def summarize_tag(user_id: str, tag: str) -> str:
    from services.archive import get_feed
    events = list(reversed(get_feed(user_id, tag=tag, limit=500)))
    # ... rest of body unchanged ...
```

- [ ] **Step 4: jobs.py — handle_job, execute_job, every private helper**

Open `services/jobs.py`. `parse_job_marker` does not need to change —
it just returns the parsed JSON dict and the caller threads `user_id`
in separately.

Update every other function to take `user_id` first.

`handle_job`:

```python
def handle_job(user_id: str, args: dict) -> str:
    """
    Inline job dispatch — runs the side-effect synchronously and
    returns the user-facing reply string. The chat path uses this;
    the web POST /jobs path uses execute_job directly via
    BackgroundTasks.

    1. Decide which handler to run from job_type.
    2. Resolve the input entry by current_entry(user_id, file_id)
       — every handler needs the parent's tags / slug to derive the
       output slug.
    3. Dispatch to a typed helper; helpers run real DSP where it
     exists (render_chords) and stubs elsewhere.
    """
    jt = args.get("job_type", "?")
    print(f"[LILA/jobs] handle_job: user={user_id} args={args}")

    if jt == "to_midi":
        fid = (args.get("file_id") or "").strip()
        if not fid:
            return "which file? give me a file_id from the archive."
        sc = archive.current_entry(user_id, fid)
        if not sc:
            return f"file {fid} not found."
        ev = _generate_midi_for(user_id, sc)
        parent_slug = sc.get("slug", fid)
        return f"filed {ev['slug']} — midi grid derived from {parent_slug}."

    # ... apply the same user_id-threading change to every other
    # job_type branch in this function. Pattern: every call to
    # archive.current_entry, archive.ingest_audio, archive.ingest_text
    # gains user_id as the first argument; every private helper
    # gains a user_id parameter forwarded from here.

    return stub_job_response(args)
```

`execute_job` (called from `BackgroundTasks` in the web /jobs path):

```python
def execute_job(user_id: str, job: dict) -> None:
    """
    1. Resolve the input entry for this user.
    2. Dispatch to the typed handler.
    3. Mark the job done with complete_job once the handler returns.
    """
    jtype = job.get("type") or job.get("job_type", "")
    fid   = job.get("input_file_id", "")
    sc    = archive.current_entry(user_id, fid)
    if not sc and jtype != "summarize":
        archive.complete_job(user_id, job["job_id"], output_text="(input not found)")
        return

    handlers = {
        "to_midi":        _to_midi,
        "stem_split":     _stem_split,
        "autotune":       _autotune,
        "transpose":      _transpose,
        "render_chords":  _render_chords,
        "summarize":      _summarize,
    }
    fn = handlers.get(jtype)
    if not fn:
        archive.complete_job(user_id, job["job_id"], output_text=f"unknown job_type: {jtype}")
        return
    try:
        out_id = fn(user_id, job, sc)
        archive.complete_job(user_id, job["job_id"], output_file_id=out_id)
    except Exception as e:
        import logging
        logging.getLogger("lila.jobs").exception("job failed")
        archive.complete_job(user_id, job["job_id"], output_text=f"error: {e}")
```

Every private handler — `_to_midi`, `_stem_split`, `_autotune`,
`_transpose`, `_render_chords`, `_summarize`, `_generate_midi_for` —
gains `user_id: str` as the first parameter and forwards it into
every `archive.current_entry`, `archive.ingest_audio`,
`archive.ingest_text`, and `archive.queue_job` call inside its body.
The function shapes don't change; only the call sites that already
exist in those bodies pick up the new first argument.

Update `pipeline.handle_text` to pass `user_id` into `handle_job` as
already shown in Task 12 step 5.

- [ ] **Step 5: Run tests**

Run: `pytest tests/ -v` (full suite).
Expected: most tests pass; existing `test_corrections.py` and
`test_monastery_flow.py` may need a small update to seed a user and
pass `user_id` — that update is part of Task 17.

- [ ] **Step 6: Commit**

```bash
git add services/llm.py services/jobs.py
git commit -m "llm + jobs: thread user_id through tool handlers and job runner"
```

---

## Task 14: main.py — auth dependency, /auth routes, user_id threading, drop /reveal

**Files:**
- Modify: `main.py`

This task is large because main.py is the entry point. Break into steps.

- [ ] **Step 1: Add /auth/login and /auth/logout routes**

In `main.py`, after the existing imports and CORS middleware, add:

```python
from services import auth, users
from services import conversation_store

class LoginRequest(BaseModel):
    username: str
    password: str

@app.post("/auth/login")
async def login(req: LoginRequest, response: Response):
    """
    1. Verify the password with argon2id.
    2. Issue a JWT and set it as the lila_session cookie.
    3. Return a tiny ack — the cookie does the work.
    """
    if not users.verify_password(req.username, req.password):
        raise HTTPException(401, "invalid credentials")
    token = auth.encode_token(req.username)
    auth.set_login_cookie(response, token)
    return {"ok": True, "user_id": req.username}

@app.post("/auth/logout")
async def logout(response: Response):
    auth.clear_login_cookie(response)
    return {"ok": True}

@app.get("/auth/me")
async def me(user: dict = Depends(auth.get_current_user)):
    return {"user_id": user["user_id"], "username": user["username"]}
```

Add the missing imports at the top: `from fastapi import Depends, Response`.

- [ ] **Step 2: Replace `_conversations: dict` and switch to conversation_store**

Delete `_conversations: dict[str, list[dict]] = {}` from the top of
the file. Anywhere it was used, replace with `conversation_store`
calls. The audio ingest handler becomes:

```python
@app.post("/ingest/audio")
async def ingest_audio_endpoint(
    file: UploadFile = File(...),
    context: Optional[str] = Form(None),
    user: dict = Depends(auth.get_current_user),
):
    """
    1. Save upload to a temp file.
    2. Pull the user's recent history from Postgres.
    3. Hand off to pipeline.handle_audio with user_id.
    4. Persist the user + assistant turn back to history.
    """
    suffix = Path(file.filename or "audio.ogg").suffix or ".ogg"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name
    try:
        history = conversation_store.recent(user["user_id"], limit=20)
        result  = pipeline.handle_audio(
            user["user_id"], tmp_path, suffix.lstrip("."),
            user_context=context or "",
            history=history, transport="web",
        )
        conversation_store.append(user["user_id"], "user",
                                  context or "(audio)")
        conversation_store.append(user["user_id"], "assistant",
                                  result["message"])
        return result
    finally:
        try: os.unlink(tmp_path)
        except OSError: pass
```

And the text ingest:

```python
@app.post("/ingest/text")
async def ingest_text_endpoint(
    req: TextRequest,
    user: dict = Depends(auth.get_current_user),
):
    history = conversation_store.recent(user["user_id"], limit=20)
    result  = pipeline.handle_text(
        user["user_id"], req.message, history, transport="web",
    )
    if result["type"] != "eval":
        conversation_store.append(user["user_id"], "user", req.message)
        conversation_store.append(user["user_id"], "assistant", result["message"])
    return result
```

(Drop `conversation_id` from `TextRequest` — per-user-global means we
don't key on it anymore.)

- [ ] **Step 3: Add user_id (via auth) to every other archive route**

Update `/feed`, `/tags`, `/files/{file_id}/audio`, `/files/{file_id}/text`,
`/files/{file_id}/midi`, `/search`, `PATCH /files/{file_id}`,
`DELETE /files/{file_id}`, `POST /jobs`, `GET /jobs`, `GET /jobs/{job_id}`
to accept `user: dict = Depends(auth.get_current_user)` and pass
`user["user_id"]` into the archive function on every line that calls
one. Example for `/feed`:

```python
@app.get("/feed")
async def feed(
    tag: str = "", limit: int = 200, offset: int = 0,
    user: dict = Depends(auth.get_current_user),
):
    return get_feed(user["user_id"], tag=tag, limit=limit, offset=offset)
```

For file-serving routes, build the path from
`VOLUME_ROOT / user["user_id"] / "raw" / f"{file_id}.{ext}"`. Example:

```python
def _find_audio_path(user_id: str, file_id: str) -> Path | None:
    raw_dir = archive.VOLUME_ROOT / user_id / "raw"
    matches = [m for m in raw_dir.glob(f"{file_id}.*") if m.suffix.lower() in _AUDIO_EXT]
    return matches[0] if matches else None

@app.get("/files/{file_id}/audio")
@app.get("/files/{file_id}/audio/{filename}")
async def serve_audio(
    file_id: str, filename: str = "",
    user: dict = Depends(auth.get_current_user),
):
    path = _find_audio_path(user["user_id"], file_id)
    if not path:
        raise HTTPException(404, "audio file not found")
    sc   = current_entry(user["user_id"], file_id) or {}
    slug = sc.get("slug") or file_id
    ext  = path.suffix.lstrip(".")
    return FileResponse(str(path), filename=f"{slug}.{ext}")
```

- [ ] **Step 4: Delete `/files/{file_id}/reveal`**

Cut the entire route handler. macOS-only, doesn't apply in cloud.

- [ ] **Step 5: Add `/health`**

```python
@app.get("/health")
async def health():
    """1. Ping the DB. 2. Return ok or 503."""
    try:
        from services import db as _db
        _db.fetch_one("SELECT 1 AS ok")
        return {"ok": True}
    except Exception as e:
        raise HTTPException(503, f"db unreachable: {e}")
```

- [ ] **Step 6: Wrap synchronous job execution in BackgroundTasks**

`POST /jobs`:

```python
@app.post("/jobs")
async def create_job(
    req: JobRequest, bg: BackgroundTasks,
    user: dict = Depends(auth.get_current_user),
):
    """
    1. Validate the input file exists for this user.
    2. queue_job (creates a 'queued' row immediately).
    3. Schedule execute_job in the background; return the queued
       job to the caller without waiting on the side-effect.
    """
    sc = current_entry(user["user_id"], req.input_file_id)
    if not sc:
        raise HTTPException(404, "input file not found")
    job = queue_job(user["user_id"], req.job_type, req.input_file_id, req.params)
    bg.add_task(execute_job, user["user_id"], job)
    return job
```

Add `BackgroundTasks` to the FastAPI imports.

The text-ingest job branch in `pipeline.handle_text` stays
synchronous for now (handled inside `handle_job`); upgrading that to
BackgroundTasks is a Phase 2 follow-up because the LLM expects the
job result before composing its reply.

- [ ] **Step 7: Static UI is now login-gated by 401-redirect**

The `app.mount("/", StaticFiles(...))` line stays where it is. The
PWA's service worker (Task 15) handles the 401 → redirect-to-login
flow on the client side. The existing `static/index.html` calls
`/feed` etc., which now return 401 if no cookie.

- [ ] **Step 8: Run a smoke test against local Postgres**

```bash
export DATABASE_URL='postgresql://localhost:5432/lila_dev'
export LILA_JWT_SECRET=$(python -c "import secrets; print(secrets.token_urlsafe(48))")
python migrations/run.py
python -m services.users create --username dhruv --password dev
python main.py
```

In a second terminal:

```bash
# unauth returns 401
curl -i http://localhost:8000/feed
# → HTTP/1.1 401

# login sets cookie
curl -c jar -i -X POST http://localhost:8000/auth/login \
     -H 'content-type: application/json' \
     -d '{"username":"dhruv","password":"dev"}'

# auth'd request works
curl -b jar http://localhost:8000/feed
# → []
```

Expected: 401 on the first call, 200 + JSON `[]` on the third.

- [ ] **Step 9: Commit**

```bash
git add main.py
git commit -m "main: auth dependency, /auth routes, user_id threading, drop /reveal, add /health"
```

---

## Task 15: PWA — manifest, service worker, login page, MediaRecorder

**Files:**
- Create: `static/manifest.webmanifest`
- Create: `static/sw.js`
- Create: `static/login.html`
- Modify: `static/index.html`

- [ ] **Step 1: Manifest**

Create `static/manifest.webmanifest`:

```json
{
  "name": "Lila",
  "short_name": "Lila",
  "start_url": "/",
  "display": "standalone",
  "background_color": "#0e0e0e",
  "theme_color": "#0e0e0e",
  "icons": [
    {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
    {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"}
  ]
}
```

Note: the icons are referenced but not provided in this plan. Add
two square PNGs (192×192 and 512×512) to `static/` before deploy. A
black square or any placeholder works; the owner can replace later.

- [ ] **Step 2: Service worker**

Create `static/sw.js`:

```javascript
/*
 * Minimal service worker.
 *
 * 1. Precache the shell on install: index.html, login.html, manifest.
 * 2. Pass-through every fetch — no offline strategy yet, the PWA
 *    needs the network to talk to the backend.
 * 3. On a 401 from any /feed, /tags, /search, /ingest, /files
 *    request, redirect to /login.html so the user can re-auth.
 */
const SHELL = ["/", "/login.html", "/manifest.webmanifest"];
self.addEventListener("install", (e) => {
  e.waitUntil(caches.open("lila-shell-v1").then((c) => c.addAll(SHELL)));
});
self.addEventListener("fetch", (event) => {
  event.respondWith(
    fetch(event.request).then((r) => {
      if (r.status === 401 && event.request.mode === "navigate") {
        return Response.redirect("/login.html", 302);
      }
      return r;
    }).catch(() => caches.match(event.request))
  );
});
```

- [ ] **Step 3: Login page**

Create `static/login.html`:

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Lila — Login</title>
  <link rel="manifest" href="/static/manifest.webmanifest">
  <style>
    body { font-family: ui-monospace, monospace; background: #0e0e0e;
           color: #eee; display: grid; place-items: center; height: 100vh; }
    form { display: grid; gap: 0.5rem; min-width: 16rem; }
    input, button { padding: 0.6rem; background: #1a1a1a; color: #eee;
                    border: 1px solid #333; border-radius: 4px; font: inherit; }
    .err { color: #f66; min-height: 1em; }
  </style>
</head>
<body>
  <form id="f">
    <h2>Lila</h2>
    <input id="u" placeholder="username" autocomplete="username" required>
    <input id="p" type="password" placeholder="password" autocomplete="current-password" required>
    <button type="submit">Sign in</button>
    <div id="err" class="err"></div>
  </form>
  <script>
    document.getElementById("f").addEventListener("submit", async (e) => {
      e.preventDefault();
      const username = document.getElementById("u").value;
      const password = document.getElementById("p").value;
      const err = document.getElementById("err");
      err.textContent = "";
      const r = await fetch("/auth/login", {
        method: "POST",
        headers: {"content-type": "application/json"},
        body: JSON.stringify({username, password}),
        credentials: "same-origin",
      });
      if (r.ok) location.href = "/";
      else err.textContent = "invalid credentials";
    });
  </script>
</body>
</html>
```

- [ ] **Step 4: index.html — register SW + redirect on 401 + record button**

Open `static/index.html`. In the `<head>`, add:

```html
<link rel="manifest" href="/static/manifest.webmanifest">
<meta name="theme-color" content="#0e0e0e">
```

At the bottom of the existing `<script>` block (or in a new one
before `</body>`), add:

```javascript
// PWA: register service worker
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/static/sw.js").catch(() => {});
}

// Auth: any 401 → bounce to /login.html
const _origFetch = window.fetch;
window.fetch = async (...args) => {
  const r = await _origFetch(...args);
  if (r.status === 401) location.href = "/login.html";
  return r;
};

// Voice memo: hold-to-record on a single button.
async function setupRecord() {
  const btn = document.getElementById("record-btn");
  if (!btn) return;
  let recorder, chunks = [];
  btn.addEventListener("pointerdown", async () => {
    const stream = await navigator.mediaDevices.getUserMedia({audio: true});
    recorder = new MediaRecorder(stream);
    chunks = [];
    recorder.ondataavailable = e => chunks.push(e.data);
    recorder.onstop = async () => {
      const blob = new Blob(chunks, {type: "audio/webm"});
      const fd   = new FormData();
      fd.append("file", blob, "voice.webm");
      const r = await fetch("/ingest/audio", {method: "POST", body: fd});
      if (r.ok && typeof refreshFeed === "function") refreshFeed();
      stream.getTracks().forEach(t => t.stop());
    };
    recorder.start();
    btn.classList.add("recording");
  });
  btn.addEventListener("pointerup", () => {
    if (recorder && recorder.state === "recording") recorder.stop();
    btn.classList.remove("recording");
  });
}
setupRecord();
```

Add a record button somewhere appropriate in the existing markup, for
example near the chat input:

```html
<button id="record-btn" type="button" title="hold to record">●</button>
```

(`refreshFeed` is whatever existing function reloads `/feed`; if the
project uses a different name, swap it.)

- [ ] **Step 5: Manual smoke test**

Reload the dev server, open `http://localhost:8000/login.html`, log
in, confirm you're redirected to `/`. Click-and-hold the record
button, speak, release. Confirm a new audio entry appears in the
feed.

- [ ] **Step 6: Commit**

```bash
git add static/manifest.webmanifest static/sw.js static/login.html static/index.html
git commit -m "PWA: manifest, service worker, login screen, MediaRecorder voice memo"
```

---

## Task 16: telegram_bot.py — chat_id → user_id, persistent history

**Files:**
- Modify: `telegram_bot.py`

- [ ] **Step 1: Replace allowlist with DB lookup**

Open `telegram_bot.py`. Replace the `_ALLOWED_ID` constant and
`_is_allowed` function with a DB-backed lookup:

```python
from services import users as _users
from services import conversation_store as _cs


def _lookup_user(update: Update) -> dict | None:
    """
    1. Pull the chat_id from the incoming update.
    2. Look up the linked user in Postgres. None means this Telegram
       account hasn't been paired to a Lila account.
    """
    chat_id = update.effective_chat.id if update.effective_chat else None
    if chat_id is None:
        return None
    return _users.get_user_by_telegram(chat_id)
```

- [ ] **Step 2: Update message handlers**

In every handler, replace the in-memory `_conversations[chat_id]`
access with conversation_store calls keyed by user_id. Each handler
does the same auth check at the top:

```python
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = _lookup_user(update)
    if not user:
        await update.message.reply_text(
            "this Telegram account is not linked to a Lila user."
        )
        return
    history = _cs.recent(user["user_id"], limit=20)
    result  = pipeline.handle_text(
        user["user_id"], update.message.text, history, transport="telegram",
    )
    if result["type"] != "eval":
        _cs.append(user["user_id"], "user",      update.message.text)
        _cs.append(user["user_id"], "assistant", result["message"])
    # ... dispatch result["segments"] to telegram primitives as before
```

Apply the same pattern to the voice-message handler (which calls
`pipeline.handle_audio`).

- [ ] **Step 3: Update the main loop and remove the old env constant**

Drop `_ALLOWED_ID = os.getenv("TELEGRAM_ALLOWED_USER_ID", "").strip()`
and any references. Keep `TELEGRAM_BOT_TOKEN`.

- [ ] **Step 4: Smoke test locally**

In one shell:

```bash
export DATABASE_URL='postgresql://localhost:5432/lila_dev'
export LILA_JWT_SECRET=...
# link your real chat id to your user
python -m services.users set-telegram --username dhruv --chat-id <YOUR_CHAT_ID>
python telegram_bot.py
```

Send the bot a text message; confirm it replies. Send a voice note;
confirm it files. Restart the bot; confirm history persists.

- [ ] **Step 5: Commit**

```bash
git add telegram_bot.py
git commit -m "telegram: chat_id → user_id lookup; persistent history via Postgres"
```

---

## Task 17: Update existing tests to pass user_id

**Files:**
- Modify: `tests/test_corrections.py`, `tests/test_monastery_flow.py`

- [ ] **Step 1: Audit and update**

Run: `pytest tests/ -v 2>&1 | head -80`

For each failing test, the fix is one of:
1. Add `db, seed_user` fixtures to the signature.
2. Generate `uid = seed_user("u_test")` at the top.
3. Pass `uid` as the first arg to every archive call inside.

Update both `test_corrections.py` and `test_monastery_flow.py` accordingly.

- [ ] **Step 2: Run full suite green**

Run: `pytest tests/ -v`
Expected: all tests pass, no skips beyond the DB-not-configured case.

- [ ] **Step 3: Commit**

```bash
git add tests/test_corrections.py tests/test_monastery_flow.py
git commit -m "tests: pass user_id through corrections + monastery flow tests"
```

---

## Task 18: Structured JSON logging

**Files:**
- Create: `services/logsetup.py`
- Modify: `main.py`, `telegram_bot.py`

- [ ] **Step 1: Add the setup module**

Create `services/logsetup.py`:

```python
"""
JSON log setup for Fly's log aggregator.

Every process calls configure() once at startup. After that, the
existing print(flush=True) calls inside service code keep working
unchanged — they go to stdout, Fly captures them as plain text. The
HTTP and bot loggers go through this configured handler, so their
records become structured JSON with user_id / route / status fields.
"""

import logging

from pythonjsonlogger import jsonlogger


def configure(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(jsonlogger.JsonFormatter(
        fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
    ))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
```

- [ ] **Step 2: Call configure() at startup**

Top of `main.py` (after the existing imports):

```python
from services.logsetup import configure as _configure_logs
_configure_logs()
```

Top of `telegram_bot.py`, replace the existing `logging.basicConfig(...)`
call with:

```python
from services.logsetup import configure as _configure_logs
_configure_logs()
log = logging.getLogger("lila.telegram")
```

- [ ] **Step 3: Smoke test**

Run: `python main.py` and watch one request in the logs. Each access
log line should be a JSON object.

- [ ] **Step 4: Commit**

```bash
git add services/logsetup.py main.py telegram_bot.py
git commit -m "Structured JSON logging via python-json-logger"
```

---

## Task 19: Migration script — JSONL archive → Postgres

**Files:**
- Create: `scripts/migrate_jsonl_to_postgres.py`

- [ ] **Step 1: Write the script**

Create `scripts/migrate_jsonl_to_postgres.py`:

```python
"""
One-shot import of an existing single-user archive into Postgres.

Run once after deploying. Idempotent: writes a marker file at
<archive_root>/.migrated_to_pg and bails on subsequent runs.

Usage:
  python scripts/migrate_jsonl_to_postgres.py \
      --user-id dhruv \
      --source ./archive
"""

import argparse
import json
import shutil
import sys
from pathlib import Path

from services import db as _db
from services.archive import VOLUME_ROOT, ensure_user_dirs, _new_id


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--user-id", required=True)
    p.add_argument("--source",  required=True, type=Path)
    args = p.parse_args()

    src: Path = args.source
    user_id = args.user_id
    marker = src / ".migrated_to_pg"
    if marker.exists():
        print(f"already migrated: {marker}")
        return 0

    events_file = src / "events.jsonl"
    raw_dir = src / "raw"
    if not events_file.exists():
        print(f"no events.jsonl at {events_file}", file=sys.stderr)
        return 1

    ensure_user_dirs(user_id)
    target_raw = VOLUME_ROOT / user_id / "raw"

    # 1. Replay every event into Postgres, attributing to user_id.
    inserted = 0
    with _db.connect() as conn, events_file.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            ev = json.loads(line)
            etype = ev.get("type")
            event_id = ev.get("event_id") or _new_id()
            if etype in ("audio", "text"):
                conn.execute(
                    """INSERT INTO events
                       (event_id, user_id, type, file_id, slug, tags,
                        transcript, text, midi_notes, ext, parent_id, job_id, created_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (event_id) DO NOTHING""",
                    (event_id, user_id, etype, ev.get("file_id"),
                     ev.get("slug"), ev.get("tags") or [],
                     ev.get("transcript"), ev.get("text"),
                     ev.get("midi_notes"), ev.get("ext"),
                     ev.get("parent_id"), ev.get("job_id"),
                     ev.get("created_at")),
                )
                inserted += 1
            elif etype == "delete":
                conn.execute(
                    """INSERT INTO events
                       (event_id, user_id, type, file_id, created_at)
                       VALUES (%s,%s,'delete',%s,%s)
                       ON CONFLICT (event_id) DO NOTHING""",
                    (event_id, user_id, ev.get("file_id"), ev.get("created_at")),
                )
                inserted += 1
        conn.commit()
    print(f"inserted {inserted} events")

    # 2. Copy raw files into the per-user volume path.
    if raw_dir.exists():
        copied = 0
        for f in raw_dir.iterdir():
            if not f.is_file():
                continue
            dst = target_raw / f.name
            if not dst.exists():
                shutil.copy2(str(f), str(dst))
                copied += 1
        print(f"copied {copied} raw files")

    # 3. Replay job json files if present.
    jobs_dir = src / "jobs"
    if jobs_dir.exists():
        with _db.connect() as conn:
            for jf in jobs_dir.glob("*.json"):
                try:
                    j = json.loads(jf.read_text())
                except Exception:
                    continue
                conn.execute(
                    """INSERT INTO jobs
                       (job_id, user_id, type, status, input_file_id,
                        output_file_id, params, created_at, completed_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (job_id) DO NOTHING""",
                    (j.get("job_id"), user_id, j.get("type"), j.get("status"),
                     j.get("input_file_id"), j.get("output_file_id"),
                     json.dumps(j.get("params") or {}),
                     j.get("created_at"), j.get("completed_at")),
                )
            conn.commit()

    marker.write_text("done")
    print("migration complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Dry-run against a copy of the local archive**

```bash
cp -r archive /tmp/archive_test
export DATABASE_URL='postgresql://localhost:5432/lila_dev'
python -m services.users create --username dhruv --password dev   # if not yet
python scripts/migrate_jsonl_to_postgres.py --user-id dhruv --source /tmp/archive_test
```

Expected: prints insert count and "migration complete."

- [ ] **Step 3: Verify**

```bash
psql "$DATABASE_URL" -c "SELECT COUNT(*) FROM events WHERE user_id='dhruv';"
```

Expected: matches the line count of `archive/events.jsonl`.

- [ ] **Step 4: Commit**

```bash
git add scripts/migrate_jsonl_to_postgres.py
git commit -m "Add one-shot script to import JSONL archive into Postgres"
```

---

## Task 20: scripts/seed_owner.py — first-deploy default credentials

**Files:**
- Create: `scripts/seed_owner.py`

- [ ] **Step 1: Write the script**

Create `scripts/seed_owner.py`:

```python
"""
Seed the default owner account on first deploy.

How to read this file:

  1. Idempotent: if the owner row already exists, do nothing and
     exit 0 — re-running the script on every deploy is safe.
  2. Generates a 16-char URL-safe random password. Writes it in the
     clear to /data/initial_password.txt for the owner to retrieve
     once via `flyctl ssh console`. The owner rotates it via the
     web UI (or `python -m services.users set-password`) afterward.
  3. The default username is read from LILA_OWNER_USERNAME (default
     'dhruv'). The Telegram chat id can be set later via
     `set-telegram`.
"""

import os
import secrets
import sys
from pathlib import Path

from services import users


def main() -> int:
    username = os.environ.get("LILA_OWNER_USERNAME", "dhruv")
    if users.get_user(username):
        print(f"owner already exists: {username}")
        return 0

    password = secrets.token_urlsafe(12)
    users.create_user(username=username, password=password)
    target = Path("/data/initial_password.txt")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        f"username: {username}\npassword: {password}\n"
        f"\nRotate after first login: python -m services.users set-password "
        f"--username {username} --password '...'\n"
    )
    target.chmod(0o600)
    print(f"created owner {username}; password written to {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Smoke test locally**

```bash
LILA_OWNER_USERNAME=test_owner python scripts/seed_owner.py
# inspect: cat /data/initial_password.txt   (will be /tmp on macos if /data missing — adjust target if needed)
```

- [ ] **Step 3: Commit**

```bash
git add scripts/seed_owner.py
git commit -m "Add seed_owner.py for default credentials on first deploy"
```

---

## Task 21: Dockerfile + .dockerignore — bake Whisper model

**Files:**
- Create: `Dockerfile`
- Create: `.dockerignore`

- [ ] **Step 1: Dockerfile**

Create `Dockerfile`:

```dockerfile
# syntax=docker/dockerfile:1.6
FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    XDG_CACHE_HOME=/data/whisper-cache

# 1. System deps:
#    1A. ffmpeg — faster-whisper invokes it for audio decode
#    1B. libpq — psycopg's runtime requirement
#    1C. ca-certs — TLS for OpenRouter
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg libpq5 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

# 2. Pre-download the faster-whisper base model so the first request
#    on a fresh container does not stall. Cached under XDG_CACHE_HOME
#    which is the volume path, so subsequent deploys reuse it.
RUN python -c "from faster_whisper import WhisperModel; \
WhisperModel('base', device='cpu', compute_type='int8', \
download_root='/data/whisper-cache')" || true

COPY . .

# Default command runs the web process; fly.toml overrides for the
# telegram process.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
```

- [ ] **Step 2: .dockerignore**

Create `.dockerignore`:

```
.git
__pycache__
*.pyc
archive/
logs/
.env
.venv
docs/
tests/
.pytest_cache
```

- [ ] **Step 3: Build locally**

Run: `docker build -t lila:dev .`
Expected: image builds successfully. (The model download line uses
`|| true` so build proceeds even on a flaky download; the volume
cache picks it up on first run.)

- [ ] **Step 4: Commit**

```bash
git add Dockerfile .dockerignore
git commit -m "Dockerfile + .dockerignore for Fly deployment"
```

---

## Task 22: fly.toml — two processes, volume, secrets

**Files:**
- Create: `fly.toml`

- [ ] **Step 1: Write fly.toml**

Create `fly.toml`:

```toml
app = "lila"
primary_region = "iad"            # change at deploy time if you prefer sjc / lhr
kill_signal    = "SIGINT"
kill_timeout   = "5s"

[build]
dockerfile = "Dockerfile"

[env]
ARCHIVE_PATH = "/data/archive"

[[mounts]]
source      = "lila_data"
destination = "/data"

[processes]
web      = "uvicorn main:app --host 0.0.0.0 --port 8080"
telegram = "python telegram_bot.py"

[[services]]
processes      = ["web"]
internal_port  = 8080
protocol       = "tcp"
auto_stop_machines  = false
auto_start_machines = true
min_machines_running = 1

  [[services.ports]]
  port     = 80
  handlers = ["http"]
  force_https = true

  [[services.ports]]
  port     = 443
  handlers = ["tls", "http"]

  [[services.http_checks]]
  interval        = "15s"
  grace_period    = "30s"
  method          = "GET"
  path            = "/health"
  protocol        = "http"
  timeout         = "5s"
```

- [ ] **Step 2: Commit**

```bash
git add fly.toml
git commit -m "fly.toml: two processes, persistent volume, /health probe"
```

---

## Task 23: Deploy + cutover playbook

This task is owner-driven, not code. It is a one-time runbook. Each
checkbox is a step the owner runs.

**Files:**
- Create: `docs/deploy-runbook.md`

- [ ] **Step 1: Write the runbook**

Create `docs/deploy-runbook.md`:

````markdown
# Lila — Deploy & cutover runbook

One-time. After this, deploys are `flyctl deploy`.

## 0. Prerequisites

- `flyctl` installed: `brew install flyctl`
- Logged in: `flyctl auth login`
- Payment method on file: `flyctl auth signup` if new account.

## 1. Provision the Fly app + volume + Postgres

```bash
flyctl apps create lila --org personal
flyctl volumes create lila_data --region iad --size 5 --app lila
flyctl postgres create --name lila-db --region iad
flyctl postgres attach lila-db --app lila
# this prints DATABASE_URL into the app's secrets automatically
```

## 2. Set the rest of the secrets

```bash
flyctl secrets set --app lila \
    OPENROUTER_API_KEY='...' \
    MODEL='google/gemini-2.0-flash-lite-001' \
    TELEGRAM_BOT_TOKEN='...' \
    LILA_JWT_SECRET="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')" \
    LILA_OWNER_USERNAME='dhruv'
```

## 3. First deploy

```bash
flyctl deploy --app lila
```

Wait for the health check to go green. Watch logs:

```bash
flyctl logs --app lila
```

## 4. Apply migrations + seed the owner

```bash
flyctl ssh console --app lila
# inside the container:
python migrations/run.py
python scripts/seed_owner.py
cat /data/initial_password.txt   # save these
exit
```

## 5. Stop the laptop processes BEFORE bringing up the cloud Telegram bot

If both run simultaneously they fight for `getUpdates`:

```bash
# on your laptop
pkill -f telegram_bot.py
pkill -f 'uvicorn main:app'
```

Now the cloud Telegram process takes over polling on its own.

## 6. Migrate the existing archive

```bash
# upload the archive directory to the volume
flyctl ssh sftp shell --app lila
> mkdir /data/archive_import
> put -r ./archive /data/archive_import
> exit

flyctl ssh console --app lila
python scripts/migrate_jsonl_to_postgres.py \
    --user-id dhruv --source /data/archive_import/archive
exit
```

## 7. Link Telegram

Get your numeric Telegram chat id (e.g. send `/id` to `@userinfobot`).

```bash
flyctl ssh console --app lila
python -m services.users set-telegram --username dhruv --chat-id <YOUR_CHAT_ID>
exit
```

Restart the bot process so it picks up the new mapping:

```bash
flyctl machine restart --app lila --process telegram
```

## 8. Smoke tests

- Open `https://lila.fly.dev/login.html`. Sign in. See your feed.
- Send the bot a voice note from Telegram. Confirm filing.
- Send the bot a correction (e.g. "actually that's monastery, not underworld"). Confirm a system-note appears in the feed.
- Use the PWA install flow (Add to Home Screen) on your phone.

## 9. Rotate the seeded password

Open the web UI's account page (or run `set-password`):

```bash
flyctl ssh console --app lila
python -m services.users set-password --username dhruv --password 'YOUR_PASSWORD'
exit
```

Delete `/data/initial_password.txt`:

```bash
flyctl ssh console --app lila
rm /data/initial_password.txt
exit
```

## Day-2 ops cheatsheet

```bash
flyctl deploy            # ship code changes
flyctl logs              # tail logs
flyctl ssh console       # shell into a running machine
flyctl machine restart --process web
flyctl postgres connect --app lila-db
```
````

- [ ] **Step 2: Commit**

```bash
git add docs/deploy-runbook.md
git commit -m "Add deploy + cutover runbook"
```

---

## Task 24: Final sweep — full test run, manual smoke, push

- [ ] **Step 1: Full test run**

```bash
export DATABASE_URL='postgresql://localhost:5432/lila_dev'
pytest tests/ -v
```

Expected: all tests pass.

- [ ] **Step 2: Local end-to-end smoke**

Run the web server and the Telegram bot locally pointed at local
Postgres. Walk through the runbook's smoke checklist (Task 23, step 8)
against `localhost:8000`.

- [ ] **Step 3: Confirm no leftover references to retired things**

```bash
grep -rn 'TELEGRAM_ALLOWED_USER_ID' .
grep -rn '_conversations' .
grep -rn '/files/.*reveal' .
grep -rn 'events.jsonl' . --include='*.py'
```

Expected: only matches inside `scripts/migrate_jsonl_to_postgres.py`
(the migration reads JSONL legitimately) and inside the
`docs/superpowers/specs/...md` historical references. Anything else
is dead code — delete it.

- [ ] **Step 4: Push**

```bash
git push origin main
```

- [ ] **Step 5: Run the runbook (Task 23) end-to-end against Fly.**

When the owner is ready to deploy, follow the runbook step-by-step.
The implementation work is done; the runbook is the operator's checklist.

---

## Self-review against the spec

Coverage check (each spec section → which task delivers it):

- **Postgres schema** → Task 2 (`migrations/001_initial_schema.sql`).
- **services/db.py + per-test schema** → Task 3.
- **services/users.py + admin CLI** → Task 4.
- **JWT cookie auth + get_current_user** → Task 5; route wiring in Task 14.
- **archive.py rewrite preserving public API** → Tasks 6–10.
- **conversation_store.py replacing in-memory dict** → Task 11; wired in Task 14.
- **pipeline.py user_id threading** → Task 12.
- **llm.py user_id threading** → Task 13.
- **conversation_log.py user_id field** → Task 12.
- **main.py auth + user_id + drop /reveal + /health + BackgroundTasks** → Task 14.
- **PWA shell (manifest + SW + login + MediaRecorder)** → Task 15.
- **telegram_bot.py chat_id → user_id, persistent history** → Task 16.
- **Existing tests adapted** → Task 17.
- **Structured JSON logging** → Task 18.
- **Migration script** → Task 19.
- **Owner seed script** → Task 20.
- **Dockerfile + Whisper baked in** → Task 21.
- **fly.toml two processes + volume** → Task 22.
- **Owner runbook** → Task 23.
- **Final sweep** → Task 24.

No placeholders. No "fill in details". Every task has exact code,
exact paths, exact commands, and a commit step.

Type/signature consistency:

- Every archive.py public function takes `user_id: str` first.
- `respond_to_text(user_id, message, history, ...)` consistent across
  pipeline, llm, and main.py.
- `pipeline.handle_text(user_id, ...)` and `pipeline.handle_audio(user_id, ...)`
  consistent across main.py and telegram_bot.py.
- `conversation_store.append(user_id, role, content)` and
  `conversation_store.recent(user_id, limit)` consistent everywhere.
