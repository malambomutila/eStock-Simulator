from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker, Session
from contextlib import contextmanager
from typing import Generator
import logging

from config.settings import settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    """Shared declarative base — all ORM models inherit from this."""
    pass


def _configure_sqlite(engine):
    """Enable WAL mode and foreign-key enforcement for SQLite connections."""
    if engine.dialect.name != "sqlite":
        return

    @event.listens_for(engine, "connect")
    def set_sqlite_pragmas(dbapi_conn, _connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def _make_engine(url: str):
    eng = create_engine(
        url,
        echo=settings.db_echo_sql,
        connect_args={"check_same_thread": False} if "sqlite" in url else {},
    )
    _configure_sqlite(eng)
    return eng


engine = _make_engine(settings.database_url)

SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,
)


def rebind_engine(url: str) -> None:
    """
    Replace the process-global engine and session factory.

    Intended for tests and isolated scripts; call ``init_db()`` after rebind.
    """
    global engine, SessionLocal
    try:
        engine.dispose(close=True)
    except Exception:  # noqa: BLE001
        pass
    engine = _make_engine(url)
    SessionLocal = sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )


@contextmanager
def get_db() -> Generator[Session, None, None]:
    """
    Yield a database session and guarantee cleanup.

    Usage:
        with get_db() as db:
            db.add(obj)
    """
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db_session() -> Generator[Session, None, None]:
    """FastAPI dependency that yields a session and closes it after the request."""
    with get_db() as session:
        yield session


def init_db() -> None:
    """Create all tables declared on Base. Safe to call multiple times (CREATE IF NOT EXISTS)."""
    from core import models  # noqa: F401 — side-effect import registers models on Base

    Base.metadata.create_all(bind=engine)
    logger.info("Database tables initialised at %s", settings.database_url)


def verify_connection() -> bool:
    """Return True if the database is reachable."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:
        logger.error("Database connection failed: %s", exc)
        return False
