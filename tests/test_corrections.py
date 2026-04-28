"""
Edit-in-place tests with single-step undo.

How to read this file:

  1. The archive is event-sourced for INGEST (every recording, every
     filed text fragment is a new line in events.jsonl).
  2. Edits to existing entries (retag, slug fix, lyric fix) rewrite
     the matching event in place. We accept this for simplicity — no
     fold layer, no correction events.
  3. To compensate for the lost audit trail, every edit pre-snapshots
     the affected events to archive/.last_action.json. A single
     undo_last_action() call restores them. Batch edits in one call
     are undone as one unit.
  4. Each test below runs against a clean, isolated archive
     (`temp_archive` fixture). No LLM, no HTTP — pure
     services.archive API.
"""

import json

from services import archive


def _events(root) -> list[dict]:
    return [
        json.loads(l)
        for l in (root / "events.jsonl").read_text().splitlines()
        if l
    ]


def test_update_file_meta_edits_event_in_place(temp_archive, fake_audio):
    """
    1. File a parent audio entry.
    2. Call update_file_meta to change tags + transcript.
    3. The event log should still have ONE event for this file_id,
       and that event should now carry the new tags + transcript.
       The old values must be gone — that is the whole point of
       edit-in-place.
    """

    # 1. Ingest.
    parent = archive.ingest_audio(
        fake_audio,
        slug="air-conditioner-drone",
        tags=["monastery", "foley", "drone"],
        ext="ogg",
        transcript="steady hum",
    )

    # 2. Edit.
    ok = archive.update_file_meta(
        parent["file_id"],
        tags=["sketches", "foley", "drone"],
        transcript="air conditioner — steady hum",
    )
    assert ok is True

    # 3. There is still exactly one audio event for this file_id and
    #    it carries the new values.
    log = _events(temp_archive)
    audio_events = [
        ev for ev in log
        if ev.get("type") == "audio" and ev.get("file_id") == parent["file_id"]
    ]
    assert len(audio_events) == 1
    assert audio_events[0]["tags"] == ["sketches", "foley", "drone"]
    assert audio_events[0]["transcript"] == "air conditioner — steady hum"


def test_update_file_meta_shifts_entry_between_tag_filters(
    temp_archive, fake_audio
):
    """
    After an in-place edit:
      1. The entry leaves the OLD tag's filter.
      2. The entry appears in the NEW tag's filter.
      3. The unfiltered feed still shows it exactly once.
    """

    # 1. File the wrong-tag scenario.
    parent = archive.ingest_audio(
        fake_audio,
        slug="air-conditioner-drone",
        tags=["monastery", "foley", "drone"],
        ext="ogg",
        transcript="steady hum",
    )

    # 2. Sanity — pre-edit, monastery sees it, sketches doesn't.
    assert parent["file_id"] in [
        e["file_id"] for e in archive.get_feed(tag="monastery")
    ]
    assert parent["file_id"] not in [
        e["file_id"] for e in archive.get_feed(tag="sketches")
    ]

    # 3. Edit.
    archive.update_file_meta(
        parent["file_id"],
        tags=["sketches", "foley", "drone"],
    )

    # 4. Assertions:
    #    4A. Monastery filter no longer includes it.
    #    4B. Sketches filter does.
    #    4C. Unfiltered: present exactly once.
    assert parent["file_id"] not in [
        e["file_id"] for e in archive.get_feed(tag="monastery")
    ]
    assert parent["file_id"] in [
        e["file_id"] for e in archive.get_feed(tag="sketches")
    ]
    feed_ids = [e["file_id"] for e in archive.get_feed()]
    assert feed_ids.count(parent["file_id"]) == 1


def test_update_file_meta_returns_false_for_missing_file_id(temp_archive):
    """
    Editing a file_id that does not exist must return False. This
    contract lets API endpoints raise 404 cleanly without inspecting
    the event log themselves.
    """
    assert archive.update_file_meta("deadbeef", tags=["whatever"]) is False


def test_queue_job_inherits_edited_tags(temp_archive, fake_audio):
    """
    queue_job stamps the queued event with the parent's tags. After
    an in-place edit, the queued job event must carry the NEW tag
    set, not the original — otherwise derivatives stay stuck under
    the wrong project.
    """

    # 1. Ingest with the wrong tag.
    parent = archive.ingest_audio(
        fake_audio,
        slug="air-conditioner-drone",
        tags=["monastery", "foley", "drone"],
        ext="ogg",
        transcript="steady hum",
    )

    # 2. Edit it to the right tag.
    archive.update_file_meta(
        parent["file_id"],
        tags=["sketches", "foley", "drone"],
    )

    # 3. Queue a job.
    archive.queue_job("to_midi", parent["file_id"])

    # 4. The most recent job_queued event must carry the NEW tags.
    log = _events(temp_archive)
    job_event = next(
        ev for ev in reversed(log) if ev.get("type") == "job_queued"
    )
    assert job_event["tags"] == ["sketches", "foley", "drone"]
    assert "monastery" not in job_event["tags"]


