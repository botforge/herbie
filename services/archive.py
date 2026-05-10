"""
Archive service — Postgres-backed event log + per-user volume audio.

How to read this module:

  1. Postgres tables (see migrations/001_initial_schema.sql):
     1A. events              one row per audio/text/delete/job_*
     1B. jobs                one row per queued / done job
     1C. last_action         one row per user — the single-step undo buffer
  2. Volume layout under VOLUME_ROOT:
     2A. <user_id>/raw/<file_id>.<ext>   raw audio bytes
     2B. <user_id>/raw/<file_id>.txt     text payloads (kept on disk
                                         so legacy callers reading
                                         {file_id}.txt still work)
     2C. <user_id>/raw/<file_id>.mid     materialized MIDI on first
                                         reveal-style download
  3. Public API: every function takes user_id as the first
     parameter. Internals filter rows by user_id everywhere; cross-
     user reads are impossible at the SQL layer.
  4. The chat surface remains append-only. update_files_meta is
     only invoked by the web PATCH route, snapshots the prior rows
     into last_action, and supports a single-step undo via
     undo_last_action.
"""

import json
import os
import secrets
import shutil
from pathlib import Path

from dotenv import load_dotenv

from services import db as _db

load_dotenv()

VOLUME_ROOT = Path(os.getenv("ARCHIVE_PATH", "./archive"))


# ── Path helpers ─────────────────────────────────────────────────────────────

def _user_dir(user_id: str) -> Path:
    return VOLUME_ROOT / user_id


def _raw_dir(user_id: str) -> Path:
    return _user_dir(user_id) / "raw"


def ensure_user_dirs(user_id: str) -> None:
    """
    1. Create <volume>/<user_id>/raw/ if it does not exist.
    2. Idempotent — safe to call from every ingest path.
    """
    _raw_dir(user_id).mkdir(parents=True, exist_ok=True)


def _new_id() -> str:
    return secrets.token_hex(4)


# ── Module-level back-compat constants ───────────────────────────────────────
# Tests in conftest.py monkeypatch ARCHIVE_ROOT / RAW_DIR to a temp
# path. These constants stay defined so the existing _guard_real_archive
# fixture and any callers that import them keep working.
ARCHIVE_ROOT = VOLUME_ROOT
RAW_DIR      = VOLUME_ROOT
EVENTS_FILE  = VOLUME_ROOT / ".events_legacy_unused"
JOBS_DIR     = VOLUME_ROOT / ".jobs_legacy_unused"
SUMMARIES_DIR = VOLUME_ROOT / "summaries"


def ensure_archive_root() -> None:
    """Back-compat shim. Per-user dirs are created lazily by ingest."""
    VOLUME_ROOT.mkdir(parents=True, exist_ok=True)
    SUMMARIES_DIR.mkdir(parents=True, exist_ok=True)


# ── Text ingest ──────────────────────────────────────────────────────────────

def ingest_text(
    user_id: str,
    slug: str,
    tags: list[str],
    text: str,
    parent_id: str | None = None,
    midi_notes: str | None = None,
) -> dict:
    """
    1. Inherit parent tags when parent_id is given.
    2. Persist the user-visible text (or midi notes when present)
       to <volume>/<user>/raw/<fid>.txt so legacy file-serving
       routes that look up {file_id}.txt continue to work.
    3. INSERT one text event row.
    """
    ensure_user_dirs(user_id)
    if parent_id:
        parent = current_entry(user_id, parent_id)
        inherited = parent.get("tags", [])
        tags = inherited + [t for t in tags if t not in inherited]

    file_id = _new_id()
    (_raw_dir(user_id) / f"{file_id}.txt").write_text(midi_notes or text)

    event_id = _new_id()
    with _db.connect() as conn:
        cur = conn.execute(
            """INSERT INTO events
               (event_id, user_id, type, file_id, slug, tags,
                text, midi_notes, ext, parent_id, job_id)
               VALUES (%s, %s, 'text', %s, %s, %s, %s, %s, 'txt', %s, NULL)
               RETURNING *""",
            (event_id, user_id, file_id, slug, tags, text, midi_notes, parent_id),
        )
        row = cur.fetchone()
    row["id"] = row["file_id"]
    return row


