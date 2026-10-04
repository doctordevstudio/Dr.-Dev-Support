"""
settings.py — runtime settings editable from the admin panel.

Stored in Firebase:
    settings/site   -> everything below in DEFAULTS (safe to show in the UI)
    settings/auth   -> {username, password_hash, changed_ts}  (never sent to the browser)

Environment variables still provide the first-run defaults; once a value is
saved from the Settings page, the saved value wins. Values are cached for a
few seconds so every request/handler can call `load()` cheaply.
"""
import re
import threading
import time

from werkzeug.security import check_password_hash, generate_password_hash

import config
import firebase_helper as fb

_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")

DEFAULTS = {
    # ── General ──
    "panel_name": config.PANEL_NAME,
    "site_title": "",                       # browser tab title; empty -> panel name
    "tagline": "Support Console",
    "logo_emoji": "🎧",
    # ── Appearance ──
    "theme": "dark",                        # dark | light | auto
    "accent": "#7c6cff",
    "accent2": "#ff5c9c",
    "animations": True,
    "notify_sound": True,
    # ── Panel ──
    "chats_per_page": config.CHATS_PER_PAGE,
    "messages_per_page": config.MESSAGES_PER_PAGE,
    # ── Bot behaviour ──
    "auto_reply": config.AUTO_REPLY,
    "auto_reply_cooldown": config.AUTO_REPLY_COOLDOWN_MINUTES,
    "notify_new_message": True,             # ping admin chat on every new user message
    "forward_media": True,                  # also send user's photo/video/file to admin chat
    "notify_reaction": config.REACTION_NOTIFY,
    "notify_edit": True,
    "broadcast_confirm": True,              # /broadcast in Telegram asks "Send? Yes/No"
    "broadcast_delay": config.BROADCAST_DELAY,
    # ── Anti-spam ──
    "spam_max_msgs": config.ANTI_SPAM_MAX_MSGS,
    "spam_window": config.ANTI_SPAM_WINDOW_SECONDS,
    "spam_min_gap": config.ANTI_SPAM_MIN_GAP_SECONDS,
    "spam_dup_limit": config.ANTI_SPAM_DUPLICATE_LIMIT,
    "spam_mute": config.ANTI_SPAM_MUTE_SECONDS,
    "spam_max_strikes": config.ANTI_SPAM_MAX_STRIKES,
}

PRESETS = {
    "Violet": ("#7c6cff", "#ff5c9c"),
    "Ocean": ("#2f9bff", "#3dd6d0"),
    "Emerald": ("#22c55e", "#3dd6d0"),
    "Sunset": ("#ff7a45", "#ff5c9c"),
    "Rose": ("#ec4899", "#a855f7"),
    "Gold": ("#f5a623", "#ef4444"),
}

# key -> (type, min, max)
_NUMS = {
    "chats_per_page": (int, 5, 100),
    "messages_per_page": (int, 10, 100),
    "auto_reply_cooldown": (int, 0, 1440),
    "broadcast_delay": (float, 0.03, 2.0),
    "spam_max_msgs": (int, 2, 60),
    "spam_window": (int, 2, 120),
    "spam_min_gap": (float, 0.0, 10.0),
    "spam_dup_limit": (int, 2, 20),
    "spam_mute": (int, 5, 3600),
    "spam_max_strikes": (int, 1, 20),
}
_BOOLS = {"animations", "notify_sound", "notify_new_message", "forward_media",
          "notify_reaction", "notify_edit", "broadcast_confirm"}
_STRS = {"panel_name": 40, "site_title": 70, "tagline": 40, "logo_emoji": 8, "auto_reply": 500}

_lock = threading.Lock()
_cache = {"data": None, "ts": 0.0, "auth": None, "auth_ts": 0.0}
TTL = 5.0


def _apply(s: dict):
    """Push values into `config` so antispam / bot code picks them up."""
    config.PANEL_NAME = s["panel_name"]
    config.CHATS_PER_PAGE = s["chats_per_page"]
    config.MESSAGES_PER_PAGE = s["messages_per_page"]
    config.AUTO_REPLY = s["auto_reply"]
    config.AUTO_REPLY_COOLDOWN_MINUTES = s["auto_reply_cooldown"]
    config.REACTION_NOTIFY = s["notify_reaction"]
    config.BROADCAST_DELAY = s["broadcast_delay"]
    config.ANTI_SPAM_MAX_MSGS = s["spam_max_msgs"]
    config.ANTI_SPAM_WINDOW_SECONDS = s["spam_window"]
    config.ANTI_SPAM_MIN_GAP_SECONDS = s["spam_min_gap"]
    config.ANTI_SPAM_DUPLICATE_LIMIT = s["spam_dup_limit"]
    config.ANTI_SPAM_MUTE_SECONDS = s["spam_mute"]
    config.ANTI_SPAM_MAX_STRIKES = s["spam_max_strikes"]


