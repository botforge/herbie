"""
Herbie — FastAPI server
Thin transport layer. All logic in services/.
"""

import os
import tempfile
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv()

from services.archive import (
    RAW_DIR, ARCHIVE_ROOT,
    ensure_archive_root,
    get_feed, get_all_tags,
    current_entry,
    update_file_meta,
    delete_file,
    queue_job, get_jobs,
    search,
    migrate_v1,
)
from services.jobs import execute_job
from services.pipeline import handle_text, handle_audio

# Force unbuffered stdout so [HERBIE/*] prints appear immediately under uvicorn --reload
import functools
print = functools.partial(print, flush=True)

app = FastAPI(title="Herbie", description="Personal music archivist")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_conversations: dict[str, list[dict]] = {}


# ─────────────────────────────────────────────────────────────────────────────
# Startup
# ─────────────────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    ensure_archive_root()
    n = migrate_v1()
    if n:
        import logging
        logging.getLogger("herbie").info(f"migrated {n} v1 files to event log")
    static_dir = Path(__file__).parent / "static"
    static_dir.mkdir(exist_ok=True)


# ─────────────────────────────────────────────────────────────────────────────
# Feed + Tags
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/feed")
async def feed(tag: str = "", limit: int = 200, offset: int = 0):
    return get_feed(tag=tag, limit=limit, offset=offset)


@app.get("/tags")
async def list_tags():
    return get_all_tags()


# ─────────────────────────────────────────────────────────────────────────────
# File serving  (raw files by file_id)
# ─────────────────────────────────────────────────────────────────────────────

_AUDIO_EXT = {".ogg", ".mp3", ".wav", ".m4a", ".flac", ".aac", ".opus"}


def _find_audio_path(file_id: str) -> Path | None:
    matches = [m for m in RAW_DIR.glob(f"{file_id}.*") if m.suffix.lower() in _AUDIO_EXT]
    return matches[0] if matches else None


@app.get("/files/{file_id}/audio")
@app.get("/files/{file_id}/audio/{filename}")
async def serve_audio(file_id: str, filename: str = ""):
    """
    Serve the raw audio with a proper attachment Content-Disposition so
    drag-to-desktop lands as a real file. The optional /{filename} suffix
    lets the drag URL end in a real extension — so even browsers that
    ignore the Chrome DownloadURL protocol name the drop correctly.
    """
    path = _find_audio_path(file_id)
    if not path:
        raise HTTPException(404, "audio file not found")
    sc   = current_entry(file_id) or {}
    slug = sc.get("slug") or file_id
    ext  = path.suffix.lstrip(".")
    return FileResponse(str(path), filename=f"{slug}.{ext}")


@app.get("/files/{file_id}/text")
async def serve_text(file_id: str):
    p = RAW_DIR / f"{file_id}.txt"
    if not p.exists():
        raise HTTPException(404, "text file not found")
    return {"text": p.read_text()}


@app.get("/files/{file_id}/midi")
@app.get("/files/{file_id}/midi/{filename}")
async def serve_midi(file_id: str, filename: str = ""):
    """Render the sidecar's NOTE data to a real .mid file for DAW drag-in."""
    from fastapi.responses import Response
    from services.jobs import notes_text_to_midi_bytes
    sc = current_entry(file_id)
    if not sc:
        raise HTTPException(404, "file not found")

    notes = sc.get("midi_notes") or ""
    if not notes:
        raw = RAW_DIR / f"{file_id}.txt"
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
# Meta update (inline edit from UI)
# ─────────────────────────────────────────────────────────────────────────────

class PatchFileRequest(BaseModel):
    transcript: Optional[str] = None
    tags: Optional[list[str]] = None


@app.patch("/files/{file_id}")
async def patch_file(file_id: str, req: PatchFileRequest):
    """
    1. Inline edits from the web UI rewrite the matching event in
       place via update_file_meta.
    2. update_file_meta also snapshots the prior state into the undo
       buffer, so a follow-up call to undo_last_action() can restore
       the entry if the edit was wrong.
    3. Returns 404 if the file_id is missing or already deleted —
       update_file_meta returns False in that case.
    """
    ok = update_file_meta(
        file_id, transcript=req.transcript, tags=req.tags,
    )
    if not ok:
        raise HTTPException(404, "file not found")
    return {"ok": True}


@app.delete("/files/{file_id}")
async def delete_file_endpoint(file_id: str):
    ok = delete_file(file_id)
    if not ok:
        raise HTTPException(404, "file not found")
    return {"ok": True}


