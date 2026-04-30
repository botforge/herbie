"""
Unit tests for services.render — the LLM-reply parser shared by every
transport. The parser walks an LLM string, finds [[audio:<8hex>]]
markers, resolves each one against the archive, and emits a typed
segment list the transport can dispatch.

How to read this file:

  1. parse_reply on a plain string returns a single text segment —
     the no-marker case must be a no-op so transports can hand any
     reply through it without branching.
  2. A reply with one resolved marker becomes three segments — text
     before, the audio segment with on-disk path + slug filename,
     and any trailing text — preserving order.
  3. Markers that reference unknown file_ids or files that have been
     soft-deleted must NOT crash the parser; they emit audio_miss
     segments so the transport can show a fallback message.
  4. File on disk missing (entry exists but raw bytes gone) emits
     an audio_miss with reason='file_missing' so a transport can
     distinguish the two failure modes.
"""

from services import archive, render


# ── plain text path ─────────────────────────────────────────────────────────

def test_parse_reply_returns_single_text_segment_for_plain_string(temp_archive):
    """A reply with no markers passes through as one text segment."""
    out = render.parse_reply("hello there")
    assert out == [{"kind": "text", "text": "hello there"}]


def test_parse_reply_returns_empty_list_for_empty_input(temp_archive):
    """Empty string → no segments. Caller decides how to handle silence."""
    assert render.parse_reply("") == []


# ── resolved marker ─────────────────────────────────────────────────────────

def test_parse_reply_resolves_marker_to_audio_segment(temp_archive, fake_audio):
    """
    1. File a real audio entry so the marker has something to resolve to.
    2. Wrap a marker in surrounding prose so we exercise the
       text-then-audio-then-text path.
    3. Three segments come back in order: leading text, audio
       (with on-disk path and a slug-derived filename), trailing text.
    """
    ev   = archive.ingest_audio(
        fake_audio, slug="religion-customary",
        tags=["monastery"], ext="ogg", transcript="r is c",
    )
    fid  = ev["file_id"]
    text = f"here it is\n[[audio:{fid[:8]}]]\nhope that helps"

    segs = render.parse_reply(text)

    assert [s["kind"] for s in segs] == ["text", "audio", "text"]
    assert segs[0]["text"] == "here it is\n"
    assert segs[1]["file_id"] == fid[:8]
    assert segs[1]["filename"] == "religion-customary.ogg"
    assert segs[1]["path"].exists()
    assert segs[2]["text"] == "\nhope that helps"


# ── failure modes ──────────────────────────────────────────────────────────

def test_parse_reply_emits_audio_miss_for_unknown_file_id(temp_archive):
    """An LLM hallucinated marker shouldn't crash — emit audio_miss."""
    segs = render.parse_reply("[[audio:deadbeef]]")
    assert len(segs) == 1
    assert segs[0] == {"kind": "audio_miss", "file_id": "deadbeef", "reason": "no_entry"}


def test_parse_reply_emits_audio_miss_when_raw_file_was_deleted_off_disk(
    temp_archive, fake_audio
):
    """
    1. File a real audio entry, then delete the on-disk bytes
       directly (sidecar event still present, raw file gone).
    2. parse_reply should detect the missing file and emit
       audio_miss with reason='file_missing' — distinct from
       no_entry so transports can show different fallbacks.
    """
    ev  = archive.ingest_audio(
        fake_audio, slug="ghost", tags=["x"],
        ext="ogg", transcript="",
    )
    fid = ev["file_id"]
    (archive.RAW_DIR / f"{fid}.ogg").unlink()

    segs = render.parse_reply(f"[[audio:{fid[:8]}]]")

    assert len(segs) == 1
    assert segs[0]["kind"] == "audio_miss"
    assert segs[0]["reason"] == "file_missing"
    assert segs[0]["file_id"] == fid[:8]
