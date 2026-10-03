"""
telegram_bot.py — customer support Telegram bot.

Now uses HTML parse mode everywhere (safer for user content — `_`, `*`,
`[` etc. all pass through unchanged because we HTML-escape any untrusted
text). All user-facing strings are HTML-escaped before being sent.
"""
import datetime
import html as html_lib
import io
import time

import telebot
from telebot import types

import antispam
import config
import firebase_helper as fb

bot = telebot.TeleBot(config.BOT_TOKEN, parse_mode="HTML") if config.BOT_TOKEN else None

BLOCKED_MSG = "🚫 You have been blocked from contacting support."

REACTIONS = ["👍", "❤️", "😂", "😮", "😢", "🔥"]


def now_str():
    return datetime.datetime.now(config.IST).strftime("%Y-%m-%d %H:%M:%S")


def now_ts():
    return int(time.time())


def h(s):
    """HTML-escape untrusted text so `<`, `>`, `&` don't break Telegram's
    HTML parser. Everything we send goes through this."""
    return html_lib.escape(s or "", quote=False)


def _display_name(user) -> str:
    fn = getattr(user, "first_name", "") or ""
    ln = getattr(user, "last_name", "") or ""
    return f"{fn} {ln}".strip() or "Friend"


def _is_blocked(cid: str) -> bool:
    return bool(fb.get(f"support/{cid}/meta/blocked"))


def get_welcome_message() -> str:
    """Runtime welcome message from Firebase, falling back to env default."""
    try:
        v = fb.get("settings/welcome/message")
        if v and isinstance(v, str) and v.strip():
            return v
    except Exception:
        pass
    return config.DEFAULT_WELCOME_MESSAGE


def get_welcome_format() -> str:
    """'html' or 'text' — controls how the welcome is sent."""
    try:
        v = fb.get("settings/welcome/format")
        if v in ("html", "text"):
            return v
    except Exception:
        pass
    return "html"


def _get_file_url(file_id: str) -> str:
    try:
        info = bot.get_file(file_id)
        return f"https://api.telegram.org/file/bot{config.BOT_TOKEN}/{info.file_path}"
    except Exception as e:
        print(f"[FILE] {file_id} -> {e}")
        return ""


def refresh_file_url(file_id: str) -> str:
    if not bot or not file_id:
        return ""
    return _get_file_url(file_id)


def get_user_photo_file_id(user_id: int) -> str:
    """Fetch just the file_id (never the URL — URLs expire; file_ids are
    stable for as long as Telegram keeps the file)."""
    try:
        photos = bot.get_user_profile_photos(user_id, limit=1)
        if photos and photos.photos:
            return photos.photos[0][-1].file_id
    except Exception as e:
        print(f"[PHOTO-ID] {e}")
    return ""


