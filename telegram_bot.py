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

from services.archive import ensure_archive_root
from services import pipeline

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
    from services.archive import RAW_DIR, current_entry
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
        sc  = current_entry(fid)
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
    """
    1. Pass every text message directly to the LLM tool loop — no
       pre-classification. The LLM decides whether to call file_text,
       edit_entries, list_entries, read_entries, or queue_job.
    2. If the user is replying to a prior bot message, prepend that
       original message as context so the LLM knows exactly which file
       is being referenced without needing to search.
    3. queue_job exits the tool loop early with a marker string; handle
       the side-effect here and push the job reply to history.
    """
    if not _is_allowed(update):
        return

    msg = update.message.text.strip()
    chat_id = update.effective_chat.id
    history = _history(chat_id)

    reply_context = _extract_reply_context(update)
    llm_msg = f"[replying to: {reply_context}]\n{msg}" if reply_context else msg

    result = pipeline.handle_text(llm_msg, history)
    reply = result["message"]

    await _send(update, reply)
    _push(chat_id, "user", llm_msg)
    _push(chat_id, "assistant", reply)


# ── Thinking indicator ────────────────────────────────────────────────────────

async def _thinking_indicator(update: Update) -> None:
    """Immediate placeholder sent while the pipeline processes audio.
    Swap the string here to change the feel — this is the only place to touch."""
    await update.message.reply_text("...")


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

    await _ingest_audio(update, context, tmp_path, ext="ogg")


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

    await _ingest_audio(update, context, tmp_path, ext=ext)


async def _ingest_audio(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    tmp_path: str,
    ext: str,
):
    """
    1. Forward tmp_path + any user caption to pipeline.handle_audio,
       which transcribes, names, and ingests the file in one call.
    2. Send the filing confirmation reply and push both sides of the
       exchange into this chat's conversation history.
    3. Always delete the tmp file on exit, even if the pipeline raised.
    """
    chat_id = update.effective_chat.id
    history = _history(chat_id)

    await _thinking_indicator(update)

    try:
        user_context = update.message.caption or ""
        result = pipeline.handle_audio(tmp_path, ext, user_context, history)
        reply = result["message"]

        await _send(update, reply)
        _push(chat_id, "user", user_context or result.get("transcript") or "(voice note)")
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
