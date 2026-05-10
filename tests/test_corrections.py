"""
Edit-in-place tests with single-step undo.

How to read this file:

  1. The archive is event-sourced: every recording and filed text
     fragment is a row in the Postgres `events` table.
  2. Edits to existing entries (retag, slug fix, lyric fix) rewrite
     the matching row in place via update_file_meta / update_files_meta.
     We accept in-place mutation here for simplicity — the chat
     surface stays append-only via file_system_note.
  3. To compensate for the lost audit trail, every edit pre-snapshots
     the affected rows to `last_action`. A single undo_last_action()
     call restores them. Batch edits in one call are undone as one unit.
  4. Each test below runs against a clean DB (the `db` fixture truncates
     all tables) and a temp VOLUME_ROOT (`temp_volume`). No LLM, no
     HTTP — pure services.archive API.
"""

from services import archive


def test_update_file_meta_edits_event_in_place(
    db, seed_user, fake_audio, temp_volume,
):
    """
    1. Seed a user and file a parent audio entry.
    2. Call update_file_meta to change tags + transcript.
    3. current_entry must reflect the new values, and get_feed
       must still contain exactly one event for this file_id —
       the old values must be gone, which is the whole point of
       edit-in-place.
    """

    uid = seed_user("u_edit")

    # 1. Ingest.
    parent = archive.ingest_audio(
        uid,
        fake_audio,
        slug="air-conditioner-drone",
        tags=["monastery", "foley", "drone"],
        ext="ogg",
        transcript="steady hum",
    )

    # 2. Edit.
    ok = archive.update_file_meta(
        uid,
        parent["file_id"],
        tags=["sketches", "foley", "drone"],
        transcript="air conditioner — steady hum",
    )
    assert ok is True

    # 3. current_entry carries the new values; feed still has exactly
    #    one audio event for this file_id.
    entry = archive.current_entry(uid, parent["file_id"])
    assert entry["tags"]       == ["sketches", "foley", "drone"]
    assert entry["transcript"] == "air conditioner — steady hum"

    feed = archive.get_feed(uid)
    matching = [e for e in feed if e["file_id"] == parent["file_id"]]
    assert len(matching) == 1


def test_update_file_meta_shifts_entry_between_tag_filters(
    db, seed_user, fake_audio, temp_volume,
):
    """
    After an in-place edit:
      1. The entry leaves the OLD tag's filter.
      2. The entry appears in the NEW tag's filter.
      3. The unfiltered feed still shows it exactly once.
    """

    uid = seed_user("u_shift")

    # 1. File the wrong-tag scenario.
    parent = archive.ingest_audio(
        uid,
        fake_audio,
        slug="air-conditioner-drone",
        tags=["monastery", "foley", "drone"],
        ext="ogg",
        transcript="steady hum",
    )

    # 2. Sanity — pre-edit, monastery sees it, sketches doesn't.
    assert parent["file_id"] in [
        e["file_id"] for e in archive.get_feed(uid, tag="monastery")
    ]
    assert parent["file_id"] not in [
        e["file_id"] for e in archive.get_feed(uid, tag="sketches")
    ]

    # 3. Edit.
    archive.update_file_meta(
        uid,
        parent["file_id"],
        tags=["sketches", "foley", "drone"],
    )

    # 4. Assertions:
    #    4A. Monastery filter no longer includes it.
    #    4B. Sketches filter does.
    #    4C. Unfiltered: present exactly once.
    assert parent["file_id"] not in [
        e["file_id"] for e in archive.get_feed(uid, tag="monastery")
    ]
    assert parent["file_id"] in [
        e["file_id"] for e in archive.get_feed(uid, tag="sketches")
    ]
    feed_ids = [e["file_id"] for e in archive.get_feed(uid)]
    assert feed_ids.count(parent["file_id"]) == 1


def test_update_file_meta_returns_false_for_missing_file_id(
    db, seed_user, temp_volume,
):
    """
    1. Seed a user with an empty archive.
    2. Editing a file_id that does not exist must return False. This
       contract lets API endpoints raise 404 cleanly without inspecting
       the event log themselves.
    """
    uid = seed_user("u_missing")
    assert archive.update_file_meta(uid, "deadbeef", tags=["whatever"]) is False


