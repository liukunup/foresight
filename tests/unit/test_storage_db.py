"""Tests for the engines of the service.

The API must never be able to write to the dump: for SQLite that is
structural — the database is opened through a read-only URI — and for
PostgreSQL and MySQL it is a setting of the session, so that every
connection of the engine of the API is read-only. The engine of the
collector, on the other hand, must stay writable: it is the one that
writes the dump.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from foresight.config import DatabaseConfig
from foresight.storage.db import create_engine


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The arguments the engine was built with."""
    captured: dict[str, Any] = {}

    def fake_create_async_engine(url: str, **kwargs: Any) -> Any:
        captured["url"] = url
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(
        "foresight.storage.db.create_async_engine", fake_create_async_engine
    )
    return captured


def test_the_postgresql_engine_of_the_api_is_read_only(captured: dict[str, Any]):
    """PostgreSQL knows the read-only session as a setting of the connection."""
    create_engine(DatabaseConfig(backend="postgresql"), readonly=True)

    assert captured["connect_args"] == {
        "server_settings": {"default_transaction_read_only": "on"}
    }


def test_the_mysql_engine_of_the_api_is_read_only(captured: dict[str, Any]):
    """MySQL knows the read-only session as a statement of the connection."""
    create_engine(DatabaseConfig(backend="mysql"), readonly=True)

    assert captured["connect_args"] == {
        "init_command": "SET SESSION TRANSACTION READ ONLY"
    }


def test_the_engine_of_the_collector_may_write(captured: dict[str, Any]):
    """The collector writes the dump: its engine stays writable."""
    create_engine(DatabaseConfig(backend="postgresql"))
    create_engine(DatabaseConfig(backend="mysql"))

    assert "connect_args" not in captured


async def test_the_sqlite_engine_of_the_api_cannot_write(tmp_path):
    """SQLite needs no setting: the URI of the driver refuses the writes."""
    path = tmp_path / "ro.db"
    writer = create_engine(DatabaseConfig(backend="sqlite", path=str(path)))
    async with writer.begin() as conn:
        await conn.execute(text("CREATE TABLE t (a INTEGER)"))
    await writer.dispose()

    reader = create_engine(
        DatabaseConfig(backend="sqlite", path=str(path)), readonly=True
    )
    try:
        with pytest.raises(SQLAlchemyError):
            async with reader.begin() as conn:
                await conn.execute(text("INSERT INTO t VALUES (1)"))
    finally:
        await reader.dispose()
