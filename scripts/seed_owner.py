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
    target = Path(os.environ.get("LILA_INITIAL_PASSWORD_FILE", "/data/initial_password.txt"))
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
