"""Tests for the application factory.

The factory owns the engine it builds: it opens the database read-only
and it disposes the engine when the application shuts down.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from foresight.config import DatabaseConfig
from foresight.storage.db import create_engine, init_db


async def test_storage_engine_can_open_the_database_read_only(tmp_path):
    """`create_engine(..., readonly=True)` opens the read-only URL.

    The read-only engine is the one the API uses, so the URL — not a
    convention — has to carry the guarantee.
    """
    path = tmp_path / "ro.db"
    writer = create_engine(DatabaseConfig(backend="sqlite", path=str(path)))
    await init_db(writer)
    await writer.dispose()

    engine = create_engine(
        DatabaseConfig(backend="sqlite", path=str(path)), readonly=True
    )
    try:
        assert engine.url.database.startswith("file:")
        assert dict(engine.url.query)["mode"] == "ro"
        with pytest.raises(OperationalError) as failure:
            async with engine.connect() as conn:
                await conn.execute(
                    text("INSERT INTO messages (message_id, content) VALUES (1, 'x')")
                )
        assert "readonly database" in str(failure.value).lower()
    finally:
        await engine.dispose()


async def test_app_builds_a_read_only_engine(api_harness):
    """The engine the application builds must not be able to write.

    The read-only guarantee has to hold by construction: if the
    application opened the writable URL, the API could be used to change
    the dump behind the collector's back.
    """
    harness = await api_harness(inject_engine=False)
    async with harness.lifespan():
        engine = harness.engine
        url = engine.url
        assert url.database.startswith("file:")
        assert dict(url.query)["mode"] == "ro"
        with pytest.raises(OperationalError) as failure:
            async with engine.connect() as conn:
                await conn.execute(
                    text("INSERT INTO messages (message_id, content) VALUES (1, 'x')")
                )
        assert "readonly database" in str(failure.value).lower()


async def test_app_disposes_the_engine_it_created(api_harness):
    """The engine the application built is closed when it shuts down.

    A connection that is only closed when the garbage collector reaches
    it keeps the database file open.
    """
    harness = await api_harness(inject_engine=False)
    engine = harness.engine
    pool = engine.pool
    async with harness.lifespan():
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    assert engine.pool is not pool


async def test_app_does_not_dispose_an_injected_engine(api_harness):
    """An injected engine belongs to the caller, which may still use it."""
    harness = await api_harness()
    engine = harness.engine
    pool = engine.pool
    async with harness.lifespan():
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    assert engine.pool is pool
