"""
Archive service — flat, tag-based, event-sourced, append-only.

How to read this module:

  1. Layout on disk under ARCHIVE_ROOT:
     1A. raw/           immutable originals, named {file_id}.{ext}
     1B. events.jsonl   append-only log — the SINGLE source of truth
     1C. jobs/          {job_id}.json per pending / done job
     1D. summaries/     {tag}_summary.md built on demand
  2. Every state change is a new line appended to events.jsonl. No
     event is ever rewritten in place. Soft delete, retag, rename,
     transcript fix — all of them are new "correction" or "delete"
     events that fold over the original.
  3. Reads (get_feed, get_all_tags, current_entry) replay the log to
     compute the live view. The fold is the only place "current
     state" exists; there is no shadow store.

Core API:
  ingest_audio(src_path, slug, tags, ext, transcript, parent_id) → event
  ingest_text(slug, tags, text, parent_id)                       → event
  apply_correction(file_id, slug?, tags?, transcript?)           → event | None
  current_entry(file_id)                                         → dict
  get_feed(tag, limit, offset)                                   → list[dict]
  get_all_tags()                                                 → list[dict]
  delete_file(file_id)                                           → bool
  queue_job(job_type, input_file_id, params)                     → dict
  complete_job(job_id, output_file_id, output_text)              → None
  get_jobs(status)                                               → list[dict]
  ensure_archive_root()                                          → None
  migrate_v1()                                                   → int

Compat wrappers (telegram_bot.py / cli.py unchanged):
  file_audio / file_lyrics / get_next_version
  get_projects / get_project_files
"""

import json
import os
import re
import secrets
import shutil
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ARCHIVE_ROOT  = Path(os.getenv("ARCHIVE_PATH", "./archive"))
RAW_DIR       = ARCHIVE_ROOT / "raw"
EVENTS_FILE   = ARCHIVE_ROOT / "events.jsonl"
JOBS_DIR      = ARCHIVE_ROOT / "jobs"
SUMMARIES_DIR = ARCHIVE_ROOT / "summaries"

# Tags that are content descriptors, not song/project names
_META_TAGS = {
    "lyric", "audio", "vocal-memo", "draft", "sketch", "loop", "drone",
    "foley", "midi", "stem", "summary", "job-output", "voice-note", "sample",
    "organic", "harsh", "warm", "granular", "glitchy", "sparse", "dense",
    "op1", "field-recording", "youtube", "synth", "vocal", "guitar",
    "raw", "repetition", "uncertainty", "lyrics", "hook", "melody",
}


def ensure_archive_root():
    """
    1. Create the on-disk skeleton (raw/, jobs/, summaries/) and an
       empty events.jsonl if it does not exist.
    2. If a legacy sidecars/ directory survives from a prior version
       of the codebase, replay any sidecars that have no matching
       audio/text event into events.jsonl. This is idempotent — once
       every sidecar has an event, the bootstrap returns 0 and this
       call becomes a no-op for fresh archives.
    """
    for d in (RAW_DIR, JOBS_DIR, SUMMARIES_DIR):
        d.mkdir(parents=True, exist_ok=True)
    if not EVENTS_FILE.exists():
        EVENTS_FILE.touch()
    _bootstrap_events_from_orphan_sidecars()


def _bootstrap_events_from_orphan_sidecars() -> int:
    """
    1. Locate the legacy archive/sidecars/ directory. If it does not
       exist, this archive is already on the events-only model — return.
    2. Build the set of file_ids that already have an audio/text event.
    3. For each sidecar with no matching event, reconstruct an event
       from the sidecar contents and append it. The fields mapped:
         3A. file_id ← sidecar.id (or filename stem as fallback)
         3B. type, slug, tags, transcript, ext, parent_id, job_id,
             created_at copied verbatim
         3C. midi_notes / text copied if present
    4. Return the count of bootstrapped events so callers (or the
       startup log) can see whether work was done.
    """
    legacy_dir = ARCHIVE_ROOT / "sidecars"
    if not legacy_dir.exists():
        return 0
    have_events: set[str] = {
        ev.get("file_id") for ev in _read_events()
        if ev.get("type") in ("audio", "text")
    }
    bootstrapped = 0
    for sc_path in sorted(legacy_dir.iterdir()):
        if sc_path.suffix != ".json":
            continue
        try:
            sc = json.loads(sc_path.read_text())
        except Exception:
            continue
        fid = sc.get("id") or sc_path.stem
        if fid in have_events:
            continue
        ev = {
            "event_id":   _new_id(),
            "type":       sc.get("type", "audio"),
            "file_id":    fid,
            "slug":       sc.get("slug"),
            "tags":       sc.get("tags", []),
            "transcript": sc.get("transcript", ""),
            "ext":        sc.get("ext", "ogg"),
            "parent_id":  sc.get("parent_id"),
            "job_id":     sc.get("job_id"),
            "created_at": sc.get("created_at", datetime.now().isoformat()),
        }
        if "midi_notes" in sc:
            ev["midi_notes"] = sc["midi_notes"]
        if "text" in sc:
            ev["text"] = sc["text"]
        _append_event(ev)
        bootstrapped += 1
    return bootstrapped


