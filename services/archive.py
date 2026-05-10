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
from datetime import datetime
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


# ── Forward stubs — reimplemented in Tasks 7–10 ──────────────────────────────
# These names are imported at module level by services/render.py,
# services/jobs.py, and services/pipeline.py. Defining them here as
# NotImplementedError stubs keeps those modules importable (so pytest
# can collect every test file) while ensuring any call to them fails
# loudly — the intended red state for Tasks 6–9.

def current_entry(*args, **kwargs):
    raise NotImplementedError("archive.current_entry not yet reimplemented (Task 7)")


def ingest_audio(*args, **kwargs):
    raise NotImplementedError("archive.ingest_audio not yet reimplemented (Task 7)")


def ingest_text(*args, **kwargs):
    raise NotImplementedError("archive.ingest_text not yet reimplemented (Task 8)")


def complete_job(*args, **kwargs):
    raise NotImplementedError("archive.complete_job not yet reimplemented (Task 9)")


def get_slug_version(*args, **kwargs):
    raise NotImplementedError("archive.get_slug_version not yet reimplemented (Task 10)")


def stage_audio(*args, **kwargs):
    raise NotImplementedError("archive.stage_audio not yet reimplemented (Task 7)")


def commit_audio(*args, **kwargs):
    raise NotImplementedError("archive.commit_audio not yet reimplemented (Task 7)")
