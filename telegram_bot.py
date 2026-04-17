#!/usr/bin/env python3
"""
Herbie — Telegram bot transport layer.

Calls the exact same service functions as the CLI and FastAPI server.
All core logic lives in services/. This file is only I/O plumbing.

Usage:
  python telegram_bot.py

Required .env:
  TELEGRAM_BOT_TOKEN=...
  TELEGRAM_ALLOWED_USER_ID=...   # your numeric Telegram user ID (optional but recommended)
"""

import asyncio
import logging
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

from services.archive import (
    ensure_archive_root,
    file_audio,
    file_lyrics,
    get_next_version,
)
from services.archive import execute_archive_action
from services.llm import (
    detect_lyric_intent,
    extract_lyric_project,
    parse_archive_action,
    respond_to_audio,
    respond_to_text,
)
from services.transcribe import process_audio
from services.jobs import (
    handle_job,
    parse_job_marker,
)

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("herbie.telegram")

# ── Auth ─────────────────────────────────────────────────────────────────────

_ALLOWED_ID = os.getenv("TELEGRAM_ALLOWED_USER_ID", "").strip()


def _is_allowed(update: Update) -> bool:
    if not _ALLOWED_ID:
        return True  # open if no allowlist configured
    return str(update.effective_user.id) == _ALLOWED_ID


# ── Per-user conversation history ────────────────────────────────────────────

# { chat_id: [{"role": ..., "content": ...}] }
_conversations: dict[int, list[dict]] = {}


def _history(chat_id: int) -> list[dict]:
    return _conversations.setdefault(chat_id, [])


def _push(chat_id: int, role: str, content: str):
    hist = _history(chat_id)
    hist.append({"role": role, "content": content})
    _conversations[chat_id] = hist[-20:]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_reply_context(update: Update) -> str | None:
    """
    If the user is replying to a previous Telegram message, return a
    short description of that original message so the LLM knows exactly
    which file or exchange is being referenced.

    Returns None if there is no reply context.
    """
    replied = getattr(update.message, "reply_to_message", None)
    if replied is None:
        return None

    parts = []

    # Was it a bot message (likely a filing confirmation)?
    if replied.text:
        parts.append(replied.text[:300])

    # Was it a voice note?
    if replied.voice:
        parts.append(f"voice note (duration {replied.voice.duration}s)")

    # Was it an audio file?
    if replied.audio:
        name = replied.audio.file_name or "audio file"
        parts.append(f"audio file: {name}")

    # Was it a document (audio sent as file)?
    if replied.document:
        name = replied.document.file_name or "document"
        parts.append(f"document: {name}")

    # Caption on a media message
    if replied.caption:
        parts.append(f"caption: {replied.caption[:100]}")

    return " | ".join(parts) if parts else None


AUDIO_MARKER_RE = re.compile(r"\[\[audio:([a-fA-F0-9]{8})\]\]")


async def _send(update: Update, text: str):
    """
    Send a reply, expanding [[audio:<file_id>]] markers into actual file
    attachments (sendDocument — preserves filename and lets the user save
    the raw audio). Text chunks between markers go as reply_text.
    """
    from services.archive import RAW_DIR, _read_sidecar
    from telegram import InputFile

    markers = list(AUDIO_MARKER_RE.finditer(text))
    log.info(f"[telegram/_send] reply_len={len(text)} markers={[m.group(1) for m in markers]}")

    if not markers:
        if not text.strip():
            await update.message.reply_text("(empty response)")
            return
        for chunk in _split(text, 4096):
            await update.message.reply_text(chunk)
        return

    last = 0
    for m in markers:
        pre = text[last : m.start()].strip()
        if pre:
            for chunk in _split(pre, 4096):
                await update.message.reply_text(chunk)

        fid = m.group(1).lower()
        sc  = _read_sidecar(fid)
        log.info(f"[telegram/_send] marker {fid} sidecar_found={bool(sc)}")

        if not sc:
            await update.message.reply_text(f"(audio {fid} not found)")
        else:
            ext  = "." + (sc.get("ext") or "ogg").lstrip(".")
            path = RAW_DIR / f"{fid}{ext}"
            log.info(f"[telegram/_send] resolving path={path} exists={path.exists()}")
            if not path.exists():
                await update.message.reply_text(f"(audio file missing on disk: {path.name})")
            else:
                slug     = sc.get("slug", "") or fid
                filename = f"{slug}{ext}"
                try:
                    with path.open("rb") as f:
                        doc = InputFile(f, filename=filename)
                        log.info(f"[telegram/_send] reply_document for {fid} as {filename}")
                        await update.message.reply_document(document=doc, caption=slug or None)
                except Exception as e:
                    log.error(f"[telegram/_send] send failed for {fid}: {e}", exc_info=True)
                    await update.message.reply_text(f"(couldn't send {fid}: {e})")

        last = m.end()

    tail = text[last:].strip()
    if tail:
        for chunk in _split(tail, 4096):
            await update.message.reply_text(chunk)


