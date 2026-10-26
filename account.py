"""
account.py — the super admin's PERSONAL Telegram account (MTProto via Telethon).

What it does
------------
* Link:  Settings -> phone number -> code (sent inside Telegram) -> 2FA password if the
         account has one. Only the session string is stored (Firebase `private/account`,
         unreadable for browsers because the rules are locked). Nothing about the account
         (name, number, id) is kept, shown, logged or notified.
* Account tab: every PRIVATE chat with a real person (no bots, no groups/channels, no
  "Saved Messages") is stored like a bot chat, under the chat id  a<user id>  with
  meta.source = "account". The panel can read, reply, edit, delete, react and send files.
* Photos / videos / files RECEIVED in those chats are forwarded to the channel set in
  Settings, with "Forwarded from: full name, @username, chat id, date, time" below.
* Edits are never overwritten (every version is kept) and deletions never delete data:
  the message stays and gets `tg_deleted` (the panel shows 🚫).
* Username changes of P2P users are detected and only the super admin is told.
* Optional AI answers (Groq) for these chats — off until `/account onai`.
* Redeploy-safe session: the saved session is NEVER deleted automatically (only "Unlink" does
  that). After a redeploy a supervisor keeps retrying (with back-off) until the account is
  connected again, so a slow network / Firebase hiccup at boot can no longer leave it "unlinked".
  Only one instance uses the session at a time (lock in Firebase, released on shutdown), because
  Telegram kills a session that is used from two places at once during an overlapping deploy.
* Redeploy-safe: nothing is fetched twice. The history import never overwrites stored data, and
  after every start a CATCH-UP fetches only what arrived while the server was down (newer than the
  last stored message of each chat) and forwards that media once.

Everything runs on one asyncio loop in its own thread; the Flask / bot threads call the
small synchronous wrappers at the bottom.
"""
import asyncio
import atexit
import datetime
import io
import threading
import time
import uuid

import ai
import config
import firebase_helper as fb
import settings

try:                                    # telethon is optional — the rest of the app works without it
    from telethon import TelegramClient, events, errors, functions
    from telethon import types as ttypes
    from telethon.sessions import StringSession
    HAVE_TELETHON = True
except Exception:                       # pragma: no cover
    HAVE_TELETHON = False

SESSION_PATH = "private/account/session"
LOCK_PATH = "private/account/lock"                # which instance currently owns the session
IMPORT_FLAG = "private/account/imported"
ALIVE_PATH = "private/account/alive_ts"          # heartbeat: when this server last ran
MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024


class AccountError(Exception):
    """Message is safe to show in the panel."""


# ── ids ──────────────────────────────────────────────────────────────────────
def cid_of(uid) -> str:
    return f"a{int(uid)}"


def is_acct(cid) -> bool:
    c = str(cid or "")
    return len(c) > 1 and c[0] == "a" and c[1:].isdigit()


def uid_of(cid) -> int:
    return int(str(cid)[1:])


# ── runtime state ────────────────────────────────────────────────────────────
_loop = None
_client = None
_me_id = None
_auth = {"client": None, "phone": "", "hash": "", "step": "", "ts": 0.0}   # linking in progress
_state_lock = threading.RLock()
_own_sent = {}          # msg id -> time   (sent through our own API; the API stores them itself)
_own_edit = {}
_own_del = {}
_meta_seen = {}         # cid -> {"name": .., "username": ..}
_forward_ent = {}       # channel -> entity
INSTANCE = uuid.uuid4().hex[:12]                  # this process
_st = {"saved": None, "dead": False, "lost": False, "sess": "", "hb": False, "sup": False}
BOOT_DELAY = 45.0       # seconds after (re)connecting before the catch-up / import start
LOCK_STALE = 60.0       # a lock whose heartbeat is older than this (seconds) is dead
HEARTBEAT = 20.0
_BACKOFF = [3.0, 300.0]  # supervisor retry delay: first, maximum (seconds)


def configured() -> bool:
    return bool(HAVE_TELETHON and config.TG_API_ID and config.TG_API_HASH)


def linked() -> bool:
    """Connected right now."""
    return _client is not None and _me_id is not None


def status() -> dict:
    """What the Settings page may show: nothing about the account itself.
    linked = a session is saved (it stays 'linked' while the server reconnects after a redeploy);
    online = connected right now; dead = Telegram revoked the session (link again)."""
    online = linked()
    return {
        "telethon": HAVE_TELETHON,
        "configured": configured(),
        # while a restarted server is still reading / connecting the saved session it is NOT "unlinked"
        "linked": bool(online or _st["saved"] or _st["dead"] or (_st["saved"] is None and _st["sup"])),
        "online": online,
        "dead": bool(_st["dead"]),
        "step": _auth["step"] if _auth["client"] is not None else "",
    }


