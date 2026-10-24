"""
ai.py — AI support answers (Groq, OpenAI-compatible chat API).

* API keys live in Firebase under `private/ai_keys` (a list). Add them one per line in
  Settings; with the database rules locked (".read": false) only this server can read them.
* ROUND ROBIN: every answer starts with the next key in the list. A key that answers
  401/403 is parked for 10 min, one that answers 429 for the time Groq asks for — the
  request simply continues with the next key, so one bad / exhausted key never stops the bot.
* Models come from the GROQ_MODELS env var (comma separated, tried in order).
* Every answered question is saved in `ai_history` (who asked, chat id, name, question,
  answer, source) — that is what the "AI Support" page shows.
"""
import datetime
import re
import threading
import time

import requests

import config
import firebase_helper as fb

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
KEYS_PATH = "private/ai_keys"
HISTORY_PATH = "ai_history"

_session = requests.Session()
_session.mount("https://", requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=16))

_lock = threading.Lock()
_rr = {"i": 0}
_cool = {}                                   # key -> unix time until which it is parked
_keys_cache = {"keys": [], "ts": 0.0}
KEYS_TTL = 20.0

_KEY_RE = re.compile(r"^[A-Za-z0-9_\-]{16,200}$")


# ── Short in-memory conversation memory (no Firebase read on the hot path) ──
_mem = {}
_mem_lock = threading.Lock()


def remember(cid: str, role: str, text: str):
    with _mem_lock:
        d = _mem.setdefault(str(cid), [])
        d.append({"role": role, "content": str(text)[:1500]})
        del d[:-10]
        if len(_mem) > 2000:                       # keep memory bounded
            for k in list(_mem)[:500]:
                _mem.pop(k, None)


def recall(cid: str) -> list:
    with _mem_lock:
        return list(_mem.get(str(cid), []))


# ── Keys ─────────────────────────────────────────────────────────────────────
def parse_keys(text: str) -> list:
    """One key per line; blanks and duplicates are dropped. ValueError on a malformed line."""
    out = []
    for line in str(text or "").splitlines():
        k = line.strip()
        if not k:
            continue
        if not _KEY_RE.match(k):
            raise ValueError(f"This does not look like an API key: {k[:12]}…")
        if k not in out:
            out.append(k)
    if len(out) > 50:
        raise ValueError("Maximum 50 keys")
    return out


def get_keys(force: bool = False) -> list:
    now = time.time()
    with _lock:
        if not force and now - _keys_cache["ts"] < KEYS_TTL:
            return list(_keys_cache["keys"])
    data = fb.get(KEYS_PATH)
    keys = [k for k in (data if isinstance(data, list) else (list(data.values()) if isinstance(data, dict) else []))
            if isinstance(k, str) and k]
    with _lock:
        _keys_cache["keys"], _keys_cache["ts"] = keys, time.time()
    return list(keys)


def set_keys(text: str) -> int:
    keys = parse_keys(text)
    if fb.put(KEYS_PATH, keys if keys else []) is None and keys:
        raise ValueError("Could not save to Firebase")
    with _lock:
        _keys_cache["keys"], _keys_cache["ts"] = keys, time.time()
        _cool.clear()
        _rr["i"] = 0
    fb.log_event("ai_keys_updated", count=len(keys))
    return len(keys)


def keys_text() -> str:
    return "\n".join(get_keys(force=True))


def _ordered_keys() -> list:
    """Round robin: the list rotated so the next key comes first; parked keys go last."""
    keys = get_keys()
    if not keys:
        return []
    with _lock:
        start = _rr["i"] % len(keys)
        _rr["i"] = (start + 1) % len(keys)
        now = time.time()
        rotated = keys[start:] + keys[:start]
        ready = [k for k in rotated if _cool.get(k, 0) <= now]
        parked = [k for k in rotated if _cool.get(k, 0) > now]
    return ready + parked


def _park(key: str, seconds: float):
    with _lock:
        _cool[key] = time.time() + seconds


def available() -> bool:
    return bool(get_keys())


