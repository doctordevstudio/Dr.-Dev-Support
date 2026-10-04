"""
app.py — Support Bot Admin Panel (Flask)
"""
import math
import mimetypes
import re
import threading
import time
import traceback
import unicodedata
from datetime import datetime, timedelta
from functools import wraps
from urllib.parse import quote

import requests
from flask import (
    Flask, Response, abort, jsonify, redirect, render_template, request,
    session, url_for, flash,
)
from werkzeug.exceptions import HTTPException

import broadcast as bc
import config
import firebase_helper as fb
import settings
import telegram_bot as tg

app = Flask(__name__)
app.secret_key = config.SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = config.MAX_UPLOAD_MB * 1024 * 1024
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=7)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"          # blocks cross-site POSTs (CSRF)
app.config["SESSION_COOKIE_SECURE"] = config.PANEL_URL.lower().startswith("https://")

_login_attempts = {}
_lock = threading.Lock()
_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,80}$")


def _valid_id(value: str) -> str:
    """chat ids / message ids / broadcast ids go straight into Firebase paths."""
    if not _ID_RE.match(value or ""):
        abort(404)
    return value


def _login_locked(ip: str) -> int:
    with _lock:
        rec = _login_attempts.get(ip)
        if not rec:
            return 0
        count, first_ts, locked_until = rec
        return max(0, int(locked_until - time.time()))


def _record_failed_login(ip: str):
    with _lock:
        count, first_ts, _ = _login_attempts.get(ip, (0, time.time(), 0))
        count += 1
        locked_until = 0
        if count >= config.LOGIN_MAX_ATTEMPTS:
            locked_until = time.time() + config.LOGIN_LOCKOUT_SECONDS
        _login_attempts[ip] = (count, first_ts, locked_until)


def _clear_failed_login(ip: str):
    with _lock:
        _login_attempts.pop(ip, None)


def _is_xhr() -> bool:
    """True for fetch()/XHR calls (they expect JSON, not a redirect to /login)."""
    p = request.path
    return (
        request.method != "GET"
        or request.headers.get("X-Requested-With") == "fetch"
        or p.startswith(("/api/", "/broadcast/api/"))
        or p in ("/unread_count", "/broadcast/count")
        or p.endswith("/messages")
    )


def login_required(f):
    @wraps(f)
    def decorated(*a, **kw):
        ok = session.get("admin") and session.get("pv", 0) == settings.auth_version()
        if not ok:
            session.clear()                      # password changed elsewhere -> sign out
            if _is_xhr():
                return jsonify({"error": "Session expired — please log in again"}), 401
            return redirect(url_for("login"))
        return f(*a, **kw)
    return decorated


@app.context_processor
def inject_globals():
    s = settings.load()
    return {
        "S": s,
        "panel_name": s["panel_name"],
        "site_title": s["site_title"] or s["panel_name"],
        "theme_css": settings.css_vars(s),
        "max_upload_mb": config.MAX_UPLOAD_MB,
        "using_default_pw": settings.using_default_password(),
    }


