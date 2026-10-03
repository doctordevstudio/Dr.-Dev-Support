"""
app.py — Support Bot Admin Panel (Flask)
"""
import json
import math
import secrets
import threading
import time
from functools import wraps

import requests
from flask import (
    Flask, Response, jsonify, redirect, render_template, request,
    session, url_for, flash, stream_with_context,
)

import config
import firebase_helper as fb
import telegram_bot as tg

app = Flask(__name__)
app.secret_key = config.SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = config.MAX_UPLOAD_MB * 1024 * 1024

_login_attempts = {}
_lock = threading.Lock()


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


def login_required(f):
    @wraps(f)
    def decorated(*a, **kw):
        if not session.get("admin"):
            return redirect(url_for("login"))
        return f(*a, **kw)
    return decorated


@app.context_processor
def inject_globals():
    return {"panel_name": config.PANEL_NAME, "max_upload_mb": config.MAX_UPLOAD_MB}


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

        user_ok = secrets.compare_digest(entered_user, config.ADMIN_USERNAME)
        pass_ok = secrets.compare_digest(entered_pass, config.ADMIN_PASSWORD)

        if user_ok and pass_ok:
            session["admin"] = True
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
def _load_all_chats():
    raw = fb.get("support") or {}
    items = []
    for cid, data in raw.items():
        meta = (data or {}).get("meta", {}) or {}
        if not meta:
            continue
        meta = {**meta, "chat_id": cid}
        items.append(meta)
    items.sort(key=lambda c: c.get("last_ts", 0), reverse=True)
    return items


def _filter_chats(items, q: str):
    if not q:
        return items
    q = q.lower().strip()
    out = []
    for c in items:
        haystack = " ".join([
            str(c.get("chat_id", "")),
            str(c.get("user_name", "")),
            str(c.get("username", "")),
        ]).lower()
        if q in haystack:
            out.append(c)
    return out


@app.route("/")
@login_required
def chats():
    q = request.args.get("q", "").strip()
    page = max(1, int(request.args.get("page", 1)))
    per = config.CHATS_PER_PAGE

    items = _load_all_chats()
    items = _filter_chats(items, q)
    total = len(items)
    pages = max(1, math.ceil(total / per))
    start = (page - 1) * per
    page_items = items[start:start + per]

    total_unread = sum(c.get("unread", 0) for c in items)
    return render_template(
        "chats.html",
        chats=page_items,
        total_unread=total_unread,
        total=total,
        page=page,
        pages=pages,
        q=q,
    )


@app.route("/api/chats")
@login_required
def api_chats():
    q = request.args.get("q", "").strip()
    page = max(1, int(request.args.get("page", 1)))
    per = config.CHATS_PER_PAGE

    items = _load_all_chats()
    items = _filter_chats(items, q)
    total = len(items)
    pages = max(1, math.ceil(total / per))
    start = (page - 1) * per
    page_items = items[start:start + per]

    return jsonify({
        "chats": page_items,
        "total": total,
        "page": page,
        "pages": pages,
        "q": q,
    })


def _get_all_messages(cid):
    raw = fb.get(f"support/{cid}/messages") or {}
    return sorted(raw.values(), key=lambda m: m.get("ts", 0))


def _get_meta(cid):
    return fb.get(f"support/{cid}/meta") or {}


@app.route("/chat/<cid>")
@login_required
def chat_view(cid):
    meta = _get_meta(cid)
    all_msgs = _get_all_messages(cid)
    raw = fb.get(f"support/{cid}/messages") or {}
    for mid, m in raw.items():
        if m.get("from") == "user" and not m.get("read"):
            fb.patch(f"support/{cid}/messages/{mid}", {"read": True})
    fb.patch(f"support/{cid}/meta", {"unread": 0})

    per = config.MESSAGES_PER_PAGE
    total = len(all_msgs)
    slice_start = max(0, total - per)
    messages = all_msgs[slice_start:]

    return render_template(
        "chat.html",
        cid=cid,
        meta=meta,
        messages=messages,
        total=total,
        has_more=slice_start > 0,
        reactions=tg.REACTIONS,
    )


