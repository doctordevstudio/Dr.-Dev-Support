"""
telegram_bot.py — customer support Telegram bot.

Notes
-----
* The TeleBot instance has NO default parse_mode. In pyTelegramBotAPI,
  `parse_mode=None` means "use the bot-wide default", so with a default of
  HTML a "plain text" send was silently still parsed as HTML. Now every send
  states its mode explicitly: HTML sends pass parse_mode="HTML", plain sends
  pass nothing at all.
* All user-provided text that is echoed back inside HTML goes through h().
* Reactions are two-way:
    user  -> Telegram `message_reaction` update -> stored + admin notified
    admin -> panel click -> stored + set on the Telegram message (user sees it)
* TWO admin roles: "super" (SUPER_ADMIN_CHAT_ID) and "admin" (ADMIN_CHAT_ID).
  Both are notified about every customer message and both can use the bot
  commands. Every action is attributed to the role that did it (`fb.acting_as`)
  and written to that role's own log.
* Replies work like Telegram: a message sent with a reply target arrives at the
  customer as a real Telegram reply to that message. In the bot, an admin just
  replies to a notification and the answer goes to that customer.
* Reactions from the bot: an admin who reacts to a notification in Telegram puts that
  reaction on the customer's message (the notification -> message mapping is kept in
  Firebase under `admin_notify/<admin chat>/<notification id>`).
* Menu: every admin chat gets the Telegram command menu (Send, Broadcast, Help, Cancel;
  the ADMIN role does not see / cannot use Broadcast). `/send` asks for the chat id,
  then for the message / image / video / file. Every notification has a "↩️ Reply" button
  that asks for the answer and sends it as a Telegram reply.
* An ADMIN "deleting" a message only hides it from the admin's own view
  (`hidden_admin`). The super admin still sees it, the database keeps it, and
  nothing is removed from the customer's Telegram chat.
"""
import datetime
import functools
import html as html_lib
import io
import re
import secrets
import threading
import time

import telebot
from telebot import types

import account
import ai
import antispam
import config
import firebase_helper as fb
import settings

# 16 worker threads (telebot's default is 2 — one slow handler used to block every other user)
bot = telebot.TeleBot(config.BOT_TOKEN, threaded=True, num_threads=16) if config.BOT_TOKEN else None

# Blocked users are ignored completely: nothing is ever sent back to them.

# Only emoji Telegram accepts as message reactions (😂 / 😮 are NOT in that list).
# Grouped for the panel: the first group is the quick-reaction row, the rest opens with "+".
REACTION_GROUPS = [
    ("Popular", ["👍", "❤️", "🔥", "😁", "😱", "😢"]),
    ("Faces", ["🥰", "😍", "🤩", "😘", "🤣", "😭", "😇", "😎", "🤓", "🤗", "🤪", "🤔", "🤯", "🥱",
               "🥴", "😴", "😨", "😐", "🤨", "😡", "🤬", "🤮", "🤡", "😈"]),
    ("Hands & people", ["👎", "👏", "🙏", "👌", "🤝", "🫡", "🙈", "🙉", "🙊", "💅", "🤷", "👀", "👻", "👾"]),
    ("Hearts & symbols", ["💔", "💘", "💋", "💯", "⚡", "🆒", "🎉", "🏆", "💊", "🗿"]),
    ("Food, animals & fun", ["🐳", "🦄", "🍓", "🍌", "🌭", "🍾", "🌚", "🎃", "🎄", "🎅"]),
]
REACTIONS = [e for _, group in REACTION_GROUPS for e in group]

ROLE_LABEL = {"super": "Super Admin", "admin": "Admin"}


# ── Small helpers ─────────────────────────────────────────────────────────────
def now_str():
    return datetime.datetime.now(config.IST).strftime("%Y-%m-%d %H:%M:%S")


def now_ts():
    return int(time.time())


def h(s):
    """HTML-escape untrusted text so `<`, `>`, `&` don't break Telegram's parser."""
    return html_lib.escape(s or "", quote=False)


def html_to_plain(s: str) -> str:
    """Strip Telegram-HTML tags -> what the user actually sees."""
    return html_lib.unescape(re.sub(r"<[^>]+>", "", s or ""))


def _display_name(user) -> str:
    fn = getattr(user, "first_name", "") or ""
    ln = getattr(user, "last_name", "") or ""
    return f"{fn} {ln}".strip() or "Friend"


# ── Chat meta cache: hot paths never wait for a Firebase read ────────────────
_meta_cache = {}
_META_TTL = 45.0


def get_meta(cid: str, fresh: bool = False) -> dict:
    now = time.time()
    ent = _meta_cache.get(cid)
    if ent and not fresh and now - ent[0] < _META_TTL:
        return ent[1]
    meta = fb.get(f"support/{cid}/meta")
    meta = meta if isinstance(meta, dict) else {}
    _meta_cache[cid] = (now, meta)
    return meta


def update_meta(cid: str, **fields):
    """Write meta fields (+ chat_index) and keep the cache coherent."""
    if not fields:
        return
    fb.patch(f"support/{cid}/meta", fields)
    ent = _meta_cache.get(cid)
    if ent:
        ent[1].update(fields)
    else:
        _meta_cache[cid] = (time.time(), dict(fields))


def _is_blocked(cid: str) -> bool:
    return bool(get_meta(cid).get("blocked"))


# ── Never handle the same message twice (redeploys can redeliver the last updates) ──
_seen_ids = {}
_seen_lock = threading.Lock()


def _already_seen(cid: str, mid: str) -> bool:
    """In-memory: the same message twice in one run."""
    k = (cid, mid)
    with _seen_lock:
        if k in _seen_ids:
            return True
        _seen_ids[k] = 1
        if len(_seen_ids) > 20000:
            for old in list(_seen_ids)[:5000]:
                _seen_ids.pop(old, None)
    return False


def _already_stored(cid: str, mid: str) -> bool:
    """In Firebase: handled by the previous instance before the redeploy."""
    return bool(fb.get(f"support/{cid}/messages/{mid}/msg_id"))


def _is_old(msg, seconds: int = 10) -> bool:
    d = getattr(msg, "date", None)
    return bool(d) and time.time() - int(d) > seconds


def send_formatted(chat_id, text: str, fmt: str = "html", **kw):
    """Send `text` as real Telegram HTML or as truly plain text."""
    if fmt == "html":
        return bot.send_message(chat_id, text, parse_mode="HTML", **kw)
    return bot.send_message(chat_id, text, **kw)


# ── Reactions: data model ─────────────────────────────────────────────────────
# Stored as   reactions: {"admin": "👍", "user": "❤️,🔥"}   (comma separated;
# an empty string keeps the key alive in Firebase so nothing is ever deleted).
# Legacy data ({"👍": 2}) is still understood and treated as admin reactions.
def canon_emoji(e: str) -> str:
    e = (e or "").strip()
    base = e.replace("\ufe0f", "")
    return "❤️" if base == "❤" else e


def _tg_emoji(e: str) -> str:
    """Telegram's reaction list uses '❤' without the variation selector."""
    return (e or "").replace("\ufe0f", "")


def reaction_view(raw) -> dict:
    out = {"admin": [], "user": []}
    if not isinstance(raw, dict):
        return out
    if "admin" in raw or "user" in raw:
        for k in ("admin", "user"):
            v = raw.get(k)
            if isinstance(v, str):
                out[k] = [canon_emoji(x) for x in v.split(",") if x.strip()]
            elif isinstance(v, list):
                out[k] = [canon_emoji(x) for x in v if isinstance(x, str) and x.strip()]
        return out
    out["admin"] = [
        canon_emoji(e) for e, c in raw.items()
        if isinstance(c, (int, float)) and c > 0
    ]
    return out


def _reaction_store(view: dict) -> dict:
    return {"admin": ",".join(view.get("admin", [])), "user": ",".join(view.get("user", []))}


def _tg_id(mid: str):
    """Telegram message id for a stored message key (None if it has none)."""
    s = str(mid)
    if s.startswith("admin_"):
        s = s[len("admin_"):]
    return int(s) if s.isdigit() else None


# ── Replies (Telegram style) ──────────────────────────────────────────────────
def _reply_kw(tg_id) -> dict:
    """Keyword arguments that make a Telegram send call a reply to `tg_id`."""
    if not tg_id:
        return {}
    rp = getattr(types, "ReplyParameters", None)
    if rp:
        return {"reply_parameters": rp(message_id=int(tg_id), allow_sending_without_reply=True)}
    return {"reply_to_message_id": int(tg_id), "allow_sending_without_reply": True}


def _snippet(m: dict, limit: int = 120) -> str:
    txt = (m.get("text") or m.get("caption") or "").strip()
    if txt:
        return (txt[:limit] + "…") if len(txt) > limit else txt
    return {"photo": "🖼️ Photo", "video": "🎬 Video", "document": "📄 File"}.get(m.get("type"), "Message")


def _reply_ctx(chat_id: str, reply_key: str):
    """(telegram kwargs, stored reply info) for a reply to the stored message `reply_key`.
    Unknown / deleted / hidden targets simply mean "no reply" — the message is still sent."""
    if not reply_key:
        return {}, None
    target = fb.get(f"support/{chat_id}/messages/{reply_key}")
    if not isinstance(target, dict) or target.get("deleted"):
        return {}, None
    if fb.get_actor() == "admin" and target.get("hidden_admin"):
        return {}, None
    info = {"msg_id": reply_key, "from": target.get("from", "user"),
            "type": target.get("type", "text"), "text": _snippet(target)}
    return _reply_kw(_tg_id(reply_key)), info


def _incoming_reply(cid: str, msg):
    """Reply info for a customer message that is a Telegram reply to another message."""
    rt = getattr(msg, "reply_to_message", None)
    if not rt:
        return None
    frm = "admin" if getattr(getattr(rt, "from_user", None), "is_bot", False) else "user"
    ctype = getattr(rt, "content_type", "text")
    snip = _snippet({"text": getattr(rt, "text", None), "caption": getattr(rt, "caption", None),
                     "type": ctype})
    try:
        key = _resolve_message_key(cid, rt.message_id)
    except Exception:
        key = ""
    return {"msg_id": key, "from": frm, "type": ctype, "text": snip}


# ── Welcome message ───────────────────────────────────────────────────────────
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


# ── Files / avatars ───────────────────────────────────────────────────────────
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
    """Fetch just the file_id (URLs expire; file_ids are stable)."""
    try:
        photos = bot.get_user_profile_photos(user_id, limit=1)
        if photos and photos.photos:
            return photos.photos[0][-1].file_id
    except Exception as e:
        print(f"[PHOTO-ID] {e}")
    return ""


# ── Admin notifications ───────────────────────────────────────────────────────
def _panel_link(chat_id: str) -> str:
    if config.PANEL_URL:
        return f"{config.PANEL_URL.rstrip('/')}/chat/{chat_id}"
    return ""


