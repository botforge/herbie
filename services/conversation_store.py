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
