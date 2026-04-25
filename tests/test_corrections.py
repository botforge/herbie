"""
Correction events: the append-only mutation primitive.

How to read this file:

  1. Every test here exercises the correction flow on a clean, isolated
     archive (the `temp_archive` fixture). No LLM, no HTTP — pure
     services.archive API.
  2. The invariant under test: every state change is a NEW event in
     events.jsonl. Existing events are never rewritten in place,
     sidecars are not the source of truth, and the log alone tells the
     full story.
  3. We also assert the user-visible consequence: a correction shifts
     an entry between tag filters in get_feed without touching the
     audio event itself.
"""

import json

from services import archive


def _events(root) -> list[dict]:
    return [
        json.loads(l)
        for l in (root / "events.jsonl").read_text().splitlines()
        if l
    ]


def test_apply_correction_appends_event_without_mutating_original(
    temp_archive, fake_audio
):
    """
    Calling apply_correction must:
      1. Append exactly one event of type "correction" referencing the
         target file_id.
      2. Leave the original audio event byte-identical to what was
         written at ingest. This is the property that lets us claim the
         event log is append-only.
    """

    # 1. File a parent audio entry the test will correct.
    parent = archive.ingest_audio(
        fake_audio,
        slug="air-conditioner-drone",
        tags=["monastery", "foley", "drone"],
        ext="ogg",
        transcript="steady hum",
    )

    # 2. Snapshot the audio event verbatim so we can prove later that
    #    nothing rewrote it.
    before = _events(temp_archive)
    assert len(before) == 1
    audio_event_before = dict(before[0])

    # 3. Apply a correction: the user clarifies this entry is not
    #    monastery; it belongs in sketches.
    archive.apply_correction(
        parent["file_id"],
        tags=["sketches", "foley", "drone"],
    )

    # 4. The log should now have exactly two events: the original
    #    audio event (unchanged) and a new correction event.
    after = _events(temp_archive)
    assert len(after) == 2

    # 5. Original audio event survives byte-for-byte.
    #    5A. Same dict contents.
    #    5B. Therefore same line in the JSONL — _patch_events was not
    #        called, no rewrite happened.
    assert after[0] == audio_event_before

    # 6. New event is a correction with the expected shape.
    correction = after[1]
    assert correction["type"] == "correction"
    assert correction["file_id"] == parent["file_id"]
    assert correction["tags"] == ["sketches", "foley", "drone"]
    assert "created_at" in correction


def test_correction_shifts_entry_between_tag_filters(temp_archive, fake_audio):
    """
    After a correction changes an entry's tags, the entry must:
      1. Disappear from filtered queries on the OLD tag.
      2. Appear in filtered queries on the NEW tag.
      3. Still appear (exactly once) in the unfiltered feed — the
         correction itself does not duplicate the entry, it just
         updates how the folded view classifies it.
    The original audio event still lives in the log unchanged; only
    the displayed feed reflects the latest correction.
    """

    # 1. File the same wrong-tag scenario: an air-conditioner drone
    #    that ended up tagged "monastery" by mistake.
    parent = archive.ingest_audio(
        fake_audio,
        slug="air-conditioner-drone",
        tags=["monastery", "foley", "drone"],
        ext="ogg",
        transcript="steady hum",
    )

    # 2. Sanity-check: before any correction, the entry shows up
    #    under monastery and not under sketches.
    assert parent["file_id"] in [
        e["file_id"] for e in archive.get_feed(tag="monastery")
    ]
    assert parent["file_id"] not in [
        e["file_id"] for e in archive.get_feed(tag="sketches")
    ]

    # 3. Apply the correction.
    archive.apply_correction(
        parent["file_id"],
        tags=["sketches", "foley", "drone"],
    )

    # 4. After the correction:
    #    4A. Filter by monastery: the entry is gone.
    #    4B. Filter by sketches: the entry is now present.
    #    4C. Unfiltered: the entry is present exactly once. The
    #        correction is folded into the displayed view; it does
    #        not produce a duplicate row.
    assert parent["file_id"] not in [
        e["file_id"] for e in archive.get_feed(tag="monastery")
    ]
    assert parent["file_id"] in [
        e["file_id"] for e in archive.get_feed(tag="sketches")
    ]
    feed_ids = [e["file_id"] for e in archive.get_feed()]
    assert feed_ids.count(parent["file_id"]) == 1

    # 5. The folded entry surfaced through get_feed should carry the
    #    NEW tags — that is the whole point of folding.
    folded = next(
        e for e in archive.get_feed() if e["file_id"] == parent["file_id"]
    )
    assert folded["tags"] == ["sketches", "foley", "drone"]