# ── IDs ─────────────────────────────────────────────────────────────────────

def _new_id() -> str:
    return secrets.token_hex(4)


# ── Event log ────────────────────────────────────────────────────────────────

def _append_event(event: dict) -> None:
    with EVENTS_FILE.open("a") as f:
        f.write(json.dumps(event) + "\n")


def _read_events() -> list[dict]:
    if not EVENTS_FILE.exists():
        return []
    events = []
    with EVENTS_FILE.open() as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except Exception:
                    pass
    return events


# ── Audio ingest ─────────────────────────────────────────────────────────────

def ingest_audio(
    src_path,
    slug: str,
    tags: list[str],
    ext: str,
    transcript: str = "",
    parent_id: str | None = None,
) -> dict:
    ensure_archive_root()
    if parent_id:
        inherited = current_entry(parent_id).get("tags", [])
        tags = inherited + [t for t in tags if t not in inherited]

    file_id = _new_id()
    ext = ext.lstrip(".")
    shutil.copy2(str(src_path), str(RAW_DIR / f"{file_id}.{ext}"))

    now = datetime.now().isoformat()
    event = {"event_id": _new_id(), "type": "audio", "file_id": file_id,
             "slug": slug, "tags": tags, "transcript": transcript,
             "ext": ext, "parent_id": parent_id, "job_id": None,
             "created_at": now}
    _append_event(event)
    return event


# ── Text ingest ──────────────────────────────────────────────────────────────

def ingest_text(
    slug: str,
    tags: list[str],
    text: str,
    parent_id: str | None = None,
    midi_notes: str | None = None,
) -> dict:
    """
    Create a text entry. `text` is what the user sees (description / lyrics).
    `midi_notes` — optional raw NOTE list, used by the UI piano-roll canvas
    and kept separate from the displayed body.
    """
    ensure_archive_root()
    if parent_id:
        inherited = current_entry(parent_id).get("tags", [])
        tags = inherited + [t for t in tags if t not in inherited]

    file_id = _new_id()
    # Raw file keeps the canonical payload: notes if provided, else the text
    (RAW_DIR / f"{file_id}.txt").write_text(midi_notes or text)

    now = datetime.now().isoformat()
    event = {"event_id": _new_id(), "type": "text", "file_id": file_id,
             "slug": slug, "tags": tags, "text": text, "ext": "txt",
             "parent_id": parent_id, "job_id": None, "created_at": now}
    if midi_notes is not None:
        event["midi_notes"] = midi_notes
    _append_event(event)
    return event


# ── Feed ─────────────────────────────────────────────────────────────────────

def _deleted_ids(all_events: list[dict]) -> set[str]:
    return {e.get("file_id") for e in all_events
            if e.get("type") == "delete" and e.get("file_id")}


def _fold_corrections(all_events: list[dict]) -> dict[str, dict]:
    """
    Replay correction events to compute the current state of each entry.

    1. Walk events in the order they were written (oldest first — that
       is the natural append order in events.jsonl).
    2. For each correction event, look up the per-file_id state we are
       building and overwrite only the dimensions the correction
       carried (slug / tags / transcript).
    3. Return a map { file_id: {slug?, tags?, transcript?} } that
       callers can layer on top of the original audio/text event to
       produce the folded view.
    """
    overrides: dict[str, dict] = {}
    for ev in all_events:
        if ev.get("type") != "correction":
            continue
        fid = ev.get("file_id")
        if not fid:
            continue
        cur = overrides.setdefault(fid, {})
        for field in ("slug", "tags", "transcript"):
            if field in ev:
                cur[field] = ev[field]
    return overrides


