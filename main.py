"""
Lila — FastAPI server (multi-user cloud deployment)

Thin transport layer. All logic lives in services/. Every protected
route depends on auth.get_current_user, which reads the lila_session
cookie, validates the JWT, looks up the user row, and forwards the
user dict to the route. We then thread user["user_id"] into every
archive call so cross-user reads/writes are impossible.
"""

import os
import tempfile
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Response,
    UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv()

from services.logsetup import configure as _configure_logs
_configure_logs()

from services import archive, auth, conversation_store, pipeline, users
from services.archive import (
    current_entry,
    delete_file,
    ensure_archive_root,
    get_all_tags,
    get_feed,
    get_jobs,
    queue_job,
    search,
    update_file_meta,
)
from services.jobs import execute_job

# Force unbuffered stdout so [LILA/*] prints appear immediately under uvicorn --reload
import functools
print = functools.partial(print, flush=True)

app = FastAPI(title="Lila", description="Personal music archivist")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─────────────────────────────────────────────────────────────────────────────
# Startup
# ─────────────────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    """
    1. Create the volume root + summaries dir if missing.
    2. Per-user raw/ subdirs are created lazily by ingest, so there's
       no per-user work to do here.
    3. Make sure the static/ dir exists for the SPA mount below.
    """
    ensure_archive_root()
    static_dir = Path(__file__).parent / "static"
    static_dir.mkdir(exist_ok=True)


# ─────────────────────────────────────────────────────────────────────────────
# Auth
# ─────────────────────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    username: str
    password: str


@app.post("/auth/login")
async def login(req: LoginRequest, response: Response):
    """
    1. Verify the password with argon2id (services/users).
    2. Issue a JWT and set it as the lila_session cookie.
    3. Return a tiny ack — the cookie does the work on subsequent
       requests via auth.get_current_user.
    """
    if not users.verify_password(req.username, req.password):
        raise HTTPException(401, "invalid credentials")
    token = auth.encode_token(req.username)
    auth.set_login_cookie(response, token)
    return {"ok": True, "user_id": req.username}


@app.post("/auth/logout")
async def logout(response: Response):
    """1. Clear the lila_session cookie. The JWT itself is stateless
    so server-side revocation is a no-op."""
    auth.clear_login_cookie(response)
    return {"ok": True}


@app.get("/auth/me")
async def me(user: dict = Depends(auth.get_current_user)):
    """1. Echo back the authenticated user — used by the web UI to
    decide between the login screen and the main app."""
    return {"user_id": user["user_id"], "username": user["username"]}


# ─────────────────────────────────────────────────────────────────────────────
# Health
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    """
    1. Ping the DB with a trivial SELECT.
    2. Return ok on success, 503 on any failure — Render and load
       balancers can hit this to gate traffic.
    """
    try:
        from services import db as _db
        _db.fetch_one("SELECT 1 AS ok")
        return {"ok": True}
    except Exception as e:
        raise HTTPException(503, f"db unreachable: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Feed + Tags
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/feed")
async def feed(
    tag: str = "",
    limit: int = 200,
    offset: int = 0,
    user: dict = Depends(auth.get_current_user),
):
    """1. Return audio/text events for the authenticated user only."""
    return get_feed(user["user_id"], tag=tag, limit=limit, offset=offset)


@app.get("/tags")
async def list_tags(user: dict = Depends(auth.get_current_user)):
    """1. Tag tally restricted to the authenticated user's events."""
    return get_all_tags(user["user_id"])


# ─────────────────────────────────────────────────────────────────────────────
# File serving (raw files by file_id, scoped under <volume>/<user_id>/raw/)
# ─────────────────────────────────────────────────────────────────────────────

_AUDIO_EXT = {".ogg", ".mp3", ".wav", ".m4a", ".flac", ".aac", ".opus"}


def _find_audio_path(user_id: str, file_id: str) -> Path | None:
    """
    1. Glob the user's raw/ dir for file_id.<anything>.
    2. Filter to known audio extensions; return the first match (or None).
    """
    raw_dir = archive.VOLUME_ROOT / user_id / "raw"
    matches = [m for m in raw_dir.glob(f"{file_id}.*") if m.suffix.lower() in _AUDIO_EXT]
    return matches[0] if matches else None


