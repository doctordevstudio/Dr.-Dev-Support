"""
firebase_helper.py — thin REST client for Firebase Realtime Database.

Design rules
------------
* NOTHING is ever hard-deleted from the database. There is deliberately no
  `delete()` helper here. "Delete" in the admin panel always means setting a
  `deleted: true` flag (soft delete) on the record.
* One shared `requests.Session` (keep-alive) instead of a new TLS handshake
  per call — this alone makes every panel action noticeably faster.
* `chat_index/<chat_id>` mirrors `support/<chat_id>/meta`. The chat list,
  unread badge and broadcast recipient list read this tiny index instead of
  downloading every message of every chat (which is what made the panel
  freeze once the database grew).
* `log_event()` is fire-and-forget (background thread) so logging never slows
  down a Telegram handler or an HTTP request.
"""
import datetime
import queue
import re
import threading
import time

import requests
from requests.adapters import HTTPAdapter

import config

TIMEOUT = 12

_session = requests.Session()
_adapter = HTTPAdapter(pool_connections=4, pool_maxsize=24, max_retries=1)
_session.mount("https://", _adapter)
_session.mount("http://", _adapter)


def _now_ist_str():
    return datetime.datetime.now(config.IST).strftime("%Y-%m-%d %H:%M:%S")


def now_ist():
    return _now_ist_str()


def now_ts():
    return int(time.time())


def _base() -> str:
    return config.FIREBASE_URL.rstrip("/")


def _enabled() -> bool:
    return bool(_base())


def _url(path: str) -> str:
    return f"{_base()}/{path.strip('/')}.json"


def _params(extra=None) -> dict:
    p = {}
    if config.FIREBASE_SECRET:
        p["auth"] = config.FIREBASE_SECRET
    if extra:
        p.update(extra)
    return p


def _request(method: str, path: str, data=None, params=None):
    """Low-level call. Returns (ok, parsed_json_or_None, status_code)."""
    if not _enabled():
        return False, None, 0
    try:
        r = _session.request(
            method, _url(path),
            json=data if method in ("PUT", "PATCH", "POST") else None,
            params=_params(params), timeout=TIMEOUT,
        )
        try:
            body = r.json()
        except Exception:
            body = None
        return r.status_code == 200, body, r.status_code
    except Exception as e:
        print(f"[FB {method}] {path} -> {e}")
        return False, None, 0


# ── Basic verbs ───────────────────────────────────────────────────────────────
_META_RE = re.compile(r"^support/([^/]+)/meta$")


def get(path: str, **query):
    ok, body, _ = _request("GET", path, params=query or None)
    return body if ok else None


def shallow(path: str):
    """Keys only (values become `true`) — cheap way to list/count children."""
    ok, body, _ = _request("GET", path, params={"shallow": "true"})
    return body if ok and isinstance(body, dict) else {}


def _mirror_meta(path: str, data, replace: bool):
    m = _META_RE.match(path.strip("/"))
    if not m or not isinstance(data, dict):
        return
    cid = m.group(1)
    _request("PUT" if replace else "PATCH", f"chat_index/{cid}", data)
    _invalidate_index()


def put(path: str, data):
    ok, body, _ = _request("PUT", path, data)
    if ok:
        _mirror_meta(path, data, replace=True)
        return body
    return None


def patch(path: str, data: dict):
    """PATCH. With path "" and slash-separated keys this is a multi-path update."""
    ok, body, _ = _request("PATCH", path, data)
    if ok:
        _mirror_meta(path, data, replace=False)
        return body
    return None


def post(path: str, data):
    ok, body, _ = _request("POST", path, data)
    return body if ok else None


# ── Indexed queries (with graceful fallback) ─────────────────────────────────
# Firebase only allows orderBy=<child> if the rules contain ".indexOn". If they
# don't, the REST API answers 400 — we then remember that and fall back to
# downloading the whole node. Add the rules shown in the README for best speed.
_idx_state = {"broken_until": 0.0}