def _send_media(chat_id, kind, file_id, caption, parse_mode=None, markup=None):
    fn = bot.send_photo if kind == "photo" else bot.send_video if kind == "video" else bot.send_document
    kw = {"caption": caption, "reply_markup": markup}
    if parse_mode:
        kw["parse_mode"] = parse_mode
    return fn(chat_id, file_id, **kw)


def _remember_notification(admin_id, notif_msg, chat_id: str, msg_key: str):
    """notification message (in the admin's chat) -> customer chat + message, so a Telegram
    reaction on the notification can be applied to the customer's message."""
    try:
        if msg_key and notif_msg is not None:
            fb.put(f"admin_notify/{admin_id}/{notif_msg.message_id}",
                   {"cid": str(chat_id), "mid": str(msg_key), "ts": now_ts()})
    except Exception as e:
        print(f"[NOTIFY-MAP] {e}")


def _notify_targets(audience: str = "all") -> list:
    """Chats that get a notification. audience "super": only the super admin (personal
    account events). `/bot killadmin` mutes the ADMIN role for everything else."""
    ids = config.ADMIN_CHAT_IDS()
    if audience == "super":
        return [i for i in ids if config.role_of_chat(i) == "super"]
    if not settings.load().get("notify_admin_role", True):
        return [i for i in ids if config.role_of_chat(i) == "super"]
    return ids


def _send_to_admins(html_text: str, plain_text: str, chat_id: str, event: str,
                    chat_link: str = "", media=None, msg_key: str = "", audience: str = "all"):
    """Media (+caption) -> HTML (+button) -> HTML -> plain text. Always logs the outcome.
    `media` = (kind, file_id): the user's photo/video/file is delivered to the admin chat too.
    All admin chats are served IN PARALLEL."""
    ids = _notify_targets(audience)
    if not config.ADMIN_CHAT_IDS():
        fb.log_event("admin_notify_failed", scope="super", actor="system", chat_id=chat_id,
                     error="SUPER_ADMIN_CHAT_ID / ADMIN_CHAT_ID not configured (empty)")
        print("[NOTIFY] SUPER_ADMIN_CHAT_ID and ADMIN_CHAT_ID are empty — nothing to send to.")
        return
    if not ids:
        return                                      # e.g. admin role muted with /bot killadmin
    if not bot:
        fb.log_event("admin_notify_failed", scope="super", actor="system", chat_id=chat_id,
                     error="Bot not initialized (BOT_TOKEN missing)")
        return

    mk = types.InlineKeyboardMarkup()
    if chat_link:
        mk.add(types.InlineKeyboardButton("🖥️ Open in Admin Panel", url=chat_link))
    # tap -> the bot asks for your message and sends it to the customer as a reply
    mk.add(types.InlineKeyboardButton("↩️ Reply", callback_data=f"rp:{chat_id}:{msg_key or 0}"))

    def one(admin_id):
        sent, last_err, sent_msg = False, "", None
        # each admin's delivery result goes ONLY to that admin's own log
        lscope = config.role_of_chat(admin_id) or "super"
        if media:
            kind, file_id = media
            for pm, cap in (("HTML", html_text), (None, plain_text)):
                try:
                    sent_msg = _send_media(admin_id, kind, file_id, cap[:1024], pm, mk)
                    sent = True
                    break
                except Exception as e:
                    last_err = f"media: {e}"
                    print(f"[NOTIFY] {admin_id} media failed: {e}")
            if sent:
                _remember_notification(admin_id, sent_msg, chat_id, msg_key)
                fb.log_event("admin_notified", scope=lscope, actor="system", chat_id=chat_id,
                             admin_id=str(admin_id), msg_type=event)
                return
        try:
            sent_msg = bot.send_message(admin_id, html_text, parse_mode="HTML", reply_markup=mk)
            sent = True
        except Exception as e:
            last_err = f"html: {e}"
            print(f"[NOTIFY] {admin_id} HTML failed: {e}")
            try:
                sent_msg = bot.send_message(admin_id, html_text, parse_mode="HTML")
                sent, last_err = True, ""
            except Exception as e2:
                last_err = f"html-nomarkup: {e2}"
                try:
                    sent_msg = bot.send_message(admin_id, plain_text)
                    sent, last_err = True, ""
                except Exception as e3:
                    last_err = f"plain: {e3}"
                    print(f"[NOTIFY] {admin_id} plain failed: {e3}")
        if sent:
            _remember_notification(admin_id, sent_msg, chat_id, msg_key)
            fb.log_event("admin_notified", scope=lscope, actor="system", chat_id=chat_id,
                         admin_id=str(admin_id), msg_type=event)
        else:
            fb.log_event("admin_notify_failed", scope=lscope, actor="system", chat_id=chat_id,
                         admin_id=str(admin_id), error=last_err)

    if len(ids) == 1:
        one(ids[0])
        return
    futs = [fb.bg(one, i) for i in ids]
    for f in futs:                                  # wait, so callers still see "done" semantics
        try:
            f and f.result(timeout=40)
        except Exception as e:
            print(f"[NOTIFY] {e}")


def send_admin_notify(chat_id: str, user_name: str, username: str, text: str,
                      msg_type: str = "text", file_id: str = "", file_name: str = "",
                      msg_id: str = "", reply_text: str = ""):
    """Notify EVERY admin chat (super admin + admin) about a new incoming message.
    Photos / videos / files are delivered to the admin chats as well (setting: forward_media).
    The notification carries the chat id and message id: an admin who replies to it in
    Telegram answers exactly that message (see `_notification_target`)."""
    s = settings.load()
    media = msg_type in ("photo", "video", "document") and bool(file_id)
    forward = media and s["forward_media"]
    if not forward and not s["notify_new_message"]:
        return
    icon = {"photo": "🖼️", "video": "🎬", "document": "📄"}.get(msg_type, "💬")
    preview_raw = text or f"[{msg_type}]"
    limit = 150 if forward else 250
    preview = (preview_raw[:limit] + "…") if len(preview_raw) > limit else preview_raw
    uname_html = ("@" + h(username)) if username else "—"
    uname_plain = ("@" + username) if username else "—"
    file_html = f"📎 <b>File:</b> {h(file_name)}\n" if (forward and file_name) else ""
    file_plain = f"📎 File: {file_name}\n" if (forward and file_name) else ""
    mid_html = f"🔖 <b>Msg ID:</b> <code>{h(str(msg_id))}</code>\n" if msg_id else ""
    mid_plain = f"🔖 Msg ID: {msg_id}\n" if msg_id else ""
    rt = (reply_text or "").strip()
    rt = (rt[:100] + "…") if len(rt) > 100 else rt
    rep_html = f"↩️ <b>In reply to:</b> {h(rt)}\n" if rt else ""
    rep_plain = f"↩️ In reply to: {rt}\n" if rt else ""

    html_text = (
        "📩 <b>New Support Message</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>Name:</b> {h(user_name)}\n"
        f"🔗 <b>Username:</b> {uname_html}\n"
        f"🆔 <b>Chat ID:</b> <code>{h(str(chat_id))}</code>\n"
        f"{mid_html}"
        f"{file_html}"
        f"{rep_html}"
        f"{icon} <b>Message:</b> {h(preview)}\n"
        f"🕐 <b>Time (IST):</b> {now_str()}\n"
        "💬 <i>Reply to this message to answer the customer.</i>"
    )
    plain_text = (
        "📩 New Support Message\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 Name: {user_name}\n"
        f"🔗 Username: {uname_plain}\n"
        f"🆔 Chat ID: {chat_id}\n"
        f"{mid_plain}"
        f"{file_plain}"
        f"{rep_plain}"
        f"{icon} Message: {preview}\n"
        f"🕐 Time (IST): {now_str()}\n"
        "💬 Reply to this message to answer the customer."
    )
    _send_to_admins(html_text, plain_text, chat_id, msg_type, _panel_link(chat_id),
                    media=(msg_type, file_id) if forward else None, msg_key=str(msg_id or ""))


def send_admin_reaction_notify(chat_id, user_name, username, emojis, target_text, target_from):
    if not config.REACTION_NOTIFY:
        return
    em = " ".join(emojis)
    target = (target_text or "").strip() or "[media]"
    target = (target[:120] + "…") if len(target) > 120 else target
    whose = "your reply" if target_from == "admin" else "their own message"
    uname_html = ("@" + h(username)) if username else "—"
    uname_plain = ("@" + username) if username else "—"
    html_text = (
        f"{h(em)} <b>New reaction</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>Name:</b> {h(user_name)}\n"
        f"🔗 <b>Username:</b> {uname_html}\n"
        f"🆔 <b>Chat ID:</b> <code>{h(str(chat_id))}</code>\n"
        f"↪️ <b>Reacted to {whose}:</b> {h(target)}\n"
        f"🕐 <b>Time (IST):</b> {now_str()}"
    )
    plain_text = (
        f"{em} New reaction\n"
        f"👤 Name: {user_name}\n🔗 Username: {uname_plain}\n🆔 Chat ID: {chat_id}\n"
        f"↪️ Reacted to {whose}: {target}\n🕐 Time (IST): {now_str()}"
    )
    _send_to_admins(html_text, plain_text, chat_id, "reaction", _panel_link(chat_id))


def send_admin_given_reaction_notify(chat_id: str, emoji: str, msg: dict, actor: str, shown: bool = True):
    audience = "super" if account.is_acct(chat_id) else "all"
    """An admin reacted (panel or bot): tell BOTH admin chats. The customer sees the emoji on
    their own Telegram message (set_message_reaction)."""
    if not config.REACTION_NOTIFY or not emoji:
        return
    meta = get_meta(chat_id)
    name = meta.get("user_name") or chat_id
    un = meta.get("username") or ""
    who = ROLE_LABEL.get(actor, "Admin")
    icon = "👑" if actor == "super" else "🛡️"
    target = ((msg or {}).get("text") or (msg or {}).get("caption") or "").strip() or "[media]"
    target = (target[:120] + "…") if len(target) > 120 else target
    uname_html = ("@" + h(un)) if un else "—"
    uname_plain = ("@" + un) if un else "—"
    seen_html = "👀 <i>The customer sees this reaction on their message.</i>" if shown else \
                "⚠️ <i>Saved in the panel only — Telegram refused the reaction.</i>"
    seen_plain = "The customer sees this reaction on their message." if shown else \
                 "Saved in the panel only — Telegram refused the reaction."
    html_text = (
        f"{h(emoji)} <b>Reaction given</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"{icon} <b>By:</b> {who}\n"
        f"👤 <b>To:</b> {h(name)}\n"
        f"🔗 <b>Username:</b> {uname_html}\n"
        f"🆔 <b>Chat ID:</b> <code>{h(str(chat_id).lstrip('a') if account.is_acct(chat_id) else str(chat_id))}</code>\n"
        f"{'👤 <b>Source:</b> P2P chat' + chr(10) if account.is_acct(chat_id) else ''}"
        f"↪️ <b>On message:</b> {h(target)}\n"
        f"🕐 <b>Time (IST):</b> {now_str()}\n"
        f"{seen_html}"
    )
    plain_text = (
        f"{emoji} Reaction given\nBy: {who}\nTo: {name}\nUsername: {uname_plain}\n"
        f"Chat ID: {str(chat_id).lstrip('a') if account.is_acct(chat_id) else chat_id}\n"
        f"{'Source: P2P chat' + chr(10) if account.is_acct(chat_id) else ''}"
        f"On message: {target}\nTime (IST): {now_str()}\n{seen_plain}"
    )
    _send_to_admins(html_text, plain_text, chat_id, "reaction_given", _panel_link(chat_id),
                    audience=audience)