def current_entry(file_id: str) -> dict:
    """
    Return the folded current state of an entry by file_id.

    1. Read every event in the log.
    2. If the file_id has a delete event, return {} — the entry is
       gone from the user's perspective even if its bytes remain on
       disk.
    3. Find the originating audio/text event for the file_id. If
       none, return {} — the file_id was never ingested.
    4. Layer all corrections on top of the original event in
       event-log order, so the latest user-stated truth wins per
       dimension (slug / tags / transcript).
    5. Alias `id` to `file_id` so legacy call sites that read
       `entry["id"]` keep working without a rename pass.
    """
    all_events = _read_events()
    if file_id in _deleted_ids(all_events):
        return {}
    origin = next(
        (e for e in all_events
         if e.get("file_id") == file_id
         and e.get("type") in ("audio", "text")),
        None,
    )
    if origin is None:
        return {}
    overrides = _fold_corrections(all_events).get(file_id, {})
    folded = dict(origin)
    folded.update(overrides)
    folded["id"] = folded.get("file_id")
    return folded


def _apply_overrides(event: dict, overrides: dict[str, dict]) -> dict:
    """
    Return a shallow copy of `event` with any per-file_id correction
    overrides merged in. Used to fold corrections onto audio/text
    events without mutating the original event dict.
    """
    fid = event.get("file_id")
    patch = overrides.get(fid) if fid else None
    if not patch:
        return event
    folded = dict(event)
    folded.update(patch)
    return folded


def get_feed(tag: str = "", limit: int = 100, offset: int = 0) -> list[dict]:
    """
    Return the user-facing feed.

    1. Read every event from events.jsonl.
    2. Compute the set of file_ids that have been soft-deleted, plus
       the per-file_id correction overrides.
    3. Keep only audio/text events (drop meta_update, delete, and
       correction — corrections are folded into their parent's row,
       not shown as separate rows in the feed).
    4. Drop entries whose file_id has been soft-deleted.
    5. Layer correction overrides on top so each row reflects the
       latest user-stated truth (slug, tags, transcript).
    6. Reverse to newest-first, optionally filter by tag against the
       FOLDED tag set, and slice for pagination.
    """
    all_events = _read_events()
    deleted    = _deleted_ids(all_events)
    overrides  = _fold_corrections(all_events)

    events = [e for e in all_events
              if e.get("type") in ("audio", "text")
              and e.get("file_id") not in deleted]
    events = [_apply_overrides(e, overrides) for e in events]
    events.reverse()
    if tag:
        events = [e for e in events if tag in e.get("tags", [])]
    return events[offset: offset + limit]


def get_all_tags() -> list[dict]:
    """
    Count the occurrences of every tag across the live, folded feed.

    1. Read every event and compute deletions + correction overrides.
    2. For each live audio/text entry, fold its tags through the
       overrides (so a corrected tag set replaces the original).
    3. Tally each tag once per entry.
    4. Return sorted by descending count, the shape the web UI uses.
    """
    all_events = _read_events()
    deleted    = _deleted_ids(all_events)
    overrides  = _fold_corrections(all_events)

    counts: dict[str, int] = {}
    for ev in all_events:
        if ev.get("type") not in ("audio", "text"):
            continue
        if ev.get("file_id") in deleted:
            continue
        folded = _apply_overrides(ev, overrides)
        for t in folded.get("tags", []):
            counts[t] = counts.get(t, 0) + 1
    return sorted([{"tag": t, "count": c} for t, c in counts.items()],
                  key=lambda x: x["count"], reverse=True)


# ── Deletion (soft) ──────────────────────────────────────────────────────────

def delete_file(file_id: str) -> bool:
    """
    Soft-delete: append a 'delete' event. The raw file stays on disk
    so the entry can be recovered by removing the delete event from
    events.jsonl. The entry is filtered out of get_feed and
    get_all_tags going forward.

    1. Confirm the file_id refers to a live entry (not missing, not
       already deleted) by asking current_entry.
    2. Append the delete event. Idempotent if already deleted —
       current_entry returns {} for already-deleted entries, so the
       call short-circuits.
    """
    if not current_entry(file_id):
        return False
    now = datetime.now().isoformat()
    _append_event({
        "event_id":   _new_id(),
        "type":       "delete",
        "file_id":    file_id,
        "created_at": now,
    })
    return True


# ── Corrections (append-only metadata mutation) ──────────────────────────────

