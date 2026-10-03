"""
telegram_bot.py — customer support Telegram bot.
"""
import datetime
import html as html_lib
import io
import json
import threading
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
    return html_lib.escape(s or "", quote=False)


def _display_name(user) -> str:
    fn = getattr(user, "first_name", "") or ""
    ln = getattr(user, "last_name", "") or ""
    return f"{fn} {ln}".strip() or "Friend"


def _is_blocked(cid: str) -> bool:
    return bool(fb.get(f"support/{cid}/meta/blocked"))


def get_welcome_message() -> str:
    try:
        v = fb.get("settings/welcome/message")
        if v and isinstance(v, str) and v.strip():
            return v
    except Exception:
        pass
    return config.DEFAULT_WELCOME_MESSAGE


def get_welcome_format() -> str:
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
    try:
        photos = bot.get_user_profile_photos(user_id, limit=1)
        if photos and photos.photos:
            return photos.photos[0][-1].file_id
    except Exception as e:
        print(f"[PHOTO-ID] {e}")
    return ""


def send_admin_notify(chat_id, user_name, username, text, msg_type="text"):
    """Notify every configured admin chat id. Never silently fails: logs
    every attempt to /logs and falls back to plain-text if HTML fails."""
    ids = config.ADMIN_CHAT_IDS()
    if not ids:
        fb.log_event("admin_notify_failed", chat_id=chat_id,
                     error="ADMIN_CHAT_ID empty")
        return
    if not bot:
        fb.log_event("admin_notify_failed", chat_id=chat_id,
                     error="Bot not initialized")
        return

    icon = {"photo": "🖼️", "video": "🎬", "document": "📄"}.get(msg_type, "💬")
    preview_raw = text or f"[{msg_type}]"
    preview = (preview_raw[:250] + "…") if len(preview_raw) > 250 else preview_raw

    chat_link = ""
    if config.PANEL_URL:
        chat_link = f"{config.PANEL_URL.rstrip('/')}/chat/{chat_id}"

    html_text = (
        "📩 <b>New Support Message</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>Name:</b> {h(user_name)}\n"
        f"🔗 <b>Username:</b> {'@' + h(username) if username else '—'}\n"
        f"🆔 <b>Chat ID:</b> <code>{h(str(chat_id))}</code>\n"
        f"{icon} <b>Message:</b> {h(preview)}\n"
        f"🕐 <b>Time (IST):</b> {now_str()}"
    )
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
        try:
            bot.send_message(admin_id, html_text, parse_mode="HTML", reply_markup=mk)
            sent = True
        except Exception as e:
            last_err = f"html: {e}"
            try:
                bot.send_message(admin_id, html_text, parse_mode="HTML")
                sent = True
                last_err = ""
            except Exception as e2:
                last_err = f"html-nomarkup: {e2}"
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
    """Soft delete ONLY. The message row stays in Firebase forever — we just
    flip `deleted: true` so the UI shows a placeholder. Bot-side, best-effort
    delete our own message from Telegram (48h window)."""
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


def set_reaction(chat_id: str, mid: str, emoji: str, actor: str = "admin") -> dict:
    """Add a reaction to a message. Stored at
    /support/{chat_id}/messages/{mid}/reactions/{emoji} as a count.
    Also echoes the reaction back into the OTHER side's chat as a small
    system reply so both sides see it."""
    if emoji not in REACTIONS:
        return {"error": "Invalid reaction"}
    path = f"support/{chat_id}/messages/{mid}/reactions"
    current = fb.get(path) or {}
    if not isinstance(current, dict):
        current = {}
    current[emoji] = int(current.get(emoji, 0)) + 1
    fb.put(path, current)
    fb.log_event("message_reaction", chat_id=chat_id, msg_id=mid, emoji=emoji, actor=actor)

    # Echo into the other side's Telegram chat
    if bot:
        msgs = fb.get(f"support/{chat_id}/messages") or {}
        m = msgs.get(mid) or {}
        other = "user" if actor == "admin" else "admin"
        try:
            if other == "user":
                # Admin reacted → notify the user in their chat
                bot.send_message(chat_id, f"{emoji} Admin reacted to your message",
                                 parse_mode=None)
            else:
                # User reacted → notify every admin
                for admin_id in config.ADMIN_CHAT_IDS():
                    try:
                        bot.send_message(admin_id,
                                         f"{emoji} User reacted to a message in chat {chat_id}",
                                         parse_mode=None)
                    except Exception:
                        pass
        except Exception as e:
            print(f"[REACT-ECHO] {e}")

    return {"ok": True, "reactions": current}


