from settings import settings

try:
    from sqlalchemy import create_engine, inspect, text
    from sqlalchemy.orm import DeclarativeBase, sessionmaker
except ModuleNotFoundError:  # pragma: no cover - optional runtime dependency
    create_engine = None
    inspect = None
    text = None
    sessionmaker = None

    class DeclarativeBase:
        metadata = None


class Base(DeclarativeBase):
    pass


engine = None
SessionLocal = None

if settings.database_url and create_engine and sessionmaker:
    connect_args = {}
    if settings.database_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    engine = create_engine(settings.database_url, future=True, pool_pre_ping=True, connect_args=connect_args)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False, future=True)


def database_enabled() -> bool:
    return engine is not None


def initialize_database():
    if not engine:
        return False
    import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _apply_lightweight_migrations()
    return True


def _apply_lightweight_migrations():
    if engine is None or inspect is None or text is None:
        return
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if "api_keys" not in tables:
        return
    existing = {col["name"] for col in inspector.get_columns("api_keys")}
    additions = {
        "secret_hash": "VARCHAR(128)",
        "display_prefix": "VARCHAR(32)",
        "scopes": "JSON",
        "environment": "VARCHAR(16)",
        "last_used_at": "VARCHAR(64)",
        "last_used_ip": "VARCHAR(128)",
        "expires_at": "VARCHAR(64)",
        "rotated_from_key_id": "VARCHAR(128)",
        "rotation_due_at": "VARCHAR(64)",
        "revoked_at": "VARCHAR(64)",
        "disabled_at": "VARCHAR(64)",
    }
    with engine.begin() as conn:
        for name, ddl_type in additions.items():
            if name in existing:
                continue
            conn.execute(text(f"ALTER TABLE api_keys ADD COLUMN {name} {ddl_type}"))