def send_admin_edit_notify(chat_id, user_name, username, old_text, new_text):
    if not settings.load()["notify_edit"]:
        return
    old_p = (old_text or "")[:150]
    new_p = (new_text or "")[:150]
    uname_html = ("@" + h(username)) if username else "—"
    html_text = (
        "✏️ <b>User edited a message</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>Name:</b> {h(user_name)}\n"
        f"🔗 <b>Username:</b> {uname_html}\n"
        f"🆔 <b>Chat ID:</b> <code>{h(str(chat_id))}</code>\n"
        f"➖ <b>Before:</b> {h(old_p)}\n"
        f"➕ <b>Now:</b> {h(new_p)}"
    )
    plain_text = (f"✏️ User edited a message\nName: {user_name}\nChat ID: {chat_id}\n"
                  f"Before: {old_p}\nNow: {new_p}")
    _send_to_admins(html_text, plain_text, chat_id, "edit", _panel_link(chat_id))


# ── Storage helpers ───────────────────────────────────────────────────────────
def mark_admin_msgs_seen(chat_id: str):
    """User wrote back -> the latest admin messages become 'read' (one small query, one write)."""
    ok, data = fb.query_by_ts(f"support/{chat_id}/messages", limit_last=40)
    if not ok:
        data = fb.get(f"support/{chat_id}/messages") or {}
    upd = {
        f"support/{chat_id}/messages/{mid}/read": True
        for mid, m in (data or {}).items()
        if isinstance(m, dict) and m.get("from") == "admin" and not m.get("read")
    }
    if upd:
        fb.multi_update(upd)


def store_message(chat_id: str, msg_id: str, data: dict, extra: dict = None, count_unread=None):
    """ONE Firebase request: the message + the meta fields + unread counter (server-side
    increment, no read-before-write). `extra` = more root paths to write in the same request."""
    cid = str(chat_id)
    base = f"support/{cid}"
    last = data.get("text") or data.get("caption") or f"[{data.get('type', 'message')}]"
    ts = int(data.get("ts") or now_ts())
    up = {
        f"{base}/messages/{msg_id}": data,
        f"{base}/meta/last_message": last,
        f"{base}/meta/last_time": data.get("time", now_str()),
        f"{base}/meta/last_ts": ts,
        f"{base}/meta/chat_id": cid,
    }
    if data.get("user_name"):
        up[f"{base}/meta/user_name"] = data["user_name"]
    if "username" in data and data.get("from") == "user":
        up[f"{base}/meta/username"] = data.get("username") or ""
    if data.get("source"):
        up[f"{base}/meta/source"] = data["source"]
    if count_unread is None:
        count_unread = data.get("from") == "user"
    if count_unread:
        up[f"{base}/meta/unread"] = fb.inc(1)
    if extra:
        up.update(extra)
    res = fb.multi_update(up)
    ent = _meta_cache.get(cid)
    if ent:
        ent[1].update({"last_message": last, "chat_id": cid, "last_ts": ts,
                       **({"user_name": data["user_name"]} if data.get("user_name") else {}),
                       **({"username": data.get("username") or ""} if data.get("from") == "user" else {})})
    return res


def forward_bot_media(cid: str, kind: str, file_id: str, caption: str, name: str, username: str, ts=None):
    """Media sent to the BOT -> the bot channel / group from Settings (a different one than the
    personal account's). Always shows "Forwarded from" + full name, @username, chat id, date, time."""
    chan = settings.load().get("forward_channel_bot") or ""
    if not chan or not bot or not file_id:
        return
    target = int(chan) if chan.lstrip("-").isdigit() else "@" + chan
    when = datetime.datetime.fromtimestamp(ts or time.time(), config.IST).strftime("%Y-%m-%d %H:%M:%S")
    un = ("@" + username) if username else "—"
    foot_plain = (f"📨 Forwarded from: {name}\n🔗 Username: {un}\n"
                  f"🆔 Chat ID: {cid}\n🕐 {when} IST")
    foot_html = (f"📨 <b>Forwarded from:</b> {h(name)}\n🔗 <b>Username:</b> {h(un)}\n"
                 f"🆔 <b>Chat ID:</b> <code>{h(str(cid))}</code>\n🕐 {when} IST")
    orig = (caption or "").strip()
    room = 1024 - len(foot_plain) - 120               # HTML escaping makes the footer a bit longer
    cap_html = (h(orig[:max(0, room)]) + "\n\n" if orig else "") + foot_html
    cap_plain = (orig[:max(0, room)] + "\n\n" if orig else "") + foot_plain
    send = {"photo": bot.send_photo, "video": bot.send_video}.get(kind, bot.send_document)
    last = ""
    for cap, pm in ((cap_html, "HTML"), (cap_plain, None)):
        try:
            send(target, file_id, caption=cap[:1024], **({"parse_mode": pm} if pm else {}))
            fb.log_event("media_forwarded", msg_type=kind, source="bot")
            return
        except Exception as e:
            last = str(e)
    print(f"[FORWARD-BOT] {last}")
    fb.log_event("forward_failed", scope="super", actor="system", source="bot", error=last[:200])


def _update_profile(cid: str, user, meta: dict = None):
    """Name / username / photo bookkeeping. Detects a USERNAME CHANGE and tells the admins."""
    uname = _display_name(user)
    un = getattr(user, "username", "") or ""
    meta = meta if meta is not None else get_meta(cid)
    patch = {}
    if meta.get("user_name") != uname:
        patch["user_name"] = uname
    old_un = meta.get("username")
    if old_un != un:
        patch["username"] = un
        if meta.get("user_name") and old_un is not None:        # a known user changed it
            notify_username_change(cid, cid, uname, old_un or "", un, audience="all")
    if meta.get("chat_id") != cid:
        patch["chat_id"] = cid
    if meta.get("unreachable"):
        patch["unreachable"] = False                # they just wrote to us, so we can reach them
    # Only look for a profile photo when we have none, and at most once a day.
    file_id = meta.get("photo_file_id") or ""
    if not file_id and now_ts() - int(meta.get("photo_checked_ts") or 0) > 86400:
        file_id = get_user_photo_file_id(user.id)
        patch["photo_checked_ts"] = now_ts()
        patch["photo_file_id"] = file_id or ""
    if patch:
        update_meta(cid, **patch)
    return uname, un


def record_edit(cid: str, key: str, new_text: str):
    """A user (or the account owner on the phone) edited a message. NOTHING is overwritten
    blindly: the previous text goes to edit_history, the panel shows every version."""
    path = f"support/{cid}/messages/{key}"
    old = fb.get(path)
    if not isinstance(old, dict):
        return None
    old_text = old.get("text") or ""
    if new_text == old_text:
        return None
    fb.post(f"{path}/edit_history", {"text": old_text, "at": now_str()})
    upd = {"text": new_text, "edited": True, "edited_at": now_str()}
    if old.get("type") != "text":
        upd["caption"] = new_text
    fb.patch(path, upd)
    kw = {"scope": "super", "actor": "system"} if account.is_acct(cid) else {}
    fb.log_event("message_edited_by_user" if old.get("from") == "user" else "message_edited",
                 chat_id=cid, msg_id=key, **kw)
    return old_text


def notify_username_change(cid: str, shown_id, name: str, old: str, new: str, audience: str = "all"):
    """Bot users: both admins. P2P users of the personal account: ONLY the super admin."""
    p2p = account.is_acct(cid)
    fb.log_event("username_changed", scope=("super" if p2p else None), actor="system",
                 chat_id=str(cid), old=old or "-", new=new or "-", source=("account" if p2p else "bot"))
    old_h = ("@" + h(old)) if old else "— (none)"
    new_h = ("@" + h(new)) if new else "— (removed)"
    src_h = "👤 <b>Source:</b> P2P chat\n" if p2p else ""
    src_p = "Source: P2P chat\n" if p2p else ""
    html_text = (
        "✏️ <b>Username changed</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>Name:</b> {h(name)}\n"
        f"⬅️ <b>Old:</b> {old_h}\n"
        f"➡️ <b>New:</b> {new_h}\n"
        f"🆔 <b>Chat ID:</b> <code>{h(str(shown_id).lstrip('a') if p2p else str(shown_id))}</code>\n"
        f"{src_h}"
        f"🕐 <b>Time (IST):</b> {now_str()}"
    )
    plain_text = (
        "✏️ Username changed\n"
        f"Name: {name}\nOld: {'@' + old if old else '— (none)'}\nNew: {'@' + new if new else '— (removed)'}\n"
        f"Chat ID: {str(shown_id).lstrip('a') if p2p else shown_id}\n{src_p}Time (IST): {now_str()}"
    )
    fb.bg(_send_to_admins, html_text, plain_text, str(cid), "username_change", _panel_link(str(cid)),
          None, "", "super" if p2p else audience)


def send_msg(cid, text, **kw):
    try:
        bot.send_message(cid, text, parse_mode="HTML", **kw)
    except Exception as e:
        print(f"[SEND] {cid}: {e}")
        fb.log_event("bot_error", chat_id=str(cid), error=str(e))


def _resolve_message_key(cid: str, tg_message_id) -> str:
    """Which Firebase key holds this Telegram message? user msgs: '<id>', ours: 'admin_<id>'."""
    for cand in (str(tg_message_id), f"admin_{tg_message_id}"):
        if fb.get(f"support/{cid}/messages/{cand}/from"):
            return cand
    return ""


# ── Admin commands inside Telegram (/help /send /broadcast /cancel) ──────────
# Only the chat ids listed in SUPER_ADMIN_CHAT_ID / ADMIN_CHAT_ID can use these.
# Everybody else is a normal customer and never sees them.
_state_lock = threading.Lock()
_admin_state = {}      # admin user id -> {"action": "send"|"broadcast", "cid": str, "exp": ts}
_confirms = {}         # token -> {"admin": str, "payload": dict, "exp": ts}
STATE_TTL = 600
_FORMAT_ENTITIES = {"bold", "italic", "underline", "strikethrough", "code", "pre",
                    "text_link", "spoiler", "blockquote", "expandable_blockquote"}