def remove_reaction(chat_id: str, mid: str, emoji: str, actor: str = "admin") -> dict:
    path = f"support/{chat_id}/messages/{mid}/reactions"
    current = fb.get(path) or {}
    if not isinstance(current, dict):
        current = {}
    if emoji in current:
        current[emoji] = max(0, int(current[emoji]) - 1)
        if current[emoji] == 0:
            del current[emoji]
    fb.put(path, current)
    fb.log_event("message_reaction_removed", chat_id=chat_id, msg_id=mid, emoji=emoji, actor=actor)
    return {"ok": True, "reactions": current}


# ── Broadcast engine (runs in a background thread) ──────────────────────────
_broadcast_jobs = {}
_broadcast_lock = threading.Lock()


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


def _run_broadcast(job_id, text, fmt, recipients):
    """Runs in a background thread. Updates the job dict in-place; the SSE
    endpoint reads from it without blocking."""
    sent = 0
    failed = []
    total = len(recipients)
    fb.log_event("broadcast_started", total=total, fmt=fmt, job_id=job_id)

    for i, cid in enumerate(recipients, 1):
        try:
            if fmt == "html":
                bot.send_message(cid, text, parse_mode="HTML")
            else:
                bot.send_message(cid, text, parse_mode=None)
            sent += 1
            try:
                bmid = f"broadcast_{int(time.time()*1000)}_{cid}"
                fb.put(
                    f"support/{cid}/messages/{bmid}",
                    {
                        "msg_id": bmid, "chat_id": cid,
                        "text": text, "type": "text", "from": "admin",
                        "time": now_str(), "ts": now_ts(),
                        "read": False, "delivered": True,
                        "edited": False, "deleted": False,
                        "reactions": {}, "broadcast": True,
                    },
                )
                fb.patch(
                    f"support/{cid}/meta",
                    {"last_message": text[:120], "last_time": now_str(), "last_ts": now_ts()},
                )
            except Exception:
                pass
        except Exception as e:
            failed.append({"chat_id": cid, "error": str(e)})

        with _broadcast_lock:
            _broadcast_jobs[job_id]["sent"] = sent
            _broadcast_jobs[job_id]["failed"] = failed
            _broadcast_jobs[job_id]["progress"] = i
            _broadcast_jobs[job_id]["last_chat"] = cid

        time.sleep(0.05)  # ~20/s

    with _broadcast_lock:
        _broadcast_jobs[job_id]["done"] = True
        _broadcast_jobs[job_id]["sent"] = sent
        _broadcast_jobs[job_id]["failed"] = failed

    fb.log_event("broadcast_finished", job_id=job_id, total=total, sent=sent, failed=len(failed))


def start_broadcast(text: str, fmt: str) -> dict:
    """Kick off a broadcast and return a job_id immediately. Non-blocking."""
    if not bot:
        return {"error": "Bot not configured"}
    recipients = _all_user_chat_ids()
    if not recipients:
        return {"error": "No recipients"}

    job_id = f"bc_{int(time.time()*1000)}"
    with _broadcast_lock:
        _broadcast_jobs[job_id] = {
            "job_id": job_id, "text": text, "fmt": fmt,
            "total": len(recipients), "progress": 0,
            "sent": 0, "failed": [], "done": False,
            "started_at": now_str(),
        }

    # Persist broadcast record so it can be re-sent / edited / deleted
    fb.put(
        f"broadcasts/{job_id}",
        {
            "id": job_id, "text": text, "fmt": fmt,
            "total": len(recipients), "sent": 0, "failed": 0,
            "status": "running", "started_at": now_str(),
            "ts": now_ts(), "archived": False,
        },
    )

    threading.Thread(
        target=_run_broadcast,
        args=(job_id, text, fmt, recipients),
        daemon=True,
    ).start()

    return {"ok": True, "job_id": job_id, "total": len(recipients)}


def get_broadcast_job(job_id: str):
    with _broadcast_lock:
        return dict(_broadcast_jobs.get(job_id) or {})


def mark_broadcast_finished_in_db(job_id):
    """Called by app when SSE sees done — persists final counts."""
    with _broadcast_lock:
        j = _broadcast_jobs.get(job_id)
    if not j:
        return
    fb.patch(f"broadcasts/{job_id}", {
        "sent": j.get("sent", 0),
        "failed": len(j.get("failed", [])),
        "status": "completed",
        "finished_at": now_str(),
    })


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
