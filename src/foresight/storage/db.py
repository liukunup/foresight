"""Async engine construction and schema bootstrap."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from foresight.config import DatabaseConfig
from foresight.storage.tables import metadata


def create_engine(config: DatabaseConfig) -> AsyncEngine:
    """Create an async engine for the configured backend."""
    url = config.sqlalchemy_url
    if config.backend == "sqlite":
        path = Path(config.path)
        if path.parent != Path("."):
            path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_async_engine(url, future=True, pool_pre_ping=True)
    if config.backend == "sqlite":
        _apply_sqlite_pragmas(engine)
    return engine


def _apply_sqlite_pragmas(engine: AsyncEngine) -> None:
    """Tune SQLite for concurrent readers and a busy writer."""

    @event.listens_for(engine.sync_engine, "connect")
    def _set_pragmas(dbapi_connection, connection_record):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()


async def init_db(engine: AsyncEngine) -> None:
    """Create every table that does not exist yet."""
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