# ── Jobs ─────────────────────────────────────────────────────────────────────

def queue_job(user_id: str, job_type: str, input_file_id: str,
              params: dict | None = None) -> dict:
    """
    1. Insert a job row in 'queued' state.
    2. Append a job_queued event so the feed reflects it.
    """
    job_id = "job_" + _new_id()
    params = params or {}
    with _db.connect() as conn:
        cur = conn.execute(
            """INSERT INTO jobs
               (job_id, user_id, type, status, input_file_id, params)
               VALUES (%s, %s, %s, 'queued', %s, %s)
               RETURNING *""",
            (job_id, user_id, job_type, input_file_id, json.dumps(params)),
        )
        job = cur.fetchone()
        input_tags = []
        ce = current_entry(user_id, input_file_id)
        if ce:
            input_tags = ce.get("tags", [])
        conn.execute(
            """INSERT INTO events
               (event_id, user_id, type, tags, job_id)
               VALUES (%s, %s, 'job_queued', %s, %s)""",
            (_new_id(), user_id, input_tags, job_id),
        )
    return job


def complete_job(user_id: str, job_id: str,
                 output_file_id: str | None = None,
                 output_text: str | None = None) -> None:
    """
    1. Mark the job row as 'done' and record output_file_id + completed_at.
    2. Append a job_done event so the feed reflects completion.
    """
    with _db.connect() as conn:
        conn.execute(
            """UPDATE jobs
               SET status = 'done', output_file_id = %s, completed_at = now()
               WHERE user_id = %s AND job_id = %s""",
            (output_file_id, user_id, job_id),
        )
        conn.execute(
            """INSERT INTO events
               (event_id, user_id, type, file_id, job_id, text)
               VALUES (%s, %s, 'job_done', %s, %s, %s)""",
            (_new_id(), user_id, output_file_id, job_id, output_text),
        )


def get_jobs(user_id: str, status: str | None = None) -> list[dict]:
    """
    1. If status is given, filter to that status; otherwise return all jobs.
    2. Return newest-first.
    """
    if status:
        rows = _db.fetch_all(
            """SELECT * FROM jobs WHERE user_id = %s AND status = %s
               ORDER BY created_at DESC""",
            (user_id, status),
        )
    else:
        rows = _db.fetch_all(
            "SELECT * FROM jobs WHERE user_id = %s ORDER BY created_at DESC",
            (user_id,),
        )
    return [dict(r) for r in rows]


# ── Staging (audio that may or may not be committed) ─────────────────────────

def stage_audio(user_id: str, src_path: str, ext: str) -> tuple[str, Path]:
    """
    1. Generate file_id and copy bytes to <volume>/<user>/raw/.
    2. Do NOT insert an event yet — caller decides whether to keep
       it (see commit_audio) based on the LLM's tool choice.
    """
    ensure_user_dirs(user_id)
    file_id = _new_id()
    ext = ext.lstrip(".")
    out = _raw_dir(user_id) / f"{file_id}.{ext}"
    shutil.copy2(str(src_path), str(out))
    return file_id, out


def commit_audio(user_id: str, file_id: str, slug: str, tags: list[str],
                 ext: str, transcript: str = "") -> dict:
    """
    Insert the audio event for a previously-staged file. Pairs with
    stage_audio — used by services/pipeline.py:handle_audio.

    1. Strip any leading dot from ext so the stored value is bare (e.g. 'ogg').
    2. INSERT one audio event row with the caller-supplied file_id
       RETURNING * so the caller sees the canonical shape.
    3. Alias id ← file_id for back-compat with legacy call sites.
    """
    ext = ext.lstrip(".")
    with _db.connect() as conn:
        cur = conn.execute(
            """INSERT INTO events
               (event_id, user_id, type, file_id, slug, tags,
                transcript, ext, parent_id, job_id)
               VALUES (%s, %s, 'audio', %s, %s, %s, %s, %s, NULL, NULL)
               RETURNING *""",
            (_new_id(), user_id, file_id, slug, tags, transcript, ext),
        )
        row = cur.fetchone()
    row["id"] = row["file_id"]
    return row


