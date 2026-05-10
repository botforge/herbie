"""
Verifies that the per-test DB fixtures work end-to-end.

How to read this file:

  1. `db` truncates all domain tables so each test starts clean.
  2. `seed_user` inserts a minimal user row and returns the user_id.
  3. fetch_one retrieves the row and confirms the round-trip through
     psycopg's dict_row factory.
"""


def test_seed_user_creates_row(db, seed_user):
    from services import db as _db
    uid = seed_user("u_alpha")
    row = _db.fetch_one("SELECT user_id FROM users WHERE user_id = %s", (uid,))
    assert row["user_id"] == "u_alpha"
