"""
antispam.py — lightweight, in-memory flood protection.

Why in-memory and not Firebase: this only needs to survive for a few
seconds/minutes per user and must be checked on every single incoming
message with near-zero latency. Storing every check in Firebase would be
slow (network round trip) and would burn through the free DB's request
quota. Strikes that actually matter (temp mutes, auto-blocks) ARE logged to
Firebase via firebase_helper.log_event so they survive a dyno restart in
the audit trail — only the split-second flood counters are memory-only.

Thread-safe with a simple lock since pyTelegramBotAPI's infinity_polling
processes updates from one thread by default (add a lock anyway to be safe
if you switch to threaded polling).
"""
import threading
import time
from collections import deque

import config

_lock = threading.Lock()

# chat_id -> deque[timestamps]
_history: dict = {}
# chat_id -> (last_text, count)
_dup_tracker: dict = {}
# chat_id -> last message timestamp
_last_seen: dict = {}
# chat_id -> mute_until (epoch seconds)
_muted_until: dict = {}
# chat_id -> strike count (escalates toward auto-block)
_strikes: dict = {}


def is_muted(chat_id: str):
    """Returns remaining mute seconds if muted, else 0."""
    with _lock:
        until = _muted_until.get(chat_id, 0)
        remaining = until - time.time()
        return max(0, int(remaining))


def _mute(chat_id: str, seconds: int):
    _muted_until[chat_id] = time.time() + seconds


def strikes(chat_id: str) -> int:
    with _lock:
        return _strikes.get(chat_id, 0)


def check(chat_id: str, text: str = "") -> dict:
    """Call once per incoming user message.

    Returns a dict:
      {"blocked": False}                          -> allow through normally
      {"blocked": True, "reason": "...", "warn": "user-facing text or None",
       "auto_block": bool}                        -> drop / warn the user
    """
    now = time.time()
    text = text or ""

    with _lock:
        # Already muted?
        remaining = _muted_until.get(chat_id, 0) - now
        if remaining > 0:
            return {"blocked": True, "reason": "muted", "warn": None, "auto_block": False}

        # Minimum gap between messages (prevents rapid-fire bursts)
        last = _last_seen.get(chat_id, 0)
        gap = now - last
        _last_seen[chat_id] = now

        hist = _history.setdefault(chat_id, deque())
        hist.append(now)
        window = config.ANTI_SPAM_WINDOW_SECONDS
        while hist and now - hist[0] > window:
            hist.popleft()

        flood = len(hist) > config.ANTI_SPAM_MAX_MSGS
        burst = gap < config.ANTI_SPAM_MIN_GAP_SECONDS and last != 0

        # Duplicate-message spam (same text repeated)
        dup = False
        if text:
            prev_text, prev_count = _dup_tracker.get(chat_id, ("", 0))
            if text == prev_text:
                prev_count += 1
            else:
                prev_count = 1
            _dup_tracker[chat_id] = (text, prev_count)
            dup = prev_count >= config.ANTI_SPAM_DUPLICATE_LIMIT

        if flood or dup or burst:
            strike = _strikes.get(chat_id, 0) + 1
            _strikes[chat_id] = strike
            hist.clear()

            if strike >= config.ANTI_SPAM_MAX_STRIKES:
                return {
                    "blocked": True,
                    "reason": "flood" if flood else ("duplicate" if dup else "burst"),
                    "warn": "🚫 You have been temporarily blocked for spamming. Contact us again later.",
                    "auto_block": True,
                }

            _mute(chat_id, config.ANTI_SPAM_MUTE_SECONDS)
            return {
                "blocked": True,
                "reason": "flood" if flood else ("duplicate" if dup else "burst"),
                "warn": (
                    f"⏳ You're sending messages too fast. Please wait "
                    f"{config.ANTI_SPAM_MUTE_SECONDS}s before trying again."
                ),
                "auto_block": False,
            }

        return {"blocked": False}


def reset(chat_id: str):
    """Clear all counters for a chat (e.g. after an admin manually unblocks)."""
    with _lock:
        _history.pop(chat_id, None)
        _dup_tracker.pop(chat_id, None)
        _last_seen.pop(chat_id, None)
        _muted_until.pop(chat_id, None)
        _strikes.pop(chat_id, None)