@app.route("/chat/<cid>/messages")
@login_required
def chat_messages_json(cid):
    before_ts = request.args.get("before_ts", type=int)
    after_ts = request.args.get("after_ts", type=int)
    limit = request.args.get("limit", default=config.MESSAGES_PER_PAGE, type=int)

    all_msgs = _get_all_messages(cid)
    total = len(all_msgs)

    if after_ts is not None:
        new_msgs = [m for m in all_msgs if m.get("ts", 0) > after_ts]
        # Send full state of every message so reactions/read/delete sync
        updates = all_msgs
        return jsonify({"messages": new_msgs, "updates": updates, "has_more": False, "total": total})

    if before_ts is not None:
        older = [m for m in all_msgs if m.get("ts", 0) < before_ts]
        slice_start = max(0, len(older) - limit)
        page = older[slice_start:]
        return jsonify({"messages": page, "has_more": slice_start > 0, "total": total})

    slice_start = max(0, total - limit)
    page = all_msgs[slice_start:]
    return jsonify({"messages": page, "has_more": slice_start > 0, "total": total})


@app.route("/chat/<cid>/send", methods=["POST"])
@login_required
def chat_send(cid):
    text = request.form.get("text", "").strip()
    if not text:
        return jsonify({"error": "Empty message"})
    return jsonify(tg.admin_reply(cid, text))


@app.route("/chat/<cid>/send_file", methods=["POST"])
@login_required
def chat_send_file(cid):
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "No file provided"}), 400
    caption = request.form.get("text", "").strip()
    file_bytes = f.read()
    if len(file_bytes) > config.MAX_UPLOAD_MB * 1024 * 1024:
        return jsonify({"error": f"File exceeds {config.MAX_UPLOAD_MB}MB limit"}), 413
    filename = f.filename or "file"
    mime_type = f.content_type or "application/octet-stream"
    return jsonify(tg.admin_send_file(cid, file_bytes, filename, mime_type, caption))


@app.route("/chat/<cid>/file/<mid>/download")
@login_required
def chat_file_download(cid, mid):
    msgs = fb.get(f"support/{cid}/messages") or {}
    m = msgs.get(mid)
    if not m:
        return "Not found", 404
    file_id = m.get("file_id")
    url = tg.refresh_file_url(file_id) if file_id else ""
    if not url:
        return "File unavailable", 404
    try:
        r = requests.get(url, timeout=30, stream=True)
        if r.status_code != 200:
            return "File unavailable on Telegram's servers", 404
        filename = m.get("file_name") or f"{mid}"
        return Response(
            r.iter_content(chunk_size=65536),
            mimetype=r.headers.get("Content-Type", "application/octet-stream"),
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "private, max-age=3600",
            },
        )
    except Exception as e:
        return f"Download error: {e}", 500


@app.route("/chat/<cid>/file/<mid>/view")
@login_required
def chat_file_view(cid, mid):
    msgs = fb.get(f"support/{cid}/messages") or {}
    m = msgs.get(mid)
    if not m:
        return "Not found", 404
    file_id = m.get("file_id")
    url = tg.refresh_file_url(file_id) if file_id else ""
    if not url:
        return "File unavailable", 404
    try:
        r = requests.get(url, timeout=30, stream=True)
        if r.status_code != 200:
            return "File unavailable", 404
        return Response(
            r.iter_content(chunk_size=65536),
            mimetype=r.headers.get("Content-Type", "application/octet-stream"),
            headers={"Cache-Control": "private, max-age=3600"},
        )
    except Exception as e:
        return f"Error: {e}", 500