def test_current_entry_returns_the_event(temp_archive, fake_audio):
    """
    `current_entry(file_id)` returns the audio/text event for the
    file_id with no fold or override layer. After an edit, it
    reflects the edited state because the underlying event has been
    rewritten.
    """

    # 1. Ingest.
    parent = archive.ingest_audio(
        fake_audio,
        slug="religion-customary",
        tags=["monastery", "lyric"],
        ext="ogg",
        transcript="Religion is a customary",
    )

    # 2. Pre-edit: current_entry matches the original.
    pre = archive.current_entry(parent["file_id"])
    assert pre["slug"]       == "religion-customary"
    assert pre["transcript"] == "Religion is a customary"
    assert pre["tags"]       == ["monastery", "lyric"]

    # 3. Edit slug + transcript.
    archive.update_file_meta(
        parent["file_id"],
        slug="religion-unnecessary",
        transcript="Religion is unnecessary",
    )

    # 4. Post-edit: current_entry sees the edited values.
    post = archive.current_entry(parent["file_id"])
    assert post["slug"]       == "religion-unnecessary"
    assert post["transcript"] == "Religion is unnecessary"
    assert post["tags"]       == ["monastery", "lyric"]


def test_current_entry_returns_empty_for_missing_or_deleted(
    temp_archive, fake_audio
):
    """
    current_entry returns an empty dict for two cases callers care
    about:
      1. The file_id was never ingested.
      2. The file_id was ingested, then soft-deleted.
    Returning {} (rather than None) lets callers chain `.get(...)`
    without nil-checks at every call site.
    """

    # 1. Missing.
    assert archive.current_entry("deadbeef") == {}

    # 2. Deleted.
    parent = archive.ingest_audio(
        fake_audio,
        slug="quick-sketch",
        tags=["sketches"],
        ext="ogg",
        transcript="",
    )
    archive.delete_file(parent["file_id"])
    assert archive.current_entry(parent["file_id"]) == {}


def test_undo_reverts_a_single_edit(temp_archive, fake_audio):
    """
    The cheap safety net: if the LLM edits an entry incorrectly, the
    user can call undo_last_action() once to restore the prior state.

    1. Ingest with original tags.
    2. Edit (the "wrong" edit we want to reverse).
    3. undo_last_action restores the original tags in the log.
    4. The entry shows up under the original tag filter again, not
       the edited one.
    """

    # 1. Ingest.
    parent = archive.ingest_audio(
        fake_audio,
        slug="elephant-room",
        tags=["monastery", "lyric"],
        ext="ogg",
        transcript="found you waiting with the patience of an elephant",
    )

    # 2. The "bad" edit.
    archive.update_file_meta(
        parent["file_id"],
        tags=["sketches"],
        transcript="totally wrong transcript",
    )

    # 3. Undo.
    restored = archive.undo_last_action()
    assert restored == 1

    # 4. State matches the original ingest.
    entry = archive.current_entry(parent["file_id"])
    assert entry["tags"]       == ["monastery", "lyric"]
    assert entry["transcript"] == (
        "found you waiting with the patience of an elephant"
    )

    # 5. Tag filters reflect the restored state.
    monastery_ids = [e["file_id"] for e in archive.get_feed(tag="monastery")]
    sketches_ids  = [e["file_id"] for e in archive.get_feed(tag="sketches")]
    assert parent["file_id"]     in monastery_ids
    assert parent["file_id"] not in sketches_ids


def test_undo_reverts_a_batch_edit_as_one_unit(temp_archive, fake_audio):
    """
    A batch edit (one call that touches multiple file_ids) is
    snapshotted as a single action and undone as a single action.

    1. Ingest two entries.
    2. Edit BOTH in a single update_files_meta call.
    3. One undo_last_action restores both.
    """

    # 1. Two ingests.
    a = archive.ingest_audio(
        fake_audio, slug="a", tags=["monastery"], ext="ogg", transcript="A"
    )
    b = archive.ingest_audio(
        fake_audio, slug="b", tags=["monastery"], ext="ogg", transcript="B"
    )

    # 2. Batch edit — both moved to sketches.
    archive.update_files_meta(
        [a["file_id"], b["file_id"]],
        tags=["sketches"],
    )

    # 3. Confirm the edit landed.
    assert archive.current_entry(a["file_id"])["tags"] == ["sketches"]
    assert archive.current_entry(b["file_id"])["tags"] == ["sketches"]

    # 4. One undo restores both.
    restored = archive.undo_last_action()
    assert restored == 2
    assert archive.current_entry(a["file_id"])["tags"] == ["monastery"]
    assert archive.current_entry(b["file_id"])["tags"] == ["monastery"]


def test_undo_with_no_prior_action_is_noop(temp_archive):
    """
    Calling undo on a brand-new archive (or after the snapshot has
    already been consumed by a previous undo) returns 0 and does not
    blow up. Idempotent.
    """
    assert archive.undo_last_action() == 0
    assert archive.undo_last_action() == 0


def test_undo_only_reverts_the_most_recent_action(temp_archive, fake_audio):
    """
    Single-step undo by design: the snapshot file holds only the
    most recent action. A second edit overwrites the first
    snapshot, so undo can no longer recover the pre-first-edit
    state.

    1. Ingest.
    2. Edit #1 (sketches).
    3. Edit #2 (drafts).
    4. Undo restores to AFTER edit #1, not before it.
    """

    parent = archive.ingest_audio(
        fake_audio, slug="x", tags=["monastery"], ext="ogg", transcript=""
    )
    archive.update_file_meta(parent["file_id"], tags=["sketches"])
    archive.update_file_meta(parent["file_id"], tags=["drafts"])

    archive.undo_last_action()
    assert archive.current_entry(parent["file_id"])["tags"] == ["sketches"]
