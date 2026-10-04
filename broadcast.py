"""
broadcast.py — broadcast engine: text / photo / video / file, history,
edit, delete and re-broadcast, started from the admin panel OR from the
admin's Telegram chat (/broadcast).

How it works
------------
* A broadcast is a background job; the browser only polls its progress, so
  nothing freezes and no HTTP request is held open (the old SSE stream held a
  gunicorn thread for the whole send and broke behind proxies).
* Every broadcast is stored in Firebase  ->  broadcasts/<id>
  Per-user Telegram message ids         ->  broadcast_deliveries/<id>/<chat_id>
  Per-user failure reasons              ->  broadcast_failures/<id>/<chat_id>
  The delivery ids are what make EDIT and DELETE possible later.
* DELETE IS SOFT: Telegram messages are removed from the users' chats, but the
  broadcast record and every chat-history copy stay in Firebase (deleted=true).
* One job at a time, Telegram 429 flood waits are honoured, users who blocked
  the bot are remembered (`unreachable`) and skipped next time.
* Media: the file is uploaded ONCE (to the first reachable user); Telegram
  returns a file_id which is reused for everybody else and for re-broadcasts.
"""
import io
import random
import re
import string
import threading
import time
from html.parser import HTMLParser

import config
import firebase_helper as fb
import telegram_bot as tg

try:
    from telebot.apihelper import ApiTelegramException
except Exception:                                   # pragma: no cover
    class ApiTelegramException(Exception):
        pass

TEXT_LIMIT = 4096
CAPTION_LIMIT = 1024
KINDS = ("text", "photo", "video", "document")
ALLOWED_TAGS = {
    "b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "a",
    "code", "pre", "tg-spoiler", "tg-emoji", "blockquote", "span",
}


