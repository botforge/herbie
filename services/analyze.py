"""
Audio analysis: key detection and BPM via librosa.
Only call this on music/instrument segments, not speech.
"""

import numpy as np

try:
    import librosa
    LIBROSA_AVAILABLE = True
except ImportError:
    LIBROSA_AVAILABLE = False


# Krumhansl-Schmuckler key profiles
_MAJOR_PROFILE = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09,
                            2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
_MINOR_PROFILE = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53,
                            2.54, 4.75, 3.98, 2.69, 3.34, 3.17])

_NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F",
               "F#", "G", "G#", "A", "A#", "B"]


def _ks_key(chroma_mean: np.ndarray) -> tuple[str, float]:
    """Return (key_string, correlation) using Krumhansl-Schmuckler."""
    best_key = "unknown"
    best_corr = -np.inf

    for i in range(12):
        rotated_major = np.roll(_MAJOR_PROFILE, i)
        rotated_minor = np.roll(_MINOR_PROFILE, i)

        corr_major = np.corrcoef(chroma_mean, rotated_major)[0, 1]
        corr_minor = np.corrcoef(chroma_mean, rotated_minor)[0, 1]

        if corr_major > best_corr:
            best_corr = corr_major
            best_key = f"{_NOTE_NAMES[i]} major"
        if corr_minor > best_corr:
            best_corr = corr_minor
            best_key = f"{_NOTE_NAMES[i]} minor"

    return best_key, float(best_corr)


def analyze_audio(audio_path: str) -> dict:
    """
    Detect key and BPM for an audio file.
    Returns {"key": str, "bpm": float | None, "confidence": float}
    Only intended for music/instrument audio, not speech.
    """
    if not LIBROSA_AVAILABLE:
        return {"key": "unknown", "bpm": None, "confidence": 0.0}

    try:
        y, sr = librosa.load(audio_path, sr=None, mono=True)

        # Key detection via CQT chromagram
        chroma = librosa.feature.chroma_cqt(y=y, sr=sr)
        chroma_mean = np.mean(chroma, axis=1)
        key, confidence = _ks_key(chroma_mean)

        # BPM via beat tracking
        tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
        bpm = float(np.round(tempo, 1)) if tempo else None

        return {"key": key, "bpm": bpm, "confidence": confidence}

    except Exception as e:
        return {"key": "unknown", "bpm": None, "confidence": 0.0, "error": str(e)}


def analyze_segment(y: np.ndarray, sr: int) -> dict:
    """Same as analyze_audio but accepts a pre-loaded numpy array."""
    if not LIBROSA_AVAILABLE:
        return {"key": "unknown", "bpm": None, "confidence": 0.0}

    try:
        chroma = librosa.feature.chroma_cqt(y=y, sr=sr)
        chroma_mean = np.mean(chroma, axis=1)
        key, confidence = _ks_key(chroma_mean)

        tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
        bpm = float(np.round(tempo, 1)) if tempo else None

        return {"key": key, "bpm": bpm, "confidence": confidence}
    except Exception:
        return {"key": "unknown", "bpm": None, "confidence": 0.0}