def send_admin_notify(chat_id: str, user_name: str, username: str, text: str, msg_type: str = "text"):
    """Notify every configured admin chat id about a new incoming message.

    Always logs the outcome so failures are visible in the Activity Log
    instead of silently swallowed. Falls back to plain text (no parse mode)
    if HTML parsing fails — so the admin always sees *something*.
    """
    ids = config.ADMIN_CHAT_IDS()
    if not ids:
        fb.log_event("admin_notify_failed", chat_id=chat_id,
                     error="ADMIN_CHAT_ID not configured (empty)")
        print("[NOTIFY] ADMIN_CHAT_ID env var is empty — nothing to send to.")
        return
    if not bot:
        fb.log_event("admin_notify_failed", chat_id=chat_id,
                     error="Bot not initialized (BOT_TOKEN missing)")
        print("[NOTIFY] bot is None — BOT_TOKEN missing.")
        return

    icon = {"photo": "🖼️", "video": "🎬", "document": "📄"}.get(msg_type, "💬")
    preview_raw = text or f"[{msg_type}]"
    preview = (preview_raw[:250] + "…") if len(preview_raw) > 250 else preview_raw

    # Prefer PANEL_URL, fall back to nothing (button hidden if absent)
    chat_link = ""
    if config.PANEL_URL:
        chat_link = f"{config.PANEL_URL.rstrip('/')}/chat/{chat_id}"

    # HTML version (preferred)
    html_text = (
        "📩 <b>New Support Message</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>Name:</b> {h(user_name)}\n"
        f"🔗 <b>Username:</b> {'@' + h(username) if username else '—'}\n"
        f"🆔 <b>Chat ID:</b> <code>{h(str(chat_id))}</code>\n"
        f"{icon} <b>Message:</b> {h(preview)}\n"
        f"🕐 <b>Time (IST):</b> {now_str()}"
    )

    # Plain-text fallback (no parse mode at all — guaranteed to send)
    plain_text = (
        "📩 New Support Message\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 Name: {user_name}\n"
        f"🔗 Username: {'@' + username if username else '—'}\n"
        f"🆔 Chat ID: {chat_id}\n"
        f"{icon} Message: {preview}\n"
        f"🕐 Time (IST): {now_str()}"
    )

    mk = None
    if chat_link:
        mk = types.InlineKeyboardMarkup()
        mk.add(types.InlineKeyboardButton("🖥️ Open in Admin Panel", url=chat_link))

    for admin_id in ids:
        sent = False
        last_err = ""
        # 1st try: HTML with reply_markup
        try:
            bot.send_message(admin_id, html_text, parse_mode="HTML", reply_markup=mk)
            sent = True
        except Exception as e:
            last_err = f"html: {e}"
            print(f"[NOTIFY] {admin_id} HTML failed: {e}")
            # 2nd try: HTML without markup (sometimes the URL button is the problem)
            try:
                bot.send_message(admin_id, html_text, parse_mode="HTML")
                sent = True
                last_err = ""
            except Exception as e2:
                last_err = f"html-nomarkup: {e2}"
                print(f"[NOTIFY] {admin_id} HTML-nomarkup failed: {e2}")
                # 3rd try: plain text, no parse mode, no markup — cannot fail on formatting
                try:
                    bot.send_message(admin_id, plain_text)
                    sent = True
                    last_err = ""
                except Exception as e3:
                    last_err = f"plain: {e3}"
                    print(f"[NOTIFY] {admin_id} plain failed: {e3}")

        if sent:
            fb.log_event("admin_notified", chat_id=chat_id,
                         admin_id=str(admin_id), msg_type=msg_type)
        else:
            fb.log_event("admin_notify_failed", chat_id=chat_id,
                         admin_id=str(admin_id), error=last_err)

def mark_admin_msgs_seen(chat_id: str):
    msgs = fb.get(f"support/{chat_id}/messages") or {}
    for mid, m in msgs.items():
        if m.get("from") == "admin" and not m.get("read"):
            fb.patch(f"support/{chat_id}/messages/{mid}", {"read": True})


def store_message(chat_id: str, msg_id: str, data: dict):
    fb.put(f"support/{chat_id}/messages/{msg_id}", data)
    prev_unread = fb.get(f"support/{chat_id}/meta/unread") or 0
    fb.patch(
        f"support/{chat_id}/meta",
        {
            "last_message": data.get("text") or data.get("caption") or f"[{data.get('type','message')}]",
            "last_time": data.get("time", now_str()),
            "last_ts": now_ts(),
            "unread": prev_unread + (1 if data.get("from") == "user" else 0),
            "chat_id": str(chat_id),
            "user_name": data.get("user_name", ""),
            "username": data.get("username", ""),
        },
    )


def _update_profile(cid: str, user):
    uname = _display_name(user)
    un = getattr(user, "username", "") or ""
    meta = fb.get(f"support/{cid}/meta") or {}
    file_id = meta.get("photo_file_id") or ""
    # If we don't have a file_id yet, fetch it — this runs once per chat and
    # is what makes the avatar survive render restarts (file_id is stable,
    # URL is regenerated on demand by the /avatar proxy route).
    if not file_id:
        file_id = get_user_photo_file_id(user.id)
    fb.patch(
        f"support/{cid}/meta",
        {
            "user_name": uname,
            "username": un,
            "chat_id": cid,
            "photo_file_id": file_id or "",
        },
    )
    return uname, un


def send_msg(cid, text, **kw):
    try:
        bot.send_message(cid, text, parse_mode="HTML", **kw)
    except Exception as e:
        print(f"[SEND] {cid}: {e}")
        fb.log_event("bot_error", chat_id=str(cid), error=str(e))


