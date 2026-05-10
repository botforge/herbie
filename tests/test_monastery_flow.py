"""
Integration walkthrough: the monastery scenario.

How to read this file:

  1. Every test below seeds a real DB user (seed_user fixture),
     redirects VOLUME_ROOT to a temp path (temp_volume), and replays
     a scripted sequence of user actions against the public
     services.archive API.
  2. No LLM, no HTTP, no transcription — the assertions land on the
     DB event rows and on-disk raw files that the web UI and Telegram
     bot both render from.
  3. The raw audio used here comes from tests/fixtures/audio/ and is
     owned by the test suite. It never touches the real archive/ on
     disk; the autouse guard in conftest.py enforces that.

Fixture mapping (see tests/fixtures/audio/manifest.json):

  1. First voice note  — slug "religion-customary-vocal" → 151cd315.ogg
  2. Second voice note — slug "religion-custom-marry"    → db593e95.ogg

Both are lyric-carrying vocal memos for the monastery project, two
successive takes of the same line ("religion is a customary /
custom-marry, tell me why I only ever pray when I'm nervous ...").
"""

from services import archive


def test_ingests_two_monastery_voice_notes(
    db, seed_user, fixture_entry, temp_volume,
):
    """
    After ingesting the first and second monastery voice notes, the feed
    should contain both entries in reverse-chronological order, with
    transcripts, tags, and raw bytes preserved.
    """

    uid = seed_user("u_monastery")

    # 1. Load the two test fixture entries.
    #    1A. `first` is the earlier take ("religion-customary-vocal").
    #    1B. `second` is the later take ("religion-custom-marry").
    #    1C. Sanity-check the manifest matches the story. If these
    #        fail, the manifest was edited — re-point the test or
    #        revert the manifest.
    first  = fixture_entry("151cd315")
    second = fixture_entry("db593e95")
    assert first["slug"]  == "religion-customary-vocal"
    assert second["slug"] == "religion-custom-marry"

    # 2. Ingest both entries into the clean temp archive.
    #    2A. `ingest_audio` copies raw_path into temp_volume/<uid>/raw/,
    #        writes a fresh event row, and returns the event dict.
    #    2B. The returned dict is the event as it lands in the feed.
    ev1 = archive.ingest_audio(
        uid,
        first["raw_path"],
        slug=first["slug"],
        tags=first["tags"],
        ext=first["ext"],
        transcript=first["transcript"],
    )
    ev2 = archive.ingest_audio(
        uid,
        second["raw_path"],
        slug=second["slug"],
        tags=second["tags"],
        ext=second["ext"],
        transcript=second["transcript"],
    )

    # 3. Confirm both entries land in the feed.
    #    3A. Feed length is 2 (nothing else was ingested).
    #    3B. get_feed returns newest-first, so the second ingest is
    #        at index 0 and the first is at index 1.
    feed = archive.get_feed(uid)
    assert len(feed) == 2
    assert feed[0]["slug"] == "religion-custom-marry"
    assert feed[1]["slug"] == "religion-customary-vocal"

    # 4. Confirm the transcripts round-trip byte-for-byte.
    #    Any downstream feature that searches or matches on transcript
    #    text (chat retrieval, UI search box) depends on this.
    assert feed[0]["transcript"] == second["transcript"]
    assert feed[1]["transcript"] == first["transcript"]

    # 5. Confirm the monastery tag is attached to both entries.
    #    Without this, filtering the feed by tag=monastery in the web
    #    UI would miss them.
    assert "monastery" in feed[0]["tags"]
    assert "monastery" in feed[1]["tags"]

    # 6. Confirm the raw .ogg files physically landed in temp volume.
    #    6A. ingest_audio uses shutil.copy2, so the fixture file
    #        itself stays untouched.
    #    6B. The copy lives at temp_volume/<uid>/raw/<file_id>.ogg.
    assert (temp_volume / uid / "raw" / f"{ev1['file_id']}.ogg").exists()
    assert (temp_volume / uid / "raw" / f"{ev2['file_id']}.ogg").exists()


def test_tag_inheritance_on_derived_files(
    db, seed_user, fake_audio, temp_volume,
):
    """
    Derived entries (midi output from to_midi, stems from stem_split,
    etc.) inherit every parent tag and append their own type tag. This
    is the invariant soul.md promises under "--- TAG INHERITANCE ---".
    """

    uid = seed_user("u_monastery_inherit")

    # 1. File a parent audio entry with four tags.
    parent = archive.ingest_audio(
        uid,
        fake_audio,
        slug="monastery-op1-melody",
        tags=["op1", "monastery", "melody", "vocal"],
        ext="ogg",
        transcript="",
    )

    # 2. File a derived text entry (simulating a to_midi job output)
    #    against the parent, carrying only the "midi" type tag.
    derived = archive.ingest_text(
        uid,
        slug="monastery-op1-melody-midi",
        tags=["midi"],
        text="NOTE C4 0 1\nNOTE E4 1 1",
        parent_id=parent["file_id"],
    )

    # 3. Confirm all four parent tags carry over and "midi" is
    #    appended at the end.
    #    Order is parent-tags-first-then-new so the UI renders a
    #    stable tag chip order across the parent and its derivatives.
    assert derived["tags"] == ["op1", "monastery", "melody", "vocal", "midi"]
