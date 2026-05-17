from settings import settings

try:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import DeclarativeBase, sessionmaker
except ModuleNotFoundError:  # pragma: no cover - optional runtime dependency
    create_engine = None
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
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def database_enabled() -> bool:
    return engine is not None


def initialize_database():
    if not engine:
        return False
    import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    return True