# ── Helper: how many entries already use this slug ──────────────────────────

def get_slug_version(user_id: str, slug: str) -> int:
    """
    1. COUNT all audio/text events for this (user, slug) pair.
    2. Return the integer count — callers use this as a version
       suffix (e.g. slug-v2) to avoid duplicate slugs.
    """
    row = _db.fetch_one(
        """SELECT COUNT(*) AS n FROM events
           WHERE user_id = %s AND slug = %s
             AND type IN ('audio','text')""",
        (user_id, slug),
    )
    return int(row["n"]) if row else 0


# ── Audio ingest ─────────────────────────────────────────────────────────────

def ingest_audio(
    user_id: str,
    src_path: str,
    slug: str,
    tags: list[str],
    ext: str,
    transcript: str = "",
    parent_id: str | None = None,
) -> dict:
    """
    1. Make sure the per-user raw/ directory exists.
    2. If parent_id is given, inherit its tags so derived files
       carry their lineage automatically.
    3. Copy the source bytes to <volume>/<user>/raw/<file_id>.<ext>
       BEFORE inserting the event — the row should never refer to
       a file that does not exist on disk.
    4. INSERT one audio event row and RETURNING * so the caller sees
       the canonical shape (with created_at populated by Postgres).
    """
    ensure_user_dirs(user_id)
    if parent_id:
        parent = current_entry(user_id, parent_id)
        inherited = parent.get("tags", [])
        tags = inherited + [t for t in tags if t not in inherited]

    file_id = _new_id()
    ext = ext.lstrip(".")
    shutil.copy2(str(src_path), str(_raw_dir(user_id) / f"{file_id}.{ext}"))

    event_id = _new_id()
    with _db.connect() as conn:
        cur = conn.execute(
            """INSERT INTO events
               (event_id, user_id, type, file_id, slug, tags,
                transcript, ext, parent_id, job_id)
               VALUES (%s, %s, 'audio', %s, %s, %s, %s, %s, %s, NULL)
               RETURNING *""",
            (event_id, user_id, file_id, slug, tags, transcript, ext, parent_id),
        )
        row = cur.fetchone()
    row["id"] = row["file_id"]
    return row


# ── Reads ────────────────────────────────────────────────────────────────────

def current_entry(user_id: str, file_id: str) -> dict:
    """
    1. Look for a delete event for this (user, file_id). If one
       exists, the entry is gone from the user's perspective →
       return {}.
    2. Otherwise return the audio/text event row for the file_id.
       Empty dict (not None) so callers can chain .get(...).
    3. Alias `id` ← `file_id` so legacy call sites that read
       entry["id"] still work.
    """
    deleted = _db.fetch_one(
        """SELECT 1 FROM events
           WHERE user_id = %s AND file_id = %s AND type = 'delete'
           LIMIT 1""",
        (user_id, file_id),
    )
    if deleted:
        return {}
    row = _db.fetch_one(
        """SELECT * FROM events
           WHERE user_id = %s AND file_id = %s AND type IN ('audio','text')
           ORDER BY created_at DESC LIMIT 1""",
        (user_id, file_id),
    )
    if not row:
        return {}
    row["id"] = row["file_id"]
    return row


