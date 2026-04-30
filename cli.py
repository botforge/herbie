#!/usr/bin/env python3
"""
Lila CLI — local interaction transport.

Calls the same pipeline.handle_text / pipeline.handle_audio as the
Telegram bot and FastAPI server. No transport-specific logic — the
LLM tool loop decides whether each message becomes a file_text,
file_system_note, queue_job, or read.

Usage:
  python cli.py                          # chat mode
  python cli.py --audio file.ogg         # ingest one audio file
  python cli.py --audio file.ogg --context "hospital, op-1"
  python cli.py --lyrics                 # paste lyrics on stdin (Ctrl+D submits)
  python cli.py --lyrics --project hospital
"""

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from services.archive import ensure_archive_root
from services import pipeline


def lila_say(text: str):
    print(f"Lila: {text}")


# ─────────────────────────────────────────────────────────────────────────────
# Chat mode
# ─────────────────────────────────────────────────────────────────────────────

def run_chat():
    """
    1. Read one line at a time from stdin.
    2. Forward it to pipeline.handle_text and print the reply.
    3. Push the turn into a per-process conversation history so
       multi-turn chats work — except for eval-flagged turns (333+
       prefix), which the pipeline signals via type='eval' and we
       skip so they never enter LLM context.
    """
    print("Lila is ready. Type 'exit' to quit.\n")
    history: list[dict] = []

    while True:
        try:
            user_input = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit", "q"):
            break

        result = pipeline.handle_text(user_input, history, transport="cli")
        lila_say(result["message"])

        if result.get("type") != "eval":
            history.append({"role": "user",      "content": user_input})
            history.append({"role": "assistant", "content": result["message"]})
            history = history[-20:]


# ─────────────────────────────────────────────────────────────────────────────
# Audio mode
# ─────────────────────────────────────────────────────────────────────────────

def run_audio(audio_path: str, context: str = ""):
    """
    1. Resolve the on-disk file and ensure the archive exists.
    2. Forward path + ext + caller-supplied context to
       pipeline.handle_audio. The pipeline transcribes, stages the
       file, lets the LLM choose to file_audio or treat it as a
       voice command, and commits or discards accordingly.
    3. Print the reply (filing confirmation or chat-style response).
    """
    path = Path(audio_path)
    if not path.exists():
        print(f"Error: file not found: {audio_path}", file=sys.stderr)
        sys.exit(1)

    ensure_archive_root()
    print(f"Processing {path.name}...")

    ext = path.suffix.lstrip(".") or "ogg"
    result = pipeline.handle_audio(str(path), ext, context, [], transport="cli")
    lila_say(result["message"])


# ─────────────────────────────────────────────────────────────────────────────
# Lyrics mode
# ─────────────────────────────────────────────────────────────────────────────

def run_lyrics(project: str = ""):
    """
    1. Read the lyric paste from stdin (Ctrl+D ends).
    2. Frame it with the optional project hint so the LLM tags the
       new file_text event correctly, then forward to
       pipeline.handle_text. The LLM picks the slug and tag set.
    """
    print("Lyrics mode" + (f" — project: {project}" if project else "") + ".")
    print("Paste or type lyrics below. Press Ctrl+D when done.\n")

    try:
        text = sys.stdin.read().strip()
    except KeyboardInterrupt:
        print()
        return

    if not text:
        print("No lyrics received.")
        return

    ensure_archive_root()
    framed = f"Lyrics for {project}:\n\n{text}" if project else f"Lyrics:\n\n{text}"
    result = pipeline.handle_text(framed, [], transport="cli")
    lila_say(result["message"])


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Lila — personal music archivist CLI")
    parser.add_argument("--audio",   metavar="FILE", help="ingest an audio file")
    parser.add_argument("--context", default="",     help="text context for audio")
    parser.add_argument("--project", default="",     help="project name (lyrics mode)")
    parser.add_argument("--lyrics",  action="store_true", help="lyrics input mode")
    args = parser.parse_args()

    if args.audio:
        run_audio(args.audio, context=args.context)
    elif args.lyrics:
        run_lyrics(project=args.project)
    else:
        run_chat()


if __name__ == "__main__":
    main()
