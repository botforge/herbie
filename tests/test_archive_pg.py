"""
Postgres-backed archive tests.

How to read this file:

  1. Each test seeds at least one user via the seed_user fixture.
  2. ingest_audio / ingest_text both copy bytes / write payload to
     <volume>/<user>/raw/ and insert an event row.
  3. current_entry returns the row, or {} if absent or soft-deleted.
  4. get_feed returns newest-first audio/text events for the user,
     filtered by tag, paginated.
  5. get_all_tags counts tag occurrences across live entries.
  6. delete_file soft-deletes via a delete event.
  7. search matches case-insensitive substring across slug / tags /
     transcript / text.
  8. update_file_meta / update_files_meta mutate rows in place (web PATCH
     surface only) and snapshot prior state into last_action.
  9. undo_last_action restores the snapshot and clears the buffer.
"""

from pathlib import Path

import pytest

from services import archive


@pytest.fixture
def fake_audio(tmp_path) -> Path:
    p = tmp_path / "src.ogg"
    p.write_bytes(b"OGGS\x00\x00fake-audio")
    return p


@pytest.fixture
def temp_volume(tmp_path, monkeypatch):
    monkeypatch.setattr(archive, "VOLUME_ROOT", tmp_path / "v")
    archive.ensure_archive_root()
    return tmp_path / "v"


def test_ingest_audio_copies_bytes_and_inserts_event(
    db, seed_user, fake_audio, temp_volume,
):
    uid = seed_user("dhruv")

    ev = archive.ingest_audio(
        uid, str(fake_audio),
        slug="test-loop", tags=["sketch", "loop"],
        ext="ogg", transcript="hello",
    )

    assert ev["slug"] == "test-loop"
    assert ev["tags"] == ["sketch", "loop"]
    raw_path = temp_volume / uid / "raw" / f"{ev['file_id']}.ogg"
    assert raw_path.exists()
    assert raw_path.read_bytes() == fake_audio.read_bytes()

    same = archive.current_entry(uid, ev["file_id"])
    assert same["slug"] == "test-loop"
    assert same["transcript"] == "hello"


def test_current_entry_isolates_users(
    db, seed_user, fake_audio, temp_volume,
):
    a = seed_user("alice")
    b = seed_user("bob")

    ev = archive.ingest_audio(a, str(fake_audio), "x", [], "ogg", "")
    assert archive.current_entry(b, ev["file_id"]) == {}


def test_get_feed_newest_first_with_tag(
    db, seed_user, fake_audio, temp_volume,
):
    uid = seed_user("dhruv")
    archive.ingest_audio(uid, str(fake_audio), "older", ["a"], "ogg", "")
    archive.ingest_audio(uid, str(fake_audio), "newer", ["a"], "ogg", "")

    feed = archive.get_feed(uid, tag="a")
    assert [e["slug"] for e in feed] == ["newer", "older"]


def test_ingest_text_inserts_row_and_writes_payload(
    db, seed_user, temp_volume,
):
    uid = seed_user("dhruv")
    ev = archive.ingest_text(uid, "lyrics-1", ["lyric"], "all the broken hours")

    txt = temp_volume / uid / "raw" / f"{ev['file_id']}.txt"
    assert txt.exists()
    assert txt.read_text() == "all the broken hours"

    feed = archive.get_feed(uid)
    assert any(e["file_id"] == ev["file_id"] for e in feed)


def test_get_all_tags_counts_per_user(db, seed_user, fake_audio, temp_volume):
    uid = seed_user("dhruv")
    archive.ingest_audio(uid, str(fake_audio), "a", ["x", "y"], "ogg", "")
    archive.ingest_audio(uid, str(fake_audio), "b", ["x"],     "ogg", "")
    archive.ingest_text(uid, "n", ["x", "y"], "note")

    by_tag = {t["tag"]: t["count"] for t in archive.get_all_tags(uid)}
    assert by_tag == {"x": 3, "y": 2}


def test_delete_file_soft_deletes_and_filter_drops_it(
    db, seed_user, fake_audio, temp_volume,
):
    uid = seed_user("dhruv")
    ev = archive.ingest_audio(uid, str(fake_audio), "kill-me", ["x"], "ogg", "")
    assert archive.delete_file(uid, ev["file_id"]) is True
    assert archive.current_entry(uid, ev["file_id"]) == {}
    assert ev["file_id"] not in [e["file_id"] for e in archive.get_feed(uid)]


def test_search_matches_slug_tag_text_transcript(
    db, seed_user, fake_audio, temp_volume,
):
    uid = seed_user("dhruv")
    archive.ingest_audio(uid, str(fake_audio), "monastery-loop", ["foley"], "ogg",
                         "the bell rings once")
    archive.ingest_text(uid, "lyrics-x", ["lyric"], "hospital corridor lights")

    res = archive.search(uid, "bell")
    assert any(e["slug"] == "monastery-loop" for e in res)

    res2 = archive.search(uid, "corridor")
    assert any(e["slug"] == "lyrics-x" for e in res2)


def test_update_meta_changes_tags_and_undo_restores(
    db, seed_user, fake_audio, temp_volume,
):
    # 1. Ingest a file with tags ["a", "b"] to use as our target.
    uid = seed_user("dhruv")
    ev = archive.ingest_audio(uid, str(fake_audio), "x", ["a", "b"], "ogg", "")
    fid = ev["file_id"]

    # 2. Patch tags to ["c"] — should return True and the entry should reflect
    #    the new tag list immediately.
    assert archive.update_file_meta(uid, fid, tags=["c"]) is True
    assert archive.current_entry(uid, fid)["tags"] == ["c"]

    # 3. Undo the patch — should restore ["a", "b"] and report 1 row restored.
    n = archive.undo_last_action(uid)
    assert n == 1
    assert archive.current_entry(uid, fid)["tags"] == ["a", "b"]


def test_update_meta_unknown_returns_false(db, seed_user, temp_volume):
    # 1. Attempt to update a file_id that does not exist for the user.
    # 2. Expect False — no row was matched, so nothing was changed.
    uid = seed_user("dhruv")
    assert archive.update_file_meta(uid, "deadbeef", tags=["x"]) is False
