"""Show first, then ask: a bot that made a picture for a choice posts it before its question card goes up.

Owner 2026-11-18: "I NEVER get images of what the bot is asking me to approve". A bot drew a mock-up, looked at it
itself with vision_analyze, then called clarify. The question card has no room for a picture, and the bot had never
written the ``MEDIA:`` line that puts it in the chat, so the person was asked to pick between pictures they hadn't
seen. Over the three weeks before, 35 of 40 such questions went out that way.

One rule, held from the plugin's pre_tool_call (``check``):

- A picture counts when the bot opens a local image file with ``vision_analyze`` that it made itself: written in
  the last ``FRESH_SECONDS``, and not something the person sent (their attachments and the image caches).
- It counts as shown once the bot's own words in this turn carry ``MEDIA:`` or ``::preview`` (``note_text``, from
  post_api_request and on_interim_message), or a ``desktop_preview`` call opens it.
- A ``clarify`` while a counted picture is unshown is held once, with a message telling the bot to post the picture
  and ask again. The next ``clarify`` in the turn goes through either way, so a bot that already showed it some way
  this can't see is never stuck.
- ``note_turn`` (pre_llm_call) starts each turn clean.

Nothing here logs a path's contents; never raises into the hook.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

FRESH_SECONDS = 45 * 60
MAX_PATHS = 3
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".tiff", ".bmp"}
# Pictures the person sent or a cache made, not something the bot drew for them.
_THEIRS = (".dispatch-attachments", "/attachments/", "/image_cache/", "/images/", "/audio_cache/", "/composer-pastes/")
_SHOWN_MARKS = ("MEDIA:", "::preview")

_lock = threading.Lock()
_turns: dict[str, dict] = {}


def _state(session_id: str) -> dict:
    s = _turns.get(session_id)
    if s is None:
        s = _turns[session_id] = {"unseen": [], "held": False}
        if len(_turns) > 500:
            for key in list(_turns)[:250]:
                _turns.pop(key, None)
    return s


def _local_path(value) -> Path | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.startswith("file://"):
        text = text[len("file://"):]
    if not text or "://" in text or text.startswith("data:"):
        return None
    path = Path(text).expanduser()
    return path if path.is_absolute() else None


def made_by_bot(value, now: float | None = None) -> str | None:
    """The path when ``value`` names a fresh local image the bot made, else None."""
    path = _local_path(value)
    if path is None or path.suffix.lower() not in _IMAGE_SUFFIXES:
        return None
    text = str(path)
    if any(mark in text for mark in _THEIRS):
        return None
    try:
        age = (now or time.time()) - path.stat().st_mtime
    except OSError:
        return None
    return text if 0 <= age <= FRESH_SECONDS else None


def note_turn(session_id) -> None:
    """A new turn: nothing made in it yet."""
    with _lock:
        _turns.pop(str(session_id or ""), None)


def note_text(session_id, text) -> None:
    """The bot's own words in this turn: a MEDIA line or a preview shows what it made."""
    if not isinstance(text, str) or not any(mark in text for mark in _SHOWN_MARKS):
        return
    with _lock:
        _state(str(session_id or ""))["unseen"].clear()


def check(tool_name, args, session_id) -> dict | None:
    """The plugin's pre_tool_call: note pictures made, hold one clarify while any is unshown."""
    sid = str(session_id or "")
    args = args if isinstance(args, dict) else {}
    with _lock:
        if tool_name == "vision_analyze":
            path = made_by_bot(args.get("image_url"))
            if path:
                unseen = _state(sid)["unseen"]
                if path in unseen:
                    unseen.remove(path)
                unseen.append(path)
            return None
        if tool_name == "desktop_preview":
            _state(sid)["unseen"].clear()
            return None
        if tool_name != "clarify":
            return None
        s = _state(sid)
        if not s["unseen"] or s["held"]:
            s["unseen"].clear()
            return None
        s["held"] = True
        paths = list(reversed(s["unseen"]))[:MAX_PATHS]
        s["unseen"].clear()
    lines = "\n".join(f"MEDIA:{p}" for p in paths)
    return {"action": "block", "message": (
        "Question not shown yet. You made a picture for this but the person hasn't seen it: the question card "
        "can't show pictures, and nothing you've written this turn posts it. Write a short message with the "
        "picture first (one line per picture, exactly as below, dropping any that don't belong to this choice), "
        "then call clarify again.\n" + lines)}