@app.after_request
def no_cache(resp):
    # Admin data must never be served from a stale browser cache
    if resp.mimetype in ("text/html", "application/json"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.errorhandler(Exception)
def on_error(e):
    """fetch() callers always get JSON back instead of an HTML error page."""
    if isinstance(e, HTTPException):
        code, msg = e.code or 500, e.description
    else:
        traceback.print_exc()
        code, msg = 500, f"Internal error: {e}"
    if _is_xhr():
        return jsonify({"error": msg}), code
    if isinstance(e, HTTPException):
        return e
    return "Internal server error", 500


# ── Auth ──────────────────────────────────────────────────────────────────────
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        ip = request.headers.get("X-Forwarded-For", request.remote_addr) or "unknown"
        locked_for = _login_locked(ip)
        if locked_for:
            flash(f"Too many failed attempts. Try again in {locked_for}s.", "danger")
            return render_template("login.html")

        entered_user = request.form.get("username", "").strip()
        entered_pass = request.form.get("password", "").strip()

        if settings.check_credentials(entered_user, entered_pass):
            session.clear()
            session["admin"] = True
            session["pv"] = settings.auth_version()
            session.permanent = True
            _clear_failed_login(ip)
            fb.log_event("admin_login", ip=ip)
            return redirect(url_for("chats"))

        _record_failed_login(ip)
        fb.log_event("admin_login_failed", ip=ip, username=entered_user)
        flash("Invalid credentials", "danger")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ── Helpers ───────────────────────────────────────────────────────────────────
def _msg_sort_key(m):
    """Messages can share the same second — break ties by Telegram message id."""
    mid = str(m.get("msg_id", ""))
    num = re.search(r"(\d+)$", mid)
    return (m.get("ts", 0), int(num.group(1)) if num else 0)


def _prep(m: dict) -> dict:
    """Public shape of a message: reactions normalised to {"admin":[], "user":[]}."""
    out = dict(m)
    out["reactions"] = tg.reaction_view(m.get("reactions"))
    out.pop("edit_history", None)
    out.pop("raw_text", None)
    return out


def _ist_dt(ts):
    return datetime.fromtimestamp(int(ts), config.IST)


def _clock(d):
    return d.strftime("%I:%M %p").lstrip("0")


def friendly_ts(ts) -> str:
    """Chat-list label like WhatsApp: 2:35 PM · Yesterday · Monday · 28/09/2026."""
    if not ts:
        return ""
    d = _ist_dt(ts)
    days = (datetime.now(config.IST).date() - d.date()).days
    if days <= 0:
        return _clock(d)
    if days == 1:
        return "Yesterday"
    if days < 7:
        return d.strftime("%A")
    return d.strftime("%d/%m/%Y")


def _norm(s) -> str:
    """lower-case + strip accents, so 'José' matches 'jose'."""
    s = unicodedata.normalize("NFKD", str(s or "").lower())
    return "".join(c for c in s if not unicodedata.combining(c))


def _load_all_chats():
    items = []
    for cid, meta in fb.chat_index().items():
        if isinstance(meta, dict) and meta:
            items.append({**meta, "chat_id": cid, "last_label": friendly_ts(meta.get("last_ts"))})
    items.sort(key=lambda c: c.get("last_ts", 0) or 0, reverse=True)
    return items


def _search_chats(items, q: str):
    """Every word must match somewhere in: name, @username, chat id or last message.
    Matches in name/username/id rank above matches only in the last message."""
    terms = [t for t in _norm(q).replace("@", " ").split() if t]
    if not terms:
        return items
    ranked = []
    for c in items:
        ident = _norm(" ".join([str(c.get("user_name", "")), str(c.get("username", "")),
                                str(c.get("chat_id", ""))]))
        full = ident + " " + _norm(c.get("last_message", ""))
        if all(t in full for t in terms):
            ranked.append((0 if all(t in ident for t in terms) else 1, c))
    ranked.sort(key=lambda x: x[0])           # stable: keeps "most recent first" inside a rank
    return [c for _, c in ranked]


def _page_arg() -> int:
    return max(1, request.args.get("page", 1, type=int) or 1)


def _chats_page():
    q = request.args.get("q", "").strip()
    flt = request.args.get("filter", "all")
    if flt not in ("all", "unread", "blocked"):
        flt = "all"
    page = _page_arg()
    per = config.CHATS_PER_PAGE
    base = _search_chats(_load_all_chats(), q)
    counts = {
        "all": len(base),
        "unread": sum(1 for c in base if int(c.get("unread") or 0) > 0),
        "blocked": sum(1 for c in base if c.get("blocked")),
    }
    total_unread = sum(int(c.get("unread") or 0) for c in base)
    items = base if flt == "all" else (
        [c for c in base if int(c.get("unread") or 0) > 0] if flt == "unread"
        else [c for c in base if c.get("blocked")])
    total = len(items)
    pages = max(1, math.ceil(total / per))
    page = min(page, pages)
    start = (page - 1) * per
    return items[start:start + per], total, total_unread, page, pages, q, flt, counts


# ── Chat list ─────────────────────────────────────────────────────────────────
@app.route("/")
@login_required
def chats():
    page_items, total, total_unread, page, pages, q, flt, counts = _chats_page()
    init = {"chats": page_items, "total": total, "total_unread": total_unread,
            "page": page, "pages": pages, "q": q, "filter": flt, "counts": counts}
    return render_template("chats.html", init=init)


@app.route("/api/chats")
@login_required
def api_chats():
    page_items, total, total_unread, page, pages, q, flt, counts = _chats_page()
    return jsonify({"chats": page_items, "total": total, "total_unread": total_unread,
                    "page": page, "pages": pages, "q": q, "filter": flt, "counts": counts})


# ── Debug: send a test admin notification ────────────────────────────────────
@app.route("/debug/notify")
@login_required
def debug_notify():
    """Hit this once while logged in to verify admin notifications work."""
    ids = config.ADMIN_CHAT_IDS()
    if not ids:
        return jsonify({
            "ok": False,
            "error": "ADMIN_CHAT_ID is empty. Set it in Render → Environment, e.g. 123456789 or '123,456'."
        })
    if not tg.bot:
        return jsonify({"ok": False, "error": "Bot is not initialized — BOT_TOKEN missing or invalid."})

    results = []
    for admin_id in ids:
        try:
            tg.bot.send_message(
                admin_id,
                "✅ <b>Test notification</b>\n"
                "If you're reading this in your admin chat, notifications work.\n"
                f"🕐 {tg.now_str()}",
                parse_mode="HTML",
            )
            results.append({"admin_id": admin_id, "ok": True})
        except Exception as e:
            results.append({"admin_id": admin_id, "ok": False, "error": str(e)})
    fb.log_event("admin_notify_test", results=str(results))
    return jsonify({"ok": True, "admin_ids_configured": ids, "results": results})


# ── Messages ──────────────────────────────────────────────────────────────────
def _all_messages(cid):
    raw = fb.get(f"support/{cid}/messages") or {}
    return sorted((m for m in raw.values() if isinstance(m, dict)), key=_msg_sort_key)


def _recent(cid, limit):
    """(messages, has_more). Uses the 'ts' index when Firebase has it."""
    ok, data = fb.query_by_ts(f"support/{cid}/messages", limit_last=limit + 1)
    items = sorted((m for m in data.values() if isinstance(m, dict)), key=_msg_sort_key) if ok \
        else _all_messages(cid)
    return items[-limit:], len(items) > limit


def _older(cid, before_ts, limit):
    ok, data = fb.query_by_ts(f"support/{cid}/messages", end_at=before_ts, limit_last=limit + 1)
    if ok:
        items = sorted((m for m in data.values() if isinstance(m, dict)), key=_msg_sort_key)
    else:
        items = [m for m in _all_messages(cid) if m.get("ts", 0) <= before_ts]
    return items[-limit:], len(items) > limit


def _mark_read(cid, msgs):
    """Mark user messages as read with ONE request (was one request per message)."""
    upd = {f"{m['msg_id']}/read": True for m in msgs
           if m.get("from") == "user" and not m.get("read") and m.get("msg_id")}
    if upd:
        fb.patch(f"support/{cid}/messages", upd)
    return bool(upd)


@app.route("/chat/<cid>")
@login_required
def chat_view(cid):
    _valid_id(cid)
    meta = fb.get(f"support/{cid}/meta") or {}
    per = config.MESSAGES_PER_PAGE
    messages, has_more = _recent(cid, per)
    total = len(fb.shallow(f"support/{cid}/messages"))

    _mark_read(cid, messages)
    if meta.get("unread"):
        fb.patch(f"support/{cid}/meta", {"unread": 0})

    payload = {
        "cid": cid,
        "messages": [_prep(m) for m in messages],
        "has_more": has_more,
        "total": total,
        "reactions": tg.REACTIONS,
        "page_size": per,
        "max_mb": config.MAX_UPLOAD_MB,
        "blocked": bool(meta.get("blocked")),
        "sound": bool(settings.load()["notify_sound"]),
    }
    return render_template("chat.html", cid=cid, meta=meta, payload=payload, total=total)


@app.route("/chat/<cid>/messages")
@login_required
def chat_messages_json(cid):
    _valid_id(cid)
    before_ts = request.args.get("before_ts", type=int)
    after_ts = request.args.get("after_ts", type=int)
    around = request.args.get("around", "")
    limit = min(100, request.args.get("limit", default=config.MESSAGES_PER_PAGE, type=int) or 30)

    if around:
        # jump to a (search-)result that is not loaded yet: 25 messages of context
        # before it, everything after it (capped) — the client then replaces its list
        allm = _all_messages(cid)
        idx = next((i for i, m in enumerate(allm) if m.get("msg_id") == around), None)
        if idx is None:
            return jsonify({"error": "Message not found"}), 404
        start = max(0, idx - 25)
        chunk = allm[start:start + 400]
        return jsonify({"messages": [_prep(m) for m in chunk], "has_more": start > 0})

    if before_ts is not None:
        page, has_more = _older(cid, before_ts, limit)
        return jsonify({"messages": [_prep(m) for m in page], "has_more": has_more})

    if after_ts is not None:
        # one window of the latest messages: new ones + fresh state (reactions,
        # read ticks, edits, deletes) of the recent ones
        window, _ = _recent(cid, 60)
        if window and window[0].get("ts", 0) > after_ts:
            # more than 60 new messages since last poll — fetch them all
            ok, data = fb.query_by_ts(f"support/{cid}/messages", start_at=after_ts)
            window = sorted((m for m in data.values() if isinstance(m, dict)), key=_msg_sort_key) \
                if ok else [m for m in _all_messages(cid) if m.get("ts", 0) >= after_ts]
        # `>=` (not `>`): two messages can share a second; the client de-duplicates
        new = [m for m in window if m.get("ts", 0) >= after_ts]
        old = [m for m in window if m.get("ts", 0) < after_ts]
        # the admin is looking at this chat right now -> new user messages are read
        if request.args.get("visible") == "1":
            changed = _mark_read(cid, new)
            if changed and fb.get(f"support/{cid}/meta/unread"):
                fb.patch(f"support/{cid}/meta", {"unread": 0})
        return jsonify({"messages": [_prep(m) for m in new],
                        "updates": [_prep(m) for m in old], "has_more": False})

    page, has_more = _recent(cid, limit)
    return jsonify({"messages": [_prep(m) for m in page], "has_more": has_more})


@app.route("/chat/<cid>/search")
@login_required
def chat_search(cid):
    """All messages of this chat containing the query (oldest -> newest)."""
    _valid_id(cid)
    q = _norm(request.args.get("q", "").strip())
    if not q:
        return jsonify({"matches": [], "total": 0})
    out = []
    for m in _all_messages(cid):
        if m.get("deleted") or not m.get("msg_id"):
            continue
        hay = _norm(" ".join([m.get("text") or "", m.get("caption") or "", m.get("file_name") or ""]))
        if q in hay:
            out.append({"msg_id": m["msg_id"], "ts": m.get("ts", 0)})
    return jsonify({"matches": out[-500:], "total": len(out)})


@app.route("/chat/<cid>/send", methods=["POST"])
@login_required
def chat_send(cid):
    _valid_id(cid)
    text = request.form.get("text", "").strip()
    if not text:
        return jsonify({"error": "Empty message"})
    if len(text) > 4000:
        return jsonify({"error": "Message too long (Telegram allows 4096 characters)"})
    if fb.get(f"support/{cid}/meta/blocked"):
        return jsonify({"error": "User is blocked — unblock first"})
    res = tg.admin_reply(cid, text)
    if res.get("message"):
        res["message"] = _prep(res["message"])
    return jsonify(res)


@app.route("/chat/<cid>/send_file", methods=["POST"])
@login_required
def chat_send_file(cid):
    _valid_id(cid)
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "No file provided"}), 400
    if fb.get(f"support/{cid}/meta/blocked"):
        return jsonify({"error": "User is blocked — unblock first"})
    caption = request.form.get("text", "").strip()
    file_bytes = f.read()
    if len(file_bytes) > config.MAX_UPLOAD_MB * 1024 * 1024:
        return jsonify({"error": f"File exceeds {config.MAX_UPLOAD_MB}MB limit"}), 413
    filename = f.filename or "file"
    mime_type = f.content_type or "application/octet-stream"
    res = tg.admin_send_file(cid, file_bytes, filename, mime_type, caption)
    if res.get("message"):
        res["message"] = _prep(res["message"])
    return jsonify(res)


