"""
Herbie — FastAPI server
Thin transport layer. All logic in services/.
"""

import os
import tempfile
from datetime import datetime
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
    ingest_audio, ingest_text,
    get_feed, get_all_tags,
    update_file_meta,
    delete_file,
    queue_job, complete_job, get_jobs,
    execute_archive_action,
    search,
    migrate_v1,
    _read_sidecar,
)
from services.llm import (
    detect_lyric_intent,
    extract_lyric_project,
    parse_archive_action,
    respond_to_audio,
    respond_to_text,
)
from services.transcribe import process_audio
from services.jobs import (
    execute_job,
    handle_job,
    parse_job_marker,
)

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
    sc   = _read_sidecar(file_id) or {}
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
    sc = _read_sidecar(file_id)
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
    ok = update_file_meta(file_id, transcript=req.transcript, tags=req.tags)
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
    sc = _read_sidecar(file_id)
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
    project: Optional[str] = Form(None),
):
    suffix = Path(file.filename or "audio.ogg").suffix or ".ogg"

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name

    try:
        processing = process_audio(tmp_path)
        transcript = processing.get("speech_transcript", "")
        speech_context = processing.get("speech_context", "")
        combined_context = " | ".join(filter(None, [context, speech_context]))

        conv_id = conversation_id or "default"
        history = _conversations.get(conv_id, [])
        ext = suffix.lstrip(".")

        llm_result = respond_to_audio(
            transcript=transcript,
            user_context=combined_context,
            conversation_history=history,
            existing_version=1,
            file_ext=ext,
        )

        proj = project or llm_result["project"]
        slug = llm_result["slug"]
        tags: list[str] = llm_result.get("tags", [])
        if proj and proj not in tags:
            tags.insert(0, proj)

        from services.archive import get_slug_version
        version = get_slug_version(slug) + 1

        event = ingest_audio(tmp_path, slug, tags, ext, transcript)
        file_id = event["file_id"]

        filename = f"{slug}_v{version}.{ext}" if version > 1 else f"{slug}.{ext}"
        tag_str = ", ".join(tags)
        message = f"filed\n{filename}\n[{tag_str}]"

        history.append({"role": "user", "content": combined_context or transcript or "(audio)"})
        history.append({"role": "assistant", "content": message})
        _conversations[conv_id] = history[-20:]

        return {
            "file_id": file_id,
            "slug": slug,
            "tags": tags,
            "transcript": transcript,
            "message": message,
        }

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
    project: Optional[str] = None


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
    print(f"\n[HERBIE/text] === incoming: {req.message!r}")
    _print_job_queue()
    history = _conversations.get(req.conversation_id, [])

    # Lyric submission
    if detect_lyric_intent(req.message):
        project_name = extract_lyric_project(req.message) or req.project or "sketches"
        import re
        text = req.message
        for pat in [
            r"^[a-z0-9_-]+\s+lyr(?:ic|ics)\s*\n?",
            r"^lyr(?:ic|ics)\s+for\s+[a-z0-9_-]+\s*\n?",
            r"^words\s+for\s+[a-z0-9_-]+\s*\n?",
            r"^verse\s+for\s+[a-z0-9_-]+\s*\n?",
            r"^chorus\s+for\s+[a-z0-9_-]+\s*\n?",
            r"^hook\s+for\s+[a-z0-9_-]+\s*\n?",
            r"^bridge\s+for\s+[a-z0-9_-]+\s*\n?",
        ]:
            text = re.sub(pat, "", text, flags=re.IGNORECASE).strip()

        slug = f"{project_name}-lyrics"
        tags = [project_name, "lyric"]
        event = ingest_text(slug, tags, text)
        reply = f"filed as {project_name}-lyrics"

        history.append({"role": "user", "content": req.message})
        history.append({"role": "assistant", "content": reply})
        _conversations[req.conversation_id] = history[-20:]
        return {"message": reply, "type": "lyric", "event": event}

    # LLM chat — no archive snapshot, no project inference. Tools do it.
    print(f"[HERBIE/text] calling respond_to_text…")
    raw_reply = respond_to_text(req.message, history)
    print(f"[HERBIE/text] raw_reply prefix: {raw_reply[:120]!r}")

    # Tool call: queue_job
    job_args = parse_job_marker(raw_reply)
    if job_args is not None:
        print(f"[HERBIE/text] JOB MARKER detected: {job_args}")
        reply = handle_job(job_args)
        print(f"[HERBIE/text] job reply: {reply[:200]!r}")
        _print_job_queue()
        history.append({"role": "user",      "content": req.message})
        history.append({"role": "assistant", "content": reply})
        _conversations[req.conversation_id] = history[-20:]
        return {"message": reply, "type": "job", "job": job_args}

    print(f"[HERBIE/text] no job marker, treating as chat")

    reply, action = parse_archive_action(raw_reply)
    if action:
        execute_archive_action(action)

    history.append({"role": "user", "content": req.message})
    history.append({"role": "assistant", "content": reply})
    _conversations[req.conversation_id] = history[-20:]
    return {"message": reply, "type": "chat"}




# ─────────────────────────────────────────────────────────────────────────────
# Jobs
# ─────────────────────────────────────────────────────────────────────────────

class JobRequest(BaseModel):
    job_type: str
    input_file_id: str
    params: dict = {}


@app.post("/jobs")
async def create_job(req: JobRequest):
    sc = _read_sidecar(req.input_file_id)
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