def apply_correction(
    file_id: str,
    slug: str | None = None,
    tags: list[str] | None = None,
    transcript: str | None = None,
) -> dict | None:
    """
    Record a metadata correction against an existing entry.

    1. Find the original audio/text event for `file_id` in the log.
       If it does not exist (or is already deleted), return None and
       do nothing — there is no entry to correct.
    2. Build a new event of type "correction" that names the same
       file_id and carries only the fields that should change. Fields
       passed as None are omitted, so callers can correct one
       dimension (e.g. just tags) without restating the others.
    3. Append the correction event to events.jsonl. The original event
       is NOT touched. Sidecars are NOT touched. The log is the
       single source of truth; everything else is derived from it.
    4. Return the new correction event so callers can render it,
       broadcast it, or assert against it.
    """

    # 1. Validate the file_id refers to an entry that actually exists
    #    and has not been soft-deleted. We only accept corrections on
    #    live audio/text entries — corrections-of-corrections are
    #    intentionally not supported in this primitive.
    all_events = _read_events()
    deleted = _deleted_ids(all_events)
    target = next(
        (e for e in all_events
         if e.get("file_id") == file_id
         and e.get("type") in ("audio", "text")
         and file_id not in deleted),
        None,
    )
    if target is None:
        return None

    # 2. Build the correction payload. Only set fields the caller
    #    actually supplied; missing fields = "no change to this
    #    dimension."
    now = datetime.now().isoformat()
    correction = {
        "event_id":   _new_id(),
        "type":       "correction",
        "file_id":    file_id,
        "created_at": now,
    }
    if slug is not None:
        correction["slug"] = slug
    if tags is not None:
        correction["tags"] = tags
    if transcript is not None:
        correction["transcript"] = transcript

    # 3. Append. Nothing else is mutated.
    _append_event(correction)
    return correction


# ── Jobs ─────────────────────────────────────────────────────────────────────

def queue_job(job_type: str, input_file_id: str, params: dict | None = None) -> dict:
    ensure_archive_root()
    job_id = "job_" + _new_id()
    now = datetime.now().isoformat()
    job = {"job_id": job_id, "type": job_type, "input_file_id": input_file_id,
           "params": params or {}, "status": "queued",
           "output_file_id": None, "created_at": now, "completed_at": None}
    (JOBS_DIR / f"{job_id}.json").write_text(json.dumps(job, indent=2))
    input_tags = current_entry(input_file_id).get("tags", [])
    _append_event({"event_id": _new_id(), "type": "job_queued",
                   "job_id": job_id, "job_type": job_type,
                   "input_file_id": input_file_id, "tags": input_tags,
                   "created_at": now})
    return job


def complete_job(job_id: str, output_file_id: str | None = None,
                 output_text: str | None = None) -> None:
    p = JOBS_DIR / f"{job_id}.json"
    if not p.exists():
        return
    job = json.loads(p.read_text())
    now = datetime.now().isoformat()
    job.update({"status": "done", "output_file_id": output_file_id,
                "completed_at": now})
    p.write_text(json.dumps(job, indent=2))
    input_tags = current_entry(job.get("input_file_id", "")).get("tags", [])
    _append_event({"event_id": _new_id(), "type": "job_done",
                   "job_id": job_id, "job_type": job.get("type"),
                   "output_file_id": output_file_id, "output_text": output_text,
                   "tags": input_tags, "created_at": now})


def get_jobs(status: str | None = None) -> list[dict]:
    jobs = []
    for p in sorted(JOBS_DIR.iterdir()):
        if p.suffix != ".json":
            continue
        try:
            j = json.loads(p.read_text())
            if status is None or j.get("status") == status:
                jobs.append(j)
        except Exception:
            pass
    return sorted(jobs, key=lambda x: x.get("created_at", ""), reverse=True)


# ── Backward-compat wrappers ─────────────────────────────────────────────────

def get_slug_version(slug: str) -> int:
    return sum(1 for e in _read_events()
               if e.get("slug") == slug and e.get("type") in ("audio", "text"))


class _VP:
    """Minimal Path-alike that exposes .name for compat display."""
    def __init__(self, name: str, real: Path):
        self.name = name
        self._r = real
    def __str__(self):   return str(self._r)
    def __fspath__(self): return str(self._r)
    def relative_to(self, base): return self._r.relative_to(base)


def file_audio(src_path, slug: str, project: str, metadata: dict,
               ext: str = "ogg") -> "_VP":
    tags = list(metadata.get("tags", []))
    if project and project not in tags:
        tags.insert(0, project)
    event = ingest_audio(str(src_path), slug, tags, ext,
                         metadata.get("transcript", ""))
    v = get_slug_version(slug)
    return _VP(f"{slug}_v{v}.{ext.lstrip('.')}", RAW_DIR / f"{event['file_id']}.{ext.lstrip('.')}")