def _stream_telegram_file(cid, mid, as_attachment):
    _valid_id(cid)
    _valid_id(mid)
    m = fb.get(f"support/{cid}/messages/{mid}")     # ONE message, not the whole chat
    if not isinstance(m, dict):
        return "Not found", 404
    file_id = m.get("file_id")
    url = tg.refresh_file_url(file_id) if file_id else ""
    if not url:
        return "File unavailable", 404
    try:
        r = requests.get(url, timeout=30, stream=True)
        if r.status_code != 200:
            r.close()
            return "File unavailable on Telegram's servers", 404
        ctype = r.headers.get("Content-Type", "")
        if not ctype or ctype == "application/octet-stream":
            ctype = mimetypes.guess_type(url)[0] or (
                {"photo": "image/jpeg", "video": "video/mp4"}.get(m.get("type"), "application/octet-stream"))
        headers = {"Cache-Control": "private, max-age=3600"}
        if as_attachment:
            filename = (m.get("file_name") or mid).replace('"', "").replace("\r", "").replace("\n", "")
            if "." not in filename:
                filename += mimetypes.guess_extension(ctype.split(";")[0]) or ""
            headers["Content-Disposition"] = (
                f"attachment; filename=\"{filename.encode('ascii', 'ignore').decode() or 'file'}\"; "
                f"filename*=UTF-8''{quote(filename)}"
            )
        resp = Response(r.iter_content(chunk_size=65536), mimetype=ctype, headers=headers)
        resp.call_on_close(r.close)
        return resp
    except Exception as e:
        return f"Download error: {e}", 500