_HELP_TOP = (
    "🛠 <b>Admin help</b>\n"
    "━━━━━━━━━━━━━━━━━━━━━\n"
    "↩️ <b>Quick reply</b>\n"
    "Tap <b>↩️ Reply</b> under a notification (or swipe / reply to it) and send your answer — "
    "text, image, video or file. The customer gets it as a Telegram reply to <i>that</i> message. "
    "Multi-line messages are fine — just press Enter for a new line.\n"
    "😍 <b>React</b> to a notification with any Telegram reaction — it is put on the customer's "
    "message (remove your reaction to remove it there). Both admins are told about it.\n\n"
    "💬 <b>Write to a user</b>\n"
    "<code>/send</code> → I ask for the <b>Chat ID</b>, then for the message / image / video / file.\n"
    "<code>/send chat_id your message</code> → sends the text straight away.\n\n"
    "🤖 <b>AI &amp; blocking (bot users)</b>\n"
    "<code>/stop chat_id ai</code> → AI stops answering this user\n"
    "<code>/on chat_id ai</code> → AI answers this user again\n"
    "<code>/block chat_id</code> → blocked: nothing is answered or sent to him\n"
    "<code>/unblock chat_id</code> → unblock\n\n"
)
_HELP_SUPER = (
    "📢 <b>Broadcast to every user</b>\n"
    "<code>/broadcast your text</code> → sends the text to all users.\n"
    "<code>/broadcast</code> → then send an <b>image / video / file</b> (caption optional).\n\n"
    "🤖 <b>/bot — bot users</b>\n"
    "<code>/bot offai</code> → AI answers nobody · <code>/bot onai</code> → AI answers everybody "
    "(except the ones you switched off)\n"
    "<code>/bot chat_id ignoreai</code> → AI ignores this user · <code>/bot chat_id answerai</code> → answers again\n"
    "<code>/bot killadmin</code> → the admin gets NO notifications · <code>/bot allowadmin</code> → he gets them\n"
    "<code>/bot status</code> → current state\n\n"
    "📱 <b>/account — P2P chats of your linked account</b>\n"
    "<code>/account offai</code> → AI answers none of your P2P chats · <code>/account onai</code> → answers all "
    "(except the ones you switched off)\n"
    "<code>/account chat_id ignoreai</code> · <code>/account chat_id answerai</code>\n"
    "<code>/account status</code>\n\n"
)
_HELP_END = (
    "↩️ <code>/cancel</code> — abort the current action\n"
    "❔ <code>/help</code> — show this help\n\n"
    "💡 The chat_id is shown in every new-message notification (tap it to copy)."
)
_HELP_BROADCAST_NOTE = (
    " HTML such as <code>&lt;b&gt;bold&lt;/b&gt;</code> works in broadcasts. "
    "Every broadcast — including the ones started here — appears in the admin panel with "
    "its successful / failed counts."
)


def admin_help(role: str = "super") -> str:
    """The ADMIN role never sees (or can use) /broadcast, /bot and /account in the bot."""
    if role == "super":
        return _HELP_TOP + _HELP_SUPER + _HELP_END + _HELP_BROADCAST_NOTE
    return _HELP_TOP + _HELP_END


def admin_commands_for(role: str) -> list:
    cmds = [("send", "📤 Send a message to a user"),
            ("stop", "🔇 Stop AI for a user (/stop chat_id ai)"),
            ("on", "🔊 Start AI for a user (/on chat_id ai)"),
            ("block", "🚫 Block a user (/block chat_id)"),
            ("unblock", "✅ Unblock a user (/unblock chat_id)"),
            ("help", "❔ Show help"),
            ("cancel", "❌ Cancel the current action")]
    if role == "super":
        cmds[1:1] = [("broadcast", "📢 Broadcast to all users"),
                     ("bot", "🤖 Bot users: AI + admin alerts"),
                     ("account", "📱 P2P chats: AI control")]
    return cmds


def _menu_markup(role: str):
    mk = types.InlineKeyboardMarkup()
    row = [types.InlineKeyboardButton("📤 Send", callback_data="mn:send")]
    if role == "super":
        row.append(types.InlineKeyboardButton("📢 Broadcast", callback_data="mn:broadcast"))
    mk.row(*row)
    mk.row(types.InlineKeyboardButton("❔ Help", callback_data="mn:help"),
           types.InlineKeyboardButton("❌ Cancel", callback_data="mn:cancel"))
    return mk


def set_admin_menus():
    """Telegram's "Menu" button: every admin chat gets its own command list."""
    if not bot:
        return
    for admin_id in config.ADMIN_CHAT_IDS():
        role = config.role_of_chat(admin_id)
        try:
            cmds = [types.BotCommand(c, d) for c, d in admin_commands_for(role)]
            bot.set_my_commands(cmds, scope=types.BotCommandScopeChat(int(admin_id)))
            try:
                bot.set_chat_menu_button(int(admin_id), types.MenuButtonCommands())
            except Exception:
                pass
        except Exception as e:
            print(f"[MENU] {admin_id}: {e}")


USER_HELP = (
    "👋 <b>How to use this bot</b>\n"
    "Just write your question here — you can also send photos, videos or files.\n"
    "Our team will reply to you in this chat as soon as possible."
)


def is_admin(user_id=None, chat_id=None) -> bool:
    ids = config.ADMIN_CHAT_IDS()
    return (user_id is not None and str(user_id) in ids) or (chat_id is not None and str(chat_id) in ids)


def _set_state(aid, **kw):
    with _state_lock:
        _admin_state[str(aid)] = {**kw, "exp": time.time() + STATE_TTL}


def _get_state(aid):
    with _state_lock:
        st = _admin_state.get(str(aid))
        if st and st["exp"] < time.time():
            _admin_state.pop(str(aid), None)
            return None
        return dict(st) if st else None


def _clear_state(aid):
    with _state_lock:
        _admin_state.pop(str(aid), None)


def _extract_media(msg):
    """(kind, file_id, file_name, caption) for photo / video / document messages."""
    cap = msg.caption or ""
    if msg.content_type == "photo":
        return ("photo", msg.photo[-1].file_id, "", cap)
    if msg.content_type == "video":
        return ("video", msg.video.file_id, getattr(msg.video, "file_name", "") or "video", cap)
    if msg.content_type == "document":
        return ("document", msg.document.file_id, msg.document.file_name or "file", cap)
    return None


def _admin_text_and_format(msg, caption: bool = False):
    """Text the admin wrote + how to send it. If the admin used Telegram's own
    formatting (bold/italic/…) it is converted to HTML; otherwise typed HTML tags work."""
    import broadcast as bc
    ents = (msg.caption_entities if caption else msg.entities) or []
    raw = (msg.caption if caption else msg.text) or ""
    if any(e.type in _FORMAT_ENTITIES for e in ents):
        html_v = (msg.html_caption if caption else msg.html_text) or raw
        return html_v, "html"
    return raw, bc.detect_format(raw)


def _notification_target(msg):
    """(chat_id, message_id) when the admin replied to one of OUR notifications, else None.
    The notification text carries 'Chat ID: …' and (for new messages) 'Msg ID: …'."""
    rt = getattr(msg, "reply_to_message", None)
    if not rt or not getattr(getattr(rt, "from_user", None), "is_bot", False):
        return None
    src = (getattr(rt, "text", None) or getattr(rt, "caption", None) or "")
    m = re.search(r"Chat ID:\s*(-?\d+)", src)
    if not m:
        return None
    mm = re.search(r"Msg ID:\s*(\d+)", src)
    cid = ("a" + m.group(1)) if re.search(r"Source:\s*P2P", src) else m.group(1)
    return cid, (mm.group(1) if mm else "")


def _as_admin(fn):
    """Run an admin handler with the right actor, so everything it logs / stores is
    attributed to the super admin or the admin who triggered it."""
    @functools.wraps(fn)
    def wrapper(obj, *a, **kw):
        uid = getattr(getattr(obj, "from_user", None), "id", None)
        with fb.acting_as(config.role_of_chat(uid) or None):
            return fn(obj, *a, **kw)
    return wrapper


def _target_chat(cid: str):
    """(meta, error_html). error_html is '' when the chat can be written to."""
    if account.is_acct(cid) and fb.get_actor() != "super":       # the admin never sees P2P chats
        return None, (f"❌ No chat with ID <code>{h(cid)}</code>. "
                      "Copy the ID from a notification or from the admin panel.")
    meta = fb.chat_index().get(cid)
    if not isinstance(meta, dict) or not meta:
        meta = fb.get(f"support/{cid}/meta")
    if not isinstance(meta, dict) or not meta:
        return None, (f"❌ No chat with ID <code>{h(cid)}</code>. "
                      "Copy the ID from a notification or from the admin panel.")
    if meta.get("blocked"):
        return meta, "🚫 This user is blocked. Unblock them with /unblock chat_id or in the admin panel."
    return meta, ""


def _admin_deliver(reply_to, cid: str, text: str = "", media=None, reply_key: str = ""):
    meta, err = _target_chat(cid)
    if err:
        send_msg(reply_to, err)
        return
    name = meta.get("user_name") or cid
    if media:
        kind, fid, fname, cap = media
        res = admin_send_media_by_id(cid, kind, fid, fname, cap, reply_to=reply_key)
    else:
        res = admin_reply(cid, text, reply_to=reply_key)
    if res.get("ok"):
        send_msg(reply_to, f"✅ Sent to <b>{h(name)}</b> (<code>{h(cid)}</code>)")
    else:
        send_msg(reply_to, f"❌ Could not send: {h(str(res.get('error', 'unknown error')))}")


def _send_payload(chat_id, payload: dict):
    """Send a broadcast payload to ONE chat (used for the admin's preview)."""
    kind, text, fmt = payload["kind"], payload.get("text", ""), payload["format"]
    kw = {"parse_mode": "HTML"} if fmt == "html" else {}
    if kind == "text":
        return bot.send_message(chat_id, text, **kw)
    cap = text or None
    fn = bot.send_photo if kind == "photo" else bot.send_video if kind == "video" else bot.send_document
    return fn(chat_id, payload["file_id"], caption=cap, **kw)


def _launch_broadcast(reply_to, payload: dict):
    import broadcast as bc
    res = bc.start_send(
        payload.get("text", ""), payload["format"], kind=payload["kind"],
        file_id=payload.get("file_id", ""), file_name=payload.get("file_name", ""),
        source="bot", notify_admin=str(reply_to),
    )
    if res.get("ok"):
        send_msg(reply_to, f"🚀 Broadcast started for <b>{res['total']}</b> users.\n"
                           "I'll message you the result when it's done — it is also tracked live in the admin panel.")
    else:
        send_msg(reply_to, f"❌ {h(str(res.get('error', 'Could not start the broadcast')))}")


