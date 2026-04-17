"""
LLM layer: OpenRouter calls via the openai SDK.

Public surface (called by FastAPI routes and CLI alike):
  respond_to_audio(transcript, key, bpm, user_context, conversation_history) → dict
  respond_to_text(message, conversation_history) → str
  detect_lyric_intent(text) → bool
  append_project_note(project_name, note_type, content) → None  (fire-and-forget helper)
"""

import functools
import json
import os
import re
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

print = functools.partial(print, flush=True)

_SOUL_PATH = Path(__file__).parent.parent / "soul.md"


def _soul() -> str:
    try:
        return _SOUL_PATH.read_text()
    except FileNotFoundError:
        return "You are Herbie, a personal music archivist."


def _client() -> OpenAI:
    return OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=os.getenv("OPENROUTER_API_KEY", ""),
    )


def _model() -> str:
    return os.getenv("MODEL", "google/gemini-2.0-flash-lite-001")


# ---------------------------------------------------------------------------
# Audio ingestion response
# ---------------------------------------------------------------------------

_AUDIO_SYSTEM = """{soul}

When responding to an audio submission you MUST reply with valid JSON only.
No prose before or after the JSON block.

Schema:
{{
  "project": "<project-slug>",
  "slug": "<semantic-idea-slug>",
  "tags": ["<tag>", ...],
  "message": "<one or two line musician-to-musician confirmation>"
}}

Rules:
- project: the song or project this belongs to, lowercase hyphens only
- slug: 2-4 words, kebab-case. Pick the type that is most specific to
  this particular idea — all three are valid:

  Source-based:   "youtube-drone-pad", "op1-worm-strings"
  Evocative:      "air-conditioner-drone", "broken-tape-loop"
  Functional:     "drone-pad-opening", "bridge-variation-strings"

  Never use a slug so generic it could describe anything.
- if version > 1, acknowledge it naturally in the message
- message: one line confirming what was filed, ask at most one question
  if something critical is missing
"""


def respond_to_audio(
    transcript: str,
    user_context: str,
    conversation_history: list[dict],
    existing_version: int = 1,
    file_ext: str = "ogg",
) -> dict:
    """
    Ask the LLM to name and confirm an audio file submission.
    Returns parsed dict with keys: project, slug, tags, message.
    """
    soul = _soul()
    system = _AUDIO_SYSTEM.format(soul=soul)

    user_lines = []
    if user_context:
        user_lines.append(f"User context: {user_context}")
    if transcript:
        user_lines.append(f"Transcript of audio: {transcript}")
    user_lines.append(f"File extension: {file_ext}")
    if existing_version > 1:
        user_lines.append(f"This is version {existing_version} (previous versions exist).")

    messages = [{"role": "system", "content": system}]
    messages += conversation_history[-6:]
    messages.append({"role": "user", "content": "\n".join(user_lines)})

    try:
        resp = _client().chat.completions.create(
            model=_model(),
            messages=messages,
            temperature=0.3,
        )
        raw = resp.choices[0].message.content.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        # Extract first {...} block — guards against trailing prose after JSON
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            raw = m.group(0)
        data = json.loads(raw)
        slug = data.get("slug") or f"untitled-{_timestamp()}"
        return {
            "project": data.get("project", "sketches"),
            "slug": slug,
            "tags": data.get("tags", []),
            "message": data.get("message", "filed."),
        }
    except Exception as e:
        return {
            "project": "sketches",
            "slug": f"untitled-{_timestamp()}",
            "tags": [],
            "message": f"filed (llm error: {e}). what's this for?",
        }


# ---------------------------------------------------------------------------
# Text / chat response
# ---------------------------------------------------------------------------