# ── /start ────────────────────────────────────────────────────────────────────
if bot:

    @bot.message_handler(commands=["start"])
    def cmd_start(msg):
        cid = str(msg.chat.id)
        if _is_blocked(cid):
            send_msg(cid, BLOCKED_MSG)
            return

        uname, un = _update_profile(cid, msg.from_user)
        meta = fb.get(f"support/{cid}/meta") or {}
        fb.patch(
            f"support/{cid}/meta",
            {
                "started_at": meta.get("started_at", now_str()),
                "unread": meta.get("unread", 0),
                "blocked": False,
            },
        )
        fb.log_event("chat_started", chat_id=cid, user_name=uname, username=un)

        welcome = get_welcome_message()
        fmt = get_welcome_format()
        try:
            if fmt == "html":
                bot.send_message(cid, welcome, parse_mode="HTML")
            else:
                bot.send_message(cid, welcome, parse_mode=None)
        except Exception as e:
            # Fallback: strip any tags and send plain text
            try:
                import re
                plain = re.sub(r"<[^>]+>", "", welcome)
                bot.send_message(cid, plain, parse_mode=None)
            except Exception:
                print(f"[WELCOME] {e}")

    def _spam_gate(cid: str, uname: str, text: str) -> bool:
        result = antispam.check(cid, text)
        if not result["blocked"]:
            return False

        fb.log_event(
            "spam_detected", chat_id=cid, user_name=uname,
            reason=result["reason"], auto_block=result["auto_block"],
        )

        if result["auto_block"]:
            fb.patch(f"support/{cid}/meta", {"blocked": True, "blocked_reason": "auto: spam"})
            try:
                bot.send_message(cid, h(result["warn"]), parse_mode="HTML")
            except Exception:
                pass
            fb.log_event("auto_blocked", chat_id=cid, user_name=uname, reason="spam")
        elif result["warn"]:
            try:
                bot.send_message(cid, h(result["warn"]), parse_mode="HTML")
            except Exception:
                pass
        return True

    def _maybe_auto_reply(cid: str):
        if not config.AUTO_REPLY:
            return
        cooldown = config.AUTO_REPLY_COOLDOWN_MINUTES
        if cooldown > 0:
            last = fb.get(f"support/{cid}/meta/last_auto_reply_ts") or 0
            if now_ts() - last < cooldown * 60:
                return
        try:
            bot.send_chat_action(cid, "typing")
            time.sleep(0.6)
            bot.send_message(cid, h(config.AUTO_REPLY), parse_mode="HTML")
            fb.patch(f"support/{cid}/meta", {"last_auto_reply_ts": now_ts()})
        except Exception as e:
            print(f"[AUTO_REPLY] {cid}: {e}")

    @bot.message_handler(content_types=["text"])
    def handle_text(msg):
        cid = str(msg.chat.id)
        mid = str(msg.message_id)

        if _is_blocked(cid):
            send_msg(cid, BLOCKED_MSG)
            return

        uname, un = _update_profile(cid, msg.from_user)

        if _spam_gate(cid, uname, msg.text or ""):
            return

        store_message(
            cid, mid,
            {
                "msg_id": mid, "chat_id": cid,
                "user_name": uname, "username": un,
                "text": msg.text, "type": "text",
                "from": "user", "time": now_str(), "ts": now_ts(),
                "read": False, "delivered": True, "edited": False, "deleted": False,
                "reactions": {},
            },
        )
        fb.log_event("message_in", chat_id=cid, user_name=uname, msg_type="text")

        mark_admin_msgs_seen(cid)
        send_admin_notify(cid, uname, un, msg.text, "text")
        _maybe_auto_reply(cid)

    def _handle_media(msg, kind: str):
        cid = str(msg.chat.id)
        mid = str(msg.message_id)

        if _is_blocked(cid):
            send_msg(cid, BLOCKED_MSG)
            return

        uname, un = _update_profile(cid, msg.from_user)
        caption = msg.caption or ""

        if _spam_gate(cid, uname, caption or f"[{kind}]"):
            return

        if kind == "photo":
            file_id = msg.photo[-1].file_id
            file_name = None
        elif kind == "video":
            file_id = msg.video.file_id
            file_name = getattr(msg.video, "file_name", None)
        else:
            file_id = msg.document.file_id
            file_name = msg.document.file_name or "file"

        data = {
            "msg_id": mid, "chat_id": cid,
            "user_name": uname, "username": un,
            "text": caption, "caption": caption,
            "file_id": file_id,
            "type": kind, "from": "user",
            "time": now_str(), "ts": now_ts(),
            "read": False, "delivered": True, "edited": False, "deleted": False,
            "reactions": {},
        }
        if file_name:
            data["file_name"] = file_name

        store_message(cid, mid, data)
        fb.log_event("message_in", chat_id=cid, user_name=uname, msg_type=kind)

        mark_admin_msgs_seen(cid)
        send_admin_notify(cid, uname, un, caption or f"[{kind}]", kind)
        _maybe_auto_reply(cid)

    @bot.message_handler(content_types=["photo"])
    def handle_photo(msg):
        _handle_media(msg, "photo")

    @bot.message_handler(content_types=["video"])
    def handle_video(msg):
        _handle_media(msg, "video")

    @bot.message_handler(content_types=["document"])
    def handle_document(msg):
        _handle_media(msg, "document")