def test_queue_job_inherits_edited_tags(
    db, seed_user, fake_audio, temp_volume,
):
    """
    queue_job stamps the queued event with the parent's current tags.
    After an in-place edit, the queued job event must carry the NEW
    tag set, not the original — otherwise derivatives stay stuck under
    the wrong project.
    """

    uid = seed_user("u_queuejob")

    # 1. Ingest with the wrong tag.
    parent = archive.ingest_audio(
        uid,
        fake_audio,
        slug="air-conditioner-drone",
        tags=["monastery", "foley", "drone"],
        ext="ogg",
        transcript="steady hum",
    )

    # 2. Edit it to the right tag.
    archive.update_file_meta(
        uid,
        parent["file_id"],
        tags=["sketches", "foley", "drone"],
    )

    # 3. Queue a job — tags are read from current_entry at queue time.
    archive.queue_job(uid, "to_midi", parent["file_id"])

    # 4. The most recent job_queued event must carry the NEW tags.
    #    Fetch via get_jobs so we stay in the public API surface.
    jobs = archive.get_jobs(uid)
    assert jobs[0]["type"] == "to_midi"
    assert jobs[0]["input_file_id"] == parent["file_id"]
    # Verify through the feed that the job_queued event has the right tags.
    from services import db as _db
    row = _db.fetch_one(
        "SELECT tags FROM events WHERE user_id = %s AND type = 'job_queued' ORDER BY created_at DESC LIMIT 1",
        (uid,),
    )
    assert row is not None
    assert row["tags"] == ["sketches", "foley", "drone"]
    assert "monastery" not in row["tags"]


def test_current_entry_returns_the_event(
    db, seed_user, fake_audio, temp_volume,
):
    """
    `current_entry(uid, file_id)` returns the audio/text event for the
    file_id. After an edit it reflects the edited state because the
    underlying row has been rewritten in place.
    """

    uid = seed_user("u_current")

    # 1. Ingest.
    parent = archive.ingest_audio(
        uid,
        fake_audio,
        slug="religion-customary",
        tags=["monastery", "lyric"],
        ext="ogg",
        transcript="Religion is a customary",
    )

    # 2. Pre-edit: current_entry matches the original.
    pre = archive.current_entry(uid, parent["file_id"])
    assert pre["slug"]       == "religion-customary"
    assert pre["transcript"] == "Religion is a customary"
    assert pre["tags"]       == ["monastery", "lyric"]

    # 3. Edit slug + transcript.
    archive.update_file_meta(
        uid,
        parent["file_id"],
        slug="religion-unnecessary",
        transcript="Religion is unnecessary",
    )

    # 4. Post-edit: current_entry sees the edited values.
    post = archive.current_entry(uid, parent["file_id"])
    assert post["slug"]       == "religion-unnecessary"
    assert post["transcript"] == "Religion is unnecessary"
    assert post["tags"]       == ["monastery", "lyric"]


def test_current_entry_returns_empty_for_missing_or_deleted(
    db, seed_user, fake_audio, temp_volume,
):
    """
    current_entry returns an empty dict for two cases callers care
    about:
      1. The file_id was never ingested.
      2. The file_id was ingested, then soft-deleted.
    Returning {} (rather than None) lets callers chain `.get(...)`
    without nil-checks at every call site.
    """

    uid = seed_user("u_deleted")

    # 1. Missing — file_id that was never ingested.
    assert archive.current_entry(uid, "deadbeef") == {}

    # 2. Deleted — ingest then soft-delete via delete_file.
    parent = archive.ingest_audio(
        uid,
        fake_audio,
        slug="quick-sketch",
        tags=["sketches"],
        ext="ogg",
        transcript="",
    )
    archive.delete_file(uid, parent["file_id"])
    assert archive.current_entry(uid, parent["file_id"]) == {}


