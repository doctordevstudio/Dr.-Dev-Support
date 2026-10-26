"""
config.py — all configuration comes from environment variables.
"""
import os
from datetime import timezone, timedelta


def _clean(v: str) -> str:
    return (v or "").strip()


def _bool(name: str, default: bool) -> bool:
    raw = _clean(os.environ.get(name, ""))
    if not raw:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


# ── Timezone (IST) ────────────────────────────────────────────────────────────
IST = timezone(timedelta(hours=5, minutes=30))


# ── Telegram ──────────────────────────────────────────────────────────────────
BOT_TOKEN = _clean(os.environ.get("BOT_TOKEN", ""))


def _ids(name: str) -> list:
    raw = _clean(os.environ.get(name, ""))
    return [x.strip() for x in raw.split(",") if x.strip()]


def SUPER_ADMIN_CHAT_IDS() -> list:
    """Telegram chat id(s) of the SUPER admin (env: SUPER_ADMIN_CHAT_ID)."""
    return _ids("SUPER_ADMIN_CHAT_ID")


def ADMIN_ONLY_CHAT_IDS() -> list:
    """Telegram chat id(s) of the normal ADMIN (env: ADMIN_CHAT_ID)."""
    sup = set(SUPER_ADMIN_CHAT_IDS())
    return [x for x in _ids("ADMIN_CHAT_ID") if x not in sup]


def ADMIN_CHAT_IDS() -> list:
    """Every chat that gets notifications and may use the bot's admin commands."""
    out = []
    for x in SUPER_ADMIN_CHAT_IDS() + ADMIN_ONLY_CHAT_IDS():
        if x not in out:
            out.append(x)
    return out


def role_of_chat(chat_or_user_id) -> str:
    """'super' | 'admin' | '' (not an admin). Super wins if an id is in both lists."""
    cid = str(chat_or_user_id) if chat_or_user_id is not None else ""
    if not cid:
        return ""
    if cid in SUPER_ADMIN_CHAT_IDS():
        return "super"
    if cid in ADMIN_ONLY_CHAT_IDS():
        return "admin"
    return ""


# ── Firebase ──────────────────────────────────────────────────────────────────
FIREBASE_URL = _clean(os.environ.get("FIREBASE_URL", ""))
FIREBASE_SECRET = _clean(os.environ.get("FIREBASE_SECRET", ""))     # legacy, optional
# Firebase ADMIN access (service account). With database rules ".read": false /
# ".write": false only this server can read or write. Give ONE of:
#   FIREBASE_CREDENTIALS       the service-account JSON (raw or base64)
#   FIREBASE_CREDENTIALS_FILE  path to that JSON file
FIREBASE_CREDENTIALS = _clean(os.environ.get("FIREBASE_CREDENTIALS", ""))
FIREBASE_CREDENTIALS_FILE = _clean(os.environ.get("FIREBASE_CREDENTIALS_FILE", "")) or \
    _clean(os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", ""))

# ── Personal Telegram account (Telethon / MTProto) ───────────────────────────
# Create at https://my.telegram.org -> API development tools
TG_API_ID = int(_clean(os.environ.get("TG_API_ID", "") or os.environ.get("API_ID", "")) or 0)
TG_API_HASH = _clean(os.environ.get("TG_API_HASH", "") or os.environ.get("API_HASH", ""))

# ── AI support (Groq) ────────────────────────────────────────────────────────
GROQ_MODELS = [
    m.strip() for m in os.environ.get(
        "GROQ_MODELS", "openai/gpt-oss-120b,openai/gpt-oss-20b"
    ).split(",") if m.strip()
]

# ── Admin panel auth (ONLY from environment variables) ───────────────────────
# Two accounts: the SUPER admin sees everything, the ADMIN gets a limited view.
# There are deliberately no built-in defaults — a role without both a username
# and a password set here simply cannot log in.
SUPER_ADMIN_USERNAME = _clean(os.environ.get("SUPER_ADMIN_USERNAME", ""))
SUPER_ADMIN_PASSWORD = _clean(os.environ.get("SUPER_ADMIN_PASSWORD", ""))
ADMIN_USERNAME = _clean(os.environ.get("ADMIN_USERNAME", ""))
ADMIN_PASSWORD = _clean(os.environ.get("ADMIN_PASSWORD", ""))

# ── Flask ─────────────────────────────────────────────────────────────────────
SECRET_KEY = _clean(os.environ.get("SECRET_KEY", "")) or os.urandom(24).hex()
PORT = int(os.environ.get("PORT", 5000))
PANEL_NAME = _clean(os.environ.get("PANEL_NAME", "Support Panel"))
PANEL_URL = _clean(os.environ.get("PANEL_URL", ""))

# ── Greetings (defaults; runtime value lives in Firebase /settings/welcome) ──
DEFAULT_WELCOME_MESSAGE = os.environ.get(
    "WELCOME_MESSAGE",
    "👋 <b>Welcome to Support!</b>\n"
    "Please describe your issue and we'll get back to you shortly.\n\n"
    "📸 You can also send photos, videos or files.",
)
AUTO_REPLY = os.environ.get(
    "AUTO_REPLY",
    "✅ Message received! Admin will answer you soon.",
)
AUTO_REPLY_COOLDOWN_MINUTES = int(os.environ.get("AUTO_REPLY_COOLDOWN_MINUTES", 30))

# ── Notifications ────────────────────────────────────────────────────────────
# Ping the admin chat(s) when a user reacts to a message (set to 0 to disable)
REACTION_NOTIFY = _bool("REACTION_NOTIFY", True)

# ── Anti-spam ────────────────────────────────────────────────────────────────
ANTI_SPAM_MAX_MSGS = int(os.environ.get("ANTI_SPAM_MAX_MSGS", 6))
ANTI_SPAM_WINDOW_SECONDS = int(os.environ.get("ANTI_SPAM_WINDOW_SECONDS", 10))
ANTI_SPAM_MIN_GAP_SECONDS = float(os.environ.get("ANTI_SPAM_MIN_GAP_SECONDS", 0.6))
ANTI_SPAM_DUPLICATE_LIMIT = int(os.environ.get("ANTI_SPAM_DUPLICATE_LIMIT", 3))
ANTI_SPAM_MUTE_SECONDS = int(os.environ.get("ANTI_SPAM_MUTE_SECONDS", 60))
ANTI_SPAM_MAX_STRIKES = int(os.environ.get("ANTI_SPAM_MAX_STRIKES", 4))

# ── Login brute force ────────────────────────────────────────────────────────
LOGIN_MAX_ATTEMPTS = int(os.environ.get("LOGIN_MAX_ATTEMPTS", 5))
LOGIN_LOCKOUT_SECONDS = int(os.environ.get("LOGIN_LOCKOUT_SECONDS", 300))

# ── Uploads ──────────────────────────────────────────────────────────────────
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", 49))

# ── Pagination ───────────────────────────────────────────────────────────────
CHATS_PER_PAGE = int(os.environ.get("CHATS_PER_PAGE", 20))
MESSAGES_PER_PAGE = int(os.environ.get("MESSAGES_PER_PAGE", 30))

# ── Broadcast ────────────────────────────────────────────────────────────────
# Delay between two Telegram sends (seconds). 0.05 ≈ 20 msgs/sec.
BROADCAST_DELAY = float(os.environ.get("BROADCAST_DELAY", 0.05))
BROADCAST_HISTORY_LIMIT = int(os.environ.get("BROADCAST_HISTORY_LIMIT", 100))
