"""
User CRUD + admin CLI.

How to read this module:

  1. Public API wraps the users table — create, verify, link Telegram,
     change password, fetch by user_id or chat_id, list all rows.
  2. Passwords are hashed with argon2id via argon2-cffi. The default
     PasswordHasher settings are conservative (64 MB memory, 3 iterations).
  3. The admin CLI is the only write path into this table; there is no
     public registration UI.

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
from argon2.exceptions import InvalidHashError, VerifyMismatchError

import psycopg.errors as _pgerrors

from services import db as _db


_hasher = PasswordHasher()

# A real argon2id hash of a fixed value. Used only as a constant-time
# decoy when verify_password is called for a username that does not
# exist — the verify call still runs the full argon2 cost so callers
# cannot tell missing-user from wrong-password by response time.
_SENTINEL_HASH = _hasher.hash("__lila_sentinel__")


class UserAlreadyExists(Exception):
    pass


class UserNotFound(Exception):
    pass


def create_user(username: str, password: str,
                telegram_chat_id: int | None = None) -> str:
    """
    1. user_id is the username for now (slug shape).
    2. Hash with argon2id (defaults are conservative: 64MB, 3 iters).
    3. INSERT — duplicate username/user_id is mapped to
       UserAlreadyExists. A duplicate telegram_chat_id propagates as
       a clear ValueError naming the conflicting field; callers
       (admin CLI) surface that to the operator without mapping it
       to UserAlreadyExists, which would be misleading.
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
    except _pgerrors.UniqueViolation as e:
        cname = (getattr(getattr(e, "diag", None), "constraint_name", "") or "")
        if cname in ("users_pkey", "users_username_key"):
            raise UserAlreadyExists(username) from e
        if cname == "users_telegram_chat_id_key":
            raise ValueError(
                f"telegram_chat_id={telegram_chat_id} is already linked to another user"
            ) from e
        raise
    return user_id


def verify_password(username: str, plaintext: str) -> bool:
    """
    1. Look up the stored hash for the username.
    2. Hand it and the plaintext to argon2 for a constant-time check.
    3. Run a sentinel hash when the user does not exist so the
       function takes argon2's full cost regardless — denies a
       username-enumeration timing oracle to a remote attacker.
    4. Both VerifyMismatchError (wrong password) and InvalidHashError
       (corrupted stored hash) collapse to False.
    """
    row = _db.fetch_one(
        "SELECT password_hash FROM users WHERE username = %s", (username,),
    )
    stored = row["password_hash"] if row else _SENTINEL_HASH
    try:
        ok = _hasher.verify(stored, plaintext)
        return ok and row is not None
    except (VerifyMismatchError, InvalidHashError):
        return False


def set_telegram_chat_id(user_id: str, chat_id: int) -> None:
    """
    1. UPDATE the telegram_chat_id column for the given user_id.
    2. The column has a UNIQUE constraint so it can't be linked to two users.
    3. Calling again with the same chat_id is idempotent (no-op UPDATE).
    """
    _db.execute(
        "UPDATE users SET telegram_chat_id = %s WHERE user_id = %s",
        (chat_id, user_id),
    )


def set_password(user_id: str, new_password: str) -> None:
    """
    1. Hash the new plaintext with argon2id.
    2. UPDATE the stored hash for the given user_id.
    """
    pwd_hash = _hasher.hash(new_password)
    _db.execute(
        "UPDATE users SET password_hash = %s WHERE user_id = %s",
        (pwd_hash, user_id),
    )


def get_user(user_id: str) -> dict | None:
    """
    1. SELECT the public columns (no password_hash) for the given user_id.
    2. Return the dict row, or None if not found.
    """
    return _db.fetch_one(
        "SELECT user_id, username, telegram_chat_id, created_at FROM users WHERE user_id = %s",
        (user_id,),
    )


def get_user_by_telegram(chat_id: int) -> dict | None:
    """
    1. SELECT the public columns for the row whose telegram_chat_id matches.
    2. Return the dict row, or None if no user has that chat linked.
    """
    return _db.fetch_one(
        "SELECT user_id, username, telegram_chat_id, created_at FROM users WHERE telegram_chat_id = %s",
        (chat_id,),
    )


def list_users() -> list[dict]:
    """
    1. SELECT all users, public columns only, ordered by user_id.
    2. Return as a list of dicts (empty list if the table is empty).
    """
    return _db.fetch_all(
        "SELECT user_id, username, telegram_chat_id, created_at FROM users ORDER BY user_id"
    )


# ── admin CLI ────────────────────────────────────────────────────────────────

def _cli() -> int:
    """
    1. Build an argparse tree with four sub-commands: create, set-telegram,
       set-password, list.
    2. Dispatch to the matching service function and print a confirmation.
    3. Return 0 on success, 1 on expected errors (UserAlreadyExists or a
       ValueError such as duplicate telegram_chat_id), 2 if no sub-command
       matched (shouldn't happen with required=True, kept as a safety valve).
    """
    p = argparse.ArgumentParser(prog="python -m services.users")
    sub = p.add_subparsers(dest="cmd", required=True)

    # 1A. create — inserts a new user row.
    pc = sub.add_parser("create")
    pc.add_argument("--username", required=True)
    pc.add_argument("--password", required=True)
    pc.add_argument("--telegram-chat-id", type=int, default=None)

    # 1B. set-telegram — links a Telegram chat_id to an existing user.
    pt = sub.add_parser("set-telegram")
    pt.add_argument("--username", required=True)
    pt.add_argument("--chat-id", type=int, required=True)

    # 1C. set-password — replaces the stored password hash.
    pp = sub.add_parser("set-password")
    pp.add_argument("--username", required=True)
    pp.add_argument("--password", required=True)

    # 1D. list — prints every user row (no password hash shown).
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
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
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
