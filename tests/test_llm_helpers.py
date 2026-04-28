"""
Unit tests for the small llm helper functions that don't go through
the OpenRouter API — the filing-confirmation formatter and the
in-process tool dispatch handlers.

How to read this file:

  1. format_filing_confirmation produces the user-facing string the
     audio ingest endpoint sends back to the chat. It must match the
     compact format soul.md describes, with the transcript appended
     when present so the chat reply mirrors a feed row.
  2. _tool_edit_entries is the in-process dispatcher that the LLM
     tool loop calls when the model invokes the edit_entries tool.
     It wraps update_files_meta and returns a string the LLM reads
     back as a tool result.
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


# ── _tool_edit_entries ─────────────────────────────────────────────────────


def test_tool_edit_entries_retags_an_existing_entry(temp_archive, fake_audio):
    """
    1. File an audio entry with the wrong project tag (the LLM's
       initial guess).
    2. Invoke the edit_entries tool with the corrected tag set —
       this is what the LLM should do when the user clarifies
       "that's not underworld, that's monastery."
    3. The underlying entry must be edited in place. get_feed
       reflects the new tags; no new feed entry is created.
    """
    parent = archive.ingest_audio(
        fake_audio,
        slug="religion-customary",
        tags=["underworld", "lyric"],
        ext="ogg",
        transcript="religion is a customary",
    )

    pre_feed_len = len(archive.get_feed())

    result = llm._tool_edit_entries({
        "file_ids": [parent["file_id"]],
        "tags": ["monastery", "lyric"],
    })

    # 1. Tool result contains a confirmation string the LLM can echo.
    assert "1" in result          # one entry edited
    # 2. State on disk reflects the edit.
    entry = archive.current_entry(parent["file_id"])
    assert entry["tags"] == ["monastery", "lyric"]
    # 3. No NEW feed entry was created — the count is unchanged.
    assert len(archive.get_feed()) == pre_feed_len


def test_tool_edit_entries_returns_zero_count_for_missing_file_ids(temp_archive):
    """
    Asking edit_entries to retag a file_id that does not exist
    should return a tool-result string indicating zero edits.
    Never crash — the LLM must be able to read the result and
    apologise to the user instead of erroring out the request.
    """
    result = llm._tool_edit_entries({
        "file_ids": ["deadbeef"],
        "tags": ["whatever"],
    })
    assert "0" in result