# ── Admin actions ─────────────────────────────────────────────────────────────
def admin_reply(chat_id: str, text: str) -> dict:
    if not bot:
        return {"error": "Bot is not configured (BOT_TOKEN missing)"}
    try:
        # Send as HTML with escaping so `_`, `*`, `[` etc. don't break.
        m = bot.send_message(chat_id, h(text), parse_mode="HTML")
        mid = str(m.message_id)
        data = {
            "msg_id": f"admin_{mid}", "chat_id": chat_id,
            "text": text, "type": "text", "from": "admin",
            "time": now_str(), "ts": now_ts(),
            "read": False, "delivered": True, "edited": False, "deleted": False,
            "reactions": {},
        }
        fb.put(f"support/{chat_id}/messages/admin_{mid}", data)
        fb.patch(
            f"support/{chat_id}/meta",
            {"last_message": text, "last_time": now_str(), "last_ts": now_ts(), "unread": 0},
        )
        fb.log_event("message_out", chat_id=chat_id, msg_type="text")
        return {"ok": True, "mid": f"admin_{mid}", "message": data}
    except Exception as e:
        fb.log_event("bot_error", chat_id=chat_id, error=str(e))
        return {"error": str(e)}


def admin_send_file(chat_id: str, file_bytes: bytes, filename: str, mime_type: str, caption: str = "") -> dict:
    if not bot:
        return {"error": "Bot is not configured (BOT_TOKEN missing)"}
    try:
        f = io.BytesIO(file_bytes)
        f.name = filename
        cap = h(caption) if caption else None

        if mime_type.startswith("image/"):
            m = bot.send_photo(chat_id, f, caption=cap, parse_mode="HTML")
            file_id = m.photo[-1].file_id
            ftype = "photo"
        elif mime_type.startswith("video/"):
            m = bot.send_video(chat_id, f, caption=cap, parse_mode="HTML")
            file_id = m.video.file_id
            ftype = "video"
        else:
            m = bot.send_document(chat_id, f, caption=cap, parse_mode="HTML")
            file_id = m.document.file_id
            ftype = "document"

        mid = str(m.message_id)
        data = {
            "msg_id": f"admin_{mid}", "chat_id": chat_id,
            "text": caption, "caption": caption,
            "file_id": file_id, "file_name": filename,
            "type": ftype, "from": "admin",
            "time": now_str(), "ts": now_ts(),
            "read": False, "delivered": True, "edited": False, "deleted": False,
            "reactions": {},
        }
        fb.put(f"support/{chat_id}/messages/admin_{mid}", data)
        fb.patch(
            f"support/{chat_id}/meta",
            {"last_message": caption or f"[{ftype}]", "last_time": now_str(), "last_ts": now_ts(), "unread": 0},
        )
        fb.log_event("message_out", chat_id=chat_id, msg_type=ftype, file_name=filename)
        return {"ok": True, "mid": f"admin_{mid}", "message": data, "type": ftype}
    except Exception as e:
        fb.log_event("bot_error", chat_id=chat_id, error=str(e))
        return {"error": str(e)}


def block_user(chat_id: str) -> dict:
    fb.patch(f"support/{chat_id}/meta", {"blocked": True, "blocked_reason": "manual"})
    fb.log_event("blocked", chat_id=chat_id, reason="manual")
    try:
        bot.send_message(chat_id, BLOCKED_MSG)
    except Exception:
        pass
    return {"ok": True}


def unblock_user(chat_id: str) -> dict:
    fb.patch(f"support/{chat_id}/meta", {"blocked": False, "blocked_reason": ""})
    antispam.reset(chat_id)
    fb.log_event("unblocked", chat_id=chat_id)
    return {"ok": True}


def admin_edit_message(chat_id: str, admin_mid: str, new_text: str) -> dict:
    if not bot:
        return {"error": "Bot is not configured"}
    try:
        real_mid = int(admin_mid.replace("admin_", ""))
        bot.edit_message_text(h(new_text), chat_id, real_mid, parse_mode="HTML")
    except Exception as e:
        fb.log_event("bot_error", chat_id=chat_id, error=f"edit: {e}")
    fb.patch(
        f"support/{chat_id}/messages/{admin_mid}",
        {"text": new_text, "edited": True, "edited_at": now_str()},
    )
    fb.log_event("message_edited", chat_id=chat_id, msg_id=admin_mid)
    return {"ok": True}