@app.route("/chat/<cid>/file/<mid>/download")
@login_required
def chat_file_download(cid, mid):
    return _stream_telegram_file(cid, mid, as_attachment=True)


@app.route("/chat/<cid>/file/<mid>/view")
@login_required
def chat_file_view(cid, mid):
    return _stream_telegram_file(cid, mid, as_attachment=False)


@app.route("/avatar/<cid>")
@login_required
def avatar(cid):
    """Proxy Telegram profile photo (re-fetches a stale file_id once)."""
    _valid_id(cid)
    meta = fb.get(f"support/{cid}/meta") or {}
    file_id = meta.get("photo_file_id") or ""
    if not file_id:
        try:
            file_id = tg.get_user_photo_file_id(int(cid))
            if file_id:
                fb.patch(f"support/{cid}/meta", {"photo_file_id": file_id})
        except Exception:
            pass
    if not file_id:
        return ("", 204)

    url = tg.refresh_file_url(file_id)
    if not url:
        try:
            new_id = tg.get_user_photo_file_id(int(cid))
            if new_id and new_id != file_id:
                fb.patch(f"support/{cid}/meta", {"photo_file_id": new_id})
                url = tg.refresh_file_url(new_id)
        except Exception:
            pass
    if not url:
        return ("", 204)
    try:
        r = requests.get(url, timeout=15)
        if r.status_code != 200:
            return ("", 204)
        ct = r.headers.get("Content-Type", "")
        if not ct.startswith("image/"):
            ct = mimetypes.guess_type(url)[0] or "image/jpeg"
        return Response(r.content, mimetype=ct, headers={"Cache-Control": "private, max-age=600"})
    except Exception:
        return ("", 204)