def _admin_broadcast_flow(reply_to, aid, kind, text, fmt, file_id="", file_name=""):
    import broadcast as bc
    err = bc.validate(text, fmt, kind)
    if err:
        send_msg(reply_to, f"❌ {h(err)}")
        return
    n = len(_all_user_chat_ids())
    if not n:
        send_msg(reply_to, "❌ No recipients yet — nobody has messaged the bot.")
        return
    payload = {"kind": kind, "text": text, "format": fmt, "file_id": file_id, "file_name": file_name}
    if not settings.load()["broadcast_confirm"]:
        _launch_broadcast(reply_to, payload)
        return
    try:
        _send_payload(reply_to, payload)          # preview exactly as users will see it
    except Exception as e:
        send_msg(reply_to, f"❌ Telegram rejected this message: {h(str(e))}")
        return
    token = secrets.token_hex(4)
    with _state_lock:
        _confirms[token] = {"admin": str(aid), "payload": payload, "exp": time.time() + STATE_TTL}
    mk = types.InlineKeyboardMarkup()
    mk.row(types.InlineKeyboardButton(f"✅ Send to {n} users", callback_data=f"bc:y:{token}"),
           types.InlineKeyboardButton("❌ Cancel", callback_data=f"bc:n:{token}"))
    bot.send_message(reply_to, f"📢 This is how your broadcast will look.\nSend it to <b>{n}</b> users?",
                     parse_mode="HTML", reply_markup=mk)


def _ask_message_text(meta, cid: str) -> str:
    return (f"✍️ Now send your <b>message, image, video or file</b> for "
            f"<b>{h((meta or {}).get('user_name') or cid)}</b> (<code>{h(cid)}</code>).\n"
            "Add a caption if you like. /cancel to abort.")


def _start_send_flow(reply, aid):
    """/send (or the Send menu button): ask for the chat id first."""
    _set_state(aid, action="send_cid")
    send_msg(reply, "📤 <b>Send a message</b>\n"
                    "Send me the <b>Chat ID</b> of the customer (copy it from a notification "
                    "or the admin panel).\n/cancel to abort.")


def _start_broadcast_flow(reply, aid):
    _set_state(aid, action="broadcast")
    send_msg(reply, "📎 Send the <b>image, video or file</b> you want to broadcast "
                    "(caption optional) — or just type the text. /cancel to abort.")


_UNKNOWN = "Unknown command. Send /help to see what you can do."


def _chat_arg(group: str, raw: str):
    """/bot|/account <chat_id> -> (chat key, error). Bot chats: 123456 · P2P chats: a123456."""
    m = re.fullmatch(r"a?(-?\d+)", (raw or "").strip().lower())
    if not m:
        return None, "That doesn't look like a Chat ID."
    cid = ("a" + m.group(1)) if group == "account" else m.group(1)
    meta = get_meta(cid, fresh=True)
    if not meta:
        return None, (f"❌ No {'P2P' if group == 'account' else 'bot'} chat with ID "
                      f"<code>{h(m.group(1))}</code>.")
    return cid, ""


def _yn(v) -> str:
    return "ON ✅" if v else "OFF ⛔"


def _group_command(role: str, reply, group: str, rest: str):
    """/bot … and /account … (super admin only)."""
    if role != "super":
        send_msg(reply, _UNKNOWN)
        return
    toks = rest.split()
    flag = "ai_bot_enabled" if group == "bot" else "ai_account_enabled"
    label = "bot users" if group == "bot" else "P2P chats"
    s = settings.load()
    t0 = toks[0].lower() if toks else "status"
    try:
        if t0 == "status":
            lines = [f"🤖 <b>/{group}</b> — {label}",
                     f"AI replies: {_yn(s.get(flag))}",
                     f"AI keys: {len(ai.get_keys())}"]
            if group == "bot":
                lines.append(f"Admin gets notifications: {_yn(s.get('notify_admin_role', True))}")
            else:
                lines.append(f"Personal account linked: {_yn(account.linked())}")
            send_msg(reply, "\n".join(lines))
        elif t0 in ("offai", "onai"):
            settings.save({flag: t0 == "onai"})
            send_msg(reply, f"🤖 AI replies for {label}: <b>{'ON' if t0 == 'onai' else 'OFF'}</b>" +
                     ("\n(except the chats you switched off)" if t0 == "onai" else ""))
        elif group == "bot" and t0 in ("killadmin", "allowadmin"):
            settings.save({"notify_admin_role": t0 == "allowadmin"})
            send_msg(reply, "🔔 The admin " + ("now receives notifications." if t0 == "allowadmin"
                                              else "will NOT receive any notification anymore."))
        elif len(toks) >= 2 and toks[1].lower() in ("ignoreai", "answerai"):
            cid, err = _chat_arg(group, toks[0])
            if err:
                send_msg(reply, err)
                return
            off = toks[1].lower() == "ignoreai"
            res = set_chat_ai(cid, off)
            shown = cid.lstrip("a") if group == "account" else cid
            send_msg(reply, res.get("error") or
                     (f"🔇 AI will <b>ignore</b> <code>{h(shown)}</code>." if off
                      else f"🔊 AI will <b>answer</b> <code>{h(shown)}</code>."))
        else:
            send_msg(reply, f"Usage:\n<code>/{group} offai</code> · <code>/{group} onai</code>\n"
                            f"<code>/{group} chat_id ignoreai</code> · <code>/{group} chat_id answerai</code>\n"
                            + ("<code>/bot killadmin</code> · <code>/bot allowadmin</code>\n" if group == "bot" else "")
                            + f"<code>/{group} status</code>")
    except ValueError as e:
        send_msg(reply, f"❌ {h(str(e))}")


def _user_command(reply, cmd: str, rest: str):
    """/stop <id> ai · /on <id> ai · /block <id> · /unblock <id> — bot users, both admin roles."""
    toks = rest.split()
    if not toks:
        send_msg(reply, {"stop": "Usage: <code>/stop chat_id ai</code>", "on": "Usage: <code>/on chat_id ai</code>",
                         "block": "Usage: <code>/block chat_id</code>",
                         "unblock": "Usage: <code>/unblock chat_id</code>"}[cmd])
        return
    cid, err = _chat_arg("bot", toks[0])
    if err:
        send_msg(reply, err)
        return
    if cmd in ("stop", "on"):
        if len(toks) < 2 or toks[1].lower() != "ai":
            send_msg(reply, f"Usage: <code>/{cmd} chat_id ai</code>")
            return
        res = set_chat_ai(cid, cmd == "stop")
        send_msg(reply, res.get("error") or (f"🔇 AI will <b>ignore</b> <code>{h(cid)}</code>." if cmd == "stop"
                                             else f"🔊 AI will <b>answer</b> <code>{h(cid)}</code>."))
    elif cmd == "block":
        block_user(cid)
        send_msg(reply, f"🚫 <code>{h(cid)}</code> is blocked — nothing is answered or sent to him.")
    else:
        unblock_user(cid)
        send_msg(reply, f"✅ <code>{h(cid)}</code> is unblocked.")


_unsupported_seen = {}


