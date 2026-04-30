"""
Transport-agnostic message pipeline.

All transports (FastAPI, Telegram, WhatsApp, native app, …) call
handle_text or handle_audio. The transport is responsible only for
I/O: receiving the message, dispatching the returned segments to its
native primitives (reply_text + reply_document for Telegram, JSON
payload + frontend marker for web, etc.), and managing
per-conversation history. No routing or parsing logic lives in
transport code.

1. handle_text  — any text message or transcribed voice command
2. handle_audio — raw audio path + caller-supplied context string

Both functions return a result dict shaped like:
  {
    "message":  str,           # raw LLM reply, for logging + history
    "segments": list[Segment], # parsed for rendering — text and
                               # resolved [[audio:<id>]] markers
    "type":     str,           # 'chat' | 'job' | 'eval' | 'audio'
    ...                        # type-specific extras (job args, slug)
  }
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
from services.conversation_log import detect_flag, log_turn
from services.render import parse_reply

print = functools.partial(print, flush=True)


def _result(message: str, type_: str, **extra) -> dict:
    """
    Build the dict every transport receives. parse_reply runs on
    every reply, even deterministic ones (filing confirmations,
    eval acks, job replies) — for marker-less strings it returns a
    single text segment, so transports never special-case.
    """
    return {
        "message":  message,
        "segments": parse_reply(message),
        "type":     type_,
        **extra,
    }


def handle_text(
    message: str,
    history: list[dict],
    transport: str = "unknown",
) -> dict:
    """
    1. Check for the eval-flag prefix (3+ threes). If present, treat
       this turn as a pure feedback note: log it, ack, and return
       early via _result. The LLM is never called and the turn is
       signalled type='eval' so transports can skip pushing it into
       conversation history.
    2. Otherwise forward to the LLM tool loop (respond_to_text). The
       LLM selects the right tool — file_text, file_system_note,
       list_entries, read_entries, or queue_job — based on intent.
    3. queue_job exits the loop early with a marker string; execute
       the job side-effect here and return a job reply.
    4. Log the full turn — input, llm_message, tool calls, reply.
    5. Hand back the result dict via _result, which always parses the
       reply into segments so the transport doesn't have to.
    """
    flagged, clean_message = detect_flag(message)
    print(f"[pipeline/text] flagged={flagged} message={clean_message[:80]!r}")

    if flagged:
        ack = "flagged for eval"
        log_turn(
            transport=transport, input_type="text",
            input_text=message, llm_message=clean_message,
            reply=ack, tool_calls=[], eval_candidate=True,
        )
        return _result(ack, "eval")

    raw, tool_calls = respond_to_text(clean_message, history)

    job_args = parse_job_marker(raw)
    if job_args is not None:
        print(f"[pipeline/text] job marker: {job_args}")
        reply = handle_job(job_args)
        log_turn(
            transport=transport, input_type="text",
            input_text=message, llm_message=clean_message,
            reply=reply, tool_calls=tool_calls, eval_candidate=False,
        )
        return _result(reply, "job", job=job_args)

    log_turn(
        transport=transport, input_type="text",
        input_text=message, llm_message=clean_message,
        reply=raw, tool_calls=tool_calls, eval_candidate=False,
    )
    return _result(raw, "chat")


def handle_audio(
    tmp_path: str,
    ext: str,
    user_context: str,
    history: list[dict],
    transport: str = "unknown",
) -> dict:
    """
    1. Transcribe the audio with VAD + Whisper. Combine user_context
       with any speech_context the transcriber inferred.
    2. Check for the eval-flag prefix in the transcribed text. If the
       user spoke the flag prefix, strip it and mark as eval candidate.
    3. Stage the audio file into raw/ (generates file_id, copies bytes)
       WITHOUT creating an archive event yet.
    4. Pass the transcript to the normal LLM tool loop, prepending the
       file_audio tool so the LLM can choose to archive the recording.
       The file_audio handler closes over file_id so the event can be
       committed only if the LLM decides the content is worth keeping.
    5A. LLM calls file_audio → commit the event, return filing confirmation.
    5B. LLM calls any other tool → the instruction is executed, staged
        file is deleted, chat reply is returned.
    6. Log the full turn regardless of outcome.
    """
    print(f"[pipeline/audio] ext={ext} context={user_context!r}")

    processing = _transcribe(tmp_path)
    transcript = processing.get("speech_transcript", "")

    flagged, clean_transcript = detect_flag(transcript)

    file_id, staged_path = stage_audio(tmp_path, ext)
    print(f"[pipeline/audio] staged {file_id[:8]}.{ext} transcript={clean_transcript[:60]!r}")

    body = clean_transcript or "(no speech — instrumental or ambient recording)"
    parts = []
    if user_context:
        parts.append(f"Caption: {user_context}")
    parts.append(f'Voice note transcribed: "{body}"')
    parts.append(
        "The audio file is already staged. "
        "Default: call file_audio(slug, tags) to archive it. "
        "Only skip filing if the transcript is clearly an instruction or question — "
        "when in doubt, treat it as creative content and file it."
    )
    llm_message = "\n".join(parts)

    committed: dict = {}

    def _file_audio(args: dict) -> str:
        slug = (args.get("slug") or "untitled").strip()
        tags: list[str] = args.get("tags") or []
        version = get_slug_version(slug) + 1
        event = commit_audio(file_id, slug, tags, ext, clean_transcript)
        msg = format_filing_confirmation(
            slug=slug, ext=ext.lstrip("."), version=version,
            tags=tags, transcript=clean_transcript,
        )
        committed["event"] = event
        committed["message"] = msg
        print(f"[pipeline/audio] committed {file_id[:8]} slug={slug}")
        return f"filed. file_id={file_id[:8]} slug={slug} tags={tags}"

    raw, tool_calls = respond_to_text(
        llm_message, history,
        extra_tools=[_FILE_AUDIO_TOOL],
        extra_handlers={"file_audio": _file_audio},
    )

    if not committed:
        staged_path.unlink(missing_ok=True)
        print(f"[pipeline/audio] voice command — staged file deleted")
        log_turn(
            transport=transport, input_type="audio",
            input_text=user_context, llm_message=llm_message,
            reply=raw, tool_calls=tool_calls,
            transcript=transcript, eval_candidate=flagged,
        )
        return _result(raw, "chat")

    ev = committed["event"]
    reply = committed["message"]
    log_turn(
        transport=transport, input_type="audio",
        input_text=user_context, llm_message=llm_message,
        reply=reply, tool_calls=tool_calls,
        transcript=transcript, eval_candidate=flagged,
    )
    return _result(
        reply, "audio",
        file_id=file_id, slug=ev["slug"], tags=ev["tags"],
        transcript=clean_transcript,
    )
