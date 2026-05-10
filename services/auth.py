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
    response.delete_cookie(
        key=_COOKIE_NAME, path="/",
        httponly=True, secure=True, samesite="lax",
    )


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
