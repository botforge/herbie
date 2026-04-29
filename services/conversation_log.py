"""
Append-only conversation log for eval set construction.

1. Every turn — text or audio, from any transport — is written as one
   JSON line to logs/conversations.jsonl. Fields: turn_id, ts,
   transport, input_type, input, transcript, llm_message, tool_calls,
   reply, eval_candidate.
2. Messages prefixed with 3+ threes (e.g. "3333333 what should happen")
   are marked eval_candidate=true and the prefix is stripped before the
   message reaches the LLM, so the bot behaves normally but the log
   records that this turn was nominated for the eval set.
3. tool_calls is a list of {name, args, result} dicts captured during
   the LLM tool loop — the full trace needed to build golden examples.
"""

import json
import re
import uuid
from datetime import datetime
from pathlib import Path

_LOG_PATH = Path(__file__).parent.parent / "logs" / "conversations.jsonl"

# Any run of 3+ threes at the start of a message, optionally followed by whitespace
_FLAG_RE = re.compile(r"^3{3,}\s*")


def detect_flag(text: str) -> tuple[bool, str]:
    """
    1. Check whether text starts with the eval-flag prefix (3+ threes).
    2A. If flagged: return (True, text_with_prefix_stripped).
    2B. If not flagged: return (False, text_unchanged).
    """
    m = _FLAG_RE.match(text)
    if m:
        return True, text[m.end():]
    return False, text


def log_turn(
    *,
    transport: str,
    input_type: str,
    input_text: str,
    llm_message: str,
    reply: str,
    tool_calls: list[dict] | None = None,
    transcript: str = "",
    eval_candidate: bool = False,
) -> None:
    """
    1. Ensure the logs/ directory exists.
    2. Build the entry dict with a short UUID turn_id and ISO timestamp.
    3. Append as a single JSON line — never overwrites, always grows.
    """
    _LOG_PATH.parent.mkdir(exist_ok=True)
    entry = {
        "turn_id":       str(uuid.uuid4())[:8],
        "ts":            datetime.now().isoformat(),
        "transport":     transport,
        "input_type":    input_type,
        "input":         input_text,
        "transcript":    transcript,
        "llm_message":   llm_message,
        "tool_calls":    tool_calls or [],
        "reply":         reply,
        "eval_candidate": eval_candidate,
    }
    with _LOG_PATH.open("a") as f:
        f.write(json.dumps(entry) + "\n")
