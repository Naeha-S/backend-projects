import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote

import bcrypt
from sqlalchemy import select

from db import SessionLocal
from models import ApiKey, AuthSession, AuthSetting, AuthToken, User
from settings import settings

FREE_DAILY_LIMIT = 10
ACCESS_TOKEN_TTL_SECONDS = 15 * 60
REFRESH_TOKEN_TTL_SECONDS = 30 * 24 * 60 * 60
REAUTH_TTL_SECONDS = 5 * 60
EMAIL_VERIFICATION_TTL_SECONDS = 30 * 60
LOCKOUT_THRESHOLD = 5
LOCKOUT_MINUTES = 15
API_KEY_SECRET_BYTES = 32
API_KEY_IDENTIFIER_BYTES = 9
API_KEY_ROTATION_DAYS = max(1, settings.api_key_rotation_days)
DEFAULT_KEY_SCOPES = ["read", "write"]
ALLOWED_KEY_ENVIRONMENTS = {"live", "test"}
ALLOWED_KEY_SCOPES = {"read", "write", "admin", "analysis:read", "analysis:write", "usage:read"}

_server_secret_cache: str | None = None
_api_key_pepper_cache: str | None = None
BASE_DIR = os.path.dirname(__file__)
DATA_DIR = os.path.join(BASE_DIR, "data")
LEGACY_API_KEYS_FILE = os.path.join(DATA_DIR, "api_keys.json")
_legacy_api_keys_checked = False


@contextmanager
def _db_session():
    if SessionLocal is None:
        raise RuntimeError("DATABASE_URL must be configured before using auth persistence.")
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


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


def _setting_secret(name: str, env_value: str | None = None) -> str:
    cache_name = f"_{name}_cache"
    cached = globals().get(cache_name)
    if cached:
        return cached
    if env_value:
        globals()[cache_name] = env_value
        return env_value
    with _db_session() as session:
        record = session.get(AuthSetting, name)
        if record and record.value:
            globals()[cache_name] = record.value
            return record.value
        record = AuthSetting(key=name, value=secrets.token_urlsafe(48))
        session.add(record)
        globals()[cache_name] = record.value
        return record.value


def _api_key_pepper() -> str:
    return _setting_secret("api_key_pepper", settings.api_key_pepper or settings.auth_token_secret or None)


