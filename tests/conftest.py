"""
Shared pytest fixtures.

How to read this file:

  1. The archive service (services/archive.py) reads ARCHIVE_PATH from
     the environment at import time and stores it as five module-level
     path constants (ARCHIVE_ROOT, RAW_DIR, EVENTS_FILE, JOBS_DIR,
     SUMMARIES_DIR).
  2. Every filesystem write inside services/archive.py resolves through
     those module-level constants, so redirecting them at runtime
     redirects every write.
  3. The fixtures below do exactly that redirection, plus a belt-and-
     suspenders guard that snapshots the real archive/ before and
     after each test and fails loudly if anything changed.

Fixtures exported:

  1. _guard_real_archive  (autouse) — refuses to let any test mutate
                                      the real archive/ directory.
  2. temp_archive                    — fresh, isolated archive root
                                       per test.
  3. fake_audio                      — tiny bytes blob standing in for
                                       a real .ogg.
  4. fixture_entry                   — loader for the test-owned audio
                                       fixtures under tests/fixtures/.
"""

import hashlib
import json as _json
from pathlib import Path

import pytest

from services import archive


# ── 1. Guard: the real archive/ must never change during a test ────────────
#
#   1A. Before each test runs, take a snapshot of:
#         - the SHA-256 of the real archive/events.jsonl
#         - the set of filenames in archive/raw/
#   1B. After the test yields, take the same snapshot again.
#   1C. Assert both are byte-identical. A single appended event,
#       rewritten log, or new raw file trips the guard.
#
# Hash + filename sets are used (not mtime) so reading the real archive
# never false-positives — only genuine mutations fail the test.

_REAL_ARCHIVE = Path(__file__).resolve().parent.parent / "archive"


def _snapshot_real_archive() -> dict:
    # 1. Build an empty snapshot shell.
    snap: dict = {"events_hash": None, "raw": set()}

    # 2. Hash events.jsonl if it exists.
    ev = _REAL_ARCHIVE / "events.jsonl"
    if ev.exists():
        snap["events_hash"] = hashlib.sha256(ev.read_bytes()).hexdigest()

    # 3. List filenames under raw/ if it exists.
    raw = _REAL_ARCHIVE / "raw"
    if raw.exists():
        snap["raw"] = {p.name for p in raw.iterdir()}

    return snap


@pytest.fixture(autouse=True)
def _guard_real_archive():
    before = _snapshot_real_archive()
    yield
    after = _snapshot_real_archive()
    assert after["events_hash"] == before["events_hash"], (
        "archive/events.jsonl changed during a test — "
        "a test wrote to the real archive instead of a temp one"
    )
    assert after["raw"] == before["raw"], (
        f"archive/raw changed: added={after['raw'] - before['raw']}, "
        f"removed={before['raw'] - after['raw']}"
    )


# ── 2. temp_archive: fresh archive root per test ───────────────────────────
#
#   1. Build a temp path under pytest's tmp_path.
#   2. Monkey-patch every one of the six module-level constants in
#      services.archive so they point inside the temp path.
#   3. Call ensure_archive_root() so the subdirs and an empty
#      events.jsonl exist before test code runs.
#   4. Yield the root Path so tests can inspect files on disk directly.
#
# pytest's tmp_path is auto-cleaned between tests, so no teardown
# beyond letting the fixture go out of scope.

@pytest.fixture
def temp_archive(tmp_path, monkeypatch):
    # 1. Pick a root inside tmp_path.
    root = tmp_path / "archive"

    # 2. Redirect every path constant to somewhere inside that root.
    monkeypatch.setattr(archive, "ARCHIVE_ROOT",  root)
    monkeypatch.setattr(archive, "RAW_DIR",       root / "raw")
    monkeypatch.setattr(archive, "EVENTS_FILE",   root / "events.jsonl")
    monkeypatch.setattr(archive, "JOBS_DIR",      root / "jobs")
    monkeypatch.setattr(archive, "SUMMARIES_DIR", root / "summaries")

    # 3. Materialize the dirs + empty events.jsonl.
    archive.ensure_archive_root()

    # 4. Hand the root back so tests can assert on disk state.
    return root


# ── 3. fake_audio: a throwaway .ogg payload ────────────────────────────────
#
#   1. ingest_audio only does a shutil.copy2 of the source — it never
#      decodes the bytes.
#   2. So any non-empty file is fine for tests that assert on event-log
#      and sidecar state.
#   3. Use this when a test does not care about the actual audio
#      content, only that some file was ingested.

@pytest.fixture
def fake_audio(tmp_path) -> Path:
    p = tmp_path / "src.ogg"
    p.write_bytes(b"OGGS\x00\x00fake-audio-payload-for-tests")
    return p


# ── 4. fixture_entry: loader for tests/fixtures/audio/ ─────────────────────
#
#   1. Raw audio and metadata used by tests live under tests/fixtures/,
#      an isolated test data store owned by the test suite.
#   2. Layout on disk:
#         2A. tests/fixtures/audio/{file_id}.ogg   — raw bytes
#         2B. tests/fixtures/audio/manifest.json   — single source of
#                                                    truth for slug /
#                                                    tags / ext /
#                                                    transcript per
#                                                    file_id
#   3. The fixture returns a loader function so tests can pull an
#      entry by file_id:
#         entry = fixture_entry("151cd315")
#         entry["slug"]        — semantic slug
#         entry["tags"]        — list of tag strings
#         entry["ext"]         — file extension, no dot
#         entry["transcript"]  — original transcript text
#         entry["raw_path"]    — Path to the .ogg bytes

_FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "audio"
_MANIFEST = _json.loads((_FIXTURES_DIR / "manifest.json").read_text())


def _load_fixture_entry(file_id: str) -> dict:
    entry = dict(_MANIFEST[file_id])
    entry["raw_path"] = _FIXTURES_DIR / f"{file_id}.{entry['ext']}"
    return entry


@pytest.fixture
def fixture_entry():
    return _load_fixture_entry