def _ensure_loop():
    global _loop
    with _state_lock:
        if _loop is not None:
            return _loop
        _loop = asyncio.new_event_loop()

        def run():
            asyncio.set_event_loop(_loop)
            _loop.run_forever()
        threading.Thread(target=run, daemon=True, name="tg-account-loop").start()
        return _loop


def _run(coro, timeout: float = 60):
    loop = _ensure_loop()
    fut = asyncio.run_coroutine_threadsafe(coro, loop)
    try:
        return fut.result(timeout)
    except AccountError:
        raise
    except Exception as e:
        raise AccountError(_nice_error(e))


def _nice_error(e) -> str:
    if not HAVE_TELETHON:
        return str(e)
    name = type(e).__name__
    table = {
        "PhoneNumberInvalidError": "Invalid phone number — use the international format, e.g. +919876543210",
        "PhoneNumberBannedError": "This phone number is banned by Telegram",
        "PhoneNumberFloodError": "Too many attempts — try again later",
        "FloodWaitError": f"Telegram asks to wait ({getattr(e, 'seconds', '?')}s) — try again later",
        "PhoneCodeInvalidError": "Wrong code",
        "PhoneCodeExpiredError": "The code expired — press Send OTP again",
        "PasswordHashInvalidError": "Wrong 2FA password",
        "SessionPasswordNeededError": "2FA password needed",
        "ApiIdInvalidError": "API ID / API HASH are wrong",
        "TimeoutError": "Telegram did not answer in time — try again",
    }
    return table.get(name) or (str(e) or name)


def _tg():
    import telegram_bot as tg           # lazy: telegram_bot imports this module
    return tg


def _ist(dt) -> datetime.datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(config.IST)


def _stamp(dt) -> str:
    return _ist(dt).strftime("%Y-%m-%d %H:%M:%S")


def _gc(d: dict, keep: float = 180.0):
    now = time.time()
    for k in [k for k, t in d.items() if now - t > keep]:
        d.pop(k, None)


# ── start / link / unlink ────────────────────────────────────────────────────
def start():
    """Called once at app start: keep the saved session connected for the life of the process."""
    if not configured():
        return
    with _state_lock:
        if _st["sup"]:
            return
        _st["sup"] = True
    threading.Thread(target=_supervisor, daemon=True, name="account-supervisor").start()


def _supervisor():
    """Retries (1) reading the session, (2) taking the lock, (3) connecting — with back-off — until
    the account is online. A failure here NEVER removes the saved session: only a session that
    Telegram itself reports as revoked marks the account 'dead', and even then it is kept."""
    delay = _BACKOFF[0]
    try:
        while True:
            try:
                ok, sess, _ = fb._request("GET", SESSION_PATH)
                if not ok:
                    raise RuntimeError("Firebase not reachable")
                if not (isinstance(sess, str) and sess):
                    _st["saved"] = False                       # nothing linked (linking starts the connection itself)
                    return
                _st["saved"], _st["sess"] = True, sess
                if _client is not None:
                    return                                     # linked in the meantime
                if not _acquire_lock():
                    continue                                   # another instance is still using the session
                res = _run(_connect(sess), timeout=240)
                if res == "dead":
                    _st["dead"] = True
                    _release_lock()
                    return
                _st["dead"] = False
                return
            except Exception as e:
                print(f"[ACCOUNT] connect failed, retrying in {delay:.0f}s: {e}")
                _release_lock()
                time.sleep(delay)
                delay = min(delay * 2, _BACKOFF[1])
    finally:
        _st["sup"] = False


_DEAD_ERRORS = tuple(getattr(errors, n) for n in (
    "AuthKeyUnregisteredError", "SessionRevokedError", "SessionExpiredError",
    "UserDeactivatedError", "UserDeactivatedBanError", "AuthKeyDuplicatedError") if hasattr(errors, n)) if HAVE_TELETHON else ()


async def _connect(sess: str) -> str:
    """'ok' | 'dead'. Transient problems raise (the supervisor retries)."""
    global _client, _me_id
    client = TelegramClient(StringSession(sess), config.TG_API_ID, config.TG_API_HASH,
                            connection_retries=-1, retry_delay=3, auto_reconnect=True)
    try:
        await client.connect()
        authorized = await client.is_user_authorized()
        if not authorized:                                    # make sure it is not a one-off
            await asyncio.sleep(3)
            authorized = await client.is_user_authorized()
        if not authorized:
            await client.disconnect()
            return "dead"
        me = await client.get_me()
    except _DEAD_ERRORS:
        try:
            await client.disconnect()
        except Exception:
            pass
        return "dead"
    except Exception:
        try:
            await client.disconnect()
        except Exception:
            pass
        raise
    with _state_lock:
        _client, _me_id = client, me.id
        _st.update(dead=False, lost=False, saved=True)
    _register(client)
    # No dialog scan at boot: peers are resolved on demand from the stored access hash (see _peer).
    # The catch-up / import are heavy (pure-python crypto on a small instance), so they start
    # AFTER the website has had time to come up.
    _later(_import_history if not fb.get(IMPORT_FLAG) else _catch_up, BOOT_DELAY)
    _start_heartbeat()
    return "ok"