# ── Validation ────────────────────────────────────────────────────────────────
class _HtmlChecker(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.errors = [], []

    def handle_starttag(self, tag, attrs):
        if tag not in ALLOWED_TAGS:
            self.errors.append(f"Telegram does not support the <{tag}> tag")
            return
        if tag == "a" and not dict(attrs).get("href"):
            self.errors.append('<a> needs an href="…"')
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag not in ALLOWED_TAGS:
            return
        if self.stack and self.stack[-1] == tag:
            self.stack.pop()
        else:
            self.errors.append(f"Unexpected closing </{tag}>")

    def handle_data(self, data):
        if "<" in data:
            self.errors.append("A bare '<' must be written as &lt;")


def validate(text: str, fmt: str, kind: str = "text"):
    """Returns an error string, or None when the text can be sent."""
    text = text or ""
    if kind not in KINDS:
        return "Unknown message type"
    if fmt not in ("html", "text"):
        return "Unknown format"
    if kind == "text" and not text.strip():
        return "Message is empty"
    if not text.strip():
        return None                                  # media without caption is fine
    limit = TEXT_LIMIT if kind == "text" else CAPTION_LIMIT
    plain_len = len(tg.html_to_plain(text)) if fmt == "html" else len(text)
    if plain_len > limit:
        what = "Message" if kind == "text" else "Caption"
        return f"{what} is {plain_len} characters — Telegram's limit is {limit}"
    if fmt == "html":
        c = _HtmlChecker()
        try:
            c.feed(text)
            c.close()
        except Exception as e:
            return f"Invalid HTML: {e}"
        if c.errors:
            return c.errors[0]
        if c.stack:
            return f"<{c.stack[-1]}> is never closed"
    return None


def detect_format(text: str) -> str:
    """For /broadcast typed in Telegram: valid Telegram-HTML -> html, otherwise plain."""
    if re.search(r"</?[a-zA-Z][^>]*>", text or "") and validate(text, "html") is None:
        return "html"
    return "text"


# ── State ─────────────────────────────────────────────────────────────────────
_lock = threading.Lock()
_current = None          # the running (or last finished) job dict


def _new_id() -> str:
    return "b" + str(int(time.time() * 1000)) + "".join(random.choices(string.ascii_lowercase, k=3))


def _begin(bid: str, op: str, total: int):
    global _current
    with _lock:
        if _current and _current.get("running"):
            return None
        _current = {"bid": bid, "op": op, "total": total, "done": 0, "failed": 0,
                    "running": True, "error": ""}
        return _current


def busy() -> bool:
    return bool(_current and _current.get("running"))


def _err(e):
    code = getattr(e, "error_code", 0) or 0
    desc = getattr(e, "description", "") or str(e)
    return code, desc


def _retry_call(fn, *a, **kw):
    """Call a Telegram API method, waiting out 429 flood-control replies."""
    for attempt in range(3):
        try:
            return fn(*a, **kw)
        except ApiTelegramException as e:
            code, _ = _err(e)
            if code == 429 and attempt < 2:
                params = (getattr(e, "result_json", None) or {}).get("parameters") or {}
                time.sleep(min(int(params.get("retry_after", 2)) + 1, 30))
                continue
            raise


def _flush(batch: dict):
    """One multi-path PATCH instead of 2 requests per recipient."""
    if batch:
        fb.patch("", dict(batch))
        batch.clear()
        fb._invalidate_index()          # unreachable flags etc. must show up immediately


def _history_entry(cid, tg_id, bid, rec):
    key = f"admin_{tg_id}"
    kind, text, fmt = rec["kind"], rec.get("text", ""), rec["format"]
    plain = tg.html_to_plain(text) if fmt == "html" else text
    entry = {
        "msg_id": key, "chat_id": cid, "type": kind, "from": "admin",
        "text": plain, "raw_text": text, "format": fmt,
        "time": tg.now_str(), "ts": tg.now_ts(),
        "read": False, "delivered": True, "edited": False, "deleted": False,
        "broadcast": True, "broadcast_id": bid, "tg_msg_id": tg_id,
    }
    if kind != "text":
        entry.update({"caption": plain, "file_id": rec.get("file_id", ""),
                      "file_name": rec.get("file_name", "")})
    return key, entry


def _file_id_of(m, kind):
    if kind == "photo":
        return m.photo[-1].file_id
    if kind == "video":
        return m.video.file_id
    return m.document.file_id


def _deliver(cid, rec, file_ref):
    """Send one broadcast item to one chat. file_ref: file_id string or BytesIO."""
    kind, text, fmt = rec["kind"], rec.get("text", ""), rec["format"]
    kw = {"parse_mode": "HTML"} if fmt == "html" else {}
    if kind == "text":
        return tg.bot.send_message(cid, text, **kw)
    cap = text or None
    if kind == "photo":
        return tg.bot.send_photo(cid, file_ref, caption=cap, **kw)
    if kind == "video":
        return tg.bot.send_video(cid, file_ref, caption=cap, **kw)
    return tg.bot.send_document(cid, file_ref, caption=cap, **kw)


# ── Jobs ──────────────────────────────────────────────────────────────────────
def _run_send(job, bid, rec, recipients, file_bytes, notify_admin):
    sent = failed = 0
    last_error = ""
    batch, last_flush = {}, time.time()
    try:
        for i, cid in enumerate(recipients):
            try:
                uploading = bool(file_bytes) and not rec.get("file_id")
                if uploading:
                    ref = io.BytesIO(file_bytes)
                    ref.name = rec.get("file_name") or "file"
                else:
                    ref = rec.get("file_id")
                m = _retry_call(_deliver, cid, rec, ref)
                if rec["kind"] != "text" and not rec.get("file_id"):
                    rec["file_id"] = _file_id_of(m, rec["kind"])
                    batch[f"broadcasts/{bid}/file_id"] = rec["file_id"]
                    file_bytes = None                   # uploaded once, reuse file_id
                sent += 1
                key, entry = _history_entry(cid, m.message_id, bid, rec)
                batch[f"broadcast_deliveries/{bid}/{cid}"] = m.message_id
                batch[f"support/{cid}/messages/{key}"] = entry
            except Exception as e:
                failed += 1
                code, desc = _err(e)
                last_error = f"{cid}: {desc}"
                batch[f"broadcast_failures/{bid}/{cid}"] = desc[:200]
                low = desc.lower()
                if code == 403 or "bot was blocked" in low or "user is deactivated" in low \
                        or "chat not found" in low:
                    # They blocked the bot / deleted their account: skip next time.
                    batch[f"support/{cid}/meta/unreachable"] = True
                    batch[f"chat_index/{cid}/unreachable"] = True
                elif "can't parse entities" in low or "unsupported start tag" in low:
                    job["error"] = desc          # every recipient would fail the same way
                    break
            job["done"], job["failed"] = sent + failed, failed
            if (i + 1) % 20 == 0 or time.time() - last_flush > 2:
                batch[f"broadcasts/{bid}/sent"] = sent
                batch[f"broadcasts/{bid}/failed"] = failed
                _flush(batch)
                last_flush = time.time()
            time.sleep(config.BROADCAST_DELAY)
        state = "ready" if sent > 0 else "failed"
        batch.update({
            f"broadcasts/{bid}/sent": sent, f"broadcasts/{bid}/failed": failed,
            f"broadcasts/{bid}/state": state,
            f"broadcasts/{bid}/last_error": job["error"] or last_error,
            f"broadcasts/{bid}/finished_at": tg.now_str(),
        })
        _flush(batch)
        fb.log_event("broadcast_finished", broadcast_id=bid, total=len(recipients),
                     sent=sent, failed=failed)
    except Exception as e:                              # never leave a job "running"
        job["error"] = str(e)
        fb.patch(f"broadcasts/{bid}", {"state": "interrupted", "last_error": str(e),
                                       "sent": sent, "failed": failed})
        fb.log_event("bot_error", error=f"broadcast {bid}: {e}")
    finally:
        _invalidate()
        job["running"] = False
        if notify_admin and tg.bot:
            lines = [
                "📢 <b>Broadcast finished</b>",
                f"👥 Recipients: <b>{len(recipients)}</b>",
                f"✅ Successful: <b>{sent}</b>",
                f"❌ Failed: <b>{failed}</b>",
            ]
            if job["error"] or (failed and last_error):
                lines.append(f"⚠️ {tg.h((job['error'] or last_error)[:200])}")
            if config.PANEL_URL:
                lines.append(f"🖥️ {tg.h(config.PANEL_URL.rstrip('/'))}/broadcast")
            tg.send_msg(notify_admin, "\n".join(lines))


def _edit_call(rec, cid, tg_id, text, fmt):
    kw = {"parse_mode": "HTML"} if fmt == "html" else {}
    if rec["kind"] == "text":
        return tg.bot.edit_message_text(text, cid, int(tg_id), **kw)
    return tg.bot.edit_message_caption(caption=text, chat_id=cid, message_id=int(tg_id), **kw)


def _run_edit(job, bid, rec, text, fmt, deliveries):
    ok = failed = 0
    batch, last_flush = {}, time.time()
    plain = tg.html_to_plain(text) if fmt == "html" else text
    media = rec["kind"] != "text"
    try:
        for i, (cid, tg_id) in enumerate(deliveries.items()):
            try:
                _retry_call(_edit_call, rec, cid, tg_id, text, fmt)
                good = True
            except Exception as e:
                code, desc = _err(e)
                good = "message is not modified" in desc.lower()
                if not good:
                    job["error"] = f"{cid}: {desc}"
                    if "can't parse entities" in desc.lower():
                        failed += 1
                        break
            if good:
                ok += 1
                base = f"support/{cid}/messages/admin_{tg_id}"
                batch[f"{base}/text"] = plain
                batch[f"{base}/raw_text"] = text
                batch[f"{base}/format"] = fmt
                if media:
                    batch[f"{base}/caption"] = plain
                batch[f"{base}/edited"] = True
                batch[f"{base}/edited_at"] = tg.now_str()
            else:
                failed += 1
            job["done"], job["failed"] = ok + failed, failed
            if (i + 1) % 20 == 0 or time.time() - last_flush > 2:
                _flush(batch)
                last_flush = time.time()
            time.sleep(config.BROADCAST_DELAY)
        batch.update({
            f"broadcasts/{bid}/state": "ready",
            f"broadcasts/{bid}/edit_ok": ok, f"broadcasts/{bid}/edit_failed": failed,
            f"broadcasts/{bid}/last_error": job["error"],
        })
        _flush(batch)
        fb.log_event("broadcast_edited", broadcast_id=bid, ok=ok, failed=failed)
    except Exception as e:
        job["error"] = str(e)
        fb.patch(f"broadcasts/{bid}", {"state": "interrupted", "last_error": str(e)})
    finally:
        _invalidate()
        job["running"] = False


def _run_delete(job, bid, deliveries):
    ok = failed = 0
    batch, last_flush = {}, time.time()
    try:
        for i, (cid, tg_id) in enumerate(deliveries.items()):
            try:
                _retry_call(tg.bot.delete_message, cid, int(tg_id))
                good = True
            except Exception as e:
                _, desc = _err(e)
                good = "message to delete not found" in desc.lower()
                if not good:
                    job["error"] = f"{cid}: {desc}"
            ok += 1 if good else 0
            failed += 0 if good else 1
            # SOFT delete: the chat-history copy is only flagged, never removed
            base = f"support/{cid}/messages/admin_{tg_id}"
            batch[f"{base}/deleted"] = True
            batch[f"{base}/deleted_at"] = tg.now_str()
            batch[f"{base}/deleted_by"] = "admin"
            job["done"], job["failed"] = ok + failed, failed
            if (i + 1) % 20 == 0 or time.time() - last_flush > 2:
                _flush(batch)
                last_flush = time.time()
            time.sleep(config.BROADCAST_DELAY)
        batch.update({
            f"broadcasts/{bid}/state": "deleted",
            f"broadcasts/{bid}/delete_ok": ok, f"broadcasts/{bid}/delete_failed": failed,
            f"broadcasts/{bid}/last_error": job["error"],
        })
        _flush(batch)
        fb.log_event("broadcast_deleted", broadcast_id=bid, removed=ok, failed=failed)
    except Exception as e:
        job["error"] = str(e)
        fb.patch(f"broadcasts/{bid}", {"state": "interrupted", "last_error": str(e)})
    finally:
        _invalidate()
        job["running"] = False


# ── Public API (called from Flask routes and from the Telegram bot) ───────────
_snap = {"ts": 0.0, "data": None}


def _invalidate():
    _snap["ts"] = 0.0


def start_send(text: str = "", fmt: str = "html", rebroadcast_of: str = "", kind: str = "text",
               file_id: str = "", file_name: str = "", file_bytes: bytes = None,
               source: str = "panel", notify_admin: str = ""):
    if not tg.bot:
        return {"ok": False, "error": "Bot is not configured (BOT_TOKEN missing)"}
    err = validate(text, fmt, kind)
    if err:
        return {"ok": False, "error": err}
    if kind != "text" and not file_id and not file_bytes:
        return {"ok": False, "error": "No file attached"}
    recipients = tg._all_user_chat_ids()
    if not recipients:
        return {"ok": False, "error": "No recipients — nobody has messaged the bot yet"}

    bid = _new_id()
    job = _begin(bid, "send", len(recipients))
    if not job:
        return {"ok": False, "error": "Another broadcast operation is still running", "busy": True}

    record = {
        "id": bid, "kind": kind, "text": text, "format": fmt, "state": "sending",
        "source": source, "created_at": tg.now_str(), "created_ts": tg.now_ts(),
        "total": len(recipients), "sent": 0, "failed": 0,
        "edited": False, "deleted": False, "last_error": "",
    }
    if kind != "text":
        record["file_id"] = file_id or ""
        record["file_name"] = file_name or ""
    if rebroadcast_of:
        record["rebroadcast_of"] = rebroadcast_of
    if fb.put(f"broadcasts/{bid}", record) is None:
        job["running"] = False
        return {"ok": False, "error": "Could not save the broadcast to Firebase"}
    fb.log_event("broadcast_started", broadcast_id=bid, total=len(recipients), fmt=fmt,
                 kind=kind, source=source, resend_of=rebroadcast_of or "")
    _invalidate()
    threading.Thread(target=_run_send, args=(job, bid, dict(record), recipients, file_bytes, notify_admin),
                     daemon=True, name=f"bc-send-{bid}").start()
    return {"ok": True, "id": bid, "total": len(recipients)}


def _get_record(bid: str):
    rec = fb.get(f"broadcasts/{bid}")
    if isinstance(rec, dict):
        rec.setdefault("kind", "text")
        return rec
    return None


def _deliveries(bid: str) -> dict:
    d = fb.get(f"broadcast_deliveries/{bid}")
    return d if isinstance(d, dict) else {}


def start_resend(bid: str):
    rec = _get_record(bid)
    if not rec:
        return {"ok": False, "error": "Broadcast not found"}
    return start_send(rec.get("text", ""), rec.get("format", "html"), rebroadcast_of=bid,
                      kind=rec["kind"], file_id=rec.get("file_id", ""),
                      file_name=rec.get("file_name", ""), source="panel")


def start_edit(bid: str, text: str, fmt: str):
    if not tg.bot:
        return {"ok": False, "error": "Bot is not configured"}
    rec = _get_record(bid)
    if not rec:
        return {"ok": False, "error": "Broadcast not found"}
    if rec.get("deleted") or rec.get("state") == "deleted":
        return {"ok": False, "error": "A deleted broadcast cannot be edited"}
    if rec.get("state") in ("sending", "editing", "deleting"):
        return {"ok": False, "error": "Wait until the current operation has finished"}
    err = validate(text, fmt, rec["kind"])
    if err:
        return {"ok": False, "error": err}
    deliveries = _deliveries(bid)
    if not deliveries:
        return {"ok": False, "error": "No delivered messages were recorded for this broadcast"}

    job = _begin(bid, "edit", len(deliveries))
    if not job:
        return {"ok": False, "error": "Another broadcast operation is still running", "busy": True}

    # Keep the previous version (nothing is overwritten without a trace)
    fb.post(f"broadcast_history/{bid}", {"text": rec.get("text", ""), "format": rec.get("format", "html"),
                                         "replaced_at": tg.now_str()})
    fb.patch(f"broadcasts/{bid}", {
        "text": text, "format": fmt, "state": "editing", "edited": True,
        "edited_at": tg.now_str(), "edit_count": int(rec.get("edit_count") or 0) + 1,
    })
    fb.log_event("broadcast_edit_started", broadcast_id=bid, total=len(deliveries))
    _invalidate()
    threading.Thread(target=_run_edit, args=(job, bid, rec, text, fmt, deliveries),
                     daemon=True, name=f"bc-edit-{bid}").start()
    return {"ok": True, "id": bid, "total": len(deliveries)}


def start_delete(bid: str):
    if not tg.bot:
        return {"ok": False, "error": "Bot is not configured"}
    rec = _get_record(bid)
    if not rec:
        return {"ok": False, "error": "Broadcast not found"}
    if rec.get("deleted") or rec.get("state") == "deleted":
        return {"ok": False, "error": "Already deleted"}
    if rec.get("state") in ("sending", "editing", "deleting"):
        return {"ok": False, "error": "Wait until the current operation has finished"}
    deliveries = _deliveries(bid)

    job = _begin(bid, "delete", len(deliveries))
    if not job:
        return {"ok": False, "error": "Another broadcast operation is still running", "busy": True}

    fb.patch(f"broadcasts/{bid}", {
        "deleted": True, "deleted_at": tg.now_str(), "state": "deleting",
    })
    fb.log_event("broadcast_delete_started", broadcast_id=bid, total=len(deliveries))
    _invalidate()
    threading.Thread(target=_run_delete, args=(job, bid, deliveries),
                     daemon=True, name=f"bc-del-{bid}").start()
    return {"ok": True, "id": bid, "total": len(deliveries)}


def details(bid: str) -> dict:
    rec = _get_record(bid)
    if not rec:
        return {"ok": False, "error": "Broadcast not found"}
    fails = fb.get(f"broadcast_failures/{bid}")
    fails = fails if isinstance(fails, dict) else {}
    idx = fb.chat_index()
    out = []
    for cid, reason in fails.items():
        meta = idx.get(cid) or {}
        out.append({"chat_id": cid, "name": meta.get("user_name") or cid,
                    "username": meta.get("username") or "", "error": str(reason)})
    return {"ok": True, "failures": out, "record": {k: rec.get(k) for k in
            ("id", "state", "sent", "failed", "total", "last_error", "source", "kind")}}


def snapshot() -> dict:
    """History for the UI, newest first, with live progress and totals."""
    now = time.time()
    if _snap["data"] is None or now - _snap["ts"] > 1.0:
        raw = fb.get("broadcasts") or {}
        items = [v for v in raw.values() if isinstance(v, dict)]
        for r in items:
            r.setdefault("kind", "text")
        items.sort(key=lambda r: r.get("created_ts", 0), reverse=True)
        _snap["data"] = items[: config.BROADCAST_HISTORY_LIMIT]
        _snap["ts"] = now
    items = [dict(r) for r in _snap["data"]]
    cur = _current
    if cur and cur.get("running"):
        for r in items:
            if r.get("id") == cur["bid"]:
                r["live"] = {"op": cur["op"], "total": cur["total"],
                             "done": cur["done"], "failed": cur["failed"]}
    delivered = sum(int(r.get("sent") or 0) for r in items)
    failed = sum(int(r.get("failed") or 0) for r in items)
    return {
        "items": items,
        "busy": busy(),
        "active": cur["bid"] if busy() else "",
        "recipients": len(tg._all_user_chat_ids()),
        "stats": {
            "broadcasts": len(items),
            "delivered": delivered,
            "failed": failed,
            "rate": round(100 * delivered / (delivered + failed)) if (delivered + failed) else 0,
        },
    }


def recover_interrupted():
    """After a restart, jobs that were mid-flight can never finish — say so."""
    try:
        raw = fb.get("broadcasts") or {}
        for bid, rec in raw.items():
            if isinstance(rec, dict) and rec.get("state") in ("sending", "editing", "deleting"):
                fb.patch(f"broadcasts/{bid}", {
                    "state": "interrupted",
                    "last_error": "Server restarted while this was running — "
                                  "already-delivered messages are still recorded.",
                })
    except Exception as e:
        print(f"[BROADCAST] recover: {e}")