def _clean(raw: dict) -> dict:
    """Merge saved values over DEFAULTS and coerce every value to a safe type."""
    out = dict(DEFAULTS)
    raw = raw if isinstance(raw, dict) else {}
    for k, default in DEFAULTS.items():
        if k not in raw:
            continue
        v = raw[k]
        try:
            if k in _BOOLS:
                out[k] = bool(v) if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")
            elif k in _NUMS:
                typ, lo, hi = _NUMS[k]
                out[k] = max(lo, min(hi, typ(v)))
            elif k in _STRS:
                out[k] = str(v).strip()[: _STRS[k]]
            elif k in ("accent", "accent2"):
                out[k] = v if isinstance(v, str) and _HEX.match(v) else default
            elif k == "theme":
                out[k] = v if v in ("dark", "light", "auto") else "dark"
        except (TypeError, ValueError):
            out[k] = default
    if not out["panel_name"]:
        out["panel_name"] = DEFAULTS["panel_name"] or "Support Panel"
    return out


def load(force: bool = False) -> dict:
    now = time.time()
    with _lock:
        if not force and _cache["data"] is not None and now - _cache["ts"] < TTL:
            return _cache["data"]
    s = _clean(fb.get("settings/site"))
    _apply(s)
    with _lock:
        _cache["data"], _cache["ts"] = s, time.time()
    return s


def validate_update(updates: dict) -> dict:
    """Only known keys, coerced. Raises ValueError with a readable message."""
    if not isinstance(updates, dict):
        raise ValueError("Invalid data")
    clean = _clean({k: v for k, v in updates.items() if k in DEFAULTS})
    result = {}
    for k in updates:
        if k not in DEFAULTS:
            continue
        if k in ("accent", "accent2") and not (isinstance(updates[k], str) and _HEX.match(updates[k])):
            raise ValueError("Colours must look like #7c6cff")
        if k == "panel_name" and not str(updates[k]).strip():
            raise ValueError("Website name cannot be empty")
        if k == "theme" and updates[k] not in ("dark", "light", "auto"):
            raise ValueError("Theme must be dark, light or auto")
        result[k] = clean[k]
    return result


def save(updates: dict) -> dict:
    result = validate_update(updates)
    if result and fb.patch("settings/site", result) is None:
        raise ValueError("Could not save to Firebase")
    with _lock:
        _cache["ts"] = 0.0
    fb.log_event("settings_updated", keys=",".join(sorted(result)))
    return load(force=True)


def css_vars(s: dict) -> str:
    def rgb(h):
        return ",".join(str(int(h[i:i + 2], 16)) for i in (1, 3, 5))
    a, b = s["accent"], s["accent2"]
    return (f":root[data-theme]{{--accent:{a};--accent2:{b};--accent-rgb:{rgb(a)};--accent2-rgb:{rgb(b)};"
            f"--grad:linear-gradient(135deg,{a} 0%,{b} 100%)}}")


# ── Admin credentials ─────────────────────────────────────────────────────────
def _auth_record() -> dict:
    now = time.time()
    with _lock:
        if _cache["auth"] is not None and now - _cache["auth_ts"] < TTL:
            return _cache["auth"]
    rec = fb.get("settings/auth")
    rec = rec if isinstance(rec, dict) else {}
    with _lock:
        _cache["auth"], _cache["auth_ts"] = rec, time.time()
    return rec


def admin_username() -> str:
    return _auth_record().get("username") or config.ADMIN_USERNAME


def using_default_password() -> bool:
    rec = _auth_record()
    return not rec.get("password_hash") and config.ADMIN_PASSWORD == "admin123"


def check_credentials(user: str, password: str) -> bool:
    import secrets
    rec = fb.get("settings/auth")                    # always fresh at login time
    rec = rec if isinstance(rec, dict) else {}
    with _lock:
        _cache["auth"], _cache["auth_ts"] = rec, time.time()
    name = rec.get("username") or config.ADMIN_USERNAME
    user_ok = secrets.compare_digest(user.encode(), name.encode())
    if rec.get("password_hash"):
        pass_ok = check_password_hash(rec["password_hash"], password)
    else:
        pass_ok = secrets.compare_digest(password.encode(), config.ADMIN_PASSWORD.encode())
    return user_ok and pass_ok


def auth_version() -> int:
    """Changes whenever the password/username changes -> old sessions are logged out."""
    return int(_auth_record().get("changed_ts") or 0)


def change_credentials(current_password: str, new_username: str, new_password: str) -> int:
    if not check_credentials(admin_username(), current_password):
        raise ValueError("Current password is wrong")
    old_username = admin_username()
    new_username = (new_username or "").strip() or old_username
    if len(new_username) < 3 or len(new_username) > 40:
        raise ValueError("Username must be 3–40 characters")
    rec = {"username": new_username, "changed_ts": int(time.time())}
    existing = fb.get("settings/auth")
    existing = existing if isinstance(existing, dict) else {}
    if new_password:
        if len(new_password) < 8:
            raise ValueError("New password must be at least 8 characters")
        rec["password_hash"] = generate_password_hash(new_password)
    else:
        # username-only change keeps whatever password is in force
        if existing.get("password_hash"):
            rec["password_hash"] = existing["password_hash"]
        else:
            rec["password_hash"] = generate_password_hash(config.ADMIN_PASSWORD)
    if fb.put("settings/auth", rec) is None:
        raise ValueError("Could not save to Firebase")
    with _lock:
        _cache["auth"], _cache["auth_ts"] = rec, time.time()
    fb.log_event("admin_credentials_changed", username_changed=new_username != old_username)
    return rec["changed_ts"]