# ── Groq call ────────────────────────────────────────────────────────────────
def _call(key: str, model: str, messages: list, max_tokens: int):
    """(text|None, status). status: 'ok' | 'auth' | 'rate' | 'retry' | 'empty'"""
    payload = {"model": model, "messages": messages, "temperature": 0.3, "max_tokens": max_tokens}
    if model.startswith("openai/gpt-oss"):
        payload["reasoning_effort"] = "low"
    try:
        r = _session.post(GROQ_URL, json=payload, timeout=(4, 25),
                          headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    except Exception as e:
        print(f"[AI] network: {e}")
        return None, "retry"
    if r.status_code == 200:
        try:
            text = ((r.json()["choices"][0]["message"].get("content")) or "").strip()
        except Exception:
            return None, "retry"
        return (text, "ok") if text else (None, "empty")
    if r.status_code in (401, 403):
        return None, "auth"
    if r.status_code == 429:
        try:
            wait = float(r.headers.get("retry-after") or 60)
        except ValueError:
            wait = 60.0
        _park(key, min(max(wait, 5.0), 900.0))
        return None, "rate"
    print(f"[AI] HTTP {r.status_code}: {r.text[:120]}")
    return None, "retry"


def ask(question: str, system_prompt: str, history=None, context: str = ""):
    """Answer text or None. `history` = [{"role": "user"|"assistant", "content": "..."}]."""
    keys = _ordered_keys()
    if not keys or not (question or "").strip():
        return None
    sys_txt = (system_prompt or "").strip()
    if context:
        sys_txt = f"{sys_txt}\n\n{context}".strip()
    messages = [{"role": "system", "content": sys_txt}] if sys_txt else []
    for h in (history or [])[-8:]:
        if h.get("content"):
            messages.append({"role": h["role"], "content": str(h["content"])[:1500]})
    messages.append({"role": "user", "content": question[:3000]})

    tried = 0
    for key in keys[:4]:                           # at most 4 different keys per question
        tried += 1
        for model in config.GROQ_MODELS or ["openai/gpt-oss-20b"]:
            for max_tokens in (800, 2000):         # reasoning can eat the first budget
                text, status = _call(key, model, messages, max_tokens)
                if status == "ok":
                    return text[:3500]
                if status == "auth":
                    _park(key, 600)
                    break
                if status in ("rate", "retry"):
                    break
                # "empty" -> same model once more with a bigger budget
            else:
                continue
            break                                   # key unusable right now -> next key
    return None


# ── History ──────────────────────────────────────────────────────────────────
def record(source: str, cid: str, uid, name: str, username: str, question: str, answer: str):
    now = datetime.datetime.now(config.IST)
    fb.post(HISTORY_PATH, {
        "ts": int(now.timestamp()),
        "time": now.strftime("%Y-%m-%d %H:%M:%S"),
        "source": source,                    # "bot" | "account"
        "chat_id": str(uid if uid is not None else cid),
        "cid": str(cid),
        "name": name or "",
        "username": username or "",
        "question": (question or "")[:3000],
        "answer": (answer or "")[:3500],
    })


def day_range(kind: str, start: str = "", end: str = ""):
    """(start_ts, end_ts) in IST for today | yesterday | all | custom (YYYY-MM-DD)."""
    now = datetime.datetime.now(config.IST)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if kind == "today":
        return int(midnight.timestamp()), int(now.timestamp()) + 60
    if kind == "yesterday":
        return int((midnight - datetime.timedelta(days=1)).timestamp()), int(midnight.timestamp()) - 1
    if kind == "custom":
        def parse(s, eod):
            d = datetime.datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=config.IST)
            return int(d.timestamp()) + (86399 if eod else 0)
        a, b = parse(start, False), parse(end or start, True)
        if b < a:
            a, b = b - 86399, a + 86399
        return a, b
    return 0, int(now.timestamp()) + 60


def history(start_ts: int, end_ts: int, sources=("bot", "account"), limit: int = 500) -> list:
    ok, data = fb.query_by_ts(HISTORY_PATH, start_at=start_ts, end_at=end_ts, limit_last=limit)
    if not ok:
        data = fb.get(HISTORY_PATH) or {}
    rows = [v for v in (data or {}).values()
            if isinstance(v, dict) and start_ts <= int(v.get("ts", 0)) <= end_ts and v.get("source") in sources]
    rows.sort(key=lambda r: r.get("ts", 0), reverse=True)
    return rows[:limit]
