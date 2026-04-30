"""
LLM layer: OpenRouter calls via the openai SDK.

Public surface:
  respond_to_text(message, history, extra_tools?, extra_handlers?)
      → tuple[str, list[dict]]   used by the chat / Telegram pipeline
  respond_to_audio(transcript, user_context, history, ...)
      → dict                     legacy CLI-only filing path
  format_filing_confirmation(slug, ext, version, tags, transcript)
      → str                      shared filing-reply formatter
  summarize_tag(tag) → str       healing/summary path

The chat tool surface is APPEND-ONLY: file_text, file_system_note,
queue_job, list_entries, read_entries, plus per-turn extra tools
(file_audio for voice-note ingest). Nothing in this loop mutates an
existing entry — corrections are captured as system_notes that a
downstream healing agent reconciles.
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
        return "You are Lila, a personal music archivist."


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
            "list_entries / read_entries. Resolve the file_id yourself "
            "from list_entries / read_entries by matching the slug, tag, "
            "or recency the user named — never ask the user to type one. "
            "If multiple entries plausibly match, name them back by slug "
            "and let the user pick by name. For chord rendering, pass "
            "the chord symbols the user actually named in this message."
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
                    "description": "file_id you resolved via list_entries / read_entries. Required for audio jobs.",
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

_FILE_SYSTEM_NOTE_TOOL = {
    "type": "function",
    "function": {
        "name": "file_system_note",
        "description": (
            "File a correction about an already-filed entry: wrong "
            "slug, wrong tag, misheard transcript, deletion request, "
            "or any other fix the user is asking for."
            "\n\n"
            "Examples:"
            "\n  - 'actually that's monastery, not underworld'"
            "\n  - 'rename that to broken-glass-loop'"
            "\n  - 'fix the transcript on the religion one — should "
            "say custom-marry, not contemporary'"
            "\n  - 'you misheard, the lyric is X not Y'"
            "\n  - 'delete the air-conditioner one'"
            "\n\n"
            "Resolve the target_file_id yourself via list_entries / "
            "read_entries. Never ask the user for one. If you can't "
            "identify a specific target, file the note anyway with "
            "target_file_id omitted. After filing, reply with a short "
            "ack like 'noted'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The correction, plus enough context to identify the entry (e.g. 'religion-customary-vocal: tag should be monastery, not underworld').",
                },
                "target_file_id": {
                    "type": "string",
                    "description": "8-hex file_id of the entry being corrected, if known.",
                },
            },
            "required": ["content"],
        },
    },
}


_FILE_AUDIO_TOOL = {
    "type": "function",
    "function": {
        "name": "file_audio",
        "description": (
            "Archive the voice recording that just arrived. The audio has "
            "already been transcribed — the transcript in this message IS "
            "the content. You do not need to hear or retrieve anything.\n\n"
            "DEFAULT ACTION: call this tool. File the recording unless the "
            "transcript is unambiguously an instruction or question. When in "
            "doubt — a melody fragment, a hummed idea, half-formed lyrics, "
            "an ambient description — file it. Never ask the user to clarify "
            "before filing. Generate a slug and tags from whatever context "
            "the transcript provides.\n\n"
            "Only skip filing if the transcript is clearly a command "
            "(file_system_note, list_entries, read_entries, queue_job, etc.)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "slug": {
                    "type": "string",
                    "description": "2-4 word kebab-case slug describing the content.",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Project name, content type (lyric/voice-note/melody), texture tags.",
                },
            },
            "required": ["slug", "tags"],
        },
    },
}

_JOB_MARKER = "<<<job>>>"

_MAX_TOOL_ROUNDS = 4


def respond_to_text(
    message: str,
    conversation_history: list[dict],
    extra_tools: list[dict] | None = None,
    extra_handlers: dict | None = None,
) -> tuple[str, list[dict]]:
    """
    Multi-turn tool-calling loop.

    1. Build the message list from history + current message.
    2. Offer the standard tool set, prepended with any extra_tools
       the caller supplies (e.g. file_audio for voice-note turns).
    3A. queue_job → exits early with a marker string; caller executes
        the side-effect job.
    3B. Any extra_handlers are dispatched by name before the built-in
        handlers so the caller can inject turn-scoped tools.
    3C. All other tools (list_entries, read_entries, file_text,
        file_system_note) are executed in-loop; result fed back to LLM.
        Note: nothing in this loop mutates an existing entry —
        corrections become append-only system_note events that a
        downstream healing agent reconciles later.
    4. Return (reply_str, tool_call_log) where tool_call_log is a list
       of {name, args, result} dicts — one per tool call across all
       rounds. Used by the conversation log for eval set construction.
    """
    system = _soul()
    messages = [{"role": "system", "content": system}]
    messages += conversation_history
    messages.append({"role": "user", "content": message})

    base_tools = [_JOB_TOOL, _LIST_ENTRIES_TOOL, _READ_ENTRIES_TOOL,
                  _FILE_TEXT_TOOL, _FILE_SYSTEM_NOTE_TOOL]
    tools = (extra_tools or []) + base_tools
    tool_call_log: list[dict] = []

    try:
        print(f"[LILA/llm] respond_to_text called. message={message!r}")
        for rnd in range(_MAX_TOOL_ROUNDS):
            print(f"[LILA/llm] round {rnd + 1}: {len(messages)} msgs")
            resp = _client().chat.completions.create(
                model=_model(),
                messages=messages,
                tools=tools,
                tool_choice="auto",
                temperature=0.7,
            )
            choice = resp.choices[0]
            msg    = choice.message
            print(f"[LILA/llm] finish_reason={choice.finish_reason!r}")
            print(f"[LILA/llm] tool_calls={msg.tool_calls!r}")
            print(f"[LILA/llm] content={(msg.content or '')[:200]!r}")

            if not msg.tool_calls:
                return (msg.content or "").strip(), tool_call_log

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

            for call in msg.tool_calls:
                name = call.function.name
                args = json.loads(call.function.arguments)
                print(f"[LILA/llm] TOOL CALL → {name}({args})")

                if name == "queue_job":
                    tool_call_log.append({"name": name, "args": args, "result": "queued"})
                    return _JOB_MARKER + json.dumps(args), tool_call_log

                if extra_handlers and name in extra_handlers:
                    result = extra_handlers[name](args)
                elif name == "list_entries":
                    result = _tool_list_entries(args)
                elif name == "read_entries":
                    result = _tool_read_entries(args)
                elif name == "file_text":
                    result = _tool_file_text(args)
                elif name == "file_system_note":
                    result = _tool_file_system_note(args)
                else:
                    result = f"unknown tool: {name}"

                tool_call_log.append({"name": name, "args": args, "result": result[:500]})
                messages.append({
                    "role":         "tool",
                    "tool_call_id": call.id,
                    "content":      result,
                })

        print("[LILA/llm] max tool rounds exceeded")
        return "(I'm stuck in a tool loop — try rephrasing.)", tool_call_log
    except Exception as e:
        print(f"[LILA/llm] EXCEPTION: {type(e).__name__}: {e}")
        return f"(llm error: {e})", tool_call_log


# ---------------------------------------------------------------------------
# Tool implementations  (return strings to be fed back to the LLM)
# ---------------------------------------------------------------------------

def _tool_list_entries(args: dict) -> str:
    from services.archive import get_feed
    limit = int(args.get("limit") or 15)
    tag   = (args.get("tag") or "").strip()
    print(f"[LILA/llm/list_entries] tag={tag!r} limit={limit}")

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
    print(f"[LILA/llm/file_text] slug={slug!r} tags={tags} text_len={len(text)}")
    if not text:
        return "error: no text supplied"
    ev = ingest_text(slug, tags, text)
    fid = (ev.get("file_id") or "")[:8]
    return f"filed. file_id={fid} slug={ev.get('slug')} tags={ev.get('tags')}"


def _tool_file_system_note(args: dict) -> str:
    """
    1. Pull the correction content and the optional target_file_id.
    2. If a target is supplied, inherit its current tag set so the
       note shows up alongside the entry it references when the user
       filters by tag. Always tack on `system-note` so a healing
       agent can find every correction with a single tag query.
    3. Generate a deterministic slug — `system-note-<target8>` when a
       target is known, otherwise a timestamped slug. Slugs don't
       matter much for system notes; the tag is the index.
    4. Embed the target file_id at the top of the body so a healing
       agent reading text alone can still resolve it.
    5. Append the event via ingest_text and return a short tool-result
       string the LLM can paraphrase back as 'noted'.
    """
    from services.archive import ingest_text, current_entry
    content = (args.get("content") or "").strip()
    if not content:
        return "error: no content supplied for system_note"

    target = (args.get("target_file_id") or "").strip()
    inherited: list[str] = []
    if target:
        inherited = list(current_entry(target).get("tags", []))
    tags = inherited + (["system-note"] if "system-note" not in inherited else [])

    slug = f"system-note-{target[:8]}" if target else f"system-note-{_timestamp()}"
    body = f"[target: {target[:8]}] {content}" if target else content

    ev = ingest_text(slug, tags, body)
    fid = (ev.get("file_id") or "")[:8]
    return f"noted. file_id={fid} target={target[:8] if target else 'none'} tags={tags}"


def _tool_read_entries(args: dict) -> str:
    from services.archive import get_feed
    limit = int(args.get("limit") or 30)
    tag   = (args.get("tag") or "").strip()
    print(f"[LILA/llm/read_entries] tag={tag!r} limit={limit}")

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

    print(f"[LILA/llm/read_entries] returning {len(fragments)} fragments")
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
    print(f"[LILA/llm/summarize] tag={tag!r} got {len(events)} events total")

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

    print(f"[LILA/llm/summarize] {len(fragments)} text/transcript fragments extracted")
    if not fragments:
        return f"no text entries for tag: {tag}"

    combined = "\n\n---\n\n".join(fragments)
    print(f"[LILA/llm/summarize] combined length: {len(combined)} chars")
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
        print(f"[LILA/llm/summarize] calling LLM…")
        resp = _client().chat.completions.create(
            model=_model(), messages=messages, temperature=0.3
        )
        out = resp.choices[0].message.content.strip()
        print(f"[LILA/llm/summarize] got {len(out)} chars back")
        return out
    except Exception as e:
        print(f"[LILA/llm/summarize] EXCEPTION: {type(e).__name__}: {e}")
        return f"(summarise error: {e})"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _timestamp() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y%m%d_%H%M%S")
