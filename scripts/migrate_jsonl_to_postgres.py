"""
One-shot import of an existing single-user archive into Postgres.

Flow:
1. Read every line of events.jsonl and replay into the events table.
   1A. Legacy 'lyric' events are remapped to 'text' before insertion —
       semantics are identical (lyric body lives in the `text` column),
       and the new chat surface always emits 'text'. This prevents
       silent data loss for archives that predate the rename.
   1B. 'audio' and 'text' events get the full column set.
   1C. 'delete' events get the slim (event_id, user_id, type, file_id) set.
   1D. All other unknown types are skipped.
2. Copy raw audio/attachment files into the per-user volume path.
3. Replay job JSON files from the jobs/ directory into the jobs table.

Run once after deploying. Idempotent: writes a marker file at
<source>/.migrated_to_pg and bails on subsequent runs.

Usage:
  python scripts/migrate_jsonl_to_postgres.py \\
      --user-id dhruv \\
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
    #    'lyric' is remapped to 'text' before insertion (see 1A below).
    inserted = 0
    with _db.connect() as conn, events_file.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            ev = json.loads(line)
            etype = ev.get("type")
            event_id = ev.get("event_id") or _new_id()
            # 1A. Legacy 'lyric' events become 'text' rows. The semantics
            # are identical: they hold the user's lyric body in the
            # `text` column. The new chat surface always emits 'text';
            # this mapping prevents data loss on import.
            if etype == "lyric":
                etype = "text"
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

    marker.write_text("done")
    print("migration complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
