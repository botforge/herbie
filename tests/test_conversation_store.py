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