def _later(fn, delay: float):
    t = threading.Timer(delay, lambda: fb.bg(fn))
    t.daemon = True
    t.start()


# ── one instance at a time ───────────────────────────────────────────────────
def _write_lock():
    fb.put(LOCK_PATH, {"owner": INSTANCE, "ts": time.time()})


def _acquire_lock(timeout: float = 150.0) -> bool:
    """Take the session lock, or wait until its owner is gone (released on shutdown, or no
    heartbeat for LOCK_STALE seconds). A deploy that overlaps the old instance therefore waits
    for it instead of using the same session from two places."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        ok, cur, _ = fb._request("GET", LOCK_PATH)
        if ok:
            fresh = isinstance(cur, dict) and (time.time() - float(cur.get("ts", 0))) < LOCK_STALE
            if not fresh or cur.get("owner") == INSTANCE:
                _write_lock()
                time.sleep(min(2.0, LOCK_STALE / 10))        # a racing instance would overwrite us now
                ok2, cur2, _ = fb._request("GET", LOCK_PATH)
                if ok2 and isinstance(cur2, dict) and cur2.get("owner") == INSTANCE:
                    return True
        time.sleep(min(3.0, LOCK_STALE / 10))
    return False


def _release_lock():
    try:
        ok, cur, _ = fb._request("GET", LOCK_PATH)
        if ok and isinstance(cur, dict) and cur.get("owner") == INSTANCE:
            fb._request("DELETE", LOCK_PATH)
    except Exception as e:
        print(f"[ACCOUNT] release lock: {e}")


def shutdown():
    """Process is stopping (redeploy): hand the session over — disconnect WITHOUT logging out."""
    c = _client
    if c is not None and _loop is not None:
        try:
            asyncio.run_coroutine_threadsafe(c.disconnect(), _loop).result(5)
        except Exception:
            pass
    _release_lock()


atexit.register(shutdown)


def _start_heartbeat():
    with _state_lock:
        if _st["hb"]:
            return
        _st["hb"] = True
    asyncio.ensure_future(_heartbeat())


def _persist_session():
    """Telethon can change the saved session (DC migration / new auth key). Keep Firebase current."""
    c = _client
    if c is None:
        return
    cur = c.session.save()
    if cur and cur != _st["sess"]:
        if fb.put(SESSION_PATH, cur) is not None:
            _st["sess"] = cur


def _beat(n: int):
    """One heartbeat (worker thread): refresh the lock, remember we are alive, keep the session current."""
    ok, cur, _ = fb._request("GET", LOCK_PATH)
    if ok and isinstance(cur, dict) and cur.get("owner") not in (None, INSTANCE) \
            and (time.time() - float(cur.get("ts", 0))) < LOCK_STALE:
        _st["lost"] = True                                   # another instance took over: step aside
        return
    _write_lock()
    if n % 3 == 0:
        fb.put(ALIVE_PATH, int(time.time()))
    if n % 30 == 0:
        _persist_session()


async def _heartbeat():
    global _client, _me_id
    loop = asyncio.get_event_loop()
    n = 0
    try:
        while _client is not None:
            try:
                await loop.run_in_executor(None, _beat, n)
            except Exception as e:
                print(f"[ACCOUNT] heartbeat: {e}")
            if _st["lost"]:
                c = _client
                with _state_lock:
                    _client, _me_id = None, None
                try:
                    await c.disconnect()
                except Exception:
                    pass
                return
            n += 1
            await asyncio.sleep(HEARTBEAT)
    finally:
        _st["hb"] = False


def _phone(p: str) -> str:
    p = "".join(ch for ch in str(p or "") if ch.isdigit() or ch == "+")
    if p and not p.startswith("+"):
        p = "+" + p
    return p


def send_code(phone: str) -> dict:
    if not HAVE_TELETHON:
        raise AccountError("Install the 'telethon' package (see requirements.txt)")
    if not configured():
        raise AccountError("Set TG_API_ID and TG_API_HASH in the environment variables first")
    phone = _phone(phone)
    if len(phone) < 8:
        raise AccountError("Enter your mobile number with the country code, e.g. +919876543210")
    _run(_do_send_code(phone), timeout=45)
    return {"ok": True, "step": "code"}


async def _do_send_code(phone: str):
    old = _auth["client"]
    if old is not None:
        try:
            await old.disconnect()
        except Exception:
            pass
    c = TelegramClient(StringSession(), config.TG_API_ID, config.TG_API_HASH)
    await c.connect()
    sent = await c.send_code_request(phone)
    _auth.update(client=c, phone=phone, hash=sent.phone_code_hash, step="code", ts=time.time())


def verify_code(code: str) -> dict:
    if _auth["client"] is None:
        raise AccountError("Press Send OTP first")
    code = "".join(ch for ch in str(code or "") if ch.isdigit())
    if not code:
        raise AccountError("Enter the code you received in Telegram")
    return _run(_do_verify_code(code), timeout=45)


async def _do_verify_code(code: str) -> dict:
    c = _auth["client"]
    try:
        await c.sign_in(phone=_auth["phone"], code=code, phone_code_hash=_auth["hash"])
    except errors.SessionPasswordNeededError:
        _auth["step"] = "password"
        return {"ok": True, "step": "password"}
    await _finish_login(c)
    return {"ok": True, "step": "done"}


def verify_password(password: str) -> dict:
    if _auth["client"] is None or _auth["step"] != "password":
        raise AccountError("Verify the code first")
    if not password:
        raise AccountError("Enter your 2FA password")
    return _run(_do_verify_password(password), timeout=45)


async def _do_verify_password(password: str) -> dict:
    c = _auth["client"]
    await c.sign_in(password=password)
    await _finish_login(c)
    return {"ok": True, "step": "done"}


async def _finish_login(c):
    """Save the session, go live. NOTHING about the account is shown, logged or sent anywhere."""
    global _client, _me_id
    me = await c.get_me()
    sess = c.session.save()
    if fb.put(SESSION_PATH, sess) is None:
        raise AccountError("Could not save the session to Firebase")
    old = _client
    _write_lock()                                    # this instance owns the session now
    with _state_lock:
        _client, _me_id = c, me.id
        _auth.update(client=None, phone="", hash="", step="", ts=0.0)
        _st.update(saved=True, dead=False, lost=False, sess=sess)
    if old is not None and old is not c:
        try:
            await old.disconnect()
        except Exception:
            pass
    _register(c)
    fb.put(IMPORT_FLAG, False)
    _later(_import_history, 3)
    _start_heartbeat()


def unlink() -> dict:
    """Log the session out at Telegram and forget it. Stored chats stay in Firebase."""
    global _client, _me_id
    c = _client
    if c is not None:
        try:
            _run(c.log_out(), timeout=30)
        except Exception as e:
            print(f"[ACCOUNT] log_out: {e}")
    with _state_lock:
        _client, _me_id = None, None
        _st.update(saved=False, dead=False, sess="")
    fb.put(SESSION_PATH, "")                          # the ONLY place the saved session is removed
    _release_lock()
    return {"ok": True}


# ── event handlers ───────────────────────────────────────────────────────────
def _register(client):
    client.add_event_handler(_on_new, events.NewMessage())
    client.add_event_handler(_on_edit, events.MessageEdited())
    client.add_event_handler(_on_deleted, events.MessageDeleted())
    client.add_event_handler(_on_raw, events.Raw(types=(ttypes.UpdateUserName,
                                                       ttypes.UpdateMessageReactions)))


def _full_name(u) -> str:
    return f"{getattr(u, 'first_name', '') or ''} {getattr(u, 'last_name', '') or ''}".strip() or "Unknown"


def _username_of(u) -> str:
    un = getattr(u, "username", None)
    if not un:
        for x in (getattr(u, "usernames", None) or []):
            if getattr(x, "active", True) and getattr(x, "username", ""):
                un = x.username
                break
    return un or ""


def _is_person(u) -> bool:
    return (isinstance(u, ttypes.User) and not u.bot and not getattr(u, "deleted", False)
            and u.id != _me_id and u.id != 777000)


def _kind(msg):
    """(type, file_name) for photo/video/document messages, else (None, None)."""
    if msg.photo:
        return "photo", None
    if msg.video and not msg.video_note:
        return "video", (msg.file.name if msg.file and msg.file.name else "video.mp4")
    if msg.document or msg.video_note or msg.voice or msg.audio:
        name = (msg.file.name if msg.file and msg.file.name else
                ("voice.ogg" if msg.voice else "video_note.mp4" if msg.video_note else "file"))
        return "document", name
    return None, None


async def _on_new(event):
    try:
        if not event.is_private:
            return
        msg = event.message
        user = await event.get_chat()
        if not _is_person(user):
            return
        info = {
            "uid": user.id, "name": _full_name(user), "username": _username_of(user),
            "hash": getattr(user, "access_hash", None),
            "out": bool(msg.out), "id": msg.id, "ts": int(msg.date.timestamp()),
            "time": _stamp(msg.date), "text": msg.message or "",
            "reply_id": msg.reply_to.reply_to_msg_id if msg.reply_to else None,
        }
        info["type"], info["file_name"] = _kind(msg)
        info["size"] = getattr(msg.file, "size", 0) if msg.file else 0
        if info["out"]:
            await asyncio.sleep(1.0)                    # let our own API register what it sent
            if msg.id in _own_sent:
                return
        elif info["type"] and not msg.sticker:
            asyncio.ensure_future(_forward_media(msg, info))
        fb.bg(_process_message, info)
    except Exception as e:
        print(f"[ACCOUNT] new: {e}")


def _meta(cid: str) -> dict:
    return _tg().get_meta(cid)


_seen_msgs = {}                 # (uid, id, out) -> 1 : the same message is never handled twice per run


def _first_time(info: dict) -> bool:
    k = (info["uid"], info["id"], bool(info["out"]))
    with _state_lock:
        if k in _seen_msgs:
            return False
        _seen_msgs[k] = 1
        if len(_seen_msgs) > 20000:
            for old in list(_seen_msgs)[:5000]:
                _seen_msgs.pop(old, None)
    return True


def _process_message(info: dict, imported: bool = False, catchup: bool = False):
    """Store one P2P message (worker thread).
    imported: old history — stored quietly, never overwrites, triggers nothing.
    catchup:  arrived while we were offline — handled like a live message, except no AI answer."""
    if not _first_time(info):
        return
    tg = _tg()
    uid, cid = info["uid"], cid_of(info["uid"])
    out = info["out"]
    key = f"admin_{info['id']}" if out else str(info["id"])
    meta = tg.get_meta(cid)
    text = info["text"]
    if not text and not info["type"]:
        text = "[unsupported message]"
    data = {
        "msg_id": key, "chat_id": cid, "tg_id": info["id"], "source": "account",
        "text": text, "type": info["type"] or "text",
        "from": "admin" if out else "user",
        "time": info["time"], "ts": info["ts"],
        "read": bool(out or imported), "delivered": True, "edited": False, "deleted": False,
    }
    if out:
        data["sent_by"] = "super"
    else:
        data["user_name"], data["username"] = info["name"], info["username"]
    if info["type"]:
        data.update(caption=text, file_id=f"acct:{info['id']}", file_name=info["file_name"] or "file")
        if info.get("size"):
            data["file_size"] = info["size"]
    if info.get("reply_id"):
        rk = _resolve_key(cid, info["reply_id"])
        target = fb.get(f"support/{cid}/messages/{rk}") if rk else None
        data["reply_to"] = {"msg_id": rk, "from": (target or {}).get("from", "user"),
                            "type": (target or {}).get("type", "text"),
                            "text": tg._snippet(target or {"text": "Message"})}
    extra = {f"acct_idx/{info['id']}": cid}
    if info.get("hash") and not meta.get("ah"):
        extra[f"support/{cid}/meta/ah"] = info["hash"]
    if not out:
        extra[f"support/{cid}/meta/uid"] = uid
        # username / name change?  (only the super admin is told)
        if not imported:
            _check_identity(cid, uid, info["name"], info["username"], meta)
    # the AI's context: remember both sides (also my own messages typed on the phone)
    hist = ai.context(cid, exclude=key) if (not out and not imported) else None
    if not imported:
        ai.remember(cid, "assistant" if out else "user", text if (text and text != "[unsupported message]") else f"[{data['type']}]")
    tg.store_message(cid, key, data, extra=extra, count_unread=(not out and not imported))
    if not imported:
        fb.log_event("message_in" if not out else "message_out", scope="super", actor="system",
                     chat_id=cid, msg_type=data["type"], source="account")
    if not out and not imported and not catchup and text:
        _ai_answer(cid, uid, info, hist or [])


def _resolve_key(cid: str, tg_id) -> str:
    for cand in (str(tg_id), f"admin_{tg_id}"):
        if fb.get(f"support/{cid}/messages/{cand}/from"):
            return cand
    return ""


def _check_identity(cid, uid, name, username, meta):
    tg = _tg()
    seen = _meta_seen.get(cid) or {"name": meta.get("user_name"), "username": meta.get("username")}
    if meta.get("user_name") is not None and seen.get("username") is not None \
            and (seen.get("username") or "") != (username or ""):
        tg.notify_username_change(cid, uid, name, seen.get("username") or "", username or "", audience="super")
    _meta_seen[cid] = {"name": name, "username": username}


# ── AI for P2P chats ─────────────────────────────────────────────────────────
def _ai_answer(cid: str, uid: int, info: dict, hist: list):
    tg = _tg()
    try:
        s = settings.load()
        if not s.get("ai_account_enabled"):
            return
        meta = tg.get_meta(cid)
        if meta.get("ai_off") or not ai.available():
            return
        text = info["text"].strip()
        if not text:
            return
        answer = ai.ask(text, s.get("ai_prompt_account") or "", history=hist,     # last 10 messages
                        context=f"The other person's name: {info['name']}")
        if not answer:
            return
        res = send_text(uid, answer, reply_to_id=info["id"], _from_ai=True)
        ai.remember(cid, "assistant", answer)
        data = _out_record(cid, res, answer, sent_by="ai")
        data["ai"] = True
        tg.store_message(cid, data["msg_id"], data, extra={f"acct_idx/{res['id']}": cid})
        ai.record("account", cid, uid, info["name"], info["username"], text, answer)
    except Exception as e:
        print(f"[ACCOUNT-AI] {e}")
        fb.log_event("bot_error", scope="super", actor="system", error=f"account ai: {e}")


def _out_record(cid, res, text, sent_by="super", ftype="text", fname="", rinfo=None) -> dict:
    d = {
        "msg_id": f"admin_{res['id']}", "chat_id": cid, "tg_id": res["id"], "source": "account",
        "text": text, "type": ftype, "from": "admin", "sent_by": sent_by,
        "time": res["time"], "ts": res["ts"],
        "read": False, "delivered": True, "edited": False, "deleted": False,
    }
    if ftype != "text":
        d.update(caption=text, file_id=f"acct:{res['id']}", file_name=fname or "file")
    if rinfo:
        d["reply_to"] = rinfo
    return d


# ── edits / deletes / raw updates ────────────────────────────────────────────
async def _on_edit(event):
    try:
        if not event.is_private:
            return
        msg = event.message
        if msg.id in _own_edit and time.time() - _own_edit[msg.id] < 8:
            return
        user = await event.get_chat()
        if not _is_person(user):
            return
        fb.bg(_process_edit, user.id, msg.id, bool(msg.out), msg.message or "")
    except Exception as e:
        print(f"[ACCOUNT] edit: {e}")


def _process_edit(uid: int, msg_id: int, out: bool, new_text: str):
    cid = cid_of(uid)
    key = f"admin_{msg_id}" if out else str(msg_id)
    _tg().record_edit(cid, key, new_text)


async def _on_deleted(event):
    try:
        ids = [i for i in (event.deleted_ids or []) if i not in _own_del]
        if ids:
            fb.bg(_process_deleted, ids)
    except Exception as e:
        print(f"[ACCOUNT] deleted: {e}")


def _process_deleted(ids):
    """Never deletes data: flags the stored message so the panel can show 🚫."""
    _gc(_own_del)
    for i in ids:
        cid = fb.get(f"acct_idx/{i}")
        if not isinstance(cid, str) or not cid:
            continue
        for key, by in ((str(i), "user"), (f"admin_{i}", "me")):
            m = fb.get(f"support/{cid}/messages/{key}")
            if isinstance(m, dict) and not m.get("tg_deleted"):
                fb.patch(f"support/{cid}/messages/{key}",
                         {"tg_deleted": True, "tg_deleted_at": _tg().now_str(), "tg_deleted_by": by})
                fb.log_event("message_deleted_remote", scope="super", actor="system",
                             chat_id=cid, msg_id=key, by=by, source="account")
                break


async def _on_raw(upd):
    try:
        if isinstance(upd, ttypes.UpdateUserName):
            fb.bg(_process_username, upd.user_id, getattr(upd, "first_name", ""), getattr(upd, "last_name", ""),
                  next((x.username for x in (getattr(upd, "usernames", None) or []) if getattr(x, "active", True)), ""))
        elif isinstance(upd, ttypes.UpdateMessageReactions) and isinstance(upd.peer, ttypes.PeerUser):
            results = getattr(upd.reactions, "results", None) or []
            mine, theirs = [], []
            for r in results:
                e = getattr(r.reaction, "emoticon", None) or "⭐"
                (mine if getattr(r, "chosen_order", None) is not None else theirs).append(e)
            fb.bg(_process_reactions, upd.peer.user_id, upd.msg_id, mine, theirs)
    except Exception as e:
        print(f"[ACCOUNT] raw: {e}")


def _process_username(uid, first, last, username):
    cid = cid_of(uid)
    meta = _tg().get_meta(cid, fresh=True)
    if not meta.get("user_name"):
        return                                       # not one of our P2P chats
    name = f"{first or ''} {last or ''}".strip() or meta.get("user_name")
    old = meta.get("username") or ""
    if (old or "") != (username or ""):
        _tg().notify_username_change(cid, uid, name, old, username or "", audience="super")
    _tg().update_meta(cid, user_name=name, username=username or "")
    _meta_seen[cid] = {"name": name, "username": username or ""}


def _process_reactions(uid, msg_id, mine, theirs):
    tg = _tg()
    cid = cid_of(uid)
    for key in (str(msg_id), f"admin_{msg_id}"):
        m = fb.get(f"support/{cid}/messages/{key}")
        if isinstance(m, dict):
            view = tg.reaction_view(m.get("reactions"))
            view["admin"], view["user"] = mine, theirs
            fb.put(f"support/{cid}/messages/{key}/reactions", tg._reaction_store(view))
            return


# ── forward received media to the channel ────────────────────────────────────
async def _forward_media(msg, info: dict):
    chan = settings.load().get("forward_channel") or ""
    if not chan or _client is None:
        return
    try:
        ent = _forward_ent.get(chan)
        if ent is None:
            ent = await _client.get_entity(int(chan) if chan.lstrip("-").isdigit() else chan)
            _forward_ent[chan] = ent
        un = f"@{info['username']}" if info["username"] else "—"
        footer = (f"\n\n📨 Forwarded from: {info['name']}\n"
                  f"🔗 Username: {un}\n"
                  f"🆔 Chat ID: {info['uid']}\n"
                  f"🕐 {info['time']} IST")
        orig = (info["text"] or "").strip()
        room = 1024 - len(footer) - 1
        cap = (orig[:max(0, room)] + footer) if orig else footer.strip()
        await _client.send_file(ent, msg.media, caption=cap, parse_mode=None)
        fb.log_event("media_forwarded", scope="super", actor="system", msg_type=info["type"], source="account")
    except Exception as e:
        print(f"[ACCOUNT] forward: {e}")
        fb.log_event("forward_failed", scope="super", actor="system", error=str(e)[:200])


# ── one-off import of recent private chats (Account tab is not empty after linking) ──
def _import_history(max_dialogs: int = 60, per_chat: int = 25):
    if _client is None:
        return
    try:
        _run(_import_async(max_dialogs, per_chat), timeout=600)
        fb.put(IMPORT_FLAG, True)
    except Exception as e:
        print(f"[ACCOUNT] import: {e}")


def _info_of(msg, user) -> dict:
    info = {"uid": user.id, "name": _full_name(user), "username": _username_of(user),
            "hash": getattr(user, "access_hash", None),
            "out": bool(msg.out), "id": msg.id, "ts": int(msg.date.timestamp()),
            "time": _stamp(msg.date), "text": msg.message or "",
            "reply_id": msg.reply_to.reply_to_msg_id if msg.reply_to else None}
    info["type"], info["file_name"] = _kind(msg)
    info["size"] = getattr(msg.file, "size", 0) if msg.file else 0
    return info


def _stored_ids(cid: str) -> set:
    """Telegram ids already stored for a chat (one cheap keys-only request)."""
    ids = set()
    for k in fb.shallow(f"support/{cid}/messages"):
        k = str(k).replace("admin_", "")
        if k.isdigit():
            ids.add(int(k))
    return ids


async def _import_async(max_dialogs: int, per_chat: int):
    """One-off import. Messages that are already stored are skipped, so a repeated import
    (e.g. a redeploy in the middle of the first one) can never overwrite reactions, edit
    history or 🚫 marks."""
    loop = asyncio.get_event_loop()
    batch = []
    async for d in _client.iter_dialogs(limit=max_dialogs):
        if not d.is_user or not _is_person(d.entity):
            continue
        user = d.entity
        have = await loop.run_in_executor(None, _stored_ids, cid_of(user.id))
        try:
            msgs = await _client.get_messages(user, limit=per_chat)
        except errors.FloodWaitError:
            break
        except Exception:
            continue
        for msg in reversed(msgs):
            if msg.action is not None or msg.id in have:
                continue
            batch.append(_info_of(msg, user))
        await asyncio.sleep(0.4)                      # stay far away from flood limits
    for info in batch:                                # Firebase calls never block the Telegram loop
        await loop.run_in_executor(None, _process_message, info, True)


# ── catch-up after a restart / redeploy ──────────────────────────────────────
def _catch_up():
    if _client is None:
        return
    try:
        _run(_catch_up_async(), timeout=900)
        fb.put(ALIVE_PATH, int(time.time()))
    except Exception as e:
        print(f"[ACCOUNT] catch-up: {e}")


async def _catch_up_async(max_dialogs: int = 60):
    """Fetch ONLY what is newer than the last stored message of each chat. Messages from the
    downtime are stored like live ones (unread, media forwarded once, no AI answer); anything
    older is stored quietly. Already stored messages are never touched."""
    loop = asyncio.get_event_loop()
    now = int(time.time())
    alive = int(await loop.run_in_executor(None, fb.get, ALIVE_PATH) or 0)
    since = alive if alive else now - 6 * 3600              # start of the downtime
    since = max(since, now - 7 * 86400) - 300
    work = []
    async for d in _client.iter_dialogs(limit=max_dialogs):
        if not d.is_user or not _is_person(d.entity):
            continue
        user = d.entity
        have = await loop.run_in_executor(None, _stored_ids, cid_of(user.id))
        last = max(have) if have else 0
        try:
            msgs = await _client.get_messages(user, limit=100 if last else 25, min_id=last)
        except errors.FloodWaitError:
            break
        except Exception:
            continue
        for msg in reversed(msgs):                          # oldest first
            if msg.action is not None or msg.id in have:
                continue
            info = _info_of(msg, user)
            work.append((msg, info, info["ts"] >= since))
        await asyncio.sleep(0.4)
    for msg, info, recent in work:
        await loop.run_in_executor(None, _process_message, info, not recent, recent)
        if recent and not info["out"] and info["type"] and not msg.sticker:
            await _forward_media(msg, info)                 # once: it was not stored before
            await asyncio.sleep(0.5)
    if work:
        fb.log_event("account_catchup", scope="super", actor="system", messages=len(work))


# ── actions used by the panel / bot (synchronous wrappers) ───────────────────
def _need():
    if _client is None:
        raise AccountError("The personal account is not linked")


async def _peer(uid: int):
    """Input peer of a user. Telethon only knows users it has seen in this run, so when the cache
    misses we use the access hash stored with the chat — no dialog scan needed."""
    try:
        return await _client.get_input_entity(uid)
    except ValueError:
        pass
    loop = asyncio.get_event_loop()
    ah = (await loop.run_in_executor(None, _tg().get_meta, cid_of(uid))).get("ah")
    if ah:
        return ttypes.InputPeerUser(user_id=int(uid), access_hash=int(ah))
    await _client.get_dialogs(limit=300)
    return await _client.get_input_entity(uid)


def _res(m) -> dict:
    return {"id": m.id, "ts": int(m.date.timestamp()), "time": _stamp(m.date)}


def send_text(uid: int, text: str, reply_to_id=None, _from_ai: bool = False) -> dict:
    _need()

    async def go():
        m = await _client.send_message(await _peer(uid), text, reply_to=reply_to_id or None, parse_mode=None)
        _own_sent[m.id] = time.time()
        return _res(m)
    _gc(_own_sent)
    return _run(go())


def send_file(uid: int, data: bytes, filename: str, mime: str, caption: str = "",
              reply_to_id=None, kind: str = "document") -> dict:
    _need()

    async def go():
        f = io.BytesIO(data)
        f.name = filename or "file"
        m = await _client.send_file(await _peer(uid), f, caption=caption or None,
                                    reply_to=reply_to_id or None, parse_mode=None,
                                    force_document=(kind == "document"),
                                    supports_streaming=(kind == "video"))
        _own_sent[m.id] = time.time()
        return _res(m)
    _gc(_own_sent)
    return _run(go(), timeout=300)


def edit_text(uid: int, msg_id: int, text: str):
    _need()
    _own_edit[int(msg_id)] = time.time()

    async def go():
        await _client.edit_message(await _peer(uid), int(msg_id), text, parse_mode=None)
    _gc(_own_edit)
    _run(go())


def delete_message(uid: int, msg_id: int):
    _need()
    _own_del[int(msg_id)] = time.time()

    async def go():
        await _client.delete_messages(await _peer(uid), [int(msg_id)], revoke=True)
    _run(go())


def react(uid: int, msg_id: int, emoji: str):
    """emoji '' removes the reaction."""
    _need()

    async def go():
        reaction = [ttypes.ReactionEmoji(emoticon=emoji)] if emoji else []
        await _client(functions.messages.SendReactionRequest(
            peer=await _peer(uid), msg_id=int(msg_id), reaction=reaction))
    _run(go())


def download(uid: int, msg_id: int):
    """(bytes, mime, filename) of one message's media — fetched through the account on demand."""
    _need()
    if not msg_id:
        raise AccountError("This message has no Telegram id")

    async def go():
        m = await _client.get_messages(await _peer(uid), ids=int(msg_id))
        if isinstance(m, list):
            m = m[0] if m else None
        if not m or not m.media:
            raise AccountError("The file no longer exists on Telegram (deleted by the sender)")
        size = getattr(m.file, "size", 0) or 0
        if size > MAX_DOWNLOAD_BYTES:
            raise AccountError("File is larger than 50 MB — open it in Telegram")
        buf = io.BytesIO()
        await _client.download_media(m, file=buf)
        data = buf.getvalue()
        if not data:
            raise AccountError("Telegram returned an empty file")
        mime = (m.file.mime_type if m.file else "") or ("image/jpeg" if m.photo else "application/octet-stream")
        name = (m.file.name if m.file and m.file.name else f"{msg_id}")
        return data, mime, name
    try:
        return _run(go(), timeout=300)
    except AccountError as e:
        if "no longer exists" in str(e) or "larger than" in str(e):
            raise
        print(f"[ACCOUNT] download retry ({e})")
        return _run(go(), timeout=300)              # one retry for transient network errors