_JOB_TOOL = {
    "type": "function",
    "function": {
        "name": "queue_job",
        "description": (
            "Queue a job that creates a NEW file in the archive."
            "\n\n"
            "Job types and required args:"
            "\n  to_midi       — needs file_id  (audio file to convert)"
            "\n  stem_split    — needs file_id"
            "\n  autotune      — needs file_id"
            "\n  transpose     — needs file_id (and optional semitones param)"
            "\n  render_chords — needs chords (list of chord symbols like "
            "                   ['Em','Am','D','G']). Optional tag to attach."
            "\n\n"
            "Do NOT call for reading, listing, or summarizing — use "
            "list_entries / read_entries. Never guess a file_id from prior "
            "context. For chord rendering, pass the chord symbols the user "
            "actually named in this message."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "job_type": {
                    "type": "string",
                    "enum": ["to_midi", "stem_split", "autotune",
                             "transpose", "render_chords"],
                },
                "file_id": {
                    "type": "string",
                    "description": "file_id supplied by the user. Required for audio jobs.",
                },
                "chords": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "For render_chords: chord symbols e.g. ['Em','Am','D','G'].",
                },
                "tag": {
                    "type": "string",
                    "description": "For render_chords: project/tag name to attach to the output.",
                },
                "semitones": {
                    "type": "integer",
                    "description": "For transpose: semitones to shift (positive up, negative down).",
                },
            },
            "required": ["job_type"],
        },
    },
}

_LIST_ENTRIES_TOOL = {
    "type": "function",
    "function": {
        "name": "list_entries",
        "description": (
            "Fetch compact metadata (file_id, slug, tags, when) for recent "
            "archive entries. Use this to answer listing questions or to find "
            "a file_id. Format the result in your reply however fits the user's "
            "intent (clean monospace list, conversational summary, etc.)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tag":   {"type": "string",  "description": "Optional tag filter."},
                "limit": {"type": "integer", "description": "Default 15."},
            },
            "required": [],
        },
    },
}

_FILE_TEXT_TOOL = {
    "type": "function",
    "function": {
        "name": "file_text",
        "description": (
            "File a text/lyric/note entry into the archive. Call this whenever "
            "the user sends lyrics, a fragment, a written note, or any text "
            "they want preserved. Generate a concise kebab-case slug (2-4 "
            "words) from the content, and relevant tags (include the project "
            "name, content type like 'lyric', and any descriptive tags). "
            "\n\n"
            "Do NOT claim you 'filed' something without calling this tool — "
            "if you did not call file_text, nothing was actually saved."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The raw text to preserve verbatim."},
                "slug": {"type": "string", "description": "kebab-case, 2-4 words, semantic."},
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "All relevant tags: project name, 'lyric'/'note', descriptors.",
                },
            },
            "required": ["text", "slug", "tags"],
        },
    },
}

_READ_ENTRIES_TOOL = {
    "type": "function",
    "function": {
        "name": "read_entries",
        "description": (
            "Fetch FULL content for archive entries: text/lyric bodies, "
            "audio transcripts, AND the raw NOTE list for midi entries "
            "(format: 'NOTE pitch start_sec dur_sec'). Use this whenever "
            "the user asks to read, see, summarize, identify key / chords / "
            "progression, or otherwise reason about the content of entries."
            "\n\n"
            "When a midi entry comes back, you HAVE the notes — analyze them "
            "directly (key, scale, chord quality, rhythm). Do not claim the "
            "data is missing."
            "\n\n"
            "For lyric/text content: return it verbatim. Do NOT narrate, "
            "analyze, frame, or praise. No 'the arc is clear', no 'you've "
            "captured'. Just the text, as if quoting it back."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tag":   {"type": "string",  "description": "Optional tag filter."},
                "limit": {"type": "integer", "description": "Default 30."},
            },
            "required": [],
        },
    },
}

_JOB_MARKER = "<<<job>>>"

_MAX_TOOL_ROUNDS = 4


