"""
config.py — all configuration comes from environment variables.
"""
import os
from datetime import timezone, timedelta


def _clean(v: str) -> str:
    return (v or "").strip()


# ── Timezone (IST) ────────────────────────────────────────────────────────────
IST = timezone(timedelta(hours=5, minutes=30))


# ── Telegram ──────────────────────────────────────────────────────────────────
BOT_TOKEN = _clean(os.environ.get("BOT_TOKEN", ""))


def ADMIN_CHAT_IDS() -> list:
    raw = _clean(os.environ.get("ADMIN_CHAT_ID", ""))
    return [x.strip() for x in raw.split(",") if x.strip()]


# ── Firebase ──────────────────────────────────────────────────────────────────
FIREBASE_URL = _clean(os.environ.get("FIREBASE_URL", ""))
FIREBASE_SECRET = _clean(os.environ.get("FIREBASE_SECRET", ""))

# ── Admin panel auth ──────────────────────────────────────────────────────────
ADMIN_USERNAME = _clean(os.environ.get("ADMIN_USERNAME", "admin"))
ADMIN_PASSWORD = _clean(os.environ.get("ADMIN_PASSWORD", "admin123"))

# ── Flask ─────────────────────────────────────────────────────────────────────
SECRET_KEY = _clean(os.environ.get("SECRET_KEY", "")) or os.urandom(24).hex()
PORT = int(os.environ.get("PORT", 5000))
PANEL_NAME = _clean(os.environ.get("PANEL_NAME", "Support Panel"))
PANEL_URL = _clean(os.environ.get("PANEL_URL", ""))

# ── Greetings ────────────────────────────────────────────────────────────────
WELCOME_MESSAGE = os.environ.get(
    "WELCOME_MESSAGE",
    "👋 *Welcome to Support!*\n"
    "Please describe your issue and we'll get back to you shortly.\n\n"
    "📸 You can also send photos, videos or files.",
)
AUTO_REPLY = os.environ.get(
    "AUTO_REPLY",
    "✅ Message received! Admin will answer you soon.",
)
AUTO_REPLY_COOLDOWN_MINUTES = int(os.environ.get("AUTO_REPLY_COOLDOWN_MINUTES", 30))

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
# 49 MB per project requirement (bot send limit is 50 MB, keep headroom).
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", 49))

# ── Pagination ───────────────────────────────────────────────────────────────
CHATS_PER_PAGE = int(os.environ.get("CHATS_PER_PAGE", 20))
MESSAGES_PER_PAGE = int(os.environ.get("MESSAGES_PER_PAGE", 30))
