"""
Job execution — stub implementations.
All jobs run synchronously (fast stubs, no real DSP).

  rhythm_to_midi  → random NOTE grid text entry + "midi" tag
  stem_split      → 4 copies of original audio (vocal/bass/drums/other)
  autotune        → original audio re-filed with "tuned" tag
  transpose       → original audio re-filed with "transposed" tag
  render_chords   → random chord MIDI text entry
  summarize       → LLM distillation of all text/lyric entries for a tag
"""

import functools
import random
from pathlib import Path

from services.archive import (
    RAW_DIR,
    complete_job,
    current_entry,
    ingest_audio,
    ingest_text,
)

print = functools.partial(print, flush=True)


# ─────────────────────────────────────────────────────────────────────────────
# Job-marker helpers  (shared by FastAPI + Telegram transports)
# ─────────────────────────────────────────────────────────────────────────────

JOB_MARKER = "<<<job>>>"


def parse_job_marker(raw_reply: str) -> dict | None:
    import json as _j
    if not raw_reply or not raw_reply.startswith(JOB_MARKER):
        return None
    try:
        return _j.loads(raw_reply[len(JOB_MARKER):])
    except Exception:
        return None


def stub_job_response(args: dict) -> str:
    jt   = args.get("job_type", "?")
    tag  = args.get("tag", "")
    fid  = args.get("file_id", "")
    subj = tag or fid or "that"
    stubs = {
        "summarize":     f"(stub) would summarize {subj}",
        "to_midi":       f"(stub) would convert {subj} to midi",
        "stem_split":    f"(stub) would split {subj} into vocal/bass/drums/other",
        "autotune":      f"(stub) would autotune {subj}",
        "transpose":     f"(stub) would transpose {subj}",
        "render_chords": f"(stub) would render chord reference for {subj}",
    }
    return stubs.get(jt, f"(stub) unknown job {jt}")


def handle_job(args: dict) -> str:
    """
    Real execution for jobs where it's implemented; stubs otherwise.
    Returns the user-facing reply string.
    """
    jt = args.get("job_type", "?")
    print(f"[HERBIE/jobs] handle_job: {args}")

    if jt == "to_midi":
        fid = (args.get("file_id") or "").strip()
        if not fid:
            return "which file? give me a file_id from the archive."
        sc = current_entry(fid)
        print(f"[HERBIE/jobs] to_midi: sidecar for {fid}: {bool(sc)}")
        if not sc:
            return f"file {fid} not found."
        ev = _generate_midi_for(sc)
        parent_slug = sc.get("slug", fid)
        print(f"[HERBIE/jobs] to_midi: filed {ev['slug']} (parent {parent_slug})")
        return f"filed {ev['slug']} — midi grid derived from {parent_slug}."

    if jt == "render_chords":
        raw = args.get("chords") or []
        if isinstance(raw, str):
            import re
            raw = [c.strip() for c in re.split(r"[\s,|\-–—]+", raw) if c.strip()]
        chords = [c for c in raw if _parse_chord(c)]
        print(f"[HERBIE/jobs] render_chords: parsed {len(chords)} of {len(raw)} — {chords}")
        if not chords:
            return "which chords? give me a progression like Em - Am - D - G."
        tag = (args.get("tag") or "").strip()
        ev  = _render_chord_progression(chords, tag=tag)
        return f"filed {ev['slug']} — {' - '.join(chords)} rendered as midi grid."

    # All remaining audio jobs need a file_id and a sidecar
    if jt in ("stem_split", "autotune", "transpose"):
        fid = (args.get("file_id") or "").strip()
        if not fid:
            return "which file? give me a file_id from the archive."
        sc = current_entry(fid)
        if not sc:
            return f"file {fid} not found."
        parent_slug = sc.get("slug", fid)

        if jt == "stem_split":
            evs = _stem_split_for(sc)
            slugs = ", ".join(e["slug"] for e in evs)
            return f"filed {len(evs)} stems from {parent_slug}: {slugs}"

        if jt == "autotune":
            ev = _copy_audio_with_tag(sc, extra_tag="tuned")
            return f"filed {ev['slug']} — autotuned copy of {parent_slug}."

        if jt == "transpose":
            semi = int(args.get("semitones") or 0)
            label = f"up{semi}" if semi > 0 else (f"down{abs(semi)}" if semi < 0 else "same")
            ev = _copy_audio_with_tag(
                sc, extra_tag=f"transposed-{label}",
                slug_suffix=f"transposed-{label}",
            )
            return f"filed {ev['slug']} — transposed {label} from {parent_slug}."

    return stub_job_response(args)