@app.get("/files/{file_id}/audio")
@app.get("/files/{file_id}/audio/{filename}")
async def serve_audio(
    file_id: str,
    filename: str = "",
    user: dict = Depends(auth.get_current_user),
):
    """
    1. Resolve the raw audio path under the user's partition.
    2. 404 if missing (either the file_id doesn't belong to this user
       or the file is gone from disk).
    3. Stream it with a slug-based attachment filename so drag-to-
       desktop lands as a real file. The optional /{filename} suffix
       in the URL lets the drag URL end in a real extension — browsers
       that ignore the Chrome DownloadURL protocol still name the
       drop correctly.
    """
    path = _find_audio_path(user["user_id"], file_id)
    if not path:
        raise HTTPException(404, "audio file not found")
    sc = current_entry(user["user_id"], file_id) or {}
    slug = sc.get("slug") or file_id
    ext = path.suffix.lstrip(".")
    return FileResponse(str(path), filename=f"{slug}.{ext}")


@app.get("/files/{file_id}/text")
async def serve_text(
    file_id: str,
    user: dict = Depends(auth.get_current_user),
):
    """1. Read the .txt sidecar from the user's raw/ dir. 404 if absent."""
    p = archive.VOLUME_ROOT / user["user_id"] / "raw" / f"{file_id}.txt"
    if not p.exists():
        raise HTTPException(404, "text file not found")
    return {"text": p.read_text()}


@app.get("/files/{file_id}/midi")
@app.get("/files/{file_id}/midi/{filename}")
async def serve_midi(
    file_id: str,
    filename: str = "",
    user: dict = Depends(auth.get_current_user),
):
    """
    Render the entry's NOTE data to a real .mid file for DAW drag-in.

    1. Look up the entry for this user; 404 if missing.
    2. Pull NOTE text from the row's midi_notes column, or fall back
       to scanning the on-disk text sidecar (legacy entries stored
       NOTE lines there).
    3. Render NOTE text → MIDI bytes via services.jobs.
    4. Return as audio/midi with a slug-based attachment filename.
    """
    from fastapi.responses import Response
    from services.jobs import notes_text_to_midi_bytes

    sc = current_entry(user["user_id"], file_id)
    if not sc:
        raise HTTPException(404, "file not found")

    notes = sc.get("midi_notes") or ""
    if not notes:
        raw = archive.VOLUME_ROOT / user["user_id"] / "raw" / f"{file_id}.txt"
        if raw.exists():
            content = raw.read_text()
            if "NOTE " in content:
                notes = content
    if not notes:
        raise HTTPException(404, "no midi data on this entry")

    midi_bytes = notes_text_to_midi_bytes(notes)
    slug = sc.get("slug") or file_id
    return Response(
        content=midi_bytes,
        media_type="audio/midi",
        headers={"Content-Disposition": f'attachment; filename="{slug}.mid"'},
    )


# ─────────────────────────────────────────────────────────────────────────────
# Meta update (inline edit from UI) + soft delete
# ─────────────────────────────────────────────────────────────────────────────

class PatchFileRequest(BaseModel):
    transcript: Optional[str] = None
    tags: Optional[list[str]] = None


@app.patch("/files/{file_id}")
async def patch_file(
    file_id: str,
    req: PatchFileRequest,
    user: dict = Depends(auth.get_current_user),
):
    """
    1. Inline edits from the web UI rewrite the matching event in
       place via update_file_meta.
    2. update_file_meta also snapshots the prior state into the user's
       last_action buffer so a follow-up call to undo_last_action()
       can restore the entry if the edit was wrong.
    3. Returns 404 if the file_id is missing or already deleted —
       update_file_meta returns False in that case.
    """
    ok = update_file_meta(
        user["user_id"], file_id, transcript=req.transcript, tags=req.tags,
    )
    if not ok:
        raise HTTPException(404, "file not found")
    return {"ok": True}


@app.delete("/files/{file_id}")
async def delete_file_endpoint(
    file_id: str,
    user: dict = Depends(auth.get_current_user),
):
    """1. Append a delete event so the entry disappears from feeds.
    Raw bytes stay on disk in case manual restore is needed."""
    ok = delete_file(user["user_id"], file_id)
    if not ok:
        raise HTTPException(404, "file not found")
    return {"ok": True}


# ─────────────────────────────────────────────────────────────────────────────
# Search
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/search")
async def search_endpoint(
    q: str = "",
    user: dict = Depends(auth.get_current_user),
):
    """1. Empty query → empty list. 2. Otherwise scoped substring
    search across the user's live entries."""
    if not q:
        return []
    return search(user["user_id"], q)