@app.route("/chat/<cid>/message/<mid>/edit", methods=["POST"])
@login_required
def chat_edit(cid, mid):
    _valid_id(cid)
    _valid_id(mid)
    if not mid.startswith("admin_"):
        return jsonify({"error": "Only admin messages can be edited"}), 400
    new_text = request.form.get("text", "").strip()
    if not new_text:
        return jsonify({"error": "Empty"})
    return jsonify(tg.admin_edit_message(cid, mid, new_text))


@app.route("/chat/<cid>/message/<mid>/delete", methods=["POST"])
@login_required
def chat_delete(cid, mid):
    """SOFT delete only — the record stays in Firebase with deleted=true."""
    _valid_id(cid)
    _valid_id(mid)
    return jsonify(tg.soft_delete_message(cid, mid, deleted_by="admin"))


@app.route("/chat/<cid>/message/<mid>/react", methods=["POST"])
@login_required
def chat_react(cid, mid):
    _valid_id(cid)
    _valid_id(mid)
    emoji = request.form.get("emoji", "").strip()
    action = request.form.get("action", "add")
    if action == "remove":
        return jsonify(tg.remove_reaction(cid, mid, emoji))
    return jsonify(tg.set_reaction(cid, mid, emoji))


@app.route("/chat/<cid>/block", methods=["POST"])
@login_required
def chat_block(cid):
    _valid_id(cid)
    return jsonify(tg.block_user(cid))


