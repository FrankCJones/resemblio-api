"""SQLAlchemy engine and session management."""
from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import get_settings


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


def _engine_kwargs(database_url: str) -> dict[str, object]:
    """Return SQLAlchemy engine options, including SQLite test ergonomics."""
    if database_url == "sqlite+pysqlite:///:memory:":
        return {"connect_args": {"check_same_thread": False}, "poolclass": StaticPool}
    if database_url.startswith("sqlite"):
        return {"connect_args": {"check_same_thread": False}}
    return {"pool_pre_ping": True}


def create_db_engine(database_url: str) -> Engine:
    """Create an engine for Postgres in production or SQLite in tests."""
    return create_engine(database_url, future=True, **_engine_kwargs(database_url))


engine = create_db_engine(get_settings().database_url)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False, expire_on_commit=False, future=True)


def reset_engine(database_url: str) -> None:
    """Swap the global engine and session factory for isolated tests."""
    global engine, SessionLocal
    engine.dispose()
    engine = create_db_engine(database_url)
    SessionLocal.configure(bind=engine)


def get_db() -> Generator[Session, None, None]:
    """Yield a request-scoped database session and always close it."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()