@app.route("/avatar/<cid>")
@login_required
def avatar(cid):
    meta = fb.get(f"support/{cid}/meta") or {}
    file_id = meta.get("photo_file_id") or ""
    if not file_id:
        try:
            uid = int(cid)
            file_id = tg.get_user_photo_file_id(uid)
            if file_id:
                fb.patch(f"support/{cid}/meta", {"photo_file_id": file_id})
        except Exception:
            pass
    if not file_id:
        return ("", 204)

    url = tg.refresh_file_url(file_id)
    if not url:
        try:
            uid = int(cid)
            new_id = tg.get_user_photo_file_id(uid)
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
        ct = r.headers.get("Content-Type", "image/jpeg")
        return Response(r.content, mimetype=ct, headers={"Cache-Control": "private, max-age=600"})
    except Exception:
        return ("", 204)


@app.route("/chat/<cid>/message/<mid>/edit", methods=["POST"])
@login_required
def chat_edit(cid, mid):
    if not mid.startswith("admin_"):
        return jsonify({"error": "Only admin messages can be edited"}), 400
    new_text = request.form.get("text", "").strip()
    if not new_text:
        return jsonify({"error": "Empty"})
    return jsonify(tg.admin_edit_message(cid, mid, new_text))


@app.route("/chat/<cid>/message/<mid>/delete", methods=["POST"])
@login_required
def chat_delete(cid, mid):
    return jsonify(tg.soft_delete_message(cid, mid, deleted_by="admin"))


@app.route("/chat/<cid>/message/<mid>/react", methods=["POST"])
@login_required
def chat_react(cid, mid):
    emoji = request.form.get("emoji", "").strip()
    action = request.form.get("action", "add")
    if action == "remove":
        return jsonify(tg.remove_reaction(cid, mid, emoji, actor="admin"))
    return jsonify(tg.set_reaction(cid, mid, emoji, actor="admin"))


@app.route("/chat/<cid>/block", methods=["POST"])
@login_required
def chat_block(cid):
    return jsonify(tg.block_user(cid))


@app.route("/chat/<cid>/unblock", methods=["POST"])
@login_required
def chat_unblock(cid):
    return jsonify(tg.unblock_user(cid))


@app.route("/unread_count")
@login_required
def unread_count():
    items = _load_all_chats()
    total = sum(c.get("unread", 0) for c in items)
    return jsonify({"count": total})


# ── Broadcast ─────────────────────────────────────────────────────────────────
@app.route("/broadcast")
@login_required
def broadcast_view():
    raw = fb.get("broadcasts") or {}
    items = sorted(raw.values(), key=lambda b: b.get("ts", 0), reverse=True) if isinstance(raw, dict) else []
    return render_template("broadcast.html", broadcasts=items)


@app.route("/broadcast/start", methods=["POST"])
@login_required
def broadcast_start():
    """Non-blocking: kicks off a background thread and returns job_id."""
    text = request.form.get("text", "")
    fmt = request.form.get("fmt", "html")
    if not text.strip():
        return jsonify({"error": "Empty message"}), 400
    if fmt not in ("html", "text"):
        fmt = "html"
    result = tg.start_broadcast(text, fmt)
    status = 200 if result.get("ok") else 400
    return jsonify(result), status


@app.route("/broadcast/status/<job_id>")
@login_required
def broadcast_status(job_id):
    """Polled by the frontend every 700ms. Tiny JSON, fast."""
    job = tg.get_broadcast_job(job_id)
    if not job:
        return jsonify({"error": "Unknown job"}), 404
    return jsonify({
        "job_id": job_id,
        "total": job.get("total", 0),
        "sent": job.get("sent", 0),
        "progress": job.get("progress", 0),
        "failed": job.get("failed", []),
        "done": job.get("done", False),
        "last_chat": job.get("last_chat"),
    })


@app.route("/broadcast/finish/<job_id>", methods=["POST"])
@login_required
def broadcast_finish(job_id):
    tg.mark_broadcast_finished_in_db(job_id)
    return jsonify({"ok": True})