def test_undo_reverts_a_single_edit(
    db, seed_user, fake_audio, temp_volume,
):
    """
    The cheap safety net: if the LLM edits an entry incorrectly, the
    user can call undo_last_action() once to restore the prior state.

    1. Seed a user and ingest with original tags.
    2. Edit (the "wrong" edit we want to reverse).
    3. undo_last_action restores the original tags in the DB.
    4. The entry shows up under the original tag filter again, not
       the edited one.
    """

    uid = seed_user("u_undo_single")

    # 1. Ingest.
    parent = archive.ingest_audio(
        uid,
        fake_audio,
        slug="elephant-room",
        tags=["monastery", "lyric"],
        ext="ogg",
        transcript="found you waiting with the patience of an elephant",
    )

    # 2. The "bad" edit.
    archive.update_file_meta(
        uid,
        parent["file_id"],
        tags=["sketches"],
        transcript="totally wrong transcript",
    )

    # 3. Undo.
    restored = archive.undo_last_action(uid)
    assert restored == 1

    # 4. State matches the original ingest.
    entry = archive.current_entry(uid, parent["file_id"])
    assert entry["tags"]       == ["monastery", "lyric"]
    assert entry["transcript"] == (
        "found you waiting with the patience of an elephant"
    )

    # 5. Tag filters reflect the restored state.
    monastery_ids = [e["file_id"] for e in archive.get_feed(uid, tag="monastery")]
    sketches_ids  = [e["file_id"] for e in archive.get_feed(uid, tag="sketches")]
    assert parent["file_id"]     in monastery_ids
    assert parent["file_id"] not in sketches_ids


def test_undo_reverts_a_batch_edit_as_one_unit(
    db, seed_user, fake_audio, temp_volume,
):
    """
    A batch edit (one call that touches multiple file_ids) is
    snapshotted as a single action and undone as a single action.

    1. Seed a user and ingest two entries.
    2. Edit BOTH in a single update_files_meta call.
    3. One undo_last_action restores both.
    """

    uid = seed_user("u_undo_batch")

    # 1. Two ingests.
    a = archive.ingest_audio(
        uid, fake_audio, slug="a", tags=["monastery"], ext="ogg", transcript="A"
    )
    b = archive.ingest_audio(
        uid, fake_audio, slug="b", tags=["monastery"], ext="ogg", transcript="B"
    )

    # 2. Batch edit — both moved to sketches.
    archive.update_files_meta(
        uid,
        [a["file_id"], b["file_id"]],
        tags=["sketches"],
    )

    # 3. Confirm the edit landed.
    assert archive.current_entry(uid, a["file_id"])["tags"] == ["sketches"]
    assert archive.current_entry(uid, b["file_id"])["tags"] == ["sketches"]

    # 4. One undo restores both.
    restored = archive.undo_last_action(uid)
    assert restored == 2
    assert archive.current_entry(uid, a["file_id"])["tags"] == ["monastery"]
    assert archive.current_entry(uid, b["file_id"])["tags"] == ["monastery"]


def test_undo_with_no_prior_action_is_noop(db, seed_user, temp_volume):
    """
    1. Seed a user whose last_action buffer is empty.
    2. Calling undo on an empty buffer returns 0 and does not blow up.
       Idempotent — calling it again still returns 0.
    """
    uid = seed_user("u_undo_noop")
    assert archive.undo_last_action(uid) == 0
    assert archive.undo_last_action(uid) == 0


def test_undo_only_reverts_the_most_recent_action(
    db, seed_user, fake_audio, temp_volume,
):
    """
    Single-step undo by design: the last_action row holds only the
    most recent action. A second edit overwrites the first snapshot,
    so undo can no longer recover the pre-first-edit state.

    1. Seed a user and ingest.
    2. Edit #1 (sketches).
    3. Edit #2 (drafts).
    4. Undo restores to AFTER edit #1, not before it.
    """

    uid = seed_user("u_undo_step")

    parent = archive.ingest_audio(
        uid, fake_audio, slug="x", tags=["monastery"], ext="ogg", transcript=""
    )
    archive.update_file_meta(uid, parent["file_id"], tags=["sketches"])
    archive.update_file_meta(uid, parent["file_id"], tags=["drafts"])

    archive.undo_last_action(uid)
    assert archive.current_entry(uid, parent["file_id"])["tags"] == ["sketches"]