# ─────────────────────────────────────────────────────────────────────────────
# Audio ingest
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/ingest/audio")
async def ingest_audio_endpoint(
    file: UploadFile = File(...),
    context: Optional[str] = Form(None),
    user: dict = Depends(auth.get_current_user),
):
    """
    1. Spool the upload to a temp file with the original suffix
       preserved so transcription picks the right decoder.
    2. Pull the user's recent conversation history (per-user-global,
       no conversation_id keying — Postgres-backed via
       conversation_store).
    3. Hand off to pipeline.handle_audio with the user_id threaded
       through; the pipeline stages the audio under the user's
       partition and decides whether to commit based on the LLM.
    4. Append both turns (user + assistant) to the user's history.
    5. Always unlink the temp file when done.
    """
    suffix = Path(file.filename or "audio.ogg").suffix or ".ogg"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name
    try:
        history = conversation_store.recent(user["user_id"], limit=20)
        result = pipeline.handle_audio(
            user["user_id"],
            tmp_path,
            suffix.lstrip("."),
            user_context=context or "",
            history=history,
            transport="web",
        )
        conversation_store.append(
            user["user_id"], "user", context or "(audio)",
        )
        conversation_store.append(
            user["user_id"], "assistant", result["message"],
        )
        return result
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Text ingest
# ─────────────────────────────────────────────────────────────────────────────

class TextRequest(BaseModel):
    """
    Per-user-global history means the body is just a message —
    history is keyed only by user_id now (was previously keyed by an
    in-memory conversation_id dict).
    """
    message: str


@app.post("/ingest/text")
async def ingest_text_endpoint(
    req: TextRequest,
    user: dict = Depends(auth.get_current_user),
):
    """
    1. Pull the user's last 20 turns from Postgres.
    2. Forward to pipeline.handle_text — this stays synchronous
       because the LLM tool loop may queue + execute a job inline,
       and the LLM expects the result before composing its reply.
    3. Skip history append for type='eval' — eval flags are pure
       feedback notes that should not pollute future LLM context.
    4. Otherwise persist both the user message and the assistant
       reply via conversation_store.
    """
    print(f"\n[LILA/text] incoming: {req.message!r}")
    history = conversation_store.recent(user["user_id"], limit=20)

    result = pipeline.handle_text(
        user["user_id"], req.message, history, transport="web",
    )

    if result.get("type") != "eval":
        conversation_store.append(user["user_id"], "user", req.message)
        conversation_store.append(user["user_id"], "assistant", result["message"])
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Jobs
# ─────────────────────────────────────────────────────────────────────────────

class JobRequest(BaseModel):
    job_type: str
    input_file_id: str
    params: dict = {}


@app.post("/jobs")
async def create_job(
    req: JobRequest,
    bg: BackgroundTasks,
    user: dict = Depends(auth.get_current_user),
):
    """
    1. Validate the input file exists for this user — guard against
       a caller queueing a job against another user's file_id.
    2. queue_job creates a 'queued' row + event immediately.
    3. Schedule execute_job in the background and return the queued
       row so the caller does not wait on the side-effect. Note:
       the chat-driven job path through pipeline.handle_text stays
       synchronous (the LLM expects the result before composing its
       reply) — this BackgroundTasks path is for the explicit
       /jobs HTTP route only.
    """
    sc = current_entry(user["user_id"], req.input_file_id)
    if not sc:
        raise HTTPException(404, "input file not found")
    job = queue_job(user["user_id"], req.job_type, req.input_file_id, req.params)
    bg.add_task(execute_job, user["user_id"], job)
    return job


@app.get("/jobs")
async def list_jobs(
    status: Optional[str] = None,
    user: dict = Depends(auth.get_current_user),
):
    """1. Newest-first list of the user's jobs, optionally filtered
    by status."""
    return get_jobs(user["user_id"], status=status)


@app.get("/jobs/{job_id}")
async def get_job(
    job_id: str,
    user: dict = Depends(auth.get_current_user),
):
    """
    1. Look up the job row by (user_id, job_id) — scoping by user_id
       prevents cross-user reads even if a caller guesses an id.
    2. 404 if missing.
    """
    from services import db as _db
    row = _db.fetch_one(
        "SELECT * FROM jobs WHERE user_id = %s AND job_id = %s",
        (user["user_id"], job_id),
    )
    if not row:
        raise HTTPException(404, "job not found")
    return dict(row)


# ─────────────────────────────────────────────────────────────────────────────
# Static UI
# ─────────────────────────────────────────────────────────────────────────────

static_dir = Path(__file__).parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
