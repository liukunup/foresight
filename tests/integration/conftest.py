from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine

from foresight.api.app import create_app
from foresight.domain.models import MessageRecord, OpKind, WriteOp
from foresight.storage.db import init_db
from foresight.storage.repository import Repository


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