def file_lyrics(project: str, text: str,
                provided_version: int | None = None) -> tuple["_VP", str]:
    slug = f"{project}-lyrics" if project else "lyrics"
    tags = [project, "lyric"] if project else ["lyric"]
    event = ingest_text(slug, tags, text)
    v = get_slug_version(slug)
    display = f"{project}_lyrics_v{v}.txt"
    return _VP(display, RAW_DIR / f"{event['file_id']}.txt"), f"filed as {display}"


def get_next_version(project: str, base_name: str) -> int:
    return get_slug_version(base_name) + 1


def get_projects() -> list[dict]:
    tag_data: dict[str, dict] = {}
    for ev in _read_events():
        if ev.get("type") in ("meta_update", "job_queued", "job_done"):
            continue
        ts = ev.get("created_at", "")
        for t in ev.get("tags", []):
            if t in _META_TAGS:
                continue
            if t not in tag_data:
                tag_data[t] = {"name": t, "file_count": 0, "last_modified": ts}
            tag_data[t]["file_count"] += 1
            if ts > tag_data[t]["last_modified"]:
                tag_data[t]["last_modified"] = ts
    return sorted(tag_data.values(), key=lambda x: x["name"])


def get_project_files(project: str) -> list[dict]:
    results = []
    for ev in _read_events():
        if project not in ev.get("tags", []):
            continue
        if ev.get("type") not in ("audio", "text"):
            continue
        results.append({
            "filename":   f"{ev.get('slug', ev['file_id'])}_v1.{ev.get('ext', 'ogg')}",
            "base_name":  ev.get("slug", ev["file_id"]),
            "type":       ev.get("type", "audio"),
            "tags":       ev.get("tags", []),
            "transcript": ev.get("transcript", ""),
            "text":       ev.get("text", ""),
            "version":    1,
            "versions":   [1],
            "created_at": ev.get("created_at"),
            "file_id":    ev.get("file_id"),
        })
    return results


# ── Migration from v1 (project/slug_vN.ext) ──────────────────────────────────

_MIGRATION_MARKER = ARCHIVE_ROOT / ".v2_migrated"


def migrate_v1() -> int:
    if _MIGRATION_MARKER.exists():
        return 0
    ensure_archive_root()
    migrated = 0

    for proj_dir in sorted(ARCHIVE_ROOT.iterdir()):
        if proj_dir.name in ("raw", "sidecars", "jobs", "summaries") or not proj_dir.is_dir():
            continue
        for f in sorted(proj_dir.iterdir()):
            if f.suffix == ".json" or not f.is_file():
                continue
            old_meta: dict = {}
            sc_path = f.with_suffix(".json")
            if sc_path.exists():
                try:
                    old_meta = json.loads(sc_path.read_text())
                except Exception:
                    pass

            slug = (old_meta.get("slug") or old_meta.get("base_name")
                    or re.sub(r"_v\d+$", "", f.stem))
            tags = list(old_meta.get("tags", []))
            if proj_dir.name not in tags:
                tags.insert(0, proj_dir.name)
            transcript = old_meta.get("transcript", "")
            ext = f.suffix.lstrip(".")
            created_at = old_meta.get("created_at", datetime.now().isoformat())
            ftype = old_meta.get("type", "audio")

            file_id = _new_id()
            shutil.copy2(str(f), str(RAW_DIR / f"{file_id}.{ext}"))

            ev: dict = {"event_id": _new_id(), "type": ftype, "file_id": file_id,
                        "slug": slug, "tags": tags, "transcript": transcript,
                        "ext": ext, "parent_id": None, "job_id": None,
                        "created_at": created_at,
                        "_migrated_from": str(f.relative_to(ARCHIVE_ROOT))}
            if ftype == "text":
                try:
                    ev["text"] = f.read_text()
                except Exception:
                    ev["text"] = ""
            _append_event(ev)
            migrated += 1

    _MIGRATION_MARKER.write_text(datetime.now().isoformat())
    return migrated


# ── Search (cross-field) ─────────────────────────────────────────────────────

def search(query: str) -> list[dict]:
    q = query.lower()
    results = []
    for ev in reversed(_read_events()):
        if ev.get("type") in ("meta_update", "job_queued", "job_done"):
            continue
        haystack = " ".join([
            ev.get("slug", ""),
            " ".join(ev.get("tags", [])),
            ev.get("transcript", ""),
            ev.get("text", ""),
        ]).lower()
        if q in haystack:
            results.append(ev)
    return results
