"""
Postgres-backed archive tests.

How to read this file:

  1. Each test seeds at least one user via the seed_user fixture.
  2. ingest_audio copies bytes to <volume>/<user>/raw/<fid>.<ext>
     and inserts an audio event row.
  3. current_entry returns the row, or {} if absent or soft-deleted.
  4. get_feed returns newest-first audio/text events for the user,
     filtered by tag, paginated.
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
