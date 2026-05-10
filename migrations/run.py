"""
Apply every migrations/*.sql file in lexical order, skipping versions
already recorded in schema_migrations.

How to read this file:

  1. Connect to DATABASE_URL.
  2. Bootstrap the schema_migrations table so the runner works even on a
     brand-new database (the first migration also creates it, but we need
     the table before we can query it).
  3. Read the set of already-applied versions from schema_migrations.
  4. For each *.sql file (sorted lexically):
     4A. Derive the version from the filename stem
         (e.g. "001_initial_schema.sql" → "001_initial_schema").
     4B. If the version is already in the applied set, print
         "skipping <file> (already applied)" and continue.
     4C. Otherwise run the file inside its own transaction, then INSERT
         the version into schema_migrations with ON CONFLICT DO NOTHING
         so that files which self-record (like 001) don't cause errors.
  5. Print "done." and exit 0.
"""

import os
import sys
from pathlib import Path

import psycopg


def main() -> int:
    # 1. Validate DATABASE_URL before doing anything else.
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
        # 2. Bootstrap schema_migrations so we can query it even on a
        #    fresh database that has never had any migration run.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version     TEXT PRIMARY KEY,
                applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        conn.commit()

        # 3. Read already-applied versions into a set for O(1) lookup.
        applied = {
            row[0]
            for row in conn.execute("SELECT version FROM schema_migrations")
        }

        # 4. Walk each SQL file in lexical order.
        for f in files:
            # 4A. Version is the filename without the .sql extension.
            version = f.stem

            # 4B. Skip if this version was already recorded.
            if version in applied:
                print(f"skipping {f.name} (already applied)")
                continue

            # 4C. Run the file in its own transaction, then record the
            #     version. ON CONFLICT DO NOTHING handles files that
            #     self-insert (e.g. 001_initial_schema.sql).
            print(f"applying {f.name}…")
            with conn.transaction():
                conn.execute(f.read_text())
                conn.execute(
                    "INSERT INTO schema_migrations (version) VALUES (%s)"
                    " ON CONFLICT (version) DO NOTHING",
                    (version,),
                )

    print("done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
