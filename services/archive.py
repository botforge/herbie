"""
Archive service — flat, tag-based, event-sourced.

How to read this module:

  1. Layout on disk under ARCHIVE_ROOT:
     1A. raw/                immutable audio bytes, named {file_id}.{ext}
     1B. events.jsonl        the feed log — one line per audio/text/
                             job/delete event
     1C. jobs/               {job_id}.json per pending / done job
     1D. summaries/          {tag}_summary.md built on demand
     1E. .last_action.json   single-step undo buffer for the most
                             recent in-place edit
  2. Ingests append. Soft deletes append. Edits to existing entries
     (retag, slug fix, lyric fix) rewrite the matching audio/text
     event in events.jsonl in place — no fold layer, no correction
     events.
  3. Every in-place edit pre-snapshots the affected events to
     .last_action.json. undo_last_action() restores them once;
     subsequent edits overwrite the buffer.
  4. Reads (get_feed, get_all_tags, current_entry) read events.jsonl
     directly — no override layer, no derivation.

Core API:
  ingest_audio(src_path, slug, tags, ext, transcript, parent_id) → event
  ingest_text(slug, tags, text, parent_id)                       → event
  update_file_meta(file_id, slug?, tags?, transcript?, text?)    → bool
  update_files_meta(file_ids, slug?, tags?, transcript?, text?)  → int
  undo_last_action()                                             → int
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


def current_entry(file_id: str) -> dict:
    """
    Return the audio/text event for a given file_id, or {} if it does
    not exist or has been soft-deleted.

    1. Read every event from the log.
    2. If a delete event names this file_id, return {} — the entry is
       gone from the user's perspective.
    3. Otherwise return the audio/text event for the file_id.
    4. Alias `id` to `file_id` so legacy call sites that read
       `entry["id"]` keep working.
    Returning {} (rather than None) lets callers chain `.get(...)`
    without nil-checks at every site.
    """
    all_events = _read_events()
    if file_id in _deleted_ids(all_events):
        return {}
    target = next(
        (e for e in all_events
         if e.get("file_id") == file_id
         and e.get("type") in ("audio", "text")),
        None,
    )
    if target is None:
        return {}
    out = dict(target)
    out["id"] = out.get("file_id")
    return out


def get_feed(tag: str = "", limit: int = 100, offset: int = 0) -> list[dict]:
    """
    Return the user-facing feed.

    1. Read every event from events.jsonl.
    2. Compute the set of file_ids that have been soft-deleted.
    3. Keep only audio/text events whose file_id is not deleted.
    4. Reverse to newest-first, optionally filter by tag, and slice
       for pagination.
    """
    all_events = _read_events()
    deleted    = _deleted_ids(all_events)
    events = [e for e in all_events
              if e.get("type") in ("audio", "text")
              and e.get("file_id") not in deleted]
    events.reverse()
    if tag:
        events = [e for e in events if tag in e.get("tags", [])]
    return events[offset: offset + limit]


def get_all_tags() -> list[dict]:
    """
    Count the occurrences of every tag across the live feed.

    1. Read every event and compute deletions.
    2. Tally each tag once per live audio/text entry.
    3. Return sorted by descending count, the shape the web UI uses.
    """
    all_events = _read_events()
    deleted    = _deleted_ids(all_events)
    counts: dict[str, int] = {}
    for ev in all_events:
        if ev.get("type") not in ("audio", "text"):
            continue
        if ev.get("file_id") in deleted:
            continue
        for t in ev.get("tags", []):
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


# ── In-place edits + single-step undo ────────────────────────────────────────
#
# How the edit/undo pair works:
#
#   1. Edits rewrite the matching audio/text event in events.jsonl in
#      place. There is no fold layer, no correction event — the log
#      reflects current state directly.
#   2. Before each edit, the affected events are snapshotted to
#      ARCHIVE_ROOT/.last_action.json. This file is the entire undo
#      buffer — it holds only the most recent action.
#   3. undo_last_action() reads the snapshot, restores each event to
#      its prior bytes via another _patch_events call, and clears the
#      buffer. Subsequent undo calls return 0.
#   4. A new edit overwrites the snapshot, so only one step of undo is
#      ever recoverable. This matches the user need ("if the LLM
#      messed up, let me back out") without becoming a full audit log.

_UNDO_FILE_NAME = ".last_action.json"


def _undo_path() -> Path:
    return ARCHIVE_ROOT / _UNDO_FILE_NAME


def _snapshot_for_undo(snapshots: list[dict]) -> None:
    """
    Persist the snapshots taken during one edit action to the undo
    buffer, replacing whatever was previously there. Each snapshot
    is {"file_id": str, "before": <full audio/text event dict>}.
    """
    _undo_path().write_text(json.dumps(snapshots, indent=2))


def _rewrite_events_with(events: list[dict], patches_by_fid: dict[str, dict]) -> None:
    """
    Walk an in-memory list of events, merge the supplied per-file_id
    patch onto every matching audio/text event, and write the whole
    log back to disk in ONE pass. Other event types and unmatched
    events pass through unchanged.

    1. The patch is a dict of fields to overlay on the event dict.
       For partial edits this contains only the changed fields; for
       undo restores it contains the entire prior event.
    2. Caller is responsible for having snapshotted the before-state
       (when needed) before calling this.
    """
    out_lines: list[str] = []
    for ev in events:
        fid = ev.get("file_id")
        if (fid in patches_by_fid
                and ev.get("type") in ("audio", "text")):
            ev = {**ev, **patches_by_fid[fid]}
        out_lines.append(json.dumps(ev))
    EVENTS_FILE.write_text("\n".join(out_lines) + "\n")


def update_files_meta(
    file_ids: list[str],
    slug: str | None = None,
    tags: list[str] | None = None,
    transcript: str | None = None,
    text: str | None = None,
) -> int:
    """
    Edit one or more entries in a single undoable action — one read
    of events.jsonl, one write, regardless of batch size.

    1. Build the patch dict from supplied fields. If nothing to
       change, return 0 without touching disk.
    2. Read events.jsonl ONCE into memory. While walking, record
       which file_ids have been soft-deleted, and capture the
       before-state of every requested audio/text event that is not
       deleted.
    3. If no requested file_id matches a live entry, return 0
       without writing the snapshot or the log.
    4. Otherwise write the snapshot list to .last_action.json
       (overwriting any prior snapshot), then rewrite events.jsonl
       ONCE with the patch applied to every captured file_id.
    5. Return the count of entries actually edited.
    """
    patch: dict = {}
    if slug       is not None: patch["slug"]       = slug
    if tags       is not None: patch["tags"]       = tags
    if transcript is not None: patch["transcript"] = transcript
    if text       is not None: patch["text"]       = text
    if not patch:
        return 0

    target  = set(file_ids)
    events  = _read_events()
    deleted = _deleted_ids(events)
    captured: dict[str, dict] = {}
    for ev in events:
        fid = ev.get("file_id")
        if (fid in target
                and fid not in deleted
                and fid not in captured
                and ev.get("type") in ("audio", "text")):
            captured[fid] = dict(ev)

    if not captured:
        return 0

    _snapshot_for_undo([
        {"file_id": fid, "before": before}
        for fid, before in captured.items()
    ])
    _rewrite_events_with(events, {fid: patch for fid in captured})
    return len(captured)


def update_file_meta(
    file_id: str,
    slug: str | None = None,
    tags: list[str] | None = None,
    transcript: str | None = None,
    text: str | None = None,
) -> bool:
    """
    Convenience wrapper for the single-entry case — delegates to
    update_files_meta so the snapshot + patch logic lives in exactly
    one place. Returns True iff the entry was found and edited.
    """
    return update_files_meta(
        [file_id],
        slug=slug, tags=tags, transcript=transcript, text=text,
    ) > 0


def undo_last_action() -> int:
    """
    Restore the snapshot saved by the most recent update_files_meta
    call — one read of events.jsonl, one write, regardless of how
    many entries the original action touched.

    1. If the undo buffer does not exist or is unparseable, return
       0 — nothing to roll back.
    2. Build a {file_id: full prior event} restore map from the
       snapshots. The patch is the entire prior event dict, so every
       changed dimension reverts in one merge.
    3. Read events.jsonl once into memory and rewrite it once with
       the restore map applied to every captured file_id.
    4. Clear the undo buffer so the next undo call is a no-op until
       a new edit creates a fresh snapshot.
    """
    p = _undo_path()
    if not p.exists():
        return 0
    try:
        snapshots = json.loads(p.read_text())
    except Exception:
        snapshots = []
    if not snapshots:
        p.unlink(missing_ok=True)
        return 0

    restore = {
        snap["file_id"]: snap["before"]
        for snap in snapshots
        if snap.get("file_id") and snap.get("before")
    }
    if restore:
        _rewrite_events_with(_read_events(), restore)
    p.unlink(missing_ok=True)
    return len(snapshots)


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