def query_by_ts(path: str, start_at=None, end_at=None, limit_last=None):
    """Returns (ok, dict). ok=False means "index not available, do it yourself"."""
    if time.time() < _idx_state["broken_until"]:
        return False, None
    params = {"orderBy": '"ts"'}
    if start_at is not None:
        params["startAt"] = str(int(start_at))
    if end_at is not None:
        params["endAt"] = str(int(end_at))
    if limit_last is not None:
        params["limitToLast"] = str(int(limit_last))
    ok, body, status = _request("GET", path, params=params)
    if ok:
        return True, (body if isinstance(body, dict) else {})
    if status == 400:
        _idx_state["broken_until"] = time.time() + 600
        print(f"[FB] index on 'ts' missing for {path} — falling back to full reads "
              f"(see README: Firebase rules)")
    return False, None


# ── Chat index (mirror of support/<cid>/meta) ────────────────────────────────
_idx_lock = threading.Lock()
_idx = {"data": None, "ts": 0.0, "checked": 0.0, "nometa": set()}
_backfill_lock = threading.Lock()


def _invalidate_index():
    with _idx_lock:
        _idx["ts"] = 0.0


def _backfill(missing: list):
    """Copy metas that are not in chat_index yet (first start after upgrade)."""
    if not missing:
        return {}
    found = {}
    if len(missing) > 5:
        raw = get("support") or {}      # one-off heavy read, only on upgrade
        for cid in missing:
            meta = ((raw.get(cid) or {}) if isinstance(raw, dict) else {}).get("meta")
            if isinstance(meta, dict) and meta:
                found[cid] = meta
    else:
        for cid in missing:
            meta = get(f"support/{cid}/meta")
            if isinstance(meta, dict) and meta:
                found[cid] = meta
    if found:
        _request("PATCH", "chat_index", found)
    return found


def chat_index(max_age: float = 3.0) -> dict:
    """{chat_id: meta}. Cached for a few seconds so polling stays cheap."""
    now = time.time()
    with _idx_lock:
        if _idx["data"] is not None and now - _idx["ts"] < max_age:
            return _idx["data"]

    data = get("chat_index")
    if not isinstance(data, dict):
        data = {}

    # Every ~60s make sure no chat is missing from the index
    if now - _idx["checked"] > 60 and _backfill_lock.acquire(blocking=False):
        try:
            keys = shallow("support")
            missing = [k for k in keys
                       if (not isinstance(data.get(k), dict) or not data.get(k))
                       and k not in _idx["nometa"]]
            if missing:
                found = _backfill(missing)
                data.update(found)
                # chats that really have no meta: don't re-check them every minute
                _idx["nometa"].update(k for k in missing if k not in found)
            _idx["checked"] = time.time()
        finally:
            _backfill_lock.release()

    with _idx_lock:
        _idx["data"] = data
        _idx["ts"] = time.time()
    return data


# ── Logging (fire-and-forget) ────────────────────────────────────────────────
_log_q: "queue.Queue" = queue.Queue(maxsize=5000)


def _log_worker():
    while True:
        entry = _log_q.get()
        try:
            post("logs", entry)
        except Exception as e:
            print(f"[LOG] {e}")


threading.Thread(target=_log_worker, daemon=True, name="fb-logger").start()


def log_event(event_type: str, **fields):
    try:
        entry = {
            "type": event_type,
            "time": _now_ist_str(),
            "ts": int(time.time()),
            **fields,
        }
        _log_q.put_nowait(entry)
    except Exception as e:
        print(f"[LOG] {event_type} -> {e}")


def get_logs(limit: int = 500) -> list:
    ok, data = query_by_ts("logs", limit_last=limit)
    if not ok:
        data = get("logs") or {}
    items = [v for v in (data or {}).values() if isinstance(v, dict)]
    items.sort(key=lambda e: e.get("ts", 0), reverse=True)
    return items[:limit]