@app.route("/chat/<cid>/unblock", methods=["POST"])
@login_required
def chat_unblock(cid):
    _valid_id(cid)
    return jsonify(tg.unblock_user(cid))


@app.route("/unread_count")
@login_required
def unread_count():
    total = sum(int(c.get("unread") or 0) for c in _load_all_chats())
    return jsonify({"count": total})


# ── Broadcast ─────────────────────────────────────────────────────────────────
@app.route("/broadcast")
@login_required
def broadcast_view():
    return render_template("broadcast.html")


@app.route("/broadcast/api/list")
@login_required
def broadcast_list():
    return jsonify(bc.snapshot())


def _fmt_arg() -> str:
    fmt = request.form.get("format", "html")
    return fmt if fmt in ("html", "text") else "html"


@app.route("/broadcast/api/send", methods=["POST"])
@login_required
def broadcast_api_send():
    text = request.form.get("text", "")
    f = request.files.get("file")
    if f and f.filename:
        data = f.read()
        if len(data) > config.MAX_UPLOAD_MB * 1024 * 1024:
            return jsonify({"ok": False, "error": f"File exceeds {config.MAX_UPLOAD_MB}MB limit"}), 413
        kind = tg.upload_kind(f.content_type or "", len(data))
        return jsonify(bc.start_send(text, _fmt_arg(), kind=kind, file_name=f.filename,
                                     file_bytes=data, source="panel"))
    return jsonify(bc.start_send(text, _fmt_arg(), source="panel"))


@app.route("/broadcast/<bid>/details")
@login_required
def broadcast_details(bid):
    return jsonify(bc.details(_valid_id(bid)))


@app.route("/broadcast/<bid>/resend", methods=["POST"])
@login_required
def broadcast_resend(bid):
    return jsonify(bc.start_resend(_valid_id(bid)))


@app.route("/broadcast/<bid>/edit", methods=["POST"])
@login_required
def broadcast_edit(bid):
    return jsonify(bc.start_edit(_valid_id(bid), request.form.get("text", ""), _fmt_arg()))


@app.route("/broadcast/<bid>/delete", methods=["POST"])
@login_required
def broadcast_delete(bid):
    """Removes the messages from users' Telegram chats and SOFT-deletes the record."""
    return jsonify(bc.start_delete(_valid_id(bid)))


@app.route("/broadcast/api/validate", methods=["POST"])
@login_required
def broadcast_validate():
    err = bc.validate(request.form.get("text", ""), _fmt_arg(), request.form.get("kind", "text"))
    return jsonify({"ok": err is None, "error": err})


@app.route("/broadcast/count")
@login_required
def broadcast_count():
    return jsonify({"count": len(tg._all_user_chat_ids())})


# ── Welcome message editor ───────────────────────────────────────────────────
@app.route("/welcome", methods=["GET"])
@login_required
def welcome_view():
    return render_template(
        "welcome.html", welcome=tg.get_welcome_message(), fmt=tg.get_welcome_format(),
        default_welcome=config.DEFAULT_WELCOME_MESSAGE,
    )


