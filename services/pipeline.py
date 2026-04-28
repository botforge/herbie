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

from services.llm import format_filing_confirmation, respond_to_audio, respond_to_text
from services.archive import get_slug_version, ingest_audio
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
    1. Transcribe the audio with VAD + Whisper via process_audio.
       Combine the caller-supplied user_context with any speech_context
       the transcriber inferred (key/BPM/etc.) into a single string.
    2. Ask the LLM to name the entry (slug, project, tags) via
       respond_to_audio, which returns strict JSON — no prose.
    3. Prepend the project tag if it isn't already in the tag list,
       then look up the current version count for the slug so the
       confirmation filename is correct (slug.ogg vs slug_v2.ogg).
    4. Ingest into the archive: appends an event to events.jsonl and
       copies the raw file into raw/.
    5. Build the filing confirmation with format_filing_confirmation
       and return it alongside the event metadata the transport needs.
    Returns dict: message, file_id, slug, tags, transcript.
    """
    print(f"[pipeline/audio] ext={ext} context={user_context!r}")

    processing = _transcribe(tmp_path)
    transcript = processing.get("speech_transcript", "")
    speech_context = processing.get("speech_context", "")
    combined_context = " | ".join(filter(None, [user_context, speech_context]))

    llm_result = respond_to_audio(
        transcript=transcript,
        user_context=combined_context,
        conversation_history=history,
        existing_version=1,
        file_ext=ext,
    )

    slug = llm_result["slug"]
    proj = llm_result["project"]
    tags: list[str] = llm_result.get("tags", [])
    if proj and proj not in tags:
        tags.insert(0, proj)

    version = get_slug_version(slug) + 1
    event = ingest_audio(tmp_path, slug, tags, ext, transcript)

    message = format_filing_confirmation(
        slug=slug, ext=ext, version=version, tags=tags, transcript=transcript,
    )
    print(f"[pipeline/audio] filed {event['file_id'][:8]} slug={slug}")

    return {
        "message": message,
        "file_id": event["file_id"],
        "slug": slug,
        "tags": tags,
        "transcript": transcript,
    }