# ─────────────────────────────────────────────────────────────────────────────
# Dispatcher
# ─────────────────────────────────────────────────────────────────────────────

def execute_job(job: dict) -> None:
    jtype = job.get("job_type") or job.get("type", "")
    fid   = job.get("input_file_id", "")
    sc    = current_entry(fid)
    if not sc and jtype != "summarize":
        return

    handlers = {
        "to_midi":        _to_midi,
        "stem_split":     _stem_split,
        "autotune":       _autotune,
        "transpose":      _transpose,
        "render_chords":  _render_chords,
        "summarize":      _summarize,
    }
    fn = handlers.get(jtype)
    if fn:
        try:
            fn(job, sc)
        except Exception as e:
            import logging
            logging.getLogger("herbie").error(f"job {jtype} failed: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# rhythm_to_midi
# ─────────────────────────────────────────────────────────────────────────────

_SCALES = {
    "minor":  ["C3","D3","Eb3","F3","G3","Ab3","Bb3","C4","D4","Eb4","F4","G4"],
    "major":  ["C3","D3","E3","F3","G3","A3","B3","C4","D4","E4","F4","G4"],
    "pentatonic": ["C3","Eb3","F3","G3","Bb3","C4","Eb4","F4","G4","Bb4"],
}

def _random_midi_notes(n: int = 24) -> str:
    scale = random.choice(list(_SCALES.values()))
    lines = []
    t = 0.0
    for _ in range(n):
        pitch = random.choice(scale)
        dur   = random.choice([0.25, 0.25, 0.5, 0.5, 0.5, 1.0])
        lines.append(f"NOTE {pitch} {t:.3f} {dur:.3f}")
        gap = random.choice([0.0, 0.0, 0.0, 0.25])
        t  += dur + gap
    return "\n".join(lines)


def _generate_midi_for(sc: dict) -> dict:
    parent_tags = list(sc.get("tags", []))
    parent_slug = sc.get("slug", "audio")
    slug        = parent_slug + "-midi"
    tags        = parent_tags + [t for t in ["midi"] if t not in parent_tags]
    notes_text  = _random_midi_notes()
    note_lines  = [l for l in notes_text.splitlines() if l.startswith("NOTE")]
    description = f"midi derived from {parent_slug} — {len(note_lines)} notes"
    return ingest_text(
        slug, tags, description,
        parent_id=sc["id"],
        midi_notes=notes_text,
    )


def _to_midi(job: dict, sc: dict) -> None:
    ev = _generate_midi_for(sc)
    complete_job(job["job_id"], output_file_id=ev["file_id"])


# ─────────────────────────────────────────────────────────────────────────────
# stem_split
# ─────────────────────────────────────────────────────────────────────────────

_STEM_NAMES = ["vocals", "bass", "drums", "other"]


def _stem_split_for(sc: dict) -> list[dict]:
    """Return 4 audio entries — stubbed stems that reuse the original audio."""
    ext      = sc.get("ext", "ogg")
    src_path = RAW_DIR / f"{sc['id']}.{ext}"
    parent_tags = list(sc.get("tags", []))
    parent_slug = sc.get("slug", "audio")
    events = []
    for stem in _STEM_NAMES:
        slug = f"{parent_slug}-{stem}"
        tags = parent_tags + [t for t in ["stem", stem] if t not in parent_tags]
        ev = ingest_audio(
            str(src_path), slug, tags, ext,
            sc.get("transcript", ""), parent_id=sc["id"],
        )
        events.append(ev)
    return events


def _copy_audio_with_tag(sc: dict, extra_tag: str, slug_suffix: str | None = None) -> dict:
    """Return one audio entry — a stubbed copy with an added tag."""
    ext         = sc.get("ext", "ogg")
    src_path    = RAW_DIR / f"{sc['id']}.{ext}"
    parent_tags = list(sc.get("tags", []))
    parent_slug = sc.get("slug", "audio")
    slug        = f"{parent_slug}-{slug_suffix or extra_tag}"
    tags        = parent_tags + [t for t in [extra_tag] if t not in parent_tags]
    return ingest_audio(
        str(src_path), slug, tags, ext,
        sc.get("transcript", ""), parent_id=sc["id"],
    )


def _stem_split(job: dict, sc: dict) -> None:
    evs = _stem_split_for(sc)
    complete_job(job["job_id"], output_file_id=evs[-1]["file_id"] if evs else None)


def _autotune(job: dict, sc: dict) -> None:
    ev = _copy_audio_with_tag(sc, extra_tag="tuned")
    complete_job(job["job_id"], output_file_id=ev["file_id"])


def _transpose(job: dict, sc: dict) -> None:
    semitones = job.get("params", {}).get("semitones", 0)
    label = f"up{semitones}" if semitones > 0 else f"down{abs(semitones)}"
    ev = _copy_audio_with_tag(sc, extra_tag=f"transposed-{label}", slug_suffix=f"transposed-{label}")
    complete_job(job["job_id"], output_file_id=ev["file_id"])


# ─────────────────────────────────────────────────────────────────────────────
# render_chords
# ─────────────────────────────────────────────────────────────────────────────

_ROOT_SEMI = {
    "C": 0, "C#": 1, "Db": 1,
    "D": 2, "D#": 3, "Eb": 3,
    "E": 4, "Fb": 4,
    "F": 5, "E#": 5, "F#": 6, "Gb": 6,
    "G": 7, "G#": 8, "Ab": 8,
    "A": 9, "A#": 10, "Bb": 10,
    "B": 11, "Cb": 11,
}

# Longest first — parser consumes the longest matching quality
_QUALITY_INTERVALS = [
    ("maj7",  [0, 4, 7, 11]),
    ("min7",  [0, 3, 7, 10]),
    ("dim7",  [0, 3, 6, 9]),
    ("m7b5",  [0, 3, 6, 10]),
    ("sus2",  [0, 2, 7]),
    ("sus4",  [0, 5, 7]),
    ("add9",  [0, 4, 7, 14]),
    ("M7",    [0, 4, 7, 11]),
    ("m7",    [0, 3, 7, 10]),
    ("dim",   [0, 3, 6]),
    ("aug",   [0, 4, 8]),
    ("min",   [0, 3, 7]),
    ("maj",   [0, 4, 7]),
    ("+",     [0, 4, 8]),
    ("°",     [0, 3, 6]),
    ("9",     [0, 4, 7, 10, 14]),
    ("7",     [0, 4, 7, 10]),
    ("6",     [0, 4, 7, 9]),
    ("m",     [0, 3, 7]),
    ("M",     [0, 4, 7]),
    ("",      [0, 4, 7]),   # bare root → major triad
]

_PITCH_NAME = ["C","C#","D","Eb","E","F","F#","G","Ab","A","Bb","B"]


def _semi_to_name(semi: int) -> str:
    pc = semi % 12
    octave = (semi // 12) - 1
    return f"{_PITCH_NAME[pc]}{octave}"


# ─────────────────────────────────────────────────────────────────────────────
# NOTE-text → Standard MIDI File (binary)
# Used by the /files/{id}/midi endpoint so midi entries can be dragged into
# any DAW as real .mid files.
# ─────────────────────────────────────────────────────────────────────────────

_NOTE_LETTER_SEMI = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}


def _note_name_to_midi(name: str) -> int:
    name = (name or "").strip()
    if not name:
        return 60
    letter = name[0].upper()
    rest = name[1:]
    acc = 0
    if rest[:1] == "#":
        acc, rest = 1, rest[1:]
    elif rest[:1] == "b":
        acc, rest = -1, rest[1:]
    if letter not in _NOTE_LETTER_SEMI:
        return 60
    try:
        octave = int(rest)
    except ValueError:
        octave = 4
    return _NOTE_LETTER_SEMI[letter] + acc + (octave + 1) * 12


def _vlq(n: int) -> bytes:
    """MIDI variable-length quantity encoding."""
    n = max(0, int(n))
    buf = [n & 0x7F]
    n >>= 7
    while n > 0:
        buf.append((n & 0x7F) | 0x80)
        n >>= 7
    return bytes(reversed(buf))


def notes_text_to_midi_bytes(notes_text: str, bpm: int = 120, ppqn: int = 480) -> bytes:
    """
    Convert our plain NOTE-per-line format into a standard MIDI file (SMF
    format 0). Expected line syntax:

        NOTE <pitch> <start_sec> <duration_sec>
    """
    notes = []
    for line in (notes_text or "").splitlines():
        parts = line.strip().split()
        if len(parts) >= 4 and parts[0].upper() == "NOTE":
            try:
                pitch = _note_name_to_midi(parts[1])
                start = float(parts[2])
                dur   = float(parts[3])
                notes.append((start, dur, pitch))
            except (ValueError, IndexError):
                continue

    sec_per_tick = 60.0 / (bpm * ppqn)
    events = []  # (abs_tick, bytes)
    for start, dur, pitch in notes:
        s_tick = int(round(start / sec_per_tick))
        e_tick = int(round((start + dur) / sec_per_tick))
        if e_tick <= s_tick:
            e_tick = s_tick + 1
        events.append((s_tick, bytes([0x90, pitch & 0x7F, 100])))  # note_on
        events.append((e_tick, bytes([0x80, pitch & 0x7F, 64])))   # note_off
    events.sort(key=lambda x: x[0])

    # Track body
    us_per_beat = int(60_000_000 / bpm)
    track = _vlq(0) + b"\xFF\x51\x03" + us_per_beat.to_bytes(3, "big")  # tempo meta
    prev = 0
    for abs_tick, ev in events:
        track += _vlq(abs_tick - prev) + ev
        prev = abs_tick
    track += _vlq(0) + b"\xFF\x2F\x00"  # end of track

    header = (
        b"MThd"
        + (6).to_bytes(4, "big")
        + (0).to_bytes(2, "big")       # format 0
        + (1).to_bytes(2, "big")       # 1 track
        + ppqn.to_bytes(2, "big")      # ticks per quarter
    )
    track_chunk = b"MTrk" + len(track).to_bytes(4, "big") + track
    return header + track_chunk


def _parse_chord(symbol: str, octave: int = 3) -> list[str]:
    """Parse a chord symbol → list of note names (e.g. 'Em' → ['E3','G3','B3'])."""
    s = symbol.strip()
    if not s:
        return []
    # Strip slash-chord bass for now
    if "/" in s:
        s = s.split("/", 1)[0].strip()

    # Root: letter + optional accidental
    root = s[0].upper()
    rest = s[1:]
    if rest[:1] in ("#", "b"):
        root += rest[:1]
        rest = rest[1:]
    if root not in _ROOT_SEMI:
        return []

    intervals = [0, 4, 7]  # fallback to major
    for q, iv in _QUALITY_INTERVALS:
        if rest == q:
            intervals = iv
            break
    else:
        # Unrecognized suffix — default to major triad
        pass

    root_midi = _ROOT_SEMI[root] + (octave + 1) * 12
    return [_semi_to_name(root_midi + i) for i in intervals]


def _chords_to_midi_text(chords: list[str], beat_duration: float = 1.0) -> str:
    lines = []
    t = 0.0
    for sym in chords:
        notes = _parse_chord(sym, octave=3)
        for n in notes:
            lines.append(f"NOTE {n} {t:.3f} {beat_duration:.3f}")
        t += beat_duration
    return "\n".join(lines)


def _render_chord_progression(chords: list[str], tag: str = "") -> dict:
    notes_text  = _chords_to_midi_text(chords)
    slug        = f"{tag}-chords" if tag else "chord-render"
    base_tags   = [tag] if tag else []
    tags        = base_tags + [t for t in ["midi", "chords"] if t not in base_tags]
    description = f"chord render: {' - '.join(chords)}"
    return ingest_text(slug, tags, description, midi_notes=notes_text)


# Legacy handler kept for the /jobs endpoint path (random progression)
def _render_chords(job: dict, sc: dict) -> None:
    chords = random.sample(
        [q for q, _ in _QUALITY_INTERVALS if q in ("m", "")][:2] +
        ["C", "Am", "F", "G", "Em", "Dm"], k=4
    )
    parent_tags = list(sc.get("tags", []))
    tag = parent_tags[0] if parent_tags else ""
    ev = _render_chord_progression(chords, tag=tag)
    complete_job(job["job_id"], output_file_id=ev["file_id"])


# ─────────────────────────────────────────────────────────────────────────────
# summarize
# ─────────────────────────────────────────────────────────────────────────────

def _summarize(job: dict, sc: dict) -> None:
    from services.llm import summarize_tag
    tag    = job.get("params", {}).get("tag") or ""
    if not tag and sc:
        tag = next((t for t in sc.get("tags", []) if t not in
                    {"audio","lyric","text","midi","stem","sketch"}), "")
    result = summarize_tag(tag) if tag else "no tag specified for summarize job"
    parent_tags = sc.get("tags", []) if sc else [tag]
    slug   = f"{tag}-summary"
    tags   = list(parent_tags) + [t for t in ["summary"] if t not in parent_tags]
    ev     = ingest_text(slug, tags, result,
                         parent_id=sc.get("id") if sc else None)
    complete_job(job["job_id"], output_file_id=ev["file_id"])