def _api_key_secret_hash(secret: str) -> str:
    return hmac.new(
        _api_key_pepper().encode("utf-8"),
        (secret or "").encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _normalize_scopes(scopes: list[str] | None) -> list[str]:
    raw = scopes or DEFAULT_KEY_SCOPES
    normalized = []
    for item in raw:
        value = (item or "").strip().lower()
        if value:
            normalized.append(value)
    expanded = set()
    for scope in normalized:
        if scope == "admin":
            expanded.add("admin")
            expanded.update(ALLOWED_KEY_SCOPES)
            expanded.update({"read", "write", "usage:read", "analysis:read", "analysis:write"})
            continue
        expanded.add(scope)
        if scope == "read":
            expanded.update({"usage:read", "analysis:read"})
        if scope == "write":
            expanded.add("analysis:write")
    filtered = [scope for scope in sorted(expanded) if scope in ALLOWED_KEY_SCOPES]
    return filtered or list(DEFAULT_KEY_SCOPES)


def _normalize_environment(environment: str | None) -> str:
    value = (environment or "live").strip().lower()
    if value not in ALLOWED_KEY_ENVIRONMENTS:
        raise ValueError("invalid_environment")
    return value


def _mask_key_display(value: str | None) -> str:
    if not value:
        return "-"
    cleaned = str(value).strip()
    if len(cleaned) <= 10:
        return cleaned[:2] + "***"
    return f"{cleaned[:12]}...{cleaned[-4:]}"


def _api_key_prefix(environment: str) -> str:
    return "pk_test_" if environment == "test" else "pk_live_"


def _api_key_identifier() -> str:
    return secrets.token_urlsafe(API_KEY_IDENTIFIER_BYTES).replace("-", "a").replace("_", "b")


def _split_presented_key(value: str | None) -> tuple[str | None, str | None, str | None]:
    cleaned = (value or "").strip()
    if not cleaned:
        return None, None, None
    for prefix in ("pk_live_", "pk_test_"):
        if cleaned.startswith(prefix):
            remainder = cleaned[len(prefix):]
            identifier, sep, secret = remainder.partition("_")
            if sep and identifier and secret:
                return prefix[:-1], identifier, secret
    return None, None, cleaned


def _server_secret() -> str:
    global _server_secret_cache
    if _server_secret_cache:
        return _server_secret_cache
    if settings.auth_token_secret:
        _server_secret_cache = settings.auth_token_secret
        return _server_secret_cache
    with _db_session() as session:
        record = session.get(AuthSetting, "token_secret")
        if record and record.value:
            _server_secret_cache = record.value
            return _server_secret_cache
        record = AuthSetting(key="token_secret", value=secrets.token_urlsafe(48))
        session.add(record)
        _server_secret_cache = record.value
        return _server_secret_cache


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


def _user_to_dict(user: User | None) -> dict | None:
    if user is None:
        return None
    return {
        "id": user.id,
        "email": user.email,
        "password_hash": user.password_hash,
        "email_verified": bool(user.email_verified),
        "created_at": user.created_at,
        "last_login_at": user.last_login_at,
        "security_tier": user.security_tier,
        "failed_login_attempts": int(user.failed_login_attempts or 0),
        "locked_until": user.locked_until,
        "mfa": {
            "enabled": bool(user.mfa_enabled),
            "secret": user.mfa_secret,
            "pending_secret": user.mfa_pending_secret,
            "enrolled_at": user.mfa_enrolled_at,
        },
    }


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


def _session_to_dict(session: AuthSession | None) -> dict | None:
    if session is None:
        return None
    return {
        "id": session.id,
        "user_id": session.user_id,
        "refresh_token_hash": session.refresh_token_hash,
        "created_at": session.created_at,
        "last_seen_at": session.last_seen_at,
        "expires_at": session.expires_at,
        "revoked_at": session.revoked_at,
        "rotated_at": session.rotated_at,
        "mfa_verified": bool(session.mfa_verified),
        "ip": session.ip,
        "user_agent": session.user_agent,
    }


def _token_to_dict(token: AuthToken | None) -> dict | None:
    if token is None:
        return None
    return {
        "id": token.id,
        "purpose": token.purpose,
        "user_id": token.user_id,
        "token_hash": token.token_hash,
        "created_at": token.created_at,
        "expires_at": token.expires_at,
        "used_at": token.used_at,
        "extra": token.extra or {},
    }


def _api_key_to_dict(record: ApiKey | None) -> dict | None:
    if record is None:
        return None
    key_id = record.key
    environment = record.environment or "live"
    masked_key = record.display_prefix or _mask_key_display(key_id)
    expires_at = record.expires_at
    rotation_due_at = record.rotation_due_at
    return {
        "id": key_id,
        "name": record.name,
        "tier": record.tier,
        "owner_user_id": record.owner_user_id,
        "environment": environment,
        "scopes": list(record.scopes or []),
        "masked_key": masked_key,
        "last_used_at": record.last_used_at,
        "last_used_ip": record.last_used_ip,
        "expires_at": expires_at,
        "rotation_due_at": rotation_due_at,
        "revoked_at": record.revoked_at,
        "disabled_at": record.disabled_at,
        "active": not bool(record.revoked_at or record.disabled_at or _key_is_expired(record)),
        "rotation_required": _key_requires_rotation(record),
        "calls_today": int(record.calls_today or 0),
        "calls_total": int(record.calls_total or 0),
        "last_reset": record.last_reset,
        "created_at": record.created_at,
    }


def _load_legacy_json(path: str, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as handle:
        try:
            return json.load(handle)
        except Exception:
            return default


def _import_legacy_api_keys(session):
    global _legacy_api_keys_checked
    if _legacy_api_keys_checked:
        return
    _legacy_api_keys_checked = True
    payload = _load_legacy_json(LEGACY_API_KEYS_FILE, {})
    if not isinstance(payload, dict) or not payload:
        return
    for legacy_key, value in payload.items():
        if not legacy_key or not isinstance(value, dict):
            continue
        legacy_secret_hash = _api_key_secret_hash(legacy_key)
        existing = session.scalar(select(ApiKey).where(ApiKey.secret_hash == legacy_secret_hash))
        if existing is not None:
            continue
        identifier = f"legacy_{_token_digest(legacy_key)[:18]}"
        session.add(
            ApiKey(
                key=identifier,
                secret_hash=legacy_secret_hash,
                display_prefix=f"legacy_{legacy_key[:4]}...{legacy_key[-4:]}",
                name=value.get("name") or "user",
                tier=value.get("tier") or "free",
                owner_user_id=value.get("owner_user_id"),
                scopes=_normalize_scopes(["read", "write", "admin"]),
                environment="live",
                last_used_at=None,
                last_used_ip=None,
                expires_at=None,
                rotated_from_key_id=None,
                rotation_due_at=None,
                revoked_at=None,
                disabled_at=None,
                calls_today=int(value.get("calls_today", 0) or 0),
                calls_total=int(value.get("calls_total", 0) or 0),
                last_reset=value.get("last_reset") or date.today().isoformat(),
                created_at=value.get("created_at") or utc_now_iso(),
            )
        )


def _ensure_legacy_api_keys_imported():
    with _db_session() as session:
        _import_legacy_api_keys(session)


def _find_user_by_email(email: str) -> tuple[str | None, dict | None]:
    normalized = _normalize_email(email)
    with _db_session() as session:
        user = session.scalar(select(User).where(User.email == normalized))
        if user is None:
            return None, None
        payload = _user_to_dict(user)
        return payload["id"], payload


def get_user(user_id: str) -> dict | None:
    with _db_session() as session:
        return _user_to_dict(session.get(User, user_id))


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


def _key_is_expired(record: ApiKey) -> bool:
    expires_at = _parse_dt(record.expires_at)
    return bool(expires_at and expires_at <= utc_now())


def _key_requires_rotation(record: ApiKey) -> bool:
    rotation_due_at = _parse_dt(record.rotation_due_at)
    return bool(rotation_due_at and rotation_due_at <= utc_now())


def _record_audit_event(event_type: str, actor_user_id: str | None, payload: dict | None = None):
    from models import AuditEvent

    with _db_session() as session:
        session.add(
            AuditEvent(
                id=uuid.uuid4().hex,
                actor_user_id=actor_user_id,
                event_type=event_type,
                created_at=utc_now_iso(),
                payload=payload or {},
            )
        )


def create_user(email: str, password: str) -> tuple[bool, dict]:
    normalized = _normalize_email(email)
    if not normalized or "@" not in normalized:
        return False, {"reason": "invalid_email"}
    if len(password or "") < 10:
        return False, {"reason": "weak_password"}
    with _db_session() as session:
        existing = session.scalar(select(User).where(User.email == normalized))
        if existing is not None:
            return False, {"reason": "email_taken"}
        user = User(
            id=uuid.uuid4().hex,
            email=normalized,
            password_hash=_hash_password(password),
            email_verified=False,
            created_at=utc_now_iso(),
            last_login_at=None,
            security_tier="standard",
            failed_login_attempts=0,
            locked_until=None,
            mfa_enabled=False,
            mfa_secret=None,
            mfa_pending_secret=None,
            mfa_enrolled_at=None,
        )
        session.add(user)
        payload = _user_to_dict(user)
        return True, {"user": _public_user(payload)}


def authenticate_user(email: str, password: str) -> tuple[bool, dict]:
    normalized = _normalize_email(email)
    with _db_session() as session:
        user = session.scalar(select(User).where(User.email == normalized))
        if user is None:
            return False, {"reason": "invalid_credentials"}
        payload = _user_to_dict(user)
        if _is_locked(payload):
            return False, {"reason": "locked", "locked_until": payload.get("locked_until")}
        if not _verify_password(password, user.password_hash):
            attempts = int(user.failed_login_attempts or 0) + 1
            user.failed_login_attempts = attempts
            if attempts >= LOCKOUT_THRESHOLD:
                user.locked_until = (utc_now() + timedelta(minutes=LOCKOUT_MINUTES)).isoformat().replace("+00:00", "Z")
                user.failed_login_attempts = 0
            return False, {"reason": "invalid_credentials"}
        return True, {"user_id": user.id, "user": payload}


def complete_login_success(user_id: str):
    with _db_session() as session:
        user = session.get(User, user_id)
        if user is None:
            return
        user.failed_login_attempts = 0
        user.locked_until = None
        user.last_login_at = utc_now_iso()


def _random_token(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(32)}"


def _create_token_record(purpose: str, user_id: str, ttl_seconds: int, extra: dict | None = None) -> str:
    plain = _random_token(purpose)
    with _db_session() as session:
        session.add(
            AuthToken(
                id=uuid.uuid4().hex,
                purpose=purpose,
                user_id=user_id,
                token_hash=_token_digest(plain),
                created_at=utc_now_iso(),
                expires_at=(utc_now() + timedelta(seconds=ttl_seconds)).isoformat().replace("+00:00", "Z"),
                used_at=None,
                extra=extra or {},
            )
        )
    return plain


def _consume_token(purpose: str, token: str) -> tuple[bool, dict]:
    token_hash = _token_digest(token)
    with _db_session() as session:
        record = session.scalar(
            select(AuthToken).where(AuthToken.purpose == purpose, AuthToken.token_hash == token_hash)
        )
        if record is None:
            return False, {"reason": "invalid_token"}
        expires_at = _parse_dt(record.expires_at)
        if record.used_at or not expires_at or expires_at <= utc_now():
            return False, {"reason": "expired_or_used"}
        record.used_at = utc_now_iso()
        return True, _token_to_dict(record)


def create_email_verification_token(user_id: str) -> str:
    return _create_token_record("email_verify", user_id, EMAIL_VERIFICATION_TTL_SECONDS)


def verify_email_token(token: str) -> tuple[bool, dict]:
    ok, info = _consume_token("email_verify", token)
    if not ok:
        return False, info
    with _db_session() as session:
        user = session.get(User, info.get("user_id"))
        if user is None:
            return False, {"reason": "user_missing"}
        user.email_verified = True
        return True, {"user": _public_user(_user_to_dict(user))}


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
    with _db_session() as session:
        user = session.get(User, user_id)
        if user is None:
            return False, {"reason": "user_missing"}
        secret = _base32_secret()
        user.mfa_pending_secret = secret
        issuer = quote("AFE")
        account = quote(user.email or user.id)
        return True, {
            "secret": secret,
            "otpauth_url": f"otpauth://totp/{issuer}:{account}?secret={secret}&issuer={issuer}&algorithm=SHA1&digits=6&period=30",
        }


def confirm_mfa_setup(user_id: str, code: str) -> tuple[bool, dict]:
    with _db_session() as session:
        user = session.get(User, user_id)
        if user is None:
            return False, {"reason": "user_missing"}
        secret = user.mfa_pending_secret
        if not secret or not verify_totp(secret, code):
            return False, {"reason": "invalid_code"}
        user.mfa_secret = secret
        user.mfa_pending_secret = None
        user.mfa_enabled = True
        user.mfa_enrolled_at = utc_now_iso()
        user.security_tier = "high_trust"
        return True, {"user": _public_user(_user_to_dict(user))}


def create_session(user_id: str, ip: str | None, user_agent: str | None, mfa_verified: bool) -> tuple[str, dict]:
    refresh_token = _random_token("refresh")
    now = utc_now_iso()
    record = AuthSession(
        id=uuid.uuid4().hex,
        user_id=user_id,
        refresh_token_hash=_token_digest(refresh_token),
        created_at=now,
        last_seen_at=now,
        expires_at=(utc_now() + timedelta(seconds=REFRESH_TOKEN_TTL_SECONDS)).isoformat().replace("+00:00", "Z"),
        revoked_at=None,
        rotated_at=None,
        mfa_verified=bool(mfa_verified),
        ip=ip,
        user_agent=user_agent,
    )
    with _db_session() as session:
        session.add(record)
        payload = _session_to_dict(record)
    return refresh_token, payload


def get_session(session_id: str) -> dict | None:
    with _db_session() as session:
        return _session_to_dict(session.get(AuthSession, session_id))


def _session_active(session: dict | None) -> bool:
    if not session or session.get("revoked_at"):
        return False
    expires_at = _parse_dt(session.get("expires_at"))
    return bool(expires_at and expires_at > utc_now())


def session_active(session: dict | None) -> bool:
    return _session_active(session)


def rotate_refresh_session(refresh_token: str, ip: str | None, user_agent: str | None) -> tuple[bool, dict]:
    presented_hash = _token_digest(refresh_token)
    with _db_session() as session:
        record = session.scalar(select(AuthSession).where(AuthSession.refresh_token_hash == presented_hash))
        if record is None:
            return False, {"reason": "invalid_session"}
        payload = _session_to_dict(record)
        if not _session_active(payload):
            return False, {"reason": "invalid_session"}
        new_refresh = _random_token("refresh")
        record.refresh_token_hash = _token_digest(new_refresh)
        record.rotated_at = utc_now_iso()
        record.last_seen_at = utc_now_iso()
        record.ip = ip
        record.user_agent = user_agent
        return True, {"refresh_token": new_refresh, "session": _session_to_dict(record)}


def revoke_session(session_id: str):
    with _db_session() as session:
        record = session.get(AuthSession, session_id)
        if record is None:
            return
        record.revoked_at = utc_now_iso()


def revoke_all_sessions(user_id: str, except_session_id: str | None = None):
    with _db_session() as session:
        records = session.scalars(select(AuthSession).where(AuthSession.user_id == user_id)).all()
        now = utc_now_iso()
        for record in records:
            if except_session_id and record.id == except_session_id:
                continue
            if not record.revoked_at:
                record.revoked_at = now


def list_sessions(user_id: str) -> list[dict]:
    with _db_session() as session:
        records = session.scalars(
            select(AuthSession).where(AuthSession.user_id == user_id).order_by(AuthSession.created_at.desc())
        ).all()
        items = []
        for record in records:
            payload = _session_to_dict(record)
            items.append(
                {
                    "id": payload.get("id"),
                    "created_at": payload.get("created_at"),
                    "last_seen_at": payload.get("last_seen_at"),
                    "expires_at": payload.get("expires_at"),
                    "revoked_at": payload.get("revoked_at"),
                    "ip": payload.get("ip"),
                    "user_agent": payload.get("user_agent"),
                    "mfa_verified": bool(payload.get("mfa_verified")),
                    "active": _session_active(payload),
                }
            )
        return items


def create_reauth_token(user_id: str, session_id: str) -> str:
    return _create_token_record("reauth", user_id, REAUTH_TTL_SECONDS, {"session_id": session_id})


def verify_reauth_token(token: str, user_id: str, session_id: str) -> bool:
    token_hash = _token_digest(token)
    with _db_session() as session:
        record = session.scalar(
            select(AuthToken).where(
                AuthToken.purpose == "reauth",
                AuthToken.user_id == user_id,
                AuthToken.token_hash == token_hash,
            )
        )
        if record is None:
            return False
        if (record.extra or {}).get("session_id") != session_id:
            return False
        expires_at = _parse_dt(record.expires_at)
        if not expires_at or expires_at <= utc_now():
            return False
        if record.used_at:
            return False
        return True


def key_hash(key: str) -> str:
    return hashlib.sha256((key or "").encode("utf-8")).hexdigest()


def _api_key_plaintext(environment: str, identifier: str, secret: str) -> str:
    return f"{_api_key_prefix(environment)}{identifier}_{secret}"


def _create_api_key_record(
    *,
    session,
    owner_user_id: str | None,
    name: str,
    tier: str,
    environment: str,
    scopes: list[str] | None,
    expires_at: str | None = None,
    rotated_from_key_id: str | None = None,
):
    normalized_environment = _normalize_environment(environment)
    normalized_scopes = _normalize_scopes(scopes)
    identifier = _api_key_identifier()
    while session.get(ApiKey, identifier) is not None:
        identifier = _api_key_identifier()
    secret = secrets.token_urlsafe(API_KEY_SECRET_BYTES)
    plaintext = _api_key_plaintext(normalized_environment, identifier, secret)
    created_at = utc_now_iso()
    record = ApiKey(
        key=identifier,
        secret_hash=_api_key_secret_hash(secret),
        display_prefix=f"{_api_key_prefix(normalized_environment)}{identifier}_****",
        name=(name or "default").strip() or "default",
        tier=tier or "free",
        owner_user_id=owner_user_id,
        scopes=normalized_scopes,
        environment=normalized_environment,
        last_used_at=None,
        last_used_ip=None,
        expires_at=expires_at,
        rotated_from_key_id=rotated_from_key_id,
        rotation_due_at=(utc_now() + timedelta(days=API_KEY_ROTATION_DAYS)).isoformat().replace("+00:00", "Z"),
        revoked_at=None,
        disabled_at=None,
        calls_today=0,
        calls_total=0,
        last_reset=date.today().isoformat(),
        created_at=created_at,
    )
    session.add(record)
    return plaintext, record


def generate_key(
    name: str = "user",
    tier: str = "free",
    owner_user_id: str | None = None,
    *,
    environment: str = "live",
    scopes: list[str] | None = None,
    expires_at: str | None = None,
) -> dict:
    with _db_session() as session:
        _import_legacy_api_keys(session)
        plaintext, record = _create_api_key_record(
            session=session,
            owner_user_id=owner_user_id,
            name=name,
            tier=tier,
            environment=environment,
            scopes=scopes,
            expires_at=expires_at,
        )
        payload = _api_key_to_dict(record)
    _record_audit_event(
        "api_key.created",
        owner_user_id,
        {"key_id": payload["id"], "environment": payload["environment"], "scopes": payload["scopes"]},
    )
    payload["plaintext_key"] = plaintext
    return payload


def _reset_if_needed(record: ApiKey):
    today = date.today().isoformat()
    if record.last_reset != today:
        record.calls_today = 0
        record.last_reset = today


def _find_api_key_record(session, presented_key: str | None) -> ApiKey | None:
    prefix, identifier, secret = _split_presented_key(presented_key)
    if not presented_key:
        return None
    if prefix and identifier and secret:
        record = session.get(ApiKey, identifier)
        if record is None:
            return None
        if record.environment != ("test" if prefix == "pk_test" else "live"):
            return None
        if not hmac.compare_digest(record.secret_hash or "", _api_key_secret_hash(secret)):
            return None
        return record
    legacy_hash = _api_key_secret_hash(secret or "")
    return session.scalar(select(ApiKey).where(ApiKey.secret_hash == legacy_hash))


def validate_key(key: str, ip: str | None = None):
    if not key:
        return False, "missing"
    _ensure_legacy_api_keys_imported()
    with _db_session() as session:
        record = _find_api_key_record(session, key)
        if record is None:
            return False, "invalid"
        if record.revoked_at or record.disabled_at or _key_is_expired(record):
            return False, "invalid"
        if _key_requires_rotation(record):
            return False, "rotation_required"
        _reset_if_needed(record)
        tier = record.tier or "free"
        if tier == "free" and int(record.calls_today or 0) >= FREE_DAILY_LIMIT:
            return False, "rate_limited"
        record.last_used_at = utc_now_iso()
        record.last_used_ip = ip
        return True, _api_key_to_dict(record)


def increment_usage(key: str):
    _ensure_legacy_api_keys_imported()
    with _db_session() as session:
        record = _find_api_key_record(session, key)
        if record is None:
            return False
        _reset_if_needed(record)
        record.calls_today = int(record.calls_today or 0) + 1
        record.calls_total = int(record.calls_total or 0) + 1
        if not record.last_reset:
            record.last_reset = date.today().isoformat()
        return True


def usage_info(key: str):
    _ensure_legacy_api_keys_imported()
    with _db_session() as session:
        record = _find_api_key_record(session, key)
        if record is None:
            return None
        _reset_if_needed(record)
        tier = record.tier or "free"
        limit = None if tier != "free" else FREE_DAILY_LIMIT
        return {
            "key_id": record.key,
            "name": record.name,
            "tier": tier,
            "environment": record.environment,
            "scopes": list(record.scopes or []),
            "masked_key": record.display_prefix,
            "calls_today": int(record.calls_today or 0),
            "calls_total": int(record.calls_total or 0),
            "limit_today": limit,
            "member_since": record.created_at,
            "last_used_at": record.last_used_at,
            "last_used_ip": record.last_used_ip,
            "expires_at": record.expires_at,
            "rotation_due_at": record.rotation_due_at,
            "revoked_at": record.revoked_at,
            "disabled_at": record.disabled_at,
            "calls_remaining": None if limit is None else max(0, limit - int(record.calls_today or 0)),
        }


def list_api_keys(owner_user_id: str) -> list[dict]:
    _ensure_legacy_api_keys_imported()
    with _db_session() as session:
        records = session.scalars(
            select(ApiKey).where(ApiKey.owner_user_id == owner_user_id).order_by(ApiKey.created_at.desc())
        ).all()
        return [_api_key_to_dict(record) for record in records]


def api_key_has_scope(record: dict | None, required_scope: str) -> bool:
    if not record:
        return False
    scopes = set(record.get("scopes") or [])
    return "admin" in scopes or required_scope in scopes


def revoke_api_key(owner_user_id: str, key_id: str) -> tuple[bool, dict]:
    with _db_session() as session:
        record = session.get(ApiKey, key_id)
        if record is None or record.owner_user_id != owner_user_id:
            return False, {"reason": "not_found"}
        record.revoked_at = utc_now_iso()
        payload = _api_key_to_dict(record)
    _record_audit_event("api_key.revoked", owner_user_id, {"key_id": key_id})
    return True, payload


def disable_api_key(owner_user_id: str, key_id: str, disabled: bool) -> tuple[bool, dict]:
    with _db_session() as session:
        record = session.get(ApiKey, key_id)
        if record is None or record.owner_user_id != owner_user_id:
            return False, {"reason": "not_found"}
        record.disabled_at = utc_now_iso() if disabled else None
        payload = _api_key_to_dict(record)
    _record_audit_event(
        "api_key.disabled" if disabled else "api_key.enabled",
        owner_user_id,
        {"key_id": key_id},
    )
    return True, payload


def rotate_api_key(owner_user_id: str, key_id: str, *, name: str | None = None, expires_at: str | None = None) -> tuple[bool, dict]:
    with _db_session() as session:
        record = session.get(ApiKey, key_id)
        if record is None or record.owner_user_id != owner_user_id:
            return False, {"reason": "not_found"}
        plaintext, new_record = _create_api_key_record(
            session=session,
            owner_user_id=owner_user_id,
            name=name or record.name,
            tier=record.tier,
            environment=record.environment,
            scopes=list(record.scopes or []),
            expires_at=expires_at or record.expires_at,
            rotated_from_key_id=record.key,
        )
        record.revoked_at = utc_now_iso()
        payload = _api_key_to_dict(new_record)
    _record_audit_event("api_key.rotated", owner_user_id, {"old_key_id": key_id, "new_key_id": payload["id"]})
    payload["plaintext_key"] = plaintext
    return True, payload
