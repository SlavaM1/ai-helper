from collections.abc import Generator
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import Settings, get_settings


class Base(DeclarativeBase):
    pass


def _ensure_sqlite_directory(database_url: str) -> None:
    prefix = "sqlite:///"
    if not database_url.startswith(prefix) or database_url in {
        "sqlite:///:memory:",
        "sqlite:///file::memory:?cache=shared",
    }:
        return

    path = database_url.removeprefix(prefix).split("?", 1)[0]
    if path:
        Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


def create_database_engine(
    database_url: str | None = None,
    *,
    busy_timeout_ms: int | None = None,
) -> Engine:
    settings = get_settings()
    url = database_url or settings.database_url
    timeout = busy_timeout_ms if busy_timeout_ms is not None else settings.sqlite_busy_timeout_ms
    _ensure_sqlite_directory(url)

    connect_args = {"check_same_thread": False} if url.startswith("sqlite:") else {}
    engine = create_engine(url, connect_args=connect_args)

    if url.startswith("sqlite:"):

        @event.listens_for(engine, "connect")
        def set_sqlite_pragmas(dbapi_connection: object, _: object) -> None:
            cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute(f"PRAGMA busy_timeout={timeout}")
                cursor.execute("PRAGMA journal_mode=WAL")
            finally:
                cursor.close()

    return engine


settings: Settings = get_settings()
engine = create_database_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)


def init_db(target_engine: Engine = engine) -> None:
    # Importing registers all model tables on Base.metadata.
    from . import models  # noqa: F401
    from .file_agent import models as file_agent_models  # noqa: F401

    Base.metadata.create_all(target_engine)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
