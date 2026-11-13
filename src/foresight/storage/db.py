"""Async engine construction and schema bootstrap."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from foresight.config import DatabaseConfig
from foresight.storage.tables import metadata


def create_engine(config: DatabaseConfig, *, readonly: bool = False) -> AsyncEngine:
    """Create an async engine for the configured backend.

    With ``readonly=True`` the engine connects through the read-only URL:
    for SQLite that is a ``file:...?mode=ro`` URI, so the driver itself
    rejects every write statement. A read-only engine never creates the
    database file either — the dump belongs to the collector.
    """
    url = config.readonly_sqlalchemy_url if readonly else config.sqlalchemy_url
    if config.backend == "sqlite" and not readonly:
        path = Path(config.path)
        if path.parent != Path("."):
            path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_async_engine(url, future=True, pool_pre_ping=True)
    if config.backend == "sqlite":
        _apply_sqlite_pragmas(engine, readonly=readonly)
    return engine


def _apply_sqlite_pragmas(engine: AsyncEngine, *, readonly: bool = False) -> None:
    """Tune SQLite for concurrent readers and a busy writer.

    A read-only connection cannot switch the journal mode: the pragma
    writes to the database header and a read-only file refuses the
    write, so only the connection-local pragmas are applied there.
    """

    @event.listens_for(engine.sync_engine, "connect")
    def _set_pragmas(dbapi_connection: Any, connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        if not readonly:
            cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()


async def init_db(engine: AsyncEngine) -> None:
    """Create every table that does not exist yet."""
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
