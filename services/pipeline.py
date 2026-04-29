"""
Transport-agnostic message pipeline.

All transports (FastAPI, Telegram, WhatsApp, native app, …) call
handle_text or handle_audio. The transport is responsible only for
I/O: receiving the message, managing per-conversation history, and
forwarding the reply. No routing logic lives in transport code.

1. handle_text  — any text message or transcribed voice command
2. handle_audio — raw audio path + caller-supplied context string
"""

import functools

from services.llm import (
    _FILE_AUDIO_TOOL,
    format_filing_confirmation,
    respond_to_text,
)
from services.archive import get_slug_version, stage_audio, commit_audio
from services.transcribe import process_audio as _transcribe
from services.jobs import handle_job, parse_job_marker

print = functools.partial(print, flush=True)


def handle_text(message: str, history: list[dict]) -> dict:
    """
    1. Forward the message to the LLM tool loop (respond_to_text).
       The LLM selects the right tool — file_text, edit_entries,
       list_entries, read_entries, or queue_job — based on intent.
    2. queue_job exits the loop early with a marker string; execute
       the job side-effect here and return a job reply.
    3A. Return dict with keys: message (str), type ('chat' | 'job').
    3B. Job replies also carry a job key with the raw job args.
    """
    print(f"[pipeline/text] message={message[:80]!r}")
    raw = respond_to_text(message, history)

    job_args = parse_job_marker(raw)
    if job_args is not None:
        print(f"[pipeline/text] job marker: {job_args}")
        reply = handle_job(job_args)
        return {"message": reply, "type": "job", "job": job_args}

    return {"message": raw, "type": "chat"}


def handle_audio(
    tmp_path: str,
    ext: str,
    user_context: str,
    history: list[dict],
) -> dict:
    """
    1. Transcribe the audio with VAD + Whisper. Combine user_context
       with any speech_context the transcriber inferred.
    2. Stage the audio file into raw/ (generates file_id, copies bytes)
       WITHOUT creating an archive event yet.
    3. Pass the transcript to the normal LLM tool loop, prepending the
       file_audio tool so the LLM can choose to archive the recording.
       The file_audio handler closes over file_id so the event can be
       committed only if the LLM decides the content is worth keeping.
    4A. LLM calls file_audio → commit the event, return filing confirmation.
    4B. LLM calls any other tool (edit_entries, list_entries, etc.) →
        the instruction is executed, staged file is deleted, chat reply
        is returned. The audio was a voice command, not a recording.
    """
    print(f"[pipeline/audio] ext={ext} context={user_context!r}")

    processing = _transcribe(tmp_path)
    transcript = processing.get("speech_transcript", "")

    file_id, staged_path = stage_audio(tmp_path, ext)
    print(f"[pipeline/audio] staged {file_id[:8]}.{ext} transcript={transcript[:60]!r}")

    # Build an unambiguous message: the transcription is already done,
    # the text below IS the content — no separate audio to retrieve.
    body = transcript or "(no speech — instrumental or ambient recording)"
    parts = []
    if user_context:
        parts.append(f"Caption: {user_context}")
    parts.append(f'Voice note transcribed: "{body}"')
    llm_message = "\n".join(parts)

    committed: dict = {}

    def _file_audio(args: dict) -> str:
        slug = (args.get("slug") or "untitled").strip()
        tags: list[str] = args.get("tags") or []
        version = get_slug_version(slug) + 1
        event = commit_audio(file_id, slug, tags, ext, transcript)
        msg = format_filing_confirmation(
            slug=slug, ext=ext.lstrip("."), version=version,
            tags=tags, transcript=transcript,
        )
        committed["event"] = event
        committed["message"] = msg
        print(f"[pipeline/audio] committed {file_id[:8]} slug={slug}")
        return f"filed. file_id={file_id[:8]} slug={slug} tags={tags}"

    raw = respond_to_text(
        llm_message, history,
        extra_tools=[_FILE_AUDIO_TOOL],
        extra_handlers={"file_audio": _file_audio},
    )

    if not committed:
        staged_path.unlink(missing_ok=True)
        print(f"[pipeline/audio] voice command — staged file deleted")
        return {"message": raw, "type": "chat"}

    ev = committed["event"]
    return {
        "message": committed["message"],
        "file_id": file_id,
        "slug": ev["slug"],
        "tags": ev["tags"],
        "transcript": transcript,
        "type": "audio",
    }
