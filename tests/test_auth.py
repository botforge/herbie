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

os.environ.setdefault("LILA_JWT_SECRET", "test-secret-do-not-use-in-prod-32b")

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
