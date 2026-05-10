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


# ── Forward stubs — reimplemented in Tasks 9–10 ──────────────────────────────
# These names are imported at module level by services/render.py,
# services/jobs.py, and services/pipeline.py. Defining them here as
# NotImplementedError stubs keeps those modules importable (so pytest
# can collect every test file) while ensuring any call to them fails
# loudly — the intended red state for Tasks 9–10.
# ingest_text (Task 8) is fully implemented above; only complete_job,
# get_slug_version, stage_audio, and commit_audio remain as stubs.

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


def complete_job(*args, **kwargs):
    raise NotImplementedError("archive.complete_job not yet reimplemented (Task 9)")


def get_slug_version(*args, **kwargs):
    raise NotImplementedError("archive.get_slug_version not yet reimplemented (Task 10)")


def stage_audio(*args, **kwargs):
    raise NotImplementedError("archive.stage_audio not yet reimplemented (Task 10)")


def commit_audio(*args, **kwargs):
    raise NotImplementedError("archive.commit_audio not yet reimplemented (Task 10)")


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
    """Counts each tag across the user's live (non-deleted) entries."""
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
    """Case-insensitive substring match across slug, tags, transcript, text."""
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
