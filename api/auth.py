import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote

import bcrypt
from settings import settings

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
KEY_FILE = os.path.join(DATA_DIR, "api_keys.json")
USERS_FILE = os.path.join(DATA_DIR, "users.json")
SESSIONS_FILE = os.path.join(DATA_DIR, "auth_sessions.json")
TOKENS_FILE = os.path.join(DATA_DIR, "auth_tokens.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "auth_settings.json")

FREE_DAILY_LIMIT = 10
ACCESS_TOKEN_TTL_SECONDS = 15 * 60
REFRESH_TOKEN_TTL_SECONDS = 30 * 24 * 60 * 60
REAUTH_TTL_SECONDS = 5 * 60
EMAIL_VERIFICATION_TTL_SECONDS = 30 * 60
LOCKOUT_THRESHOLD = 5
LOCKOUT_MINUTES = 15


def _ensure_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def _ensure_json_file(path: str, default):
    _ensure_dir()
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(default, f, indent=2)


def _read_json_file(path: str, default):
    _ensure_json_file(path, default)
    with open(path, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except Exception:
            return default.copy() if isinstance(default, dict) else list(default)


def _write_json_file(path: str, payload):
    _ensure_dir()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)


def _read_keys():
    return _read_json_file(KEY_FILE, {})


def _write_keys(payload: dict):
    _write_json_file(KEY_FILE, payload)


def _read_users():
    return _read_json_file(USERS_FILE, {})


def _write_users(payload: dict):
    _write_json_file(USERS_FILE, payload)


def _read_sessions():
    return _read_json_file(SESSIONS_FILE, {})


def _write_sessions(payload: dict):
    _write_json_file(SESSIONS_FILE, payload)


def _read_tokens():
    return _read_json_file(TOKENS_FILE, {})


def _write_tokens(payload: dict):
    _write_json_file(TOKENS_FILE, payload)


def _read_settings():
    return _read_json_file(SETTINGS_FILE, {})


def _write_settings(payload: dict):
    _write_json_file(SETTINGS_FILE, payload)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().isoformat().replace("+00:00", "Z")


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def _token_digest(value: str) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def _server_secret() -> str:
    if settings.auth_token_secret:
        return settings.auth_token_secret
    settings_payload = _read_settings()
    file_secret = settings_payload.get("token_secret")
    if file_secret:
        return file_secret
    file_secret = secrets.token_urlsafe(48)
    settings_payload["token_secret"] = file_secret
    _write_settings(settings_payload)
    return file_secret


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    padded = value + "=" * ((4 - len(value) % 4) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def _sign_message(message: str) -> str:
    mac = hmac.new(_server_secret().encode("utf-8"), message.encode("utf-8"), hashlib.sha256).digest()
    return _b64url_encode(mac)


def create_access_token(user_id: str, session_id: str, mfa_verified: bool) -> str:
    payload = {
        "sub": user_id,
        "sid": session_id,
        "mfa": bool(mfa_verified),
        "iat": int(time.time()),
        "exp": int(time.time()) + ACCESS_TOKEN_TTL_SECONDS,
    }
    body = _b64url_encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    return f"{body}.{_sign_message(body)}"


def verify_access_token(token: str | None) -> dict | None:
    if not token or "." not in token:
        return None
    body, signature = token.split(".", 1)
    if not hmac.compare_digest(_sign_message(body), signature):
        return None
    try:
        payload = json.loads(_b64url_decode(body).decode("utf-8"))
    except Exception:
        return None
    if int(payload.get("exp", 0)) <= int(time.time()):
        return None
    return payload


def _normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def _public_user(user: dict | None) -> dict | None:
    if not user:
        return None
    return {
        "id": user.get("id"),
        "email": user.get("email"),
        "email_verified": bool(user.get("email_verified")),
        "mfa_enabled": bool((user.get("mfa") or {}).get("enabled")),
        "security_tier": user.get("security_tier", "standard"),
        "created_at": user.get("created_at"),
        "last_login_at": user.get("last_login_at"),
    }


def _find_user_by_email(email: str) -> tuple[str | None, dict | None]:
    normalized = _normalize_email(email)
    for user_id, record in _read_users().items():
        if record.get("email") == normalized:
            return user_id, record
    return None, None


def get_user(user_id: str) -> dict | None:
    return _read_users().get(user_id)


def public_user(user_id: str) -> dict | None:
    return _public_user(get_user(user_id))


def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")


def _verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except Exception:
        return False


def _is_locked(user: dict) -> bool:
    locked_until = _parse_dt(user.get("locked_until"))
    return bool(locked_until and locked_until > utc_now())


def create_user(email: str, password: str) -> tuple[bool, dict]:
    normalized = _normalize_email(email)
    if not normalized or "@" not in normalized:
        return False, {"reason": "invalid_email"}
    if len(password or "") < 10:
        return False, {"reason": "weak_password"}
    existing_id, _ = _find_user_by_email(normalized)
    if existing_id:
        return False, {"reason": "email_taken"}

    users = _read_users()
    user_id = uuid.uuid4().hex
    users[user_id] = {
        "id": user_id,
        "email": normalized,
        "password_hash": _hash_password(password),
        "email_verified": False,
        "created_at": utc_now_iso(),
        "last_login_at": None,
        "security_tier": "standard",
        "failed_login_attempts": 0,
        "locked_until": None,
        "mfa": {
            "enabled": False,
            "secret": None,
            "pending_secret": None,
            "enrolled_at": None,
        },
    }
    _write_users(users)
    return True, {"user": _public_user(users[user_id])}


def authenticate_user(email: str, password: str) -> tuple[bool, dict]:
    user_id, user = _find_user_by_email(email)
    if not user_id or not user:
        return False, {"reason": "invalid_credentials"}
    if _is_locked(user):
        return False, {"reason": "locked", "locked_until": user.get("locked_until")}
    if not _verify_password(password, user.get("password_hash", "")):
        users = _read_users()
        fresh = users.get(user_id, user)
        attempts = int(fresh.get("failed_login_attempts", 0)) + 1
        fresh["failed_login_attempts"] = attempts
        if attempts >= LOCKOUT_THRESHOLD:
            fresh["locked_until"] = (utc_now() + timedelta(minutes=LOCKOUT_MINUTES)).isoformat().replace("+00:00", "Z")
            fresh["failed_login_attempts"] = 0
        users[user_id] = fresh
        _write_users(users)
        return False, {"reason": "invalid_credentials"}
    return True, {"user_id": user_id, "user": user}


def complete_login_success(user_id: str):
    users = _read_users()
    user = users.get(user_id)
    if not user:
        return
    user["failed_login_attempts"] = 0
    user["locked_until"] = None
    user["last_login_at"] = utc_now_iso()
    users[user_id] = user
    _write_users(users)


def _random_token(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(32)}"


def _create_token_record(purpose: str, user_id: str, ttl_seconds: int, extra: dict | None = None) -> str:
    tokens = _read_tokens()
    token_id = uuid.uuid4().hex
    plain = _random_token(purpose)
    record = {
        "id": token_id,
        "purpose": purpose,
        "user_id": user_id,
        "token_hash": _token_digest(plain),
        "created_at": utc_now_iso(),
        "expires_at": (utc_now() + timedelta(seconds=ttl_seconds)).isoformat().replace("+00:00", "Z"),
        "used_at": None,
        "extra": extra or {},
    }
    tokens[token_id] = record
    _write_tokens(tokens)
    return plain


def _consume_token(purpose: str, token: str) -> tuple[bool, dict]:
    token_hash = _token_digest(token)
    tokens = _read_tokens()
    for token_id, record in tokens.items():
        if record.get("purpose") != purpose:
            continue
        if not hmac.compare_digest(record.get("token_hash", ""), token_hash):
            continue
        expires_at = _parse_dt(record.get("expires_at"))
        if record.get("used_at") or not expires_at or expires_at <= utc_now():
            return False, {"reason": "expired_or_used"}
        record["used_at"] = utc_now_iso()
        tokens[token_id] = record
        _write_tokens(tokens)
        return True, record
    return False, {"reason": "invalid_token"}


def create_email_verification_token(user_id: str) -> str:
    return _create_token_record("email_verify", user_id, EMAIL_VERIFICATION_TTL_SECONDS)


def verify_email_token(token: str) -> tuple[bool, dict]:
    ok, info = _consume_token("email_verify", token)
    if not ok:
        return False, info
    user_id = info.get("user_id")
    users = _read_users()
    user = users.get(user_id)
    if not user:
        return False, {"reason": "user_missing"}
    user["email_verified"] = True
    users[user_id] = user
    _write_users(users)
    return True, {"user": _public_user(user)}


def _base32_secret(num_bytes: int = 20) -> str:
    return base64.b32encode(secrets.token_bytes(num_bytes)).decode("ascii").rstrip("=")


def _hotp(secret: str, counter: int) -> str:
    padding = "=" * ((8 - len(secret) % 8) % 8)
    key = base64.b32decode((secret + padding).encode("ascii"), casefold=True)
    msg = counter.to_bytes(8, "big")
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code_int = int.from_bytes(digest[offset:offset + 4], "big") & 0x7FFFFFFF
    return str(code_int % 1_000_000).zfill(6)


def verify_totp(secret: str, code: str, *, window: int = 1, for_time: int | None = None) -> bool:
    clean = (code or "").strip().replace(" ", "")
    if len(clean) != 6 or not clean.isdigit():
        return False
    now_ts = int(for_time if for_time is not None else time.time())
    current_counter = now_ts // 30
    for delta in range(-window, window + 1):
        if hmac.compare_digest(_hotp(secret, current_counter + delta), clean):
            return True
    return False


def start_mfa_setup(user_id: str) -> tuple[bool, dict]:
    users = _read_users()
    user = users.get(user_id)
    if not user:
        return False, {"reason": "user_missing"}
    mfa = user.setdefault("mfa", {})
    secret = _base32_secret()
    mfa["pending_secret"] = secret
    users[user_id] = user
    _write_users(users)
    issuer = quote("AFE")
    account = quote(user.get("email", user_id))
    return True, {
        "secret": secret,
        "otpauth_url": f"otpauth://totp/{issuer}:{account}?secret={secret}&issuer={issuer}&algorithm=SHA1&digits=6&period=30",
    }


def confirm_mfa_setup(user_id: str, code: str) -> tuple[bool, dict]:
    users = _read_users()
    user = users.get(user_id)
    if not user:
        return False, {"reason": "user_missing"}
    mfa = user.setdefault("mfa", {})
    secret = mfa.get("pending_secret")
    if not secret or not verify_totp(secret, code):
        return False, {"reason": "invalid_code"}
    mfa["secret"] = secret
    mfa["pending_secret"] = None
    mfa["enabled"] = True
    mfa["enrolled_at"] = utc_now_iso()
    user["security_tier"] = "high_trust"
    users[user_id] = user
    _write_users(users)
    return True, {"user": _public_user(user)}


def create_session(user_id: str, ip: str | None, user_agent: str | None, mfa_verified: bool) -> tuple[str, dict]:
    sessions = _read_sessions()
    session_id = uuid.uuid4().hex
    refresh_token = _random_token("refresh")
    session = {
        "id": session_id,
        "user_id": user_id,
        "refresh_token_hash": _token_digest(refresh_token),
        "created_at": utc_now_iso(),
        "last_seen_at": utc_now_iso(),
        "expires_at": (utc_now() + timedelta(seconds=REFRESH_TOKEN_TTL_SECONDS)).isoformat().replace("+00:00", "Z"),
        "revoked_at": None,
        "rotated_at": None,
        "mfa_verified": bool(mfa_verified),
        "ip": ip,
        "user_agent": user_agent,
    }
    sessions[session_id] = session
    _write_sessions(sessions)
    return refresh_token, session


def get_session(session_id: str) -> dict | None:
    return _read_sessions().get(session_id)


def _session_active(session: dict | None) -> bool:
    if not session or session.get("revoked_at"):
        return False
    expires_at = _parse_dt(session.get("expires_at"))
    return bool(expires_at and expires_at > utc_now())


def session_active(session: dict | None) -> bool:
    return _session_active(session)


def rotate_refresh_session(refresh_token: str, ip: str | None, user_agent: str | None) -> tuple[bool, dict]:
    sessions = _read_sessions()
    presented_hash = _token_digest(refresh_token)
    for session_id, session in sessions.items():
        if not hmac.compare_digest(session.get("refresh_token_hash", ""), presented_hash):
            continue
        if not _session_active(session):
            return False, {"reason": "invalid_session"}
        new_refresh = _random_token("refresh")
        session["refresh_token_hash"] = _token_digest(new_refresh)
        session["rotated_at"] = utc_now_iso()
        session["last_seen_at"] = utc_now_iso()
        session["ip"] = ip
        session["user_agent"] = user_agent
        sessions[session_id] = session
        _write_sessions(sessions)
        return True, {"refresh_token": new_refresh, "session": session}
    return False, {"reason": "invalid_session"}


def revoke_session(session_id: str):
    sessions = _read_sessions()
    session = sessions.get(session_id)
    if not session:
        return
    session["revoked_at"] = utc_now_iso()
    sessions[session_id] = session
    _write_sessions(sessions)


def revoke_all_sessions(user_id: str, except_session_id: str | None = None):
    sessions = _read_sessions()
    changed = False
    for session_id, session in sessions.items():
        if session.get("user_id") != user_id:
            continue
        if except_session_id and session_id == except_session_id:
            continue
        if not session.get("revoked_at"):
            session["revoked_at"] = utc_now_iso()
            sessions[session_id] = session
            changed = True
    if changed:
        _write_sessions(sessions)


def list_sessions(user_id: str) -> list[dict]:
    sessions = _read_sessions()
    items = []
    for session in sessions.values():
        if session.get("user_id") != user_id:
            continue
        items.append(
            {
                "id": session.get("id"),
                "created_at": session.get("created_at"),
                "last_seen_at": session.get("last_seen_at"),
                "expires_at": session.get("expires_at"),
                "revoked_at": session.get("revoked_at"),
                "ip": session.get("ip"),
                "user_agent": session.get("user_agent"),
                "mfa_verified": bool(session.get("mfa_verified")),
                "active": _session_active(session),
            }
        )
    return sorted(items, key=lambda item: item.get("created_at") or "", reverse=True)


def create_reauth_token(user_id: str, session_id: str) -> str:
    return _create_token_record("reauth", user_id, REAUTH_TTL_SECONDS, {"session_id": session_id})


def verify_reauth_token(token: str, user_id: str, session_id: str) -> bool:
    token_hash = _token_digest(token)
    for record in _read_tokens().values():
        if record.get("purpose") != "reauth":
            continue
        if record.get("user_id") != user_id:
            continue
        if (record.get("extra") or {}).get("session_id") != session_id:
            continue
        expires_at = _parse_dt(record.get("expires_at"))
        if not expires_at or expires_at <= utc_now():
            continue
        if record.get("used_at"):
            continue
        if hmac.compare_digest(record.get("token_hash", ""), token_hash):
            return True
    return False


def key_hash(key: str) -> str:
    return hashlib.sha256((key or "").encode("utf-8")).hexdigest()


def generate_key(name: str = "user", tier: str = "free", owner_user_id: str | None = None) -> str:
    data = _read_keys()
    while True:
        key = "afe-" + secrets.token_hex(16)
        if key not in data:
            break
    now = utc_now_iso()
    today = date.today().isoformat()
    data[key] = {
        "name": name,
        "tier": tier,
        "owner_user_id": owner_user_id,
        "calls_today": 0,
        "calls_total": 0,
        "last_reset": today,
        "created_at": now,
    }
    _write_keys(data)
    return key


def _reset_if_needed(record: dict):
    today = date.today().isoformat()
    if record.get("last_reset") != today:
        record["calls_today"] = 0
        record["last_reset"] = today


def validate_key(key: str):
    if not key:
        return False, "missing"
    data = _read_keys()
    record = data.get(key)
    if not record:
        return False, "invalid"
    _reset_if_needed(record)
    tier = record.get("tier", "free")
    if tier == "free" and record.get("calls_today", 0) >= FREE_DAILY_LIMIT:
        return False, "rate_limited"
    return True, record


def increment_usage(key: str):
    data = _read_keys()
    record = data.get(key)
    if not record:
        return False
    _reset_if_needed(record)
    record["calls_today"] = record.get("calls_today", 0) + 1
    record["calls_total"] = record.get("calls_total", 0) + 1
    record.setdefault("last_reset", date.today().isoformat())
    data[key] = record
    _write_keys(data)
    return True


def usage_info(key: str):
    data = _read_keys()
    record = data.get(key)
    if not record:
        return None
    _reset_if_needed(record)
    tier = record.get("tier", "free")
    limit = None if tier != "free" else FREE_DAILY_LIMIT
    return {
        "tier": tier,
        "calls_today": record.get("calls_today", 0),
        "calls_total": record.get("calls_total", 0),
        "limit_today": limit,
        "member_since": record.get("created_at"),
        "calls_remaining": None if limit is None else max(0, limit - record.get("calls_today", 0)),
    }