def soft_delete_message(chat_id: str, mid: str, deleted_by: str = "admin") -> dict:
    if mid.startswith("admin_") and bot:
        try:
            real_mid = int(mid.replace("admin_", ""))
            bot.delete_message(chat_id, real_mid)
        except Exception as e:
            print(f"[DELETE] {chat_id}/{mid}: {e}")

    fb.patch(
        f"support/{chat_id}/messages/{mid}",
        {"deleted": True, "deleted_at": now_str(), "deleted_by": deleted_by},
    )
    fb.log_event("message_deleted", chat_id=chat_id, msg_id=mid, deleted_by=deleted_by)
    return {"ok": True}


def set_reaction(chat_id: str, mid: str, emoji: str) -> dict:
    """Toggle a reaction on a message. Only one emoji per admin (we store
    a dict of emoji -> count, and swap when a new emoji is chosen)."""
    if emoji not in REACTIONS:
        return {"error": "Invalid reaction"}
    path = f"support/{chat_id}/messages/{mid}/reactions"
    current = fb.get(path) or {}
    if not isinstance(current, dict):
        current = {}
    # Increment chosen
    current[emoji] = int(current.get(emoji, 0)) + 1
    fb.put(path, current)
    fb.log_event("message_reaction", chat_id=chat_id, msg_id=mid, emoji=emoji)
    return {"ok": True, "reactions": current}


def remove_reaction(chat_id: str, mid: str, emoji: str) -> dict:
    path = f"support/{chat_id}/messages/{mid}/reactions"
    current = fb.get(path) or {}
    if not isinstance(current, dict):
        current = {}
    if emoji in current:
        current[emoji] = max(0, int(current[emoji]) - 1)
        if current[emoji] == 0:
            del current[emoji]
    fb.put(path, current)
    fb.log_event("message_reaction_removed", chat_id=chat_id, msg_id=mid, emoji=emoji)
    return {"ok": True, "reactions": current}


# ── Broadcast ─────────────────────────────────────────────────────────────────
def _all_user_chat_ids():
    raw = fb.get("support") or {}
    out = []
    for cid, data in raw.items():
        if not isinstance(data, dict):
            continue
        meta = data.get("meta") or {}
        if meta and not meta.get("blocked"):
            out.append(str(cid))
    return out


def broadcast_message(text: str, fmt: str = "html"):
    """Yield (sent_count, total, chat_id, error) as we go."""
    if not bot:
        yield (0, 0, None, "Bot not configured")
        return
    recipients = _all_user_chat_ids()
    total = len(recipients)
    if total == 0:
        yield (0, 0, None, "No recipients")
        return

    sent = 0
    fb.log_event("broadcast_started", total=total, fmt=fmt)
    for cid in recipients:
        try:
            if fmt == "html":
                bot.send_message(cid, text, parse_mode="HTML")
            else:
                bot.send_message(cid, text, parse_mode=None)
            sent += 1
            # also store in chat history as admin message
            try:
                data = {
                    "msg_id": f"broadcast_{int(time.time()*1000)}_{cid}",
                    "chat_id": cid,
                    "text": text, "type": "text", "from": "admin",
                    "time": now_str(), "ts": now_ts(),
                    "read": False, "delivered": True,
                    "edited": False, "deleted": False,
                    "reactions": {}, "broadcast": True,
                }
                fb.put(f"support/{cid}/messages/{data['msg_id']}", data)
                fb.patch(
                    f"support/{cid}/meta",
                    {"last_message": text[:80], "last_time": now_str(), "last_ts": now_ts()},
                )
            except Exception:
                pass
            yield (sent, total, cid, None)
        except Exception as e:
            yield (sent, total, cid, str(e))
        time.sleep(0.05)  # ~20 msgs/s, well under Telegram's limits
    fb.log_event("broadcast_finished", total=total, sent=sent, fmt=fmt)


def run_bot():
    if not bot:
        print("Bot token missing (BOT_TOKEN) — bot not started.")
        return
    print("Support bot polling started.")
    fb.log_event("bot_started")
    while True:
        try:
            bot.infinity_polling(timeout=30, long_polling_timeout=30, skip_pending=True)
        except Exception as e:
            print(f"[POLLING] crashed: {e} — restarting in 5s")
            fb.log_event("bot_error", error=f"polling crashed: {e}")
            time.sleep(5)
