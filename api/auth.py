import os
import json
import secrets
import hashlib
from datetime import datetime, date

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
KEY_FILE = os.path.join(DATA_DIR, "api_keys.json")

FREE_DAILY_LIMIT = 10


def _ensure_file():
    if not os.path.exists(DATA_DIR):
        os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(KEY_FILE):
        with open(KEY_FILE, "w", encoding="utf-8") as f:
            json.dump({}, f)


def _read_keys():
    _ensure_file()
    with open(KEY_FILE, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except Exception:
            return {}


def _write_keys(d):
    _ensure_file()
    with open(KEY_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2)


def key_hash(key: str) -> str:
    return hashlib.sha256((key or "").encode("utf-8")).hexdigest()


def generate_key(name: str = "user", tier: str = "free") -> str:
    d = _read_keys()
    while True:
        key = "afe-" + secrets.token_hex(16)
        if key not in d:
            break
    now = datetime.utcnow().isoformat() + "Z"
    today = date.today().isoformat()
    d[key] = {
        "name": name,
        "tier": tier,
        "calls_today": 0,
        "calls_total": 0,
        "last_reset": today,
        "created_at": now,
    }
    _write_keys(d)
    return key


def _reset_if_needed(record: dict):
    today = date.today().isoformat()
    if record.get("last_reset") != today:
        record["calls_today"] = 0
        record["last_reset"] = today


def validate_key(key: str):
    """Return (ok: bool, data or message: dict/str).
    If ok, returns record dict (freshly reset if needed).
    If not ok, returns (False, reason_str).
    """
    if not key:
        return False, "missing"
    d = _read_keys()
    rec = d.get(key)
    if not rec:
        return False, "invalid"
    _reset_if_needed(rec)
    tier = rec.get("tier", "free")
    if tier == "free":
        if rec.get("calls_today", 0) >= FREE_DAILY_LIMIT:
            return False, "rate_limited"
    return True, rec


def increment_usage(key: str):
    d = _read_keys()
    rec = d.get(key)
    if not rec:
        return False
    _reset_if_needed(rec)
    rec["calls_today"] = rec.get("calls_today", 0) + 1
    rec["calls_total"] = rec.get("calls_total", 0) + 1
    rec.setdefault("last_reset", date.today().isoformat())
    d[key] = rec
    _write_keys(d)
    return True


def usage_info(key: str):
    d = _read_keys()
    rec = d.get(key)
    if not rec:
        return None
    _reset_if_needed(rec)
    tier = rec.get("tier", "free")
    limit = None if tier != "free" else FREE_DAILY_LIMIT
    return {
        "tier": tier,
        "calls_today": rec.get("calls_today", 0),
        "calls_total": rec.get("calls_total", 0),
        "limit_today": limit,
        "member_since": rec.get("created_at"),
        "calls_remaining": None if limit is None else max(0, limit - rec.get("calls_today", 0)),
    }