@app.route("/broadcast/delete/<job_id>", methods=["POST"])
@login_required
def broadcast_delete(job_id):
    """Archive (soft-delete) a broadcast record. Never removes from DB."""
    fb.patch(f"broadcasts/{job_id}", {"archived": True, "archived_at": fb.now_ist()})
    fb.log_event("broadcast_archived", job_id=job_id)
    return jsonify({"ok": True})


@app.route("/broadcast/update/<job_id>", methods=["POST"])
@login_required
def broadcast_update(job_id):
    """Edit the text/format of a stored broadcast record (does not re-send)."""
    text = request.form.get("text", "")
    fmt = request.form.get("fmt", "html")
    if fmt not in ("html", "text"):
        fmt = "html"
    fb.patch(f"broadcasts/{job_id}", {"text": text, "fmt": fmt, "edited_at": fb.now_ist()})
    fb.log_event("broadcast_edited", job_id=job_id)
    return jsonify({"ok": True})


@app.route("/broadcast/resend/<job_id>", methods=["POST"])
@login_required
def broadcast_resend(job_id):
    """Re-send the stored broadcast as a new job."""
    rec = fb.get(f"broadcasts/{job_id}") or {}
    text = rec.get("text", "")
    fmt = rec.get("fmt", "html")
    if not text:
        return jsonify({"error": "Nothing to resend"}), 400
    result = tg.start_broadcast(text, fmt)
    status = 200 if result.get("ok") else 400
    return jsonify(result), status


@app.route("/broadcast/count")
@login_required
def broadcast_count():
    ids = tg._all_user_chat_ids()
    return jsonify({"count": len(ids)})


# ── Welcome message editor ───────────────────────────────────────────────────
@app.route("/welcome", methods=["GET"])
@login_required
def welcome_view():
    msg = tg.get_welcome_message()
    fmt = tg.get_welcome_format()
    return render_template("welcome.html", welcome=msg, fmt=fmt)


@app.route("/welcome/save", methods=["POST"])
@login_required
def welcome_save():
    msg = request.form.get("message", "")
    fmt = request.form.get("format", "html")
    if fmt not in ("html", "text"):
        fmt = "html"
    fb.put("settings/welcome/message", msg)
    fb.put("settings/welcome/format", fmt)
    fb.log_event("welcome_updated", fmt=fmt, length=len(msg))
    return jsonify({"ok": True})


@app.route("/welcome/preview", methods=["POST"])
@login_required
def welcome_preview():
    msg = request.form.get("message", "")
    fmt = request.form.get("format", "html")
    ids = config.ADMIN_CHAT_IDS()
    if not ids:
        return jsonify({"error": "No ADMIN_CHAT_ID configured"})
    if not tg.bot:
        return jsonify({"error": "Bot not configured"})
    sent = []
    errors = []
    for admin_id in ids:
        try:
            if fmt == "html":
                tg.bot.send_message(admin_id, msg, parse_mode="HTML")
            else:
                tg.bot.send_message(admin_id, msg, parse_mode=None)
            sent.append(admin_id)
        except Exception as e:
            errors.append(f"{admin_id}: {e}")
    return jsonify({"ok": True, "sent_to": sent, "errors": errors})


# ── Activity log viewer ───────────────────────────────────────────────────────
@app.route("/logs")
@login_required
def logs_view():
    raw = fb.get("logs") or {}
    items = sorted(raw.values(), key=lambda e: e.get("ts", 0), reverse=True)[:500]
    return render_template("logs.html", logs=items)


# ── Debug ─────────────────────────────────────────────────────────────────────
@app.route("/debug/notify")
@login_required
def debug_notify():
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


def _start_bot_once():
    global _bot_thread_started
    with _bot_lock:
        if _bot_thread_started:
            return
        if not tg.bot:
            print("BOT_TOKEN not set — support bot will not start.")
            return
        threading.Thread(target=tg.run_bot, daemon=True).start()
        _bot_thread_started = True
        print("Support bot thread started.")


_start_bot_once()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=config.PORT, debug=False)
