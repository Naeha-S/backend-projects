from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from db import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)
    last_login_at: Mapped[str | None] = mapped_column(String(64))
    security_tier: Mapped[str] = mapped_column(String(32), default="standard", nullable=False)
    failed_login_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_until: Mapped[str | None] = mapped_column(String(64))
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    mfa_secret: Mapped[str | None] = mapped_column(String(128))
    mfa_pending_secret: Mapped[str | None] = mapped_column(String(128))
    mfa_enrolled_at: Mapped[str | None] = mapped_column(String(64))


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    refresh_token_hash: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)
    last_seen_at: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[str] = mapped_column(String(64), nullable=False)
    revoked_at: Mapped[str | None] = mapped_column(String(64))
    rotated_at: Mapped[str | None] = mapped_column(String(64))
    mfa_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    ip: Mapped[str | None] = mapped_column(String(128))
    user_agent: Mapped[str | None] = mapped_column(Text)


class AuthToken(Base):
    __tablename__ = "auth_tokens"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    purpose: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    token_hash: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[str] = mapped_column(String(64), nullable=False)
    used_at: Mapped[str | None] = mapped_column(String(64))
    extra: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class AuthSetting(Base):
    __tablename__ = "auth_settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)


class ApiKey(Base):
    __tablename__ = "api_keys"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    secret_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    display_prefix: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(320), nullable=False)
    tier: Mapped[str] = mapped_column(String(32), default="free", nullable=False)
    owner_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), index=True)
    scopes: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    environment: Mapped[str] = mapped_column(String(16), default="live", index=True, nullable=False)
    last_used_at: Mapped[str | None] = mapped_column(String(64))
    last_used_ip: Mapped[str | None] = mapped_column(String(128))
    expires_at: Mapped[str | None] = mapped_column(String(64))
    rotated_from_key_id: Mapped[str | None] = mapped_column(String(128), index=True)
    rotation_due_at: Mapped[str | None] = mapped_column(String(64))
    revoked_at: Mapped[str | None] = mapped_column(String(64), index=True)
    disabled_at: Mapped[str | None] = mapped_column(String(64), index=True)
    calls_today: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    calls_total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_reset: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)


class AnalysisRecord(Base):
    __tablename__ = "analysis_records"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_key_hash: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    problem: Mapped[str] = mapped_column(Text, nullable=False)
    domain: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    actor_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
