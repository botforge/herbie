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

# TODO: does _AUDIO_SYSTEM need to exist separately, or can these instructions
# live inside soul.md? Duplicating rules across two prompts is how the filing
# format drifted before — worth collapsing if soul.md can carry the JSON schema
# rules directly for audio turns.
_AUDIO_SYSTEM = """{soul}

When responding to an audio submission you MUST reply with valid JSON only.
No prose before or after the JSON block. Do not include a confirmation
message — the server constructs the filing confirmation deterministically.

Schema:
{{
  "project": "<project-slug>",
  "slug": "<semantic-idea-slug>",
  "tags": ["<tag>", ...]
}}

Rules:
- project: the song or project this belongs to, lowercase hyphens only
- slug: 2-4 words, kebab-case. Pick the type that is most specific to
  this particular idea — all three are valid:

  Source-based:   "youtube-drone-pad", "op1-worm-strings"
  Evocative:      "air-conditioner-drone", "broken-tape-loop"
  Functional:     "drone-pad-opening", "bridge-variation-strings"

  Never use a slug so generic it could describe anything.
- tags: pick from the palette in the soul above. Do not invent moods.
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
        }
    except Exception:
        return {
            "project": "sketches",
            "slug": f"untitled-{_timestamp()}",
            "tags": [],
        }


# ---------------------------------------------------------------------------
# Filing confirmation formatter (audio ingest reply)
# ---------------------------------------------------------------------------

def format_filing_confirmation(
    slug: str,
    ext: str,
    version: int,
    tags: list[str],
    transcript: str,
) -> str:
    """
    Build the chat reply that follows an audio ingest. Mirrors what
    the user sees in a feed row, in a compact form.

    1. First line: literal "filed" — a one-word ack so the user
       knows the action succeeded.
    2. Second line: the canonical filename (slug + version suffix
       when v > 1, plus the file extension).
    3. Third line: the tag list inside square brackets.
    4. Fourth line (only if a transcript exists): the transcribed
       body in quotes, so the chat reply mirrors what the user
       sees in the web UI's transcript field. Empty transcripts
       (instrumental, foley) are omitted entirely — no quoted
       blanks.
    """
    filename = f"{slug}_v{version}.{ext}" if version > 1 else f"{slug}.{ext}"
    tag_str  = ", ".join(tags)
    lines    = ["filed", filename, f"[{tag_str}]"]
    if transcript and transcript.strip():
        lines.append(f"\"{transcript.strip()}\"")
    return "\n".join(lines)


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

_EDIT_ENTRIES_TOOL = {
    "type": "function",
    "function": {
        "name": "edit_entries",
        "description": (
            "Edit metadata on EXISTING archive entries. Use this when "
            "the user clarifies, corrects, or retags entries that are "
            "already filed — never use file_text for that."
            "\n\n"
            "Examples that should call edit_entries:"
            "\n  - 'actually that's monastery, not underworld'"
            "\n  - 'rename that to broken-glass-loop'"
            "\n  - 'fix the transcript on 151cd315 — should say custom-marry'"
            "\n  - 'retag everything tagged underworld → monastery'"
            "\n\n"
            "Resolve the file_id(s) first via list_entries / read_entries "
            "if the user did not give them explicitly. If multiple "
            "candidates plausibly match (especially for transcript / "
            "lyric edits), ASK the user to disambiguate before calling "
            "this tool — it is better to ask than to corrupt the wrong "
            "entry. Only the supplied fields are changed; omitted "
            "fields are left untouched."
            "\n\n"
            "All edits in a single call undo together via the server's "
            "single-step undo, so prefer one call with multiple "
            "file_ids over several sequential calls."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "8-hex file_ids returned by list_entries / read_entries.",
                },
                "slug": {
                    "type": "string",
                    "description": "New slug. Same kebab-case rules as ingest.",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Replacement tag set. Provide the FULL desired tag list, not a delta.",
                },
                "transcript": {
                    "type": "string",
                    "description": "Replacement transcript for an audio entry. Single entry only — never bulk-edit transcripts.",
                },
                "text": {
                    "type": "string",
                    "description": "Replacement text body for a text/lyric entry. Single entry only.",
                },
            },
            "required": ["file_ids"],
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

    tools = [_JOB_TOOL, _LIST_ENTRIES_TOOL, _READ_ENTRIES_TOOL, _FILE_TEXT_TOOL, _EDIT_ENTRIES_TOOL]

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
                elif name == "edit_entries":
                    result = _tool_edit_entries(args)
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


def _tool_edit_entries(args: dict) -> str:
    """
    1. Read the requested file_ids and the optional new fields.
    2. Delegate to update_files_meta which snapshots the prior
       state to .last_action.json (one undo step covers the
       whole batch) and rewrites events.jsonl in one pass.
    3. Return a short human-readable summary the LLM can paraphrase
       back to the user, including the count actually edited so the
       LLM knows when some file_ids did not match anything live.
    """
    from services.archive import update_files_meta, current_entry
    file_ids = args.get("file_ids") or []
    if not file_ids:
        return "error: no file_ids supplied"
    n = update_files_meta(
        file_ids,
        slug=args.get("slug"),
        tags=args.get("tags"),
        transcript=args.get("transcript"),
        text=args.get("text"),
    )
    if n == 0:
        return "edited 0 entries (none of the supplied file_ids matched a live entry)"
    summaries = []
    for fid in file_ids:
        e = current_entry(fid)
        if not e:
            continue
        summaries.append(f"  {fid[:8]}  {e.get('slug', '')}  [{', '.join(e.get('tags', []))}]")
    body = "\n".join(summaries)
    return f"edited {n} entries:\n{body}"


def _tool_read_entries(args: dict) -> str:
    from services.archive import get_feed
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
        midi  = (e.get("midi_notes") or "").strip()

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
