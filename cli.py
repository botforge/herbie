#!/usr/bin/env python3
"""
Herbie CLI — local interaction before Telegram integration.

Usage:
  python cli.py                          # chat mode
  python cli.py --audio file.ogg         # ingest audio
  python cli.py --audio file.ogg --context "hospital, op-1"
  python cli.py --lyrics                 # paste lyrics (Ctrl+D to submit)
  python cli.py --lyrics --project hospital
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from services.archive import (
    ensure_archive_root,
    file_audio,
    file_lyrics,
    get_next_version,
    get_project_files,
    get_projects,
)
from services.llm import (
    build_archive_context,
    detect_lyric_intent,
    detect_read_query,
    extract_lyric_project,
    format_read_response,
    respond_to_audio,
    respond_to_text,
)
from services.transcribe import process_audio


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def herbie_say(text: str):
    print(f"Herbie: {text}")


def _infer_project(text: str, projects: list[dict]) -> str | None:
    """Return the first project name found in text, or None."""
    lower = text.lower()
    for p in projects:
        if p["name"].lower() in lower:
            return p["name"]
    return None


def filed_say(filename: str):
    print(f"-> filed as {filename}")


# ─────────────────────────────────────────────────────────────────────────────
# Chat mode
# ─────────────────────────────────────────────────────────────────────────────

def run_chat():
    print("Herbie is ready. Type 'exit' to quit.\n")
    history = []

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

        # Lyric detection
        if detect_lyric_intent(user_input):
            project_name = extract_lyric_project(user_input) or "sketches"
            import re
            text = user_input
            trigger_patterns = [
                r"^[a-z0-9_-]+\s+lyr(?:ic|ics)\s*\n?",
                r"^lyr(?:ic|ics)\s+for\s+[a-z0-9_-]+\s*\n?",
                r"^words\s+for\s+[a-z0-9_-]+\s*\n?",
                r"^verse\s+for\s+[a-z0-9_-]+\s*\n?",
                r"^chorus\s+for\s+[a-z0-9_-]+\s*\n?",
                r"^hook\s+for\s+[a-z0-9_-]+\s*\n?",
                r"^bridge\s+for\s+[a-z0-9_-]+\s*\n?",
            ]
            for pat in trigger_patterns:
                text = re.sub(pat, "", text, flags=re.IGNORECASE).strip()

            _, diff_summary = file_lyrics(project_name, text)
            msg = f"filed as {project_name}_lyrics — {diff_summary}."
            herbie_say(msg)
            history.append({"role": "user", "content": user_input})
            history.append({"role": "assistant", "content": msg})
            continue

        all_projects = get_projects()
        project_names = [p["name"] for p in all_projects]

        # Handle read queries directly — don't send to LLM
        read_type, read_proj = detect_read_query(user_input, project_names)
        if read_type and read_proj:
            files = get_project_files(read_proj)
            reply = format_read_response(read_type, read_proj, files)
            herbie_say(reply)
            history.append({"role": "user", "content": user_input})
            history.append({"role": "assistant", "content": reply})
            history = history[-20:]
            continue

        # LLM chat — inject real archive state so it doesn't hallucinate
        active_proj = _infer_project(user_input, all_projects) or _infer_project(
            " ".join(m["content"] for m in history[-4:]), all_projects
        )
        active_files = get_project_files(active_proj) if active_proj else None
        archive_ctx = build_archive_context(all_projects, active_proj or "", active_files)
        raw_reply = respond_to_text(user_input, history, archive_context=archive_ctx)
        herbie_say(raw_reply)
        history.append({"role": "user", "content": user_input})
        history.append({"role": "assistant", "content": raw_reply})
        history = history[-20:]


# ─────────────────────────────────────────────────────────────────────────────
# Audio mode
# ─────────────────────────────────────────────────────────────────────────────

def run_audio(audio_path: str, context: str = "", project: str = ""):
    path = Path(audio_path)
    if not path.exists():
        print(f"Error: file not found: {audio_path}", file=sys.stderr)
        sys.exit(1)

    print(f"Processing {path.name}...")

    ensure_archive_root()

    processing = process_audio(str(path))
    transcript = processing.get("speech_transcript", "")
    speech_context = processing.get("speech_context", "")
    key = processing.get("key", "unknown")
    bpm = processing.get("bpm")

    short = transcript[:80] + "..." if len(transcript) > 80 else transcript or "(none)"
    print(f"  transcript: {short}")

    combined_context = " | ".join(filter(None, [context, speech_context]))
    ext = path.suffix.lstrip(".")

    llm_result = respond_to_audio(
        transcript=transcript,
        user_context=combined_context,
        conversation_history=[],
        existing_version=1,
        file_ext=ext,
    )

    proj = project or llm_result["project"]
    slug = llm_result["slug"]
    tags = llm_result.get("tags", [])
    message = llm_result.get("message", "filed.")

    existing_ver = get_next_version(proj, slug)
    if existing_ver > 1:
        llm_result2 = respond_to_audio(
            transcript=transcript,
            user_context=combined_context,
            conversation_history=[],
            existing_version=existing_ver,
            file_ext=ext,
        )
        slug = llm_result2["slug"] or slug
        message = llm_result2.get("message", message)

    metadata = {
        "transcript": transcript,
        "tags": tags,
        "created_at": datetime.now().isoformat(),
    }
    saved_path = file_audio(str(path), slug, proj, metadata, ext=ext)

    herbie_say(message)
    filed_say(Path(saved_path).name)


# ─────────────────────────────────────────────────────────────────────────────
# Lyrics mode
# ─────────────────────────────────────────────────────────────────────────────

def run_lyrics(project: str = ""):
    proj = project or "sketches"
    print(f"Lyrics mode — project: {proj}")
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
    _, diff_summary = file_lyrics(proj, text)
    herbie_say(diff_summary)


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Herbie — personal music archivist CLI"
    )
    parser.add_argument("--audio", metavar="FILE", help="ingest an audio file")
    parser.add_argument("--context", default="", help="text context for audio")
    parser.add_argument("--project", default="", help="project name override")
    parser.add_argument("--lyrics", action="store_true", help="lyrics input mode")
    args = parser.parse_args()

    if args.audio:
        run_audio(args.audio, context=args.context, project=args.project)
    elif args.lyrics:
        run_lyrics(project=args.project)
    else:
        run_chat()


if __name__ == "__main__":
    main()