def get_feed(user_id: str, tag: str = "", limit: int = 100, offset: int = 0) -> list[dict]:
    """
    1. Return audio/text events newest-first.
    2. Exclude any file_id that has a delete event.
    3. Optionally filter by tag (array containment).
    """
    if tag:
        sql = """
            SELECT e.* FROM events e
            WHERE e.user_id = %s
              AND e.type IN ('audio','text')
              AND %s = ANY(e.tags)
              AND NOT EXISTS (
                  SELECT 1 FROM events d
                  WHERE d.user_id = e.user_id
                    AND d.file_id = e.file_id
                    AND d.type = 'delete')
            ORDER BY e.created_at DESC
            LIMIT %s OFFSET %s
        """
        rows = _db.fetch_all(sql, (user_id, tag, limit, offset))
    else:
        sql = """
            SELECT e.* FROM events e
            WHERE e.user_id = %s
              AND e.type IN ('audio','text')
              AND NOT EXISTS (
                  SELECT 1 FROM events d
                  WHERE d.user_id = e.user_id
                    AND d.file_id = e.file_id
                    AND d.type = 'delete')
            ORDER BY e.created_at DESC
            LIMIT %s OFFSET %s
        """
        rows = _db.fetch_all(sql, (user_id, limit, offset))
    for r in rows:
        r["id"] = r["file_id"]
    return rows


# ── Tag tally ────────────────────────────────────────────────────────────────

def get_all_tags(user_id: str) -> list[dict]:
    """
    1. UNNEST every live (non-deleted) audio/text entry's tags into rows.
    2. GROUP BY the tag column, count occurrences.
    3. ORDER BY count DESC so the most-used tags lead the list.
    4. Return as a list of {tag, count} dicts — the shape get_feed
       callers and the web tag-cloud expect.
    """
    rows = _db.fetch_all(
        """SELECT tag, COUNT(*) AS count
           FROM (
               SELECT UNNEST(e.tags) AS tag
               FROM events e
               WHERE e.user_id = %s
                 AND e.type IN ('audio','text')
                 AND NOT EXISTS (
                     SELECT 1 FROM events d
                     WHERE d.user_id = e.user_id
                       AND d.file_id = e.file_id
                       AND d.type = 'delete')
           ) t
           GROUP BY tag
           ORDER BY count DESC""",
        (user_id,),
    )
    return [dict(r) for r in rows]


# ── Soft delete ──────────────────────────────────────────────────────────────

def delete_file(user_id: str, file_id: str) -> bool:
    """
    1. Confirm the entry is currently live; if not, return False
       (already deleted or never existed).
    2. INSERT a delete event so future reads filter the entry out.
       The raw bytes on disk are intentionally left in place so a
       manual restore is still possible by removing the delete row.
    """
    if not current_entry(user_id, file_id):
        return False
    _db.execute(
        """INSERT INTO events (event_id, user_id, type, file_id)
           VALUES (%s, %s, 'delete', %s)""",
        (_new_id(), user_id, file_id),
    )
    return True


# ── Search ───────────────────────────────────────────────────────────────────

def search(user_id: str, query: str) -> list[dict]:
    """
    Case-insensitive substring search across slug, transcript, text,
    and individual tags for one user's live entries.

    1. Wrap the query in % on both sides for LIKE matching.
    2. Filter to live (non-deleted) audio/text events for this user.
    3. Match against slug / transcript / text / each tag in turn —
       any hit qualifies the row.
    4. Return newest-first.
    """
    q = f"%{query.lower()}%"
    rows = _db.fetch_all(
        """SELECT e.* FROM events e
           WHERE e.user_id = %s
             AND e.type IN ('audio','text')
             AND NOT EXISTS (
                 SELECT 1 FROM events d
                 WHERE d.user_id = e.user_id
                   AND d.file_id = e.file_id
                   AND d.type = 'delete')
             AND (
                 LOWER(COALESCE(e.slug, ''))       LIKE %s OR
                 LOWER(COALESCE(e.transcript, '')) LIKE %s OR
                 LOWER(COALESCE(e.text, ''))       LIKE %s OR
                 EXISTS (
                     SELECT 1 FROM UNNEST(e.tags) tg
                     WHERE LOWER(tg) LIKE %s)
             )
           ORDER BY e.created_at DESC""",
        (user_id, q, q, q, q),
    )
    for r in rows:
        r["id"] = r["file_id"]
    return rows


# ── In-place edit (web PATCH only) + single-step undo ───────────────────────

