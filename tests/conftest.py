import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from foresight.storage.db import init_db
from foresight.storage.tables import metadata


@pytest.fixture
async def db_engine(tmp_path) -> AsyncIterator[AsyncEngine]:
    """An async engine on a fresh SQLite database, disposed after the test.

    The engine must be disposed: the pool of an engine keeps the database
    connection open, and a connection that is only closed when the garbage
    collector reaches it reports a `ResourceWarning` — which fails the whole
    suite under `-W error`.
    """
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    try:
        await init_db(engine)
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def schema_engine(tmp_path) -> Iterator[sa.Engine]:
    """A synchronous SQLite engine with the schema, disposed after the test.

    The same reason as for `db_engine`: the pooled connection is only closed
    when the engine is disposed.
    """
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'schema.db'}")
    try:
        metadata.create_all(engine)
        yield engine
    finally:
        engine.dispose()
