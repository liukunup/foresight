from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from foresight.api.app import create_app
from foresight.domain.models import MessageRecord, OpKind, WriteOp
from foresight.storage.db import init_db
from foresight.storage.repository import Repository


def api_config(db_path: Path) -> SimpleNamespace:
    """The configuration of an API that reads the database at `db_path`."""
    return SimpleNamespace(
        api=SimpleNamespace(host="127.0.0.1", port=8000, metrics_token=""),
        database=SimpleNamespace(
            backend="sqlite",
            path=str(db_path),
            sqlalchemy_url=f"sqlite+aiosqlite:///{db_path}",
            readonly_sqlalchemy_url=(
                f"sqlite+aiosqlite:///file:{db_path}?mode=ro&uri=true"
            ),
        ),
        media=SimpleNamespace(backend="local"),
    )


def api_message(
    message_id: int,
    content: str,
    created_at: str = "2026-11-11T10:00:00+00:00",
) -> MessageRecord:
    """A message record for the API tests."""
    return MessageRecord(
        message_id=message_id,
        guild_id=10,
        channel_id=20,
        author_id=30,
        content=content,
        message_type=0,
        created_at=created_at,
        captured_at="2026-11-11T10:00:00+00:00",
        char_count=len(content),
        word_count=len(content.split()),
    )


class ApiHarness:
    """An API application together with its client and its engine."""

    def __init__(
        self,
        app,
        client: AsyncClient,
        engine: AsyncEngine,
        owns_engine: bool,
    ) -> None:
        self.app = app
        self.client = client
        self.engine = engine
        self.owns_engine = owns_engine

    @asynccontextmanager
    async def lifespan(self):
        """Run the lifecycle of the application: startup and shutdown."""
        async with self.app.router.lifespan_context(self.app):
            yield self


@pytest.fixture
async def api_harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Build an API application over a database the test seeds.

    `api_harness(records)` returns an :class:`ApiHarness`: the
    application, a client for it and the engine behind them. The records
    are the messages to seed; an empty list seeds an empty database. The
    token comes from `FORESIGHT_API_TOKEN` unless the caller passes
    `token=None` (an open API), and the engine is injected into the
    application unless the caller asks for `inject_engine=False`, which
    lets the application build — and own — its own engine.
    """
    engines: list[AsyncEngine] = []
    harnesses: list[ApiHarness] = []

    async def _build(
        records: tuple[MessageRecord, ...] = (),
        *,
        token: str | None = "secret-token",
        inject_engine: bool = True,
        name: str = "test.db",
    ) -> ApiHarness:
        if token is None:
            monkeypatch.delenv("FORESIGHT_API_TOKEN", raising=False)
        else:
            monkeypatch.setenv("FORESIGHT_API_TOKEN", token)
        db_path = tmp_path / name
        writer = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        engines.append(writer)
        await init_db(writer)
        if records:
            repo = Repository(writer)
            await repo.apply_batch(
                [WriteOp(OpKind.MESSAGE, record) for record in records]
            )
        config = api_config(db_path)
        if inject_engine:
            app = create_app(config, engine=writer)
            engine = writer
            owns_engine = False
        else:
            app = create_app(config)
            engine = app.state.engine
            engines.append(engine)
            owns_engine = True
        client = AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        )
        harness = ApiHarness(app, client, engine, owns_engine)
        harnesses.append(harness)
        return harness

    try:
        yield _build
    finally:
        for harness in harnesses:
            await harness.client.aclose()
        for engine in engines:
            await engine.dispose()


@pytest.fixture
async def api_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """HTTP client for the API.

    The engine and the request must live in the SAME event loop, so the
    client uses httpx's ASGI transport instead of starlette's TestClient
    (TestClient runs the app in its own loop, which breaks the async
    engine with "attached to a different loop").
    """
    monkeypatch.setenv("FORESIGHT_API_TOKEN", "secret-token")

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    await init_db(engine)
    repo = Repository(engine)
    records = [
        MessageRecord(
            message_id=index + 1,
            guild_id=10,
            channel_id=20,
            author_id=30,
            content=f"message number {index}",
            message_type=0,
            created_at=f"2026-11-1{index + 1}T10:00:00+00:00",
            captured_at="2026-11-11T10:00:00+00:00",
            char_count=16,
            word_count=3,
        )
        for index in range(4)
    ]
    await repo.apply_batch([WriteOp(OpKind.MESSAGE, record) for record in records])

    config = SimpleNamespace(
        api=SimpleNamespace(host="127.0.0.1", port=8000, metrics_token=""),
        database=SimpleNamespace(
            backend="sqlite",
            path=str(tmp_path / "test.db"),
            sqlalchemy_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
            readonly_sqlalchemy_url=(
                f"sqlite+aiosqlite:///file:{tmp_path / 'test.db'}?mode=ro&uri=true"
            ),
        ),
        media=SimpleNamespace(backend="local"),
    )
    app = create_app(config, engine=engine)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client
    await engine.dispose()


@pytest.fixture
def auth():
    return {"Authorization": "Bearer secret-token"}