# ── Bot handlers ──────────────────────────────────────────────────────────────
if bot:

    # ── Admin-only handlers: registered FIRST so they win over customer handlers ──
    def _admin_only(msg):
        u = getattr(msg, "from_user", None)
        return getattr(msg.chat, "type", "private") == "private" and is_admin(getattr(u, "id", None), msg.chat.id)

    @bot.message_handler(commands=["start", "help", "send", "broadcast", "cancel", "bot", "account",
                                   "stop", "on", "block", "unblock"], func=_admin_only)
    @_as_admin
    def admin_commands(msg):
        import broadcast as bc
        aid, reply = str(msg.from_user.id), msg.chat.id
        text = msg.text or ""
        pieces = text.split(None, 1)
        cmd = pieces[0].lstrip("/").split("@")[0].lower()
        rest = pieces[1] if len(pieces) > 1 else ""

        role = config.role_of_chat(msg.from_user.id) or config.role_of_chat(reply)
        if cmd in ("start", "help"):
            _clear_state(aid)
            bot.send_message(reply, admin_help(role), parse_mode="HTML", reply_markup=_menu_markup(role))
        elif cmd == "cancel":
            had = _get_state(aid) is not None
            _clear_state(aid)
            send_msg(reply, "↩️ Cancelled." if had else "Nothing to cancel.")
        elif cmd == "send":
            parts = rest.strip().split(None, 1)
            if not parts:
                _start_send_flow(reply, aid)                  # menu / bare /send: ask for the chat id
                return
            if not re.fullmatch(r"-?\d+", parts[0]):
                send_msg(reply, "That doesn't look like a Chat ID. Send /send and I'll ask for it, or use:\n"
                                "<code>/send chat_id your message</code>")
                return
            cid = parts[0]
            body = parts[1].strip() if len(parts) > 1 else ""
            meta, err = _target_chat(cid)
            if err:
                send_msg(reply, err)
                return
            if body:
                _clear_state(aid)
                _admin_deliver(reply, cid, text=body)
            else:
                _set_state(aid, action="send", cid=cid)
                send_msg(reply, _ask_message_text(meta, cid))
        elif cmd in ("bot", "account"):
            _group_command(role, reply, cmd, rest)
        elif cmd in ("stop", "on", "block", "unblock"):
            _user_command(reply, cmd, rest)
        elif cmd == "broadcast":
            if role != "super":                               # hidden for the ADMIN role
                send_msg(reply, "Unknown command. Send /help to see what you can do.")
                return
            body = rest.strip()
            if body:
                _clear_state(aid)
                _admin_broadcast_flow(reply, aid, "text", body, bc.detect_format(body))
            else:
                _start_broadcast_flow(reply, aid)

    @bot.message_handler(func=_admin_only, content_types=["text", "photo", "video", "document"])
    @_as_admin
    def admin_content(msg):
        aid, reply = str(msg.from_user.id), msg.chat.id
        if msg.content_type == "text" and (msg.text or "").startswith("/"):
            send_msg(reply, "Unknown command. Send /help to see what you can do.")
            return
        st = _get_state(aid)
        if not st:
            # No command in progress: a reply to a notification answers that customer
            target = _notification_target(msg)
            if target:
                cid, key = target
                media = _extract_media(msg)
                if media:
                    _admin_deliver(reply, cid, media=media, reply_key=key)
                elif msg.content_type == "text":
                    _admin_deliver(reply, cid, text=msg.text or "", reply_key=key)
                return
            send_msg(reply, "ℹ️ Nothing to do right now. Send /help to see the admin commands, "
                            "or reply to a customer notification to answer it.")
            return
        if st["action"] == "send_cid":
            # step 1 of /send: the admin answers with the customer's chat id
            txt = (msg.text or "").strip() if msg.content_type == "text" else ""
            m_ = re.fullmatch(r"-?\d+", txt) or re.search(r"Chat ID:\s*(-?\d+)", txt)
            if not m_:
                _set_state(aid, action="send_cid")
                send_msg(reply, "❌ Please send only the numeric <b>Chat ID</b> (or /cancel).")
                return
            cid = m_.group(1) if m_.lastindex else m_.group(0)
            meta, err = _target_chat(cid)
            if err:
                _set_state(aid, action="send_cid")
                send_msg(reply, err + "\nSend another Chat ID or /cancel.")
                return
            _set_state(aid, action="send", cid=cid)
            send_msg(reply, _ask_message_text(meta, cid))
            return
        _clear_state(aid)
        media = _extract_media(msg)
        if st["action"] == "send":
            rkey = st.get("reply_key", "")
            if media:
                kind, fid, fname, cap = media
                _admin_deliver(reply, st["cid"], media=(kind, fid, fname, cap), reply_key=rkey)
            else:
                _admin_deliver(reply, st["cid"], text=msg.text or "", reply_key=rkey)
        elif st["action"] == "broadcast":
            if config.role_of_chat(msg.from_user.id) != "super":
                return
            if media:
                kind, fid, fname, _cap = media
                cap, fmt = _admin_text_and_format(msg, caption=True)
                _admin_broadcast_flow(reply, aid, kind, cap, fmt, fid, fname)
            else:
                txt, fmt = _admin_text_and_format(msg)
                _admin_broadcast_flow(reply, aid, "text", txt, fmt)

    @bot.callback_query_handler(func=lambda c: (c.data or "").startswith("rp:"))
    @_as_admin
    def on_reply_button(call):
        """"↩️ Reply" under a notification: ask for the message, then send it as a reply."""
        try:
            if not is_admin(call.from_user.id):
                bot.answer_callback_query(call.id, "Not allowed", show_alert=True)
                return
            _, cid, key = call.data.split(":", 2)
            key = "" if key in ("", "0") else key
            meta, err = _target_chat(cid)
            if err:
                bot.answer_callback_query(call.id)
                send_msg(call.message.chat.id, err)
                return
            aid = str(call.from_user.id)
            _set_state(aid, action="send", cid=cid, reply_key=key)
            bot.answer_callback_query(call.id, "Type your reply now")
            bot.send_message(call.message.chat.id, _ask_message_text(meta, cid), parse_mode="HTML",
                             **_reply_kw(call.message.message_id))
        except Exception as e:
            print(f"[REPLY-BTN] {e}")

    @bot.callback_query_handler(func=lambda c: (c.data or "").startswith("mn:"))
    @_as_admin
    def on_menu_button(call):
        try:
            if not is_admin(call.from_user.id):
                bot.answer_callback_query(call.id, "Not allowed", show_alert=True)
                return
            aid, chat_id = str(call.from_user.id), call.message.chat.id
            role = config.role_of_chat(call.from_user.id)
            action = call.data.split(":", 1)[1]
            bot.answer_callback_query(call.id)
            if action == "send":
                _start_send_flow(chat_id, aid)
            elif action == "broadcast":
                if role == "super":
                    _start_broadcast_flow(chat_id, aid)
                else:                                          # hidden for the ADMIN role
                    send_msg(chat_id, "Unknown command. Send /help to see what you can do.")
            elif action == "cancel":
                had = _get_state(aid) is not None
                _clear_state(aid)
                send_msg(chat_id, "↩️ Cancelled." if had else "Nothing to cancel.")
            else:
                _clear_state(aid)
                bot.send_message(chat_id, admin_help(role), parse_mode="HTML", reply_markup=_menu_markup(role))
        except Exception as e:
            print(f"[MENU-BTN] {e}")

    @bot.callback_query_handler(func=lambda c: (c.data or "").startswith("bc:"))
    @_as_admin
    def on_broadcast_confirm(call):
        try:
            if config.role_of_chat(call.from_user.id) != "super":
                bot.answer_callback_query(call.id, "Not allowed", show_alert=True)
                return
            _, ans, token = call.data.split(":", 2)
            with _state_lock:
                rec = _confirms.pop(token, None)
            chat_id = call.message.chat.id
            try:
                bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
            except Exception:
                pass
            if not rec or rec["exp"] < time.time() or rec["admin"] != str(call.from_user.id):
                bot.answer_callback_query(call.id, "This request expired — send /broadcast again.", show_alert=True)
                return
            bot.answer_callback_query(call.id)
            if ans == "y":
                _launch_broadcast(chat_id, rec["payload"])
            else:
                send_msg(chat_id, "❎ Broadcast cancelled.")
        except Exception as e:
            print(f"[BC-CONFIRM] {e}")

    # ── Customer handlers ──
    @bot.message_handler(commands=["help"])
    def cmd_help_user(msg):
        send_msg(msg.chat.id, USER_HELP)

    @bot.message_handler(content_types=["voice", "audio", "sticker", "animation", "video_note",
                                        "location", "contact"])
    def handle_unsupported(msg):
        cid = str(msg.chat.id)
        if _is_blocked(cid) or time.time() - _unsupported_seen.get(cid, 0) < 30:
            return
        _unsupported_seen[cid] = time.time()
        send_msg(cid, "ℹ️ Sorry, I can only receive text, photos, videos and files here.")

    @bot.message_handler(commands=["start"])
    def cmd_start(msg):
        cid = str(msg.chat.id)
        if _is_blocked(cid):
            return                                   # blocked users get no answer at all

        welcome = get_welcome_message()
        fmt = get_welcome_format()
        try:
            send_formatted(cid, welcome, fmt)         # the user gets the welcome FIRST
        except Exception as e:
            # Broken HTML -> strip tags and send plain text so the user still gets it
            try:
                bot.send_message(cid, html_to_plain(welcome))
            except Exception:
                print(f"[WELCOME] {e}")
            fb.log_event("bot_error", chat_id=cid, error=f"welcome: {e}")

        def after():
            uname, un = _update_profile(cid, msg.from_user)
            meta = get_meta(cid)
            update_meta(cid, started_at=meta.get("started_at", now_str()), blocked=False)
            if not meta.get("unread"):
                update_meta(cid, unread=0)
            fb.log_event("chat_started", chat_id=cid, user_name=uname, username=un)
        fb.bg(after)

    def _spam_gate(cid: str, uname: str, text: str, msg_date=None) -> bool:
        # Messages that were queued while the bot was offline arrive in a burst
        # on restart — never treat that backlog as spam.
        if msg_date and time.time() - int(msg_date) > 90:
            return False
        result = antispam.check(cid, text)
        if not result["blocked"]:
            return False

        fb.log_event(
            "spam_detected", chat_id=cid, user_name=uname,
            reason=result["reason"], auto_block=result["auto_block"],
        )

        if result["auto_block"]:
            update_meta(cid, blocked=True, blocked_reason="auto: spam")
            try:
                bot.send_message(cid, result["warn"])
            except Exception:
                pass
            fb.log_event("auto_blocked", chat_id=cid, user_name=uname, reason="spam")
        elif result["warn"]:
            try:
                bot.send_message(cid, result["warn"])
            except Exception:
                pass
        return True

    def _maybe_auto_reply(cid: str):
        if not config.AUTO_REPLY:
            return
        cooldown = config.AUTO_REPLY_COOLDOWN_MINUTES
        if cooldown > 0:
            last = get_meta(cid).get("last_auto_reply_ts") or 0
            if now_ts() - last < cooldown * 60:
                return
        try:
            bot.send_message(cid, h(config.AUTO_REPLY), parse_mode="HTML")     # no artificial delay
            fb.bg(update_meta, cid, last_auto_reply_ts=now_ts())
        except Exception as e:
            print(f"[AUTO_REPLY] {cid}: {e}")

    def _typing(cid: str):
        try:
            bot.send_chat_action(cid, "typing")
        except Exception:
            pass

    def _ai_reply(cid: str, uname: str, un: str, text: str, mid: str = "", label: str = "") -> bool:
        """AI answer for a bot user. False -> nothing was sent (caller falls back to auto-reply).
        The AI always gets the last 10 messages of this chat (both sides, incl. human replies)."""
        try:
            hist = ai.context(cid, exclude=mid)              # the 10 messages BEFORE this one
            ai.remember(cid, "user", text or label)          # remembered even when the AI is off
            s = settings.load()
            if not s.get("ai_bot_enabled", True) or not (text or "").strip():
                return False
            meta = get_meta(cid)
            if meta.get("ai_off") or meta.get("blocked") or not ai.available():
                return False
            fb.bg(_typing, cid)
            answer = ai.ask(text, s.get("ai_prompt_bot") or "", history=hist,
                            context=f"Customer name: {uname}")
            if not answer:
                return False
            m = bot.send_message(cid, answer)                    # plain text: no markup surprises
            ai.remember(cid, "assistant", answer)
            fb.bg(_persist_ai_answer, cid, uname, un, text, answer, m.message_id)
            return True
        except Exception as e:
            print(f"[AI] {cid}: {e}")
            fb.log_event("bot_error", chat_id=cid, error=f"ai: {e}")
            return False

    def _persist_ai_answer(cid, uname, un, question, answer, tg_id):
        data = {
            "msg_id": f"admin_{tg_id}", "chat_id": cid, "text": answer, "type": "text",
            "from": "admin", "sent_by": "ai", "ai": True,
            "time": now_str(), "ts": now_ts(),
            "read": False, "delivered": True, "edited": False, "deleted": False,
        }
        store_message(cid, f"admin_{tg_id}", data)
        ai.record("bot", cid, cid, uname, un, question, answer)
        fb.log_event("ai_answered", chat_id=cid, source="bot")

    def _respond(cid: str, uname: str, un: str, text: str, mid: str = "", label: str = ""):
        """What the USER sees comes first and never waits for storage / notifications."""
        if not _ai_reply(cid, uname, un, text, mid, label):
            _maybe_auto_reply(cid)

    def _persist_incoming(msg, cid, mid, data, kind, notify_text, file_id="", file_name=""):
        """Everything the user does not wait for: profile, storage, log, read marks, admin ping."""
        try:
            if _is_old(msg, 5) and _already_stored(cid, mid):
                return                               # the previous instance already stored, notified, forwarded
            uname, un = _update_profile(cid, msg.from_user)
            data["user_name"], data["username"] = uname, un
            rinfo = _incoming_reply(cid, msg)
            if rinfo:
                data["reply_to"] = rinfo
            store_message(cid, mid, data)
            fb.log_event("message_in", chat_id=cid, user_name=uname, msg_type=kind)
            mark_admin_msgs_seen(cid)
            if kind in ("photo", "video", "document") and file_id:
                fb.bg(forward_bot_media, cid, kind, file_id, data.get("caption", ""), uname, un,
                      getattr(msg, "date", None))
            send_admin_notify(cid, uname, un, notify_text, kind, file_id=file_id, file_name=file_name,
                              msg_id=mid, reply_text=(rinfo or {}).get("text", ""))
        except Exception as e:
            print(f"[PERSIST] {e}")
            fb.log_event("bot_error", chat_id=cid, error=f"persist: {e}")

    @bot.message_handler(content_types=["text"])
    def handle_text(msg):
        cid = str(msg.chat.id)
        mid = str(msg.message_id)

        if _is_blocked(cid):
            return                                   # /block: no reply, nothing stored
        if _already_seen(cid, mid) or (_is_old(msg) and _already_stored(cid, mid)):
            return                                   # redelivered after a redeploy: already handled
        settings.load()                              # cached (refreshed in the background)
        uname = _display_name(msg.from_user)
        un = getattr(msg.from_user, "username", "") or ""

        if _spam_gate(cid, uname, msg.text or "", getattr(msg, "date", None)):
            return

        data = {
            "msg_id": mid, "chat_id": cid,
            "text": msg.text, "type": "text",
            "from": "user", "time": now_str(), "ts": now_ts(),
            "read": False, "delivered": True, "edited": False, "deleted": False,
        }
        fb.bg(_persist_incoming, msg, cid, mid, data, "text", msg.text)      # in parallel …
        _respond(cid, uname, un, msg.text or "", mid)                         # … with the answer

    def _handle_media(msg, kind: str):
        cid = str(msg.chat.id)
        mid = str(msg.message_id)

        if _is_blocked(cid):
            return
        if _already_seen(cid, mid) or (_is_old(msg) and _already_stored(cid, mid)):
            return                                   # redelivered after a redeploy: already handled
        settings.load()
        uname = _display_name(msg.from_user)
        un = getattr(msg.from_user, "username", "") or ""
        caption = msg.caption or ""

        if _spam_gate(cid, uname, caption or f"[{kind}]", getattr(msg, "date", None)):
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
            "text": caption, "caption": caption,
            "file_id": file_id,
            "type": kind, "from": "user",
            "time": now_str(), "ts": now_ts(),
            "read": False, "delivered": True, "edited": False, "deleted": False,
        }
        if file_name:
            data["file_name"] = file_name
        fb.bg(_persist_incoming, msg, cid, mid, data, kind, caption or f"[{kind}]",
              file_id, file_name or "")
        _respond(cid, uname, un, caption, mid, f"[{kind}]")

    @bot.message_handler(content_types=["photo"])
    def handle_photo(msg):
        _handle_media(msg, "photo")

    @bot.message_handler(content_types=["video"])
    def handle_video(msg):
        _handle_media(msg, "video")

    @bot.message_handler(content_types=["document"])
    def handle_document(msg):
        _handle_media(msg, "document")

    # ── User edited one of their messages in Telegram ────────────────────────
    @bot.edited_message_handler(content_types=["text", "photo", "video", "document"])
    def handle_edited(msg):
        try:
            cid = str(msg.chat.id)
            mid = str(msg.message_id)
            new_text = (msg.text if msg.content_type == "text" else msg.caption) or ""
            old_text = record_edit(cid, mid, new_text)      # keeps EVERY version
            if old_text is None:
                return
            uname = _display_name(msg.from_user)
            send_admin_edit_notify(cid, uname, getattr(msg.from_user, "username", "") or "",
                                   old_text, new_text)
        except Exception as e:
            print(f"[EDITED] {e}")
            fb.log_event("bot_error", error=f"edited handler: {e}")

    # ── Admin reacted to a notification -> reaction on the customer's message ──
    def _admin_reacted_to_notification(upd):
        rec = fb.get(f"admin_notify/{upd.chat.id}/{upd.message_id}")
        if not isinstance(rec, dict) or not rec.get("cid") or not rec.get("mid"):
            return                                   # not one of our new-message notifications
        emojis = [getattr(r, "emoji", None) for r in (upd.new_reaction or [])]
        emojis = [e for e in emojis if e]            # custom / paid emoji can't be mirrored
        res = set_reaction(rec["cid"], rec["mid"], emojis[0] if emojis else "",
                           remove=not emojis, validate=False, toggle=False)
        if res.get("error"):
            send_msg(upd.chat.id, f"❌ Could not react: {h(str(res['error']))}")

    # ── User reacted to a message in Telegram ────────────────────────────────
    def handle_reaction(upd):
        try:
            if getattr(upd, "user", None) is None:      # anonymous / channel
                return
            admin_role = config.role_of_chat(getattr(upd.user, "id", None))
            if admin_role and str(upd.chat.id) == str(upd.user.id):
                with fb.acting_as(admin_role):
                    _admin_reacted_to_notification(upd)
                return
            cid = str(upd.chat.id)
            key = _resolve_message_key(cid, upd.message_id)
            if not key:
                return
            path = f"support/{cid}/messages/{key}"
            msg = fb.get(path) or {}
            view = reaction_view(msg.get("reactions"))

            new = []
            for r in (upd.new_reaction or []):
                e = getattr(r, "emoji", None)
                new.append(canon_emoji(e) if e else "⭐")     # custom/paid emoji -> star
            added = [e for e in new if e not in view["user"]]
            view["user"] = new
            fb.put(f"{path}/reactions", _reaction_store(view))
            fb.log_event("message_reaction", chat_id=cid, msg_id=key, by="user",
                         emoji=" ".join(new) or "(removed)")

            if added:
                uname = _display_name(upd.user)
                un = getattr(upd.user, "username", "") or ""
                send_admin_reaction_notify(cid, uname, un, added,
                                           msg.get("text") or msg.get("caption"),
                                           msg.get("from"))
        except Exception as e:
            print(f"[REACTION] {e}")
            fb.log_event("bot_error", error=f"reaction handler: {e}")


    # older pyTelegramBotAPI versions have no reaction updates — don't crash the whole app then
    if hasattr(bot, "message_reaction_handler"):
        bot.message_reaction_handler(func=lambda r: True)(handle_reaction)
    else:
        print("[BOT] pyTelegramBotAPI too old for message_reaction updates — upgrade to >=4.22")