def _split(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks, start = [], 0
    while start < len(text):
        chunks.append(text[start : start + limit])
        start += limit
    return chunks


# ── Text handler ──────────────────────────────────────────────────────────────

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return

    msg = update.message.text.strip()
    chat_id = update.effective_chat.id
    history = _history(chat_id)

    # Lyric submission (multi-line with trigger prefix)
    if detect_lyric_intent(msg):
        project_name = extract_lyric_project(msg) or "sketches"
        import re
        text = msg
        for pat in [
            r"^[a-z0-9_-]+\s+lyrics?\s*\n?",
            r"^lyrics?\s+for\s+[a-z0-9_-]+\s*\n?",
            r"^words\s+for\s+[a-z0-9_-]+\s*\n?",
            r"^verse\s+for\s+[a-z0-9_-]+\s*\n?",
            r"^chorus\s+for\s+[a-z0-9_-]+\s*\n?",
        ]:
            text = re.sub(pat, "", text, flags=re.IGNORECASE).strip()

        _, diff_summary = file_lyrics(project_name, text)
        reply = diff_summary
        await _send(update, reply)
        _push(chat_id, "user", msg)
        _push(chat_id, "assistant", reply)
        return

    # If the user is replying to a specific Telegram message, surface that
    # original message content to the LLM so it knows exactly which file
    # is being referenced — even if many things were filed in between.
    reply_context = _extract_reply_context(update)

    llm_msg = msg
    if reply_context:
        llm_msg = f"[replying to: {reply_context}]\n{msg}"

    # LLM chat — no archive snapshot, no project inference. Tools do it.
    raw_reply = respond_to_text(llm_msg, history)

    # Tool call: queue_job
    job_args = parse_job_marker(raw_reply)
    if job_args is not None:
        log.info(f"[telegram] JOB MARKER: {job_args}")
        reply = handle_job(job_args)
    else:
        reply, action = parse_archive_action(raw_reply)
        if action:
            execute_archive_action(action)

    await _send(update, reply)
    _push(chat_id, "user", llm_msg)
    _push(chat_id, "assistant", reply)


# ── Voice / audio handler ─────────────────────────────────────────────────────

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles voice messages (OGG/Opus from Telegram mic)."""
    if not _is_allowed(update):
        return

    voice = update.message.voice
    file = await context.bot.get_file(voice.file_id)

    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        await file.download_to_drive(tmp.name)
        tmp_path = tmp.name

    await update.message.reply_text("filing — i'll let you know when i'm done.")
    asyncio.create_task(_ingest_audio(update, context, tmp_path, ext="ogg"))


async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles audio files sent as documents or audio attachments."""
    if not _is_allowed(update):
        return

    audio = update.message.audio or update.message.document
    orig_name = getattr(audio, "file_name", None) or "audio.ogg"
    ext = Path(orig_name).suffix.lstrip(".") or "ogg"

    file = await context.bot.get_file(audio.file_id)
    with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as tmp:
        await file.download_to_drive(tmp.name)
        tmp_path = tmp.name

    await update.message.reply_text("filing — i'll let you know when i'm done.")
    asyncio.create_task(_ingest_audio(update, context, tmp_path, ext=ext))


async def _ingest_audio(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    tmp_path: str,
    ext: str,
):
    """Shared pipeline for voice and audio file ingestion."""
    chat_id = update.effective_chat.id
    history = _history(chat_id)

    try:
        # 1. VAD + Whisper
        processing = process_audio(tmp_path)
        transcript = processing.get("speech_transcript", "")
        speech_context = processing.get("speech_context", "")

        # Caption or reply text as additional context
        user_caption = update.message.caption or ""
        combined_context = " | ".join(filter(None, [user_caption, speech_context]))

        # 2. LLM naming
        llm_result = respond_to_audio(
            transcript=transcript,
            user_context=combined_context,
            conversation_history=history,
            existing_version=1,
            file_ext=ext,
        )

        proj = llm_result["project"]
        slug = llm_result["slug"]
        tags = llm_result.get("tags", [])
        message = llm_result.get("message", "filed.")

        # Version check
        existing_ver = get_next_version(proj, slug)
        if existing_ver > 1:
            llm_result2 = respond_to_audio(
                transcript=transcript,
                user_context=combined_context,
                conversation_history=history,
                existing_version=existing_ver,
                file_ext=ext,
            )
            slug = llm_result2.get("slug") or slug
            message = llm_result2.get("message", message)

        # 3. Archive
        metadata = {
            "transcript": transcript,
            "tags": tags,
            "created_at": datetime.now().isoformat(),
        }
        saved_path = file_audio(tmp_path, slug, proj, metadata, ext=ext)
        saved_filename = Path(saved_path).name

        reply = f"{proj} / {saved_filename}\n{message}"
        await _send(update, reply)

        _push(chat_id, "user", combined_context or transcript or "(voice note)")
        _push(chat_id, "assistant", reply)

    except Exception as e:
        log.exception("audio ingest failed")
        await update.message.reply_text(f"something went wrong: {e}")
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# ── /improvements command ─────────────────────────────────────────────────────

_IMPROVEMENTS_FILE = Path(__file__).parent / "improvements.md"


async def handle_improvements(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /improvements <idea>
    Appends the idea to improvements.md with a timestamp.
    Does not touch any code or affect the running bot.
    """
    if not _is_allowed(update):
        return

    text = " ".join(context.args).strip() if context.args else ""

    if not text:
        await update.message.reply_text(
            "send the improvement inline:\n/improvements your idea here"
        )
        return

    ts = datetime.now().strftime("%Y-%m-%d %H:%M")

    # Init file with header if it doesn't exist
    if not _IMPROVEMENTS_FILE.exists():
        _IMPROVEMENTS_FILE.write_text("# Herbie — improvements backlog\n\n")

    with _IMPROVEMENTS_FILE.open("a") as f:
        f.write(f"- [{ts}] {text}\n")

    await update.message.reply_text(f"logged: {text}")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set in .env")

    ensure_archive_root()

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("improvements", handle_improvements))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.AUDIO | filters.Document.AUDIO, handle_audio))

    log.info("Herbie bot starting...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