def test_latest_correction_wins_per_dimension(temp_archive, fake_audio):
    """
    Corrections compose by replaying in event-log order:
      1. The most recent correction for a given dimension wins.
      2. Dimensions not touched by the latest correction fall back to
         the previous correction (or the original event) for that
         dimension. Corrections do not need to restate everything.
    """

    # 1. File a parent entry with a transcript and a tag set.
    parent = archive.ingest_audio(
        fake_audio,
        slug="religion-customary",
        tags=["monastery", "lyric"],
        ext="ogg",
        transcript="Religion is a customary",
    )

    # 2. First correction fixes the transcript only.
    archive.apply_correction(
        parent["file_id"],
        transcript="Religion is unnecessary",
    )

    # 3. Second correction fixes the slug only.
    archive.apply_correction(
        parent["file_id"],
        slug="religion-unnecessary",
    )

    # 4. The folded view should reflect:
    #    4A. slug from correction #2 (the latest to touch that field).
    #    4B. transcript from correction #1 (correction #2 did not
    #        restate transcript, so the prior correction still
    #        applies).
    #    4C. tags from the original audio event (no correction has
    #        touched tags, so they pass through).
    folded = next(
        e for e in archive.get_feed() if e["file_id"] == parent["file_id"]
    )
    assert folded["slug"]       == "religion-unnecessary"
    assert folded["transcript"] == "Religion is unnecessary"
    assert folded["tags"]       == ["monastery", "lyric"]


def test_current_entry_returns_folded_state(temp_archive, fake_audio):
    """
    `current_entry(file_id)` should return the post-fold view of an
    entry — original audio/text fields with corrections layered on
    top — so callers can ask "what does this entry look like right
    now?" without having to re-implement the fold themselves.
    """

    # 1. File an audio entry with a known tag set and transcript.
    parent = archive.ingest_audio(
        fake_audio,
        slug="religion-customary",
        tags=["monastery", "lyric"],
        ext="ogg",
        transcript="Religion is a customary",
    )

    # 2. Apply a correction to slug + transcript.
    archive.apply_correction(
        parent["file_id"],
        slug="religion-unnecessary",
        transcript="Religion is unnecessary",
    )

    # 3. current_entry should reflect the correction in slug and
    #    transcript while preserving the untouched tags.
    entry = archive.current_entry(parent["file_id"])
    assert entry["slug"]       == "religion-unnecessary"
    assert entry["transcript"] == "Religion is unnecessary"
    assert entry["tags"]       == ["monastery", "lyric"]
    assert entry["file_id"]    == parent["file_id"]


def test_current_entry_returns_empty_for_missing_or_deleted(temp_archive, fake_audio):
    """
    `current_entry(file_id)` must return an empty dict when:
      1. The file_id was never ingested.
      2. The file_id was ingested then soft-deleted. (We treat
         deleted entries as if they no longer exist for current-state
         lookups, which keeps callers from accidentally inheriting
         tags from a deleted parent.)
    Returning {} (rather than None) lets callers chain `.get(...)`
    safely without nil-checks at every call site.
    """

    # 1. Missing file_id.
    assert archive.current_entry("deadbeef") == {}

    # 2. Ingested then deleted.
    parent = archive.ingest_audio(
        fake_audio,
        slug="quick-sketch",
        tags=["sketches"],
        ext="ogg",
        transcript="",
    )
    archive.delete_file(parent["file_id"])
    assert archive.current_entry(parent["file_id"]) == {}


def test_queue_job_inherits_corrected_tags(temp_archive, fake_audio):
    """
    queue_job stamps the queued event with the parent's tags so the
    feed and tag filters can show the job alongside its parent. After
    a correction changes the parent's tags, queue_job must pick up the
    NEW tag set, not the original one. Otherwise a corrected entry's
    derivatives stay stuck under the old (wrong) project.
    """

    # 1. File a parent audio entry tagged with the wrong project.
    parent = archive.ingest_audio(
        fake_audio,
        slug="air-conditioner-drone",
        tags=["monastery", "foley", "drone"],
        ext="ogg",
        transcript="steady hum",
    )

    # 2. Correct the tags — the user clarifies it belongs in sketches.
    archive.apply_correction(
        parent["file_id"],
        tags=["sketches", "foley", "drone"],
    )

    # 3. Queue a job against the corrected entry.
    archive.queue_job("to_midi", parent["file_id"])

    # 4. Find the most recently appended job_queued event in the log.
    log = [
        json.loads(l)
        for l in (temp_archive / "events.jsonl").read_text().splitlines()
        if l
    ]
    job_event = next(
        ev for ev in reversed(log) if ev.get("type") == "job_queued"
    )

    # 5. The job event must carry the CORRECTED tag set, never the
    #    original "monastery" tag.
    assert job_event["tags"] == ["sketches", "foley", "drone"]
    assert "monastery" not in job_event["tags"]


def test_apply_correction_on_missing_file_id_is_a_noop(temp_archive):
    """
    Applying a correction to a file_id that does not exist must:
      1. Return None (signaling "no entry to correct").
      2. Not append anything to events.jsonl. The log should look
         exactly like it did before the call.
    This protects against typos in the LLM tool call from polluting
    the log with orphan corrections.
    """

    # 1. Snapshot the empty (or near-empty) log.
    before = _events(temp_archive)

    # 2. Try to correct a file_id that was never ingested.
    result = archive.apply_correction("deadbeef", tags=["whatever"])

    # 3. Confirm the no-op contract.
    assert result is None
    assert _events(temp_archive) == before