# ── Admin actions ─────────────────────────────────────────────────────────────
def set_chat_ai(chat_id: str, off: bool) -> dict:
    """Per-chat AI switch (panel toggle, /stop /on, /bot|/account <id> ignoreai|answerai)."""
    meta = get_meta(chat_id, fresh=True)
    if not meta:
        return {"error": "Chat not found"}
    update_meta(chat_id, ai_off=bool(off))
    fb.log_event("ai_chat_off" if off else "ai_chat_on", chat_id=chat_id,
                 **({"scope": "super", "actor": "system"} if account.is_acct(chat_id) else {}))
    return {"ok": True, "ai_off": bool(off)}


def _sender() -> str:
    """Role of whoever is sending right now ('super' | 'admin'); stored on every outgoing message."""
    return fb.get_actor() or "admin"


def _acct_reply_ctx(chat_id: str, reply_key: str):
    """(telegram message id to reply to, stored reply info) for a P2P chat."""
    if not reply_key:
        return None, None
    target = fb.get(f"support/{chat_id}/messages/{reply_key}")
    if not isinstance(target, dict) or target.get("deleted"):
        return None, None
    info = {"msg_id": reply_key, "from": target.get("from", "user"),
            "type": target.get("type", "text"), "text": _snippet(target)}
    tid = target.get("tg_id") or _tg_id(reply_key) or (int(reply_key) if str(reply_key).isdigit() else None)
    return tid, info


def _acct_store(chat_id: str, res: dict, text: str, ftype="text", fname="", rinfo=None) -> dict:
    ai.remember(chat_id, "assistant", text or f"[{ftype}]")
    data = account._out_record(chat_id, res, text, sent_by=_sender(), ftype=ftype, fname=fname, rinfo=rinfo)
    store_message(chat_id, data["msg_id"], data, extra={f"acct_idx/{res['id']}": chat_id})
    update_meta(chat_id, unread=0)
    fb.log_event("message_out", scope="super", chat_id=chat_id, msg_type=ftype, source="account")
    return {"ok": True, "mid": data["msg_id"], "message": data, "type": ftype}


def _acct_reply(chat_id: str, text: str, reply_to: str = "") -> dict:
    try:
        tid, rinfo = _acct_reply_ctx(chat_id, reply_to)
        res = account.send_text(account.uid_of(chat_id), text, reply_to_id=tid)
        return _acct_store(chat_id, res, text, rinfo=rinfo)
    except account.AccountError as e:
        return {"error": str(e)}
    except Exception as e:
        fb.log_event("bot_error", scope="super", actor="system", chat_id=chat_id, error=str(e))
        return {"error": str(e)}


def admin_reply(chat_id: str, text: str, reply_to: str = "") -> dict:
    """Send a text to a customer. `reply_to` = stored key of the message to reply to:
    the customer then sees a real Telegram reply."""
    if account.is_acct(chat_id):
        return _acct_reply(chat_id, text, reply_to)
    if not bot:
        return {"error": "Bot is not configured (BOT_TOKEN missing)"}
    try:
        kw, rinfo = _reply_ctx(chat_id, reply_to)
        m = bot.send_message(chat_id, h(text), parse_mode="HTML", **kw)
        mid = str(m.message_id)
        data = {
            "msg_id": f"admin_{mid}", "chat_id": chat_id,
            "text": text, "type": "text", "from": "admin", "sent_by": _sender(),
            "time": now_str(), "ts": now_ts(),
            "read": False, "delivered": True, "edited": False, "deleted": False,
        }
        if rinfo:
            data["reply_to"] = rinfo
        ai.remember(chat_id, "assistant", text)
        saved = fb.put(f"support/{chat_id}/messages/admin_{mid}", data)
        fb.patch(
            f"support/{chat_id}/meta",
            {"last_message": text, "last_time": now_str(), "last_ts": now_ts(), "unread": 0},
        )
        fb.log_event("message_out", chat_id=chat_id, msg_type="text")
        res = {"ok": True, "mid": f"admin_{mid}", "message": data}
        if saved is None:
            res["warning"] = "Delivered on Telegram but could not be saved to Firebase"
        return res
    except Exception as e:
        fb.log_event("bot_error", chat_id=chat_id, error=str(e))
        return {"error": str(e)}


def upload_kind(mime: str, size: int) -> str:
    """How a panel upload is sent: photo only for real JPEG/PNG up to 10 MB (GIFs and
    big images would be mangled or refused as photos), video/* as video, rest as file."""
    mime = (mime or "").lower()
    if mime in ("image/jpeg", "image/jpg", "image/png") and size <= 10 * 1024 * 1024:
        return "photo"
    if mime.startswith("video/"):
        return "video"
    return "document"


def _store_admin_media(chat_id: str, m, ftype: str, file_id: str, filename: str, caption: str,
                       rinfo=None) -> dict:
    mid = str(m.message_id)
    data = {
        "msg_id": f"admin_{mid}", "chat_id": chat_id,
        "text": caption, "caption": caption,
        "file_id": file_id, "file_name": filename,
        "type": ftype, "from": "admin", "sent_by": _sender(),
        "time": now_str(), "ts": now_ts(),
        "read": False, "delivered": True, "edited": False, "deleted": False,
    }
    if rinfo:
        data["reply_to"] = rinfo
    fb.put(f"support/{chat_id}/messages/admin_{mid}", data)
    fb.patch(
        f"support/{chat_id}/meta",
        {"last_message": caption or f"[{ftype}]", "last_time": now_str(), "last_ts": now_ts(), "unread": 0},
    )
    fb.log_event("message_out", chat_id=chat_id, msg_type=ftype, file_name=filename)
    return {"ok": True, "mid": f"admin_{mid}", "message": data, "type": ftype}