def update_files_meta(
    user_id: str,
    file_ids: list[str],
    slug: str | None = None,
    tags: list[str] | None = None,
    transcript: str | None = None,
    text: str | None = None,
) -> int:
    """
    Web PATCH endpoint helper. Mutates rows in place — the chat
    surface still uses append-only file_system_note.

    1. Build the SET clause from supplied fields. If nothing to
       change, return 0.
    2. In one transaction:
       2A. SELECT every targeted live (non-deleted) audio/text row
           into a snapshot list.
       2B. Persist that snapshot to last_action (replacing any prior
           buffer for this user).
       2C. UPDATE the rows with the supplied fields.
    3. Return the number of rows actually changed.
    """
    fields: dict = {}
    if slug       is not None: fields["slug"]       = slug
    if tags       is not None: fields["tags"]       = tags
    if transcript is not None: fields["transcript"] = transcript
    if text       is not None: fields["text"]       = text
    if not fields or not file_ids:
        return 0

    set_sql = ", ".join(f"{k} = %s" for k in fields)
    params = list(fields.values())

    with _db.connect() as conn:
        snap_rows = conn.execute(
            """SELECT * FROM events
               WHERE user_id = %s
                 AND file_id = ANY(%s)
                 AND type IN ('audio','text')
                 AND NOT EXISTS (
                     SELECT 1 FROM events d
                     WHERE d.user_id = events.user_id
                       AND d.file_id = events.file_id
                       AND d.type = 'delete')""",
            (user_id, file_ids),
        ).fetchall()
        if not snap_rows:
            return 0

        snapshots = [_serialize_row_for_snapshot(r) for r in snap_rows]
        conn.execute(
            """INSERT INTO last_action (user_id, snapshots)
               VALUES (%s, %s)
               ON CONFLICT (user_id) DO UPDATE
                 SET snapshots = EXCLUDED.snapshots,
                     updated_at = now()""",
            (user_id, json.dumps(snapshots)),
        )
        upd = conn.execute(
            f"""UPDATE events SET {set_sql}
                WHERE user_id = %s AND file_id = ANY(%s)
                  AND type IN ('audio','text')""",
            (*params, user_id, file_ids),
        )
        return upd.rowcount or 0


def update_file_meta(
    user_id: str,
    file_id: str,
    slug: str | None = None,
    tags: list[str] | None = None,
    transcript: str | None = None,
    text: str | None = None,
) -> bool:
    """
    1. Delegate to update_files_meta with a single-element list.
    2. Return True if at least one row was changed, False otherwise.
    """
    return update_files_meta(
        user_id, [file_id],
        slug=slug, tags=tags, transcript=transcript, text=text,
    ) > 0


def undo_last_action(user_id: str) -> int:
    """
    1. Read the last_action row for the user. None → nothing to do,
       return 0.
    2. For each snapshot, restore the prior column values via UPDATE.
    3. Delete the buffer so subsequent undo calls are no-ops.
    4. Return the number of rows restored.
    """
    with _db.connect() as conn:
        row = conn.execute(
            "SELECT snapshots FROM last_action WHERE user_id = %s", (user_id,),
        ).fetchone()
        if not row:
            return 0
        snapshots = row["snapshots"]
        if isinstance(snapshots, str):
            snapshots = json.loads(snapshots)
        for snap in snapshots:
            conn.execute(
                """UPDATE events SET
                       slug = %s, tags = %s, transcript = %s,
                       text = %s, midi_notes = %s
                   WHERE user_id = %s AND file_id = %s
                     AND type IN ('audio','text')""",
                (
                    snap.get("slug"),
                    snap.get("tags") or [],
                    snap.get("transcript"),
                    snap.get("text"),
                    snap.get("midi_notes"),
                    user_id,
                    snap["file_id"],
                ),
            )
        conn.execute("DELETE FROM last_action WHERE user_id = %s", (user_id,))
        return len(snapshots)


def _serialize_row_for_snapshot(row: dict) -> dict:
    """Pick only the columns we restore on undo. Strip transient fields."""
    return {
        "file_id":    row["file_id"],
        "slug":       row.get("slug"),
        "tags":       row.get("tags") or [],
        "transcript": row.get("transcript"),
        "text":       row.get("text"),
        "midi_notes": row.get("midi_notes"),
    }
