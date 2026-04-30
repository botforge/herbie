"""
Reply rendering — turns an LLM reply string into a list of typed
segments so each transport can render text and embedded audio
markers in whatever shape it speaks natively (Telegram message +
document, web JSON + frontend marker, CLI line + filename).

The LLM may emit `[[audio:<file_id>]]` markers anywhere in a reply.
parse_reply walks the string, resolves each marker against the
archive, and returns segments in original order.
"""

import re
from pathlib import Path
from typing import TypedDict

from services import archive
from services.archive import current_entry

_AUDIO_MARKER_RE = re.compile(r"\[\[audio:([a-fA-F0-9]{8})\]\]")


class TextSegment(TypedDict):
    kind: str       # "text"
    text: str


class AudioSegment(TypedDict):
    kind:     str   # "audio"
    file_id:  str
    path:     Path
    filename: str   # "<slug>.<ext>" — drag-friendly name


class AudioMissSegment(TypedDict):
    kind:    str    # "audio_miss"
    file_id: str
    reason:  str    # "no_entry" | "file_missing"


Segment = TextSegment | AudioSegment | AudioMissSegment


def parse_reply(text: str) -> list[Segment]:
    """
    1. Walk the reply string. Between every two adjacent
       [[audio:<8hex>]] markers, emit a text segment carrying the
       prose (whitespace preserved — the consumer decides whether
       to strip).
    2. For each marker, resolve the file_id against the archive:
       2A. If no live entry exists → emit an audio_miss segment with
           reason="no_entry" so the consumer can tell the user the
           reference is dead.
       2B. If the entry exists but the raw file is gone → emit an
           audio_miss segment with reason="file_missing".
       2C. If both exist → emit an audio segment with the resolved
           on-disk path and a slug-derived filename for drag-friendly
           display in clients that support attachments.
    3. Emit a final text segment for any prose after the last marker.
    4. Return the segments in order; an empty input returns [].
    """
    segments: list[Segment] = []
    last = 0
    for m in _AUDIO_MARKER_RE.finditer(text):
        pre = text[last:m.start()]
        if pre:
            segments.append({"kind": "text", "text": pre})

        fid = m.group(1).lower()
        sc  = current_entry(fid)
        if not sc:
            segments.append({"kind": "audio_miss", "file_id": fid, "reason": "no_entry"})
        else:
            ext      = "." + (sc.get("ext") or "ogg").lstrip(".")
            slug     = sc.get("slug") or fid
            path     = archive.RAW_DIR / f"{fid}{ext}"
            filename = f"{slug}{ext}"
            if not path.exists():
                segments.append({
                    "kind":    "audio_miss",
                    "file_id": fid,
                    "reason":  "file_missing",
                })
            else:
                segments.append({
                    "kind":     "audio",
                    "file_id":  fid,
                    "path":     path,
                    "filename": filename,
                })
        last = m.end()

    tail = text[last:]
    if tail:
        segments.append({"kind": "text", "text": tail})

    return segments
