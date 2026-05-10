"""
Transport-agnostic message pipeline.

All transports (FastAPI, Telegram, WhatsApp, native app, …) call
handle_text or handle_audio. The transport is responsible only for
I/O: receiving the message, dispatching the returned segments to its
native primitives (reply_text + reply_document for Telegram, JSON
payload + frontend marker for web, etc.), and managing
per-conversation history. No routing or parsing logic lives in
transport code.

Every event — LLM call, archive operation, job execution, log entry —
is scoped to user_id so multi-user deployments stay isolated.

1. handle_text  — any text message or transcribed voice command
2. handle_audio — raw audio path + caller-supplied context string

Both functions accept user_id as their first positional argument and
return a result dict shaped like:
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
    user_id: str,
    message: str,
    history: list[dict],
    transport: str = "unknown",
) -> dict:
    """
    1. Check for the eval-flag prefix (3+ threes). If present, treat
       this turn as a pure feedback note: log it (scoped to user_id),
       ack, and return early via _result. The LLM is never called and
       the turn is signalled type='eval' so transports can skip pushing
       it into conversation history.
    2. Otherwise forward to the LLM tool loop (respond_to_text) scoped
       to user_id. The LLM selects the right tool — file_text,
       file_system_note, list_entries, read_entries, or queue_job —
       based on intent.
    3. queue_job exits the loop early with a marker string; execute
       the job side-effect here (scoped to user_id) and return a job
       reply.
    4. Log the full turn — user_id, input, llm_message, tool calls,
       reply — so every log row is traceable to the user.
    5. Hand back the result dict via _result, which always parses the
       reply into segments so the transport doesn't have to.
    """
    flagged, clean_message = detect_flag(message)
    print(f"[pipeline/text] flagged={flagged} message={clean_message[:80]!r}")

    if flagged:
        ack = "flagged for eval"
        log_turn(
            user_id=user_id, transport=transport, input_type="text",
            input_text=message, llm_message=clean_message,
            reply=ack, tool_calls=[], eval_candidate=True,
        )
        return _result(ack, "eval")

    raw, tool_calls = respond_to_text(user_id, clean_message, history)

    job_args = parse_job_marker(raw)
    if job_args is not None:
        print(f"[pipeline/text] job marker: {job_args}")
        reply = handle_job(user_id, job_args)
        log_turn(
            user_id=user_id, transport=transport, input_type="text",
            input_text=message, llm_message=clean_message,
            reply=reply, tool_calls=tool_calls, eval_candidate=False,
        )
        return _result(reply, "job", job=job_args)

    log_turn(
        user_id=user_id, transport=transport, input_type="text",
        input_text=message, llm_message=clean_message,
        reply=raw, tool_calls=tool_calls, eval_candidate=False,
    )
    return _result(raw, "chat")


def handle_audio(
    user_id: str,
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
    3. Stage the audio file into user_id's raw/ partition (generates
       file_id, copies bytes) WITHOUT creating an archive event yet.
    4. Pass the transcript to the normal LLM tool loop (scoped to
       user_id), prepending the file_audio tool so the LLM can choose
       to archive the recording. The file_audio handler closes over
       file_id so the event can be committed only if the LLM decides
       the content is worth keeping.
    5A. LLM calls file_audio → commit the event under user_id's archive,
        return filing confirmation.
    5B. LLM calls any other tool → the instruction is executed, staged
        file is deleted, chat reply is returned.
    6. Log the full turn (scoped to user_id) regardless of outcome.
    """
    print(f"[pipeline/audio] ext={ext} context={user_context!r}")

    processing = _transcribe(tmp_path)
    transcript = processing.get("speech_transcript", "")

    flagged, clean_transcript = detect_flag(transcript)

    file_id, staged_path = stage_audio(user_id, tmp_path, ext)
    print(f"[pipeline/audio] staged {file_id[:8]}.{ext} transcript={clean_transcript[:60]!r}")

    body = clean_transcript or "(no speech — instrumental or ambient recording)"
    parts = []
    if user_context:
        parts.append(f"Caption: {user_context}")
    parts.append(f'Voice note transcribed: "{body}"')
    parts.append(
        "The audio is staged. Default: call file_audio(slug, tags) for "
        "creative content (lyric, melody, hum, ambient). When in doubt, "
        "use this.\n\n"
        "If the transcript is itself an instruction targeting an earlier "
        "entry — strong signals: the phrase \"file system note\", or "
        "openers like \"no\", \"actually\", \"I mean\", \"wait\", \"fix\", "
        "\"delete\", \"rename\" — call file_system_note(content, "
        "target_file_id) instead. Resolve target via list_entries; if "
        "you can't identify one, file the note with target_file_id "
        "omitted.\n\n"
        "Do not reply with \"filed\" unless you actually called file_audio. "
        "If you called file_system_note, reply with a brief \"noted\"."
    )
    llm_message = "\n".join(parts)

    committed: dict = {}

    def _file_audio(args: dict) -> str:
        slug = (args.get("slug") or "untitled").strip()
        tags: list[str] = args.get("tags") or []
        version = get_slug_version(user_id, slug) + 1
        event = commit_audio(user_id, file_id, slug, tags, ext, clean_transcript)
        msg = format_filing_confirmation(
            slug=slug, ext=ext.lstrip("."), version=version,
            tags=tags, transcript=clean_transcript,
        )
        committed["event"] = event
        committed["message"] = msg
        print(f"[pipeline/audio] committed {file_id[:8]} slug={slug}")
        return f"filed. file_id={file_id[:8]} slug={slug} tags={tags}"

    raw, tool_calls = respond_to_text(
        user_id, llm_message, history,
        extra_tools=[_FILE_AUDIO_TOOL],
        extra_handlers={"file_audio": _file_audio},
    )

    if not committed:
        staged_path.unlink(missing_ok=True)
        print(f"[pipeline/audio] voice command — staged file deleted")
        log_turn(
            user_id=user_id, transport=transport, input_type="audio",
            input_text=user_context, llm_message=llm_message,
            reply=raw, tool_calls=tool_calls,
            transcript=transcript, eval_candidate=flagged,
        )
        return _result(raw, "chat")

    ev = committed["event"]
    reply = committed["message"]
    log_turn(
        user_id=user_id, transport=transport, input_type="audio",
        input_text=user_context, llm_message=llm_message,
        reply=reply, tool_calls=tool_calls,
        transcript=transcript, eval_candidate=flagged,
    )
    return _result(
        reply, "audio",
        file_id=file_id, slug=ev["slug"], tags=ev["tags"],
        transcript=clean_transcript,
    )