@app.route("/welcome/save", methods=["POST"])
@login_required
def welcome_save():
    msg = request.form.get("message", "")
    fmt = request.form.get("format", "html")
    if fmt not in ("html", "text"):
        fmt = "html"
    err = bc.validate(msg, fmt)
    if err:
        return jsonify({"ok": False, "error": err})
    if fb.put("settings/welcome/message", msg) is None or fb.put("settings/welcome/format", fmt) is None:
        return jsonify({"ok": False, "error": "Could not save to Firebase"})
    fb.log_event("welcome_updated", fmt=fmt, length=len(msg))
    return jsonify({"ok": True})


@app.route("/welcome/preview", methods=["POST"])
@login_required
def welcome_preview():
    """Send the message to the admin's own chat so it can be checked on Telegram
    exactly as users will see it (real HTML, or truly plain text)."""
    msg = request.form.get("message", "")
    fmt = request.form.get("format", "html")
    if fmt not in ("html", "text"):
        fmt = "html"
    ids = config.ADMIN_CHAT_IDS()
    if not ids:
        return jsonify({"error": "No ADMIN_CHAT_ID configured"})
    if not tg.bot:
        return jsonify({"error": "Bot not configured"})
    err = bc.validate(msg, fmt)
    if err:
        return jsonify({"error": err})
    sent, errors = [], []
    for admin_id in ids:
        try:
            tg.send_formatted(admin_id, msg, fmt)
            sent.append(admin_id)
        except Exception as e:
            errors.append(f"{admin_id}: {e}")
    return jsonify({"ok": True, "sent_to": sent, "errors": errors})


# ── Settings ──────────────────────────────────────────────────────────────────
@app.route("/settings")
@login_required
def settings_view():
    return render_template(
        "settings.html", s=settings.load(force=True), presets=settings.PRESETS,
        admin_user=settings.admin_username(), bot_ok=bool(tg.bot),
        admin_ids=config.ADMIN_CHAT_IDS(), panel_url=config.PANEL_URL,
        firebase_ok=bool(config.FIREBASE_URL),
    )


@app.route("/settings/api/save", methods=["POST"])
@login_required
def settings_save():
    try:
        s = settings.save(request.get_json(silent=True) or {})
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)})
    return jsonify({"ok": True, "settings": s})


@app.route("/settings/api/credentials", methods=["POST"])
@login_required
def settings_credentials():
    d = request.get_json(silent=True) or {}
    new_pw = d.get("new_password") or ""
    if new_pw and new_pw != (d.get("confirm_password") or ""):
        return jsonify({"ok": False, "error": "The two new passwords don't match"})
    try:
        version = settings.change_credentials(d.get("current_password") or "", d.get("username") or "", new_pw)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)})
    session["pv"] = version                      # this browser stays signed in, all others are signed out
    return jsonify({"ok": True})


# ── Activity log viewer ───────────────────────────────────────────────────────
@app.route("/logs")
@login_required
def logs_view():
    return render_template("logs.html", logs=fb.get_logs(500))


# ── Health ────────────────────────────────────────────────────────────────────
@app.route("/health")
def health():
    return jsonify({"status": "ok", "bot": bool(tg.bot)})


@app.route("/ping")
def ping():
    return "pong", 200


# ── Bot startup ───────────────────────────────────────────────────────────────
_bot_thread_started = False
_bot_lock = threading.Lock()


def _warm_up():
    """Runs once in the background: mark half-finished broadcasts, build chat index."""
    try:
        settings.load(force=True)
        bc.recover_interrupted()
        fb.chat_index(max_age=0)
    except Exception as e:
        print(f"[WARMUP] {e}")


def _start_bot_once():
    global _bot_thread_started
    with _bot_lock:
        if _bot_thread_started:
            return
        threading.Thread(target=_warm_up, daemon=True, name="warmup").start()
        if not tg.bot:
            print("BOT_TOKEN not set — support bot will not start.")
            return
        threading.Thread(target=tg.run_bot, daemon=True).start()
        _bot_thread_started = True
        print("Support bot thread started.")


_start_bot_once()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=config.PORT, debug=False)
