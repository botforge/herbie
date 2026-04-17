"""
Audio transcription via faster-whisper.

Pipeline: audio file → Whisper → transcript + speech context.

VAD (silero-vad) was removed — it existed to isolate music segments
for librosa key/BPM detection. Now that librosa is v2, Whisper handles
the full file directly. Re-add VAD when re-integrating librosa.
"""

from pathlib import Path

try:
    from faster_whisper import WhisperModel
    WHISPER_AVAILABLE = True
except ImportError:
    WHISPER_AVAILABLE = False

_whisper_model = None


def _get_whisper() -> "WhisperModel | None":
    global _whisper_model
    if _whisper_model is None and WHISPER_AVAILABLE:
        _whisper_model = WhisperModel("base", device="cpu", compute_type="int8")
    return _whisper_model


def process_audio(audio_path: str) -> dict:
    """
    Transcribe an audio file. Returns the same dict shape as before
    so callers (main.py, cli.py, telegram_bot.py) need no changes.

    The spoken preamble ("this is hospital, op-1 pad...") and any
    sung/played content all go through Whisper together. The full
    transcript is used as filing context for the LLM.

    v2 hook: when re-adding librosa, run it on the full file here
    and add "key" and "bpm" back to the return dict.
    """
    transcript = transcribe_file(audio_path)
    return {
        "speech_transcript": transcript,
        "speech_context": transcript,
        "has_music": True,   # assume music present; librosa will confirm in v2
        "has_speech": bool(transcript),
        "music_duration_sec": 0.0,  # unknown without VAD
        "raw_segments": [],
    }


def transcribe_file(audio_path: str) -> str:
    """Transcribe a file, return plain text."""
    model = _get_whisper()
    if model is None:
        return ""
    try:
        segments, _ = model.transcribe(audio_path, beam_size=5)
        return " ".join(seg.text.strip() for seg in segments).strip()
    except Exception:
        return ""