def respond_to_text(
    message: str,
    conversation_history: list[dict],
) -> str:
    """
    Multi-turn tool-calling loop.

    - queue_job → returns "<<<job>>>{json}" (handled by caller for side-effects)
    - list_entries / read_entries → data returned as tool result; LLM continues
    - no tool call → returns plain text reply
    """
    system = _soul()
    messages = [{"role": "system", "content": system}]
    messages += conversation_history
    messages.append({"role": "user", "content": message})

    tools = [_JOB_TOOL, _LIST_ENTRIES_TOOL, _READ_ENTRIES_TOOL, _FILE_TEXT_TOOL]

    try:
        print(f"[HERBIE/llm] respond_to_text called. message={message!r}")
        for rnd in range(_MAX_TOOL_ROUNDS):
            print(f"[HERBIE/llm] round {rnd + 1}: {len(messages)} msgs")
            resp = _client().chat.completions.create(
                model=_model(),
                messages=messages,
                tools=tools,
                tool_choice="auto",
                temperature=0.7,
            )
            choice = resp.choices[0]
            msg    = choice.message
            print(f"[HERBIE/llm] finish_reason={choice.finish_reason!r}")
            print(f"[HERBIE/llm] tool_calls={msg.tool_calls!r}")
            print(f"[HERBIE/llm] content={(msg.content or '')[:200]!r}")

            if not msg.tool_calls:
                return (msg.content or "").strip()

            # Record the assistant turn with its tool calls
            messages.append({
                "role": "assistant",
                "content": msg.content,
                "tool_calls": [
                    {
                        "id":   tc.id,
                        "type": "function",
                        "function": {
                            "name":      tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in msg.tool_calls
                ],
            })

            # Execute each tool call, append results as tool messages
            for call in msg.tool_calls:
                name = call.function.name
                args = json.loads(call.function.arguments)
                print(f"[HERBIE/llm] TOOL CALL → {name}({args})")

                if name == "queue_job":
                    # Exit the loop — caller handles the side-effect
                    return _JOB_MARKER + json.dumps(args)

                if name == "list_entries":
                    result = _tool_list_entries(args)
                elif name == "read_entries":
                    result = _tool_read_entries(args)
                elif name == "file_text":
                    result = _tool_file_text(args)
                else:
                    result = f"unknown tool: {name}"

                messages.append({
                    "role":         "tool",
                    "tool_call_id": call.id,
                    "content":      result,
                })

        print("[HERBIE/llm] max tool rounds exceeded")
        return "(I'm stuck in a tool loop — try rephrasing.)"
    except Exception as e:
        print(f"[HERBIE/llm] EXCEPTION: {type(e).__name__}: {e}")
        return f"(llm error: {e})"


# ---------------------------------------------------------------------------
# Tool implementations  (return strings to be fed back to the LLM)
# ---------------------------------------------------------------------------

def _tool_list_entries(args: dict) -> str:
    from services.archive import get_feed
    limit = int(args.get("limit") or 15)
    tag   = (args.get("tag") or "").strip()
    print(f"[HERBIE/llm/list_entries] tag={tag!r} limit={limit}")

    events = get_feed(tag=tag, limit=max(limit * 3, 40))
    files  = [e for e in events if e.get("type") in ("audio", "text", "lyric")][:limit]

    if not files:
        return f"no files tagged {tag}" if tag else "archive is empty"

    lines = [f"{len(files)} entries" + (f" tagged {tag}:" if tag else ":")]
    for e in files:
        fid  = (e.get("file_id") or "")[:8]
        slug = e.get("slug", "—")
        tags = ", ".join(e.get("tags", []))
        when = e.get("created_at", "")[:16].replace("T", " ")
        kind = e.get("type", "?")
        lines.append(f"  {fid}  {slug}  ({kind})  [{tags}]  {when}")
    return "\n".join(lines)


def _tool_file_text(args: dict) -> str:
    from services.archive import ingest_text
    text = (args.get("text") or "").strip()
    slug = (args.get("slug") or "untitled").strip()
    tags = args.get("tags") or []
    print(f"[HERBIE/llm/file_text] slug={slug!r} tags={tags} text_len={len(text)}")
    if not text:
        return "error: no text supplied"
    ev = ingest_text(slug, tags, text)
    fid = (ev.get("file_id") or "")[:8]
    return f"filed. file_id={fid} slug={ev.get('slug')} tags={ev.get('tags')}"


def _tool_read_entries(args: dict) -> str:
    from services.archive import get_feed, _read_sidecar
    limit = int(args.get("limit") or 30)
    tag   = (args.get("tag") or "").strip()
    print(f"[HERBIE/llm/read_entries] tag={tag!r} limit={limit}")

    events = get_feed(tag=tag, limit=max(limit * 2, 60))
    fragments = []
    for e in events:
        if e.get("type") not in ("audio", "text", "lyric"):
            continue
        text  = (e.get("text") or "").strip()
        trans = (e.get("transcript") or "").strip()

        # Pull midi_notes from the event OR from the sidecar (legacy entries
        # filed before midi_notes was an event field still have it in sidecar).
        midi  = (e.get("midi_notes") or "").strip()
        if not midi and "midi" in (e.get("tags") or []):
            sc = _read_sidecar(e.get("file_id", "")) or {}
            midi = (sc.get("midi_notes") or "").strip()

        body = text or trans
        if not body and not midi:
            continue

        fid  = (e.get("file_id") or "")[:8]
        slug = e.get("slug", "")
        tags = ", ".join(e.get("tags", []))
        when = e.get("created_at", "")[:16].replace("T", " ")
        kind = "lyric/text" if e.get("type") in ("text", "lyric") else "voice-note"
        if midi:
            kind = "midi"

        parts = [f"[{when}]  {fid}  {slug}  ({kind})  [{tags}]"]
        if body:
            parts.append(body)
        if midi:
            parts.append(f"NOTE data (pitch start_sec dur_sec):\n{midi}")

        fragments.append("\n".join(parts))
        if len(fragments) >= limit:
            break

    if not fragments:
        return f"no readable entries tagged {tag}" if tag else "no readable entries"

    print(f"[HERBIE/llm/read_entries] returning {len(fragments)} fragments")
    return "\n\n---\n\n".join(reversed(fragments))


# ---------------------------------------------------------------------------
# Lyric intent detection
# ---------------------------------------------------------------------------

# Trigger phrases that must appear at the START of the message,
# immediately followed by the project name (and optionally a newline + lyrics).
# Single-line queries like "latest hospital lyrics" must NOT trigger this.
_LYRIC_TRIGGER_PREFIXES = [
    r"^([a-z0-9_-]+)\s+lyrics?\s*\n",       # "hospital lyrics\n..."
    r"^lyrics?\s+for\s+([a-z0-9_-]+)\s*\n", # "lyrics for hospital\n..."
    r"^words\s+for\s+([a-z0-9_-]+)\s*\n",
    r"^verse\s+for\s+([a-z0-9_-]+)\s*\n",
    r"^chorus\s+for\s+([a-z0-9_-]+)\s*\n",
    r"^hook\s+for\s+([a-z0-9_-]+)\s*\n",
    r"^bridge\s+for\s+([a-z0-9_-]+)\s*\n",
]


def detect_lyric_intent(text: str) -> bool:
    """
    Return True only if text is an actual lyric submission, not a query.

    A lyric submission is either:
    1. A trigger phrase at the start of the message followed by a newline
       and the actual lyric content (multi-line)
    2. Pure multi-line poetic content with no question mark and no
       single-line query structure (≥3 lines, avg line < 60 chars)

    Single-line messages containing "lyrics" (e.g. "latest hospital lyrics",
    "show me the lyrics") are queries and must return False.
    """
    stripped = text.strip()
    lower = stripped.lower()

    # Must have at least one newline to be a submission — single-line = query
    if "\n" not in stripped:
        return False

    # Explicit trigger prefix followed by content on next line
    for pattern in _LYRIC_TRIGGER_PREFIXES:
        if re.search(pattern, lower):
            return True

    # Structural heuristic: pure lyric content (no trigger needed)
    lines = [l.strip() for l in stripped.splitlines() if l.strip()]
    if len(lines) >= 3 and "?" not in stripped:
        avg_len = sum(len(l) for l in lines) / len(lines)
        if avg_len < 60:
            return True

    return False


def extract_lyric_project(text: str) -> str | None:
    """Extract project name from a lyric trigger prefix."""
    lower = text.strip().lower()
    patterns = [
        r"^([a-z0-9_-]+)\s+lyrics?",
        r"^lyrics?\s+for\s+([a-z0-9_-]+)",
        r"^words\s+for\s+([a-z0-9_-]+)",
        r"^verse\s+for\s+([a-z0-9_-]+)",
        r"^chorus\s+for\s+([a-z0-9_-]+)",
        r"^hook\s+for\s+([a-z0-9_-]+)",
        r"^bridge\s+for\s+([a-z0-9_-]+)",
    ]
    for pat in patterns:
        m = re.match(pat, lower)
        if m:
            return m.group(1)
    return None


# ---------------------------------------------------------------------------
# Read-query detection — handle these in Python, never send to the LLM
# ---------------------------------------------------------------------------

_READ_FILES_PATTERNS = [
    r"(?:show|list|see|what(?:'s| is| are)(?: in)?)\s+(?:my\s+)?([a-z0-9_-]+)(?:\s+files?)?$",
    r"([a-z0-9_-]+)\s+files?$",
    r"what(?:'s| is) in ([a-z0-9_-]+)$",
    r"([a-z0-9_-]+)\s+project$",
]

_READ_LYRICS_PATTERNS = [
    r"(?:latest|last|current|show(?:\s+me)?)\s+([a-z0-9_-]+)\s+lyrics?$",
    r"([a-z0-9_-]+)\s+lyrics?$",
    r"show\s+(?:me\s+)?(?:the\s+)?lyrics?\s+(?:for\s+)?([a-z0-9_-]+)$",
]


def detect_read_query(text: str, known_projects: list[str]) -> tuple[str, str] | tuple[None, None]:
    """
    If text is a read/lookup query, return (query_type, project_name).
    query_type is "files" or "lyrics". Returns (None, None) otherwise.
    Only matches if the project name actually exists in known_projects.
    """
    lower = text.strip().lower()
    project_names = {p.lower(): p for p in known_projects}

    for pat in _READ_LYRICS_PATTERNS:
        m = re.fullmatch(pat, lower)
        if m:
            proj_lower = m.group(1)
            if proj_lower in project_names:
                return "lyrics", project_names[proj_lower]

    for pat in _READ_FILES_PATTERNS:
        m = re.fullmatch(pat, lower)
        if m:
            proj_lower = m.group(1)
            if proj_lower in project_names:
                return "files", project_names[proj_lower]

    return None, None


def format_read_response(query_type: str, project: str, files: list[dict]) -> str:
    """Format a direct archive read response without involving the LLM."""
    from pathlib import Path
    import os

    archive_root = Path(os.getenv("ARCHIVE_PATH", "./archive"))

    if query_type == "lyrics":
        # Find the latest lyric file
        lyric_files = sorted(
            [f for f in files if f.get("type") == "lyric"],
            key=lambda x: x.get("version", 1),
        )
        if not lyric_files:
            return f"no lyrics filed for {project} yet."
        latest = lyric_files[-1]
        lyric_path = archive_root / project / latest["filename"]
        if lyric_path.exists():
            content = lyric_path.read_text().strip()
            return f"{latest['filename']}:\n\n{content}"
        return f"lyric file {latest['filename']} not found on disk."

    if query_type == "files":
        if not files:
            return f"{project}/ is empty."
        seen = set()
        lines = [f"{project}/"]
        for f in sorted(files, key=lambda x: x.get("base_name", "")):
            base = f.get("base_name", f["filename"])
            if base in seen:
                continue
            seen.add(base)
            versions = f.get("versions", [f.get("version", 1)])
            v_str = " ".join(f"v{v}" for v in versions)
            ftype = f.get("type", "audio")
            lines.append(f"  {base}  [{v_str}]  {ftype}")
        return "\n".join(lines)

    return ""


# ---------------------------------------------------------------------------
# Archive snapshot for LLM context
# ---------------------------------------------------------------------------

def build_archive_context(
    projects: list[dict],
    active_project: str = "",
    active_files: list[dict] | None = None,
) -> str:
    """
    Format archive state as compact text for LLM system context.
    Works with both old file-dicts and new event-shaped dicts from get_project_files().
    """
    if not projects:
        return "Archive is empty."

    lines = ["Tags/projects: " + ", ".join(p["name"] for p in projects)]

    if active_project and active_files is not None:
        lines.append(f"\n{active_project}/")
        seen = set()
        for f in sorted(active_files, key=lambda x: x.get("created_at", "")):
            slug = f.get("base_name") or f.get("slug") or f.get("filename", "?")
            ftype = f.get("type", "audio")
            tags = f.get("tags", [])

            if slug not in seen:
                seen.add(slug)
                tag_str = ", ".join(tags) if tags else ""
                lines.append(f"  {slug}  [{tag_str}]  {ftype}")

            # Include full text for lyric/text entries
            if ftype in ("lyric", "text"):
                text = f.get("text", "")
                if not text:
                    # fallback: try reading from raw/ by file_id
                    from pathlib import Path
                    import os
                    fid = f.get("file_id", "")
                    raw = Path(os.getenv("ARCHIVE_PATH", "./archive")) / "raw" / f"{fid}.txt"
                    if raw.exists():
                        try:
                            text = raw.read_text().strip()
                        except Exception:
                            pass
                if text:
                    lines.append(f"\n  --- {slug} (full text) ---")
                    for line in text.splitlines():
                        lines.append(f"  {line}")
                    lines.append(f"  --- end ---\n")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Summarize intent detection
# ---------------------------------------------------------------------------

_SUMMARIZE_PATTERNS = [
    r"^summarize\s+(?:the\s+)?([a-z0-9_-]+)(?:\s+lyrics?)?$",
    r"^(?:show|give)\s+(?:me\s+)?(?:the\s+)?([a-z0-9_-]+)\s+(?:lyrics?\s+)?summary$",
    r"^([a-z0-9_-]+)\s+summary$",
    r"^latest\s+([a-z0-9_-]+)\s+(?:lyrics?|summary)$",
    r"^(?:what(?:'s| are) the )?(?:latest|current)\s+([a-z0-9_-]+)\s+lyrics?$",
]


def detect_summarize_intent(text: str, known_tags: list[str]) -> str | None:
    """
    If the message is a summarize/lyrics-distil request for a known tag,
    return the tag name. Otherwise return None.
    """
    lower = text.strip().lower()
    tag_map = {t.lower(): t for t in known_tags}
    for pat in _SUMMARIZE_PATTERNS:
        m = re.fullmatch(pat, lower)
        if m:
            candidate = m.group(1)
            if candidate in tag_map:
                return tag_map[candidate]
    return None


# ---------------------------------------------------------------------------
# Archive action parsing
# ---------------------------------------------------------------------------

_ACTION_DELIMITER = "<<<archive_action>>>"


def parse_archive_action(response: str) -> tuple[str, dict | None]:
    """
    Split an LLM response into (text, action_dict | None).
    The action block is stripped before showing the text to the user.
    """
    if _ACTION_DELIMITER not in response:
        return response.strip(), None

    parts = response.split(_ACTION_DELIMITER, 1)
    text = parts[0].strip()
    raw = parts[1].strip()

    # Strip markdown fences if the LLM wrapped it
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    try:
        action = json.loads(raw)
        return text, action
    except Exception:
        return text, None


# ---------------------------------------------------------------------------
# Lyric summarization
# ---------------------------------------------------------------------------

def summarize_tag(tag: str) -> str:
    """
    Read all text/lyric events AND audio transcripts for a tag, ask the LLM
    to distil the latest version of the lyrics. Returns plain text.
    """
    from services.archive import get_feed
    events = list(reversed(get_feed(tag=tag, limit=500)))
    print(f"[HERBIE/llm/summarize] tag={tag!r} got {len(events)} events total")

    fragments = []
    for e in events:
        etype = e.get("type")
        text  = (e.get("text") or "").strip()
        trans = (e.get("transcript") or "").strip()
        body  = text or trans
        if not body:
            continue
        when = e.get("created_at", "")[:16]
        slug = e.get("slug", "")
        kind = "lyric" if etype in ("text", "lyric") else "voice-note"
        fragments.append(f"[{when}] ({kind} · {slug})\n{body}")

    print(f"[HERBIE/llm/summarize] {len(fragments)} text/transcript fragments extracted")
    if not fragments:
        return f"no text entries for tag: {tag}"

    combined = "\n\n---\n\n".join(fragments)
    print(f"[HERBIE/llm/summarize] combined length: {len(combined)} chars")
    system = (
        _soul()
        + "\n\nYou are reading multiple iterations of lyrics/ideas for a song, "
        "in chronological order (earliest first). Voice-note transcripts are "
        "labeled. Return what is clearly the latest / best version of the "
        "lyrics as plain text. No commentary. No headings. Just the lyrics."
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": f"Summarise the latest lyrics for '{tag}':\n\n{combined}"},
    ]
    try:
        print(f"[HERBIE/llm/summarize] calling LLM…")
        resp = _client().chat.completions.create(
            model=_model(), messages=messages, temperature=0.3
        )
        out = resp.choices[0].message.content.strip()
        print(f"[HERBIE/llm/summarize] got {len(out)} chars back")
        return out
    except Exception as e:
        print(f"[HERBIE/llm/summarize] EXCEPTION: {type(e).__name__}: {e}")
        return f"(summarise error: {e})"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _timestamp() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y%m%d_%H%M%S")