@app.post("/files/{file_id}/reveal")
async def reveal_file(file_id: str):
    """
    Open the OS file manager with the file selected, so the user can drag
    it into their DAW directly. MIDI entries are materialized to disk on
    first reveal (they only exist as NOTE text otherwise).
    """
    import subprocess, platform
    sc = current_entry(file_id)
    if not sc:
        raise HTTPException(404, "file not found")

    ext   = (sc.get("ext") or "").lstrip(".")
    notes = sc.get("midi_notes") or ""
    path  = None

    if notes:
        mid_path = RAW_DIR / f"{file_id}.mid"
        if not mid_path.exists():
            from services.jobs import notes_text_to_midi_bytes
            mid_path.write_bytes(notes_text_to_midi_bytes(notes))
        path = mid_path
    else:
        candidate = RAW_DIR / f"{file_id}.{ext}" if ext else None
        if candidate and candidate.exists():
            path = candidate

    if not path or not path.exists():
        raise HTTPException(404, f"file not on disk for {file_id}")

    system = platform.system()
    try:
        if system == "Darwin":
            subprocess.Popen(["open", "-R", str(path)])
        elif system == "Windows":
            subprocess.Popen(["explorer", f"/select,{path}"])
        else:
            subprocess.Popen(["xdg-open", str(path.parent)])
    except Exception as e:
        raise HTTPException(500, f"reveal failed: {e}")

    return {"ok": True, "path": str(path)}


# ─────────────────────────────────────────────────────────────────────────────
# Search
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/search")
async def search_endpoint(q: str = ""):
    if not q:
        return []
    return search(q)


# ─────────────────────────────────────────────────────────────────────────────
# Audio ingest
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/ingest/audio")
async def ingest_audio_endpoint(
    file: UploadFile = File(...),
    context: Optional[str] = Form(None),
    conversation_id: Optional[str] = Form(None),
):
    suffix = Path(file.filename or "audio.ogg").suffix or ".ogg"
    ext = suffix.lstrip(".")
    conv_id = conversation_id or "default"

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name

    try:
        history = _conversations.get(conv_id, [])
        result = handle_audio(tmp_path, ext, context or "", history, transport="web")

        history.append({"role": "user", "content": context or result["transcript"] or "(audio)"})
        history.append({"role": "assistant", "content": result["message"]})
        _conversations[conv_id] = history[-20:]

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
    message: str
    conversation_id: str = "default"


def _print_job_queue():
    from services.archive import JOBS_DIR
    import json as _j
    jobs = []
    if JOBS_DIR.exists():
        for p in sorted(JOBS_DIR.iterdir()):
            if p.suffix == ".json":
                try: jobs.append(_j.loads(p.read_text()))
                except Exception: pass
    print(f"[HERBIE/queue] {len(jobs)} jobs total:")
    for j in jobs[-10:]:
        print(f"  • {j.get('job_id')} {j.get('type')} status={j.get('status')} "
              f"input={j.get('input_file_id')} output={j.get('output_file_id')}")


@app.post("/ingest/text")
async def ingest_text_endpoint(req: TextRequest):
    print(f"\n[HERBIE/text] incoming: {req.message!r}")
    _print_job_queue()
    history = _conversations.get(req.conversation_id, [])

    result = handle_text(req.message, history, transport="web")

    if result.get("type") != "eval":
        history.append({"role": "user",      "content": req.message})
        history.append({"role": "assistant", "content": result["message"]})
        _conversations[req.conversation_id] = history[-20:]
    return result




# ─────────────────────────────────────────────────────────────────────────────
# Jobs
# ─────────────────────────────────────────────────────────────────────────────

class JobRequest(BaseModel):
    job_type: str
    input_file_id: str
    params: dict = {}


@app.post("/jobs")
async def create_job(req: JobRequest):
    sc = current_entry(req.input_file_id)
    if not sc:
        raise HTTPException(404, "input file not found")
    job = queue_job(req.job_type, req.input_file_id, req.params)
    execute_job(job)
    return job


@app.get("/jobs")
async def list_jobs(status: Optional[str] = None):
    return get_jobs(status=status)


@app.get("/jobs/{job_id}")
async def get_job(job_id: str):
    from services.archive import JOBS_DIR
    import json
    p = JOBS_DIR / f"{job_id}.json"
    if not p.exists():
        raise HTTPException(404, "job not found")
    return json.loads(p.read_text())


# ─────────────────────────────────────────────────────────────────────────────
# Static UI
# ─────────────────────────────────────────────────────────────────────────────

static_dir = Path(__file__).parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