def admin_send_file(chat_id: str, file_bytes: bytes, filename: str, mime_type: str, caption: str = "",
                    reply_to: str = "") -> dict:
    """Panel upload -> Telegram."""
    if account.is_acct(chat_id):
        try:
            tid, rinfo = _acct_reply_ctx(chat_id, reply_to)
            kind = upload_kind(mime_type, len(file_bytes))
            res = account.send_file(account.uid_of(chat_id), file_bytes, filename, mime_type, caption,
                                    reply_to_id=tid, kind=kind)
            return _acct_store(chat_id, res, caption, ftype=kind, fname=filename, rinfo=rinfo)
        except account.AccountError as e:
            return {"error": str(e)}
    if not bot:
        return {"error": "Bot is not configured (BOT_TOKEN missing)"}
    try:
        f = io.BytesIO(file_bytes)
        f.name = filename
        cap = h(caption) if caption else None
        kw, rinfo = _reply_ctx(chat_id, reply_to)

        kind = upload_kind(mime_type, len(file_bytes))
        if kind == "photo":
            m = bot.send_photo(chat_id, f, caption=cap, parse_mode="HTML", **kw)
            file_id, ftype = m.photo[-1].file_id, "photo"
        elif kind == "video":
            m = bot.send_video(chat_id, f, caption=cap, parse_mode="HTML", **kw)
            file_id, ftype = m.video.file_id, "video"
        else:
            m = bot.send_document(chat_id, f, caption=cap, parse_mode="HTML", **kw)
            file_id, ftype = m.document.file_id, "document"
        ai.remember(chat_id, "assistant", caption or f"[{ftype}]")
        return _store_admin_media(chat_id, m, ftype, file_id, filename, caption, rinfo)
    except Exception as e:
        fb.log_event("bot_error", chat_id=chat_id, error=str(e))
        return {"error": str(e)}


def admin_send_media_by_id(chat_id: str, ftype: str, file_id: str, filename: str, caption: str = "",
                          reply_to: str = "") -> dict:
    """Telegram /send flow: the admin already uploaded the file to the bot, re-use its file_id."""
    if not bot:
        return {"error": "Bot is not configured (BOT_TOKEN missing)"}
    try:
        cap = h(caption) if caption else None
        kw, rinfo = _reply_ctx(chat_id, reply_to)
        if ftype == "photo":
            m = bot.send_photo(chat_id, file_id, caption=cap, parse_mode="HTML", **kw)
            new_id = m.photo[-1].file_id
        elif ftype == "video":
            m = bot.send_video(chat_id, file_id, caption=cap, parse_mode="HTML", **kw)
            new_id = m.video.file_id
        else:
            m = bot.send_document(chat_id, file_id, caption=cap, parse_mode="HTML", **kw)
            new_id = m.document.file_id
        ai.remember(chat_id, "assistant", caption or f"[{ftype}]")
        return _store_admin_media(chat_id, m, ftype, new_id, filename, caption, rinfo)
    except Exception as e:
        fb.log_event("bot_error", chat_id=chat_id, error=str(e))
        return {"error": str(e)}


def block_user(chat_id: str) -> dict:
    """Blocked users are ignored: no AI answer, no auto-reply, nothing is sent to them."""
    if account.is_acct(chat_id):
        return {"error": "P2P chats cannot be blocked from here — use AI off instead"}
    update_meta(chat_id, blocked=True, blocked_reason="manual")
    fb.log_event("blocked", chat_id=chat_id, reason="manual")
    return {"ok": True}


def unblock_user(chat_id: str) -> dict:
    update_meta(chat_id, blocked=False, blocked_reason="")
    antispam.reset(chat_id)
    fb.log_event("unblocked", chat_id=chat_id)
    return {"ok": True}


def admin_edit_message(chat_id: str, admin_mid: str, new_text: str) -> dict:
    if account.is_acct(chat_id):
        old = fb.get(f"support/{chat_id}/messages/{admin_mid}")
        if not isinstance(old, dict):
            return {"error": "Message not found"}
        try:
            account.edit_text(account.uid_of(chat_id), int(old.get("tg_id") or _tg_id(admin_mid)), new_text)
        except account.AccountError as e:
            return {"error": f"Telegram refused the edit: {e}"}
        record_edit(chat_id, admin_mid, new_text)
        return {"ok": True}
    if not bot:
        return {"error": "Bot is not configured"}
    path = f"support/{chat_id}/messages/{admin_mid}"
    old = fb.get(path)
    if not isinstance(old, dict):
        return {"error": "Message not found"}
    tg_id = _tg_id(admin_mid)
    warning = ""
    if tg_id:
        try:
            bot.edit_message_text(h(new_text), chat_id, tg_id, parse_mode="HTML")
        except Exception as e:
            if "message is not modified" not in str(e).lower():
                fb.log_event("bot_error", chat_id=chat_id, error=f"edit: {e}")
                return {"error": f"Telegram refused the edit: {e}"}
    else:
        warning = "This message has no Telegram id, so only the panel copy was changed"
    # keep the previous text for the audit trail
    fb.post(f"{path}/edit_history", {"text": old.get("text") or "", "at": now_str()})
    fb.patch(path, {"text": new_text, "edited": True, "edited_at": now_str()})
    fb.log_event("message_edited", chat_id=chat_id, msg_id=admin_mid)
    res = {"ok": True}
    if warning:
        res["warning"] = warning
    return res


def hide_message_for_admin(chat_id: str, mid: str) -> dict:
    """What "delete" means for the ADMIN role: the message disappears from the admin's own
    view only. It is NOT flagged deleted, NOT removed from the super admin's view, NOT
    touched in the database and NOT removed from the customer's Telegram chat."""
    path = f"support/{chat_id}/messages/{mid}"
    if not isinstance(fb.get(f"{path}/from"), str):
        return {"error": "Message not found"}
    if fb.patch(path, {"hidden_admin": True, "hidden_admin_at": now_str()}) is None:
        return {"error": "Could not save to Firebase"}
    fb.log_event("message_deleted", chat_id=chat_id, msg_id=mid, mode="hidden from admin view only")
    return {"ok": True}


def soft_delete_message(chat_id: str, mid: str, deleted_by: str = "") -> dict:
    """Delete a message.
    * ADMIN role  -> hidden from the admin's view only (see hide_message_for_admin).
    * SUPER admin -> SOFT delete: the record stays in Firebase with deleted=true."""
    if fb.get_actor() == "admin":
        return hide_message_for_admin(chat_id, mid)
    deleted_by = deleted_by or "super"
    tg_deleted = None
    if account.is_acct(chat_id):
        m = fb.get(f"support/{chat_id}/messages/{mid}")
        if isinstance(m, dict) and m.get("from") == "admin" and (m.get("tg_id") or _tg_id(mid)):
            try:
                account.delete_message(account.uid_of(chat_id), int(m.get("tg_id") or _tg_id(mid)))
                tg_deleted = True
            except account.AccountError as e:
                tg_deleted = False
                print(f"[DELETE-ACCT] {chat_id}/{mid}: {e}")
    elif mid.startswith("admin_") and bot:
        tg_id = _tg_id(mid)
        if tg_id:
            try:
                bot.delete_message(chat_id, tg_id)
                tg_deleted = True
            except Exception as e:
                tg_deleted = False
                print(f"[DELETE] {chat_id}/{mid}: {e}")

    fb.patch(
        f"support/{chat_id}/messages/{mid}",
        {"deleted": True, "deleted_at": now_str(), "deleted_by": deleted_by},
    )
    fb.log_event("message_deleted", chat_id=chat_id, msg_id=mid, deleted_by=deleted_by)
    res = {"ok": True}
    if tg_deleted is False:
        res["warning"] = "Hidden in the panel, but Telegram would not remove it from the user's chat"
    return res


def set_reaction(chat_id: str, mid: str, emoji: str = "", remove: bool = False,
                 validate: bool = True, toggle: bool = True) -> dict:
    """Admin reaction. Telegram bots can hold ONE reaction per message, so
    picking an emoji replaces the previous one and picking the same emoji again
    removes it. The reaction is also set on the real Telegram message."""
    emoji = canon_emoji(emoji)
    if not remove and validate and emoji not in REACTIONS:
        return {"error": "Invalid reaction"}

    path = f"support/{chat_id}/messages/{mid}"
    msg = fb.get(path)
    if not isinstance(msg, dict):
        return {"error": "Message not found"}

    view = reaction_view(msg.get("reactions"))
    new = [] if (remove or (toggle and emoji in view["admin"])) else [emoji]

    warning = ""
    tg_id = _tg_id(mid)
    if account.is_acct(chat_id):
        try:
            account.react(account.uid_of(chat_id), int(msg.get("tg_id") or tg_id or 0), new[0] if new else "")
        except Exception as e:
            warning = f"Saved in the panel, but Telegram refused it: {e}"
    elif not bot:
        warning = "Bot not configured — saved in the panel only"
    elif not tg_id:
        warning = "Old broadcast copy without a Telegram id — saved in the panel only"
    else:
        try:
            bot.set_message_reaction(
                chat_id, tg_id,
                reaction=[types.ReactionTypeEmoji(_tg_emoji(e)) for e in new],
            )
        except Exception as e:
            warning = f"Saved in the panel, but Telegram refused it: {e}"

    view["admin"] = new
    if fb.put(f"{path}/reactions", _reaction_store(view)) is None:
        return {"error": "Could not save the reaction to Firebase"}
    fb.log_event("message_reaction", chat_id=chat_id, msg_id=mid, by="admin",
                 emoji=" ".join(new) or "(removed)")
    if new:      # tell the super admin AND the admin (in the background — Telegram calls are slow)
        threading.Thread(
            target=send_admin_given_reaction_notify,
            args=(chat_id, new[0], msg, fb.get_actor() or "admin", not warning),
            daemon=True, name="react-notify",
        ).start()
    res = {"ok": True, "reactions": view}
    if warning:
        res["warning"] = warning
    return res


def remove_reaction(chat_id: str, mid: str, emoji: str = "") -> dict:
    return set_reaction(chat_id, mid, emoji, remove=True)


# ── Recipients (used by broadcast.py) ─────────────────────────────────────────
def _all_user_chat_ids():
    """Every non-blocked chat that Telegram can still deliver to."""
    out = []
    for cid, meta in fb.chat_index().items():
        if isinstance(meta, dict) and meta and not meta.get("blocked") and not meta.get("unreachable") \
                and meta.get("source") != "account" and not account.is_acct(cid):
            out.append(str(cid))
    return out


# ── Polling loop ──────────────────────────────────────────────────────────────
ALLOWED_UPDATES = ["message", "edited_message", "message_reaction", "callback_query"]


def run_bot():
    if not bot:
        print("Bot token missing (BOT_TOKEN) — bot not started.")
        return
    print("Support bot polling started.")
    fb.log_event("bot_started")
    set_admin_menus()
    while True:
        try:
            # skip_pending=False: messages sent while the free dyno was asleep
            # are delivered instead of silently dropped.
            bot.infinity_polling(
                timeout=30, long_polling_timeout=30,
                skip_pending=False, allowed_updates=ALLOWED_UPDATES,
            )
        except Exception as e:
            print(f"[POLLING] crashed: {e} — restarting in 5s")
            fb.log_event("bot_error", error=f"polling crashed: {e}")
            time.sleep(5)
