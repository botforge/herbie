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
    """
    1. Insert a user.
    2. Try to insert the same username again.
    3. UserAlreadyExists fires; the second row never lands.
    """
    users.create_user(username="bob", password="x")
    with pytest.raises(users.UserAlreadyExists):
        users.create_user(username="bob", password="x")


def test_set_telegram_links_chat(db):
    users.create_user(username="cara", password="x")
    users.set_telegram_chat_id("cara", 999)
    row = users.get_user_by_telegram(999)
    assert row["user_id"] == "cara"


def test_get_user_by_telegram_unknown_returns_none(db):
    """
    1. With no users in the table, look up an arbitrary chat id.
    2. The function returns None — never KeyError, never raises.
    """
    assert users.get_user_by_telegram(123456) is None


def test_verify_password_corrupt_hash_returns_false(db):
    """
    1. Insert a user, then directly corrupt their password_hash to
       garbage that does not parse as argon2.
    2. verify_password must return False, not raise.
    """
    from services import db as _db
    users.create_user(username="dent", password="don't panic")
    _db.execute(
        "UPDATE users SET password_hash = %s WHERE user_id = %s",
        ("not-a-real-hash", "dent"),
    )
    assert users.verify_password("dent", "don't panic") is False


def test_verify_password_unknown_user_returns_false(db):
    """
    1. With no users in the table, verify any password.
    2. Must return False, not raise. Sentinel hash path is exercised.
    """
    assert users.verify_password("ghost", "anything") is False
