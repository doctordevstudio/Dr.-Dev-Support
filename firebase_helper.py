"""
firebase_helper.py — thin REST client for Firebase Realtime Database.
"""
import datetime
import time
import requests
import config


def _now_ist_str():
    return datetime.datetime.now(config.IST).strftime("%Y-%m-%d %H:%M:%S")


def now_ist():
    return _now_ist_str()


def now_ts():
    return int(time.time())


TIMEOUT = 12


def _firebase_url():
    return config.FIREBASE_URL.rstrip("/")


def _url(path: str) -> str:
    base = f"{_firebase_url()}/{path}.json"
    s = config.FIREBASE_SECRET
    return f"{base}?auth={s}" if s else base


def _enabled():
    return bool(_firebase_url())


def get(path: str):
    if not _enabled():
        return None
    try:
        r = requests.get(_url(path), timeout=TIMEOUT)
        return r.json() if r.status_code == 200 else None
    except Exception as e:
        print(f"[FB GET] {path} -> {e}")
        return None


def put(path: str, data):
    if not _enabled():
        return None
    try:
        r = requests.put(_url(path), json=data, timeout=TIMEOUT)
        return r.json() if r.status_code == 200 else None
    except Exception as e:
        print(f"[FB PUT] {path} -> {e}")
        return None


def patch(path: str, data: dict):
    if not _enabled():
        return None
    try:
        r = requests.patch(_url(path), json=data, timeout=TIMEOUT)
        return r.json() if r.status_code == 200 else None
    except Exception as e:
        print(f"[FB PATCH] {path} -> {e}")
        return None


def post(path: str, data):
    if not _enabled():
        return None
    try:
        r = requests.post(_url(path), json=data, timeout=TIMEOUT)
        return r.json() if r.status_code == 200 else None
    except Exception as e:
        print(f"[FB POST] {path} -> {e}")
        return None


def delete(path: str) -> bool:
    """Hard delete — intentionally unused for messages. Kept for housekeeping."""
    if not _enabled():
        return False
    try:
        r = requests.delete(_url(path), timeout=TIMEOUT)
        return r.status_code == 200
    except Exception as e:
        print(f"[FB DEL] {path} -> {e}")
        return False


def log_event(event_type: str, **fields):
    try:
        entry = {
            "type": event_type,
            "time": _now_ist_str(),
            "ts": int(time.time()),
            **fields,
        }
        post("logs", entry)
    except Exception as e:
        print(f"[LOG] {event_type} -> {e}")
