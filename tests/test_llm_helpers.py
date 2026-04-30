"""
Unit tests for the small llm helper functions that don't go through
the OpenRouter API — the filing-confirmation formatter and the
in-process tool dispatch handlers.

How to read this file:

  1. format_filing_confirmation produces the user-facing string the
     audio ingest endpoint sends back to the chat. It must match the
     compact format soul.md describes, with the transcript appended
     when present so the chat reply mirrors a feed row.
  2. _tool_file_system_note is the in-process dispatcher the LLM
     tool loop calls when the model invokes the file_system_note
     tool. It is the bot's only correction path — appends a text
     event tagged `system-note` (inheriting the target's tags), so
     the user's correction lives in the feed without mutating the
     original entry.
"""

from services import archive, llm


# ── format_filing_confirmation ─────────────────────────────────────────────


def test_format_filing_confirmation_v1_with_transcript():
    """
    1. Version 1 → filename has no _vN suffix.
    2. Tags render as a comma-separated list inside square brackets.
    3. Transcript is included on its own line, wrapped in quotes, so
       the chat reply mirrors what the user sees in the web feed.
    """
    msg = llm.format_filing_confirmation(
        slug="religion-customary-vocal",
        ext="ogg",
        version=1,
        tags=["monastery", "lyric", "vocal", "voice-note"],
        transcript="Religion is a customary, tell me why I only ever pray when I'm nervous.",
    )

    assert msg.splitlines()[0] == "filed"
    assert "religion-customary-vocal.ogg" in msg
    assert "[monastery, lyric, vocal, voice-note]" in msg
    assert '"Religion is a customary, tell me why I only ever pray when I\'m nervous."' in msg


def test_format_filing_confirmation_v2_versioned_filename():
    """
    Version > 1 → filename suffix is _vN so the user can tell which
    take they just filed.
    """
    msg = llm.format_filing_confirmation(
        slug="religion-customary",
        ext="ogg",
        version=3,
        tags=["monastery"],
        transcript="",
    )
    assert "religion-customary_v3.ogg" in msg


def test_format_filing_confirmation_omits_transcript_line_when_empty():
    """
    Audio with no transcribed words (instrumental, foley, etc.)
    should not produce a quoted empty line — that would look
    broken. The body lines are: filed, filename, tags only.
    """
    msg = llm.format_filing_confirmation(
        slug="air-conditioner-drone",
        ext="ogg",
        version=1,
        tags=["foley", "drone"],
        transcript="",
    )
    lines = msg.splitlines()
    assert lines[0] == "filed"
    assert lines[1] == "air-conditioner-drone.ogg"
    assert lines[2] == "[foley, drone]"
    assert len(lines) == 3
    assert '""' not in msg


# ── _tool_file_system_note ─────────────────────────────────────────────────


def test_tool_file_system_note_appends_a_correction_without_mutating(
    temp_archive, fake_audio
):
    """
    1. File an audio entry with the LLM's initial (wrong) tag guess.
    2. Capture the existing tag set and feed length so we can prove
       neither is mutated by the correction.
    3. Invoke file_system_note with target_file_id pointing at the
       wrong entry, plus the user's correction text.
    4A. The original entry's tags are UNCHANGED — corrections never
        rewrite the source. The healing agent does that later.
    4B. The feed has grown by exactly one — a new text event tagged
        `system-note` plus the inherited tag set so the correction
        surfaces under the same tag filter as the entry it targets.
    """
    parent = archive.ingest_audio(
        fake_audio,
        slug="religion-customary",
        tags=["underworld", "lyric"],
        ext="ogg",
        transcript="religion is a customary",
    )
    original_tags = list(archive.current_entry(parent["file_id"])["tags"])
    pre_feed_len = len(archive.get_feed())

    result = llm._tool_file_system_note({
        "content": "tag should be monastery, not underworld",
        "target_file_id": parent["file_id"],
    })

    # 1. Tool result is a 'noted' confirmation string.
    assert "noted" in result
    # 2. Original entry's tags untouched — no in-place mutation.
    assert archive.current_entry(parent["file_id"])["tags"] == original_tags
    # 3. Feed grew by one new text event.
    feed = archive.get_feed()
    assert len(feed) == pre_feed_len + 1
    # 4. The new event is a system-note tagged with both the
    #    inherited project tag and `system-note`.
    note = feed[0]
    assert note["type"] == "text"
    assert "system-note" in note["tags"]
    assert "underworld" in note["tags"]
    assert "monastery, not underworld" in note["text"]


def test_tool_file_system_note_works_without_a_target(temp_archive):
    """
    A general correction with no specific entry to point at — e.g.
    "stop adding the foley tag to everything" — must still file
    cleanly. target_file_id is omitted, the note carries only the
    `system-note` tag, and the LLM sees a 'noted' tool result.
    """
    pre_feed_len = len(archive.get_feed())

    result = llm._tool_file_system_note({
        "content": "stop adding the foley tag to everything",
    })

    assert "noted" in result
    feed = archive.get_feed()
    assert len(feed) == pre_feed_len + 1
    note = feed[0]
    assert note["type"] == "text"
    assert note["tags"] == ["system-note"]


def test_tool_file_system_note_rejects_empty_content(temp_archive):
    """
    Calling file_system_note with no content returns an error
    string instead of writing a blank event. The LLM reads the
    error back and can recover by asking the user to elaborate.
    """
    result = llm._tool_file_system_note({"content": ""})
    assert "error" in result.lower()
