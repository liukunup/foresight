"""Tests for the health endpoint.

The health endpoint answers the question a probe has: is the service
alive, how much did it dump, when was the last row written and how big
is the database.

The size of the database is only a number for SQLite, the one backend
that stores the dump in a file. PostgreSQL and MySQL spread the dump
over the files of the server, so the endpoint must not report the size
of something else — the name of the database is not a path.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from foresight.api.app import create_app
from foresight.domain.models import MessageRecord, OpKind, WriteOp
from foresight.storage.db import init_db
from foresight.storage.repository import Repository

from .conftest import api_config


def _message(message_id: int = 1) -> MessageRecord:
    """A message record for the database of a test."""
    return MessageRecord(
        message_id=message_id,
        guild_id=10,
        channel_id=20,
        author_id=30,
        content="hello",
        message_type=0,
        created_at="2026-11-11T10:00:00+00:00",
        captured_at="2026-11-11T10:00:00+00:00",
    )


class _ServerEngine:
    """An engine that reads a SQLite database but names a server one.

    The endpoint looks at the URL of the engine to find the file of the
    database, and the URL of a PostgreSQL or MySQL database names a
    database on a server. The test must not need a server: the wrapper
    answers with the SQLite engine behind it and carries the URL of the
    database on the server.
    """

    def __init__(self, engine: AsyncEngine, url: str) -> None:
        self._engine = engine
        self.url = make_url(url)

    def connect(self):
        """Open a connection to the database behind the wrapper."""
        return self._engine.connect()


async def test_the_health_endpoint_reports_the_counts_of_the_dump(api_client, auth):
    response = await api_client.get("/api/health", headers=auth)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["total_messages"] == 4
    assert body["total_attachments"] == 0


async def test_the_health_endpoint_reports_the_size_of_a_sqlite_database(
    api_harness, auth
):
    """A SQLite database is a file, and a file has a size."""
    harness = await api_harness([_message()], name="size.db")
    response = await harness.client.get("/api/health", headers=auth)
    assert response.status_code == 200
    body = response.json()
    assert body["total_messages"] == 1
    # The file of the database holds the schema and the row, so it is
    # not empty.
    assert body["db_size_mb"] > 0


async def test_the_health_endpoint_reports_the_size_of_a_read_only_database(
    api_harness, auth
):
    """The read-only connection opens the database through a `file:` URI.

    The prefix of the URI must be stripped before the file is measured,
    and the path of the file must be unescaped: the size of a database
    that is not found is zero, and a zero would be a lie.
    """
    harness = await api_harness([_message()], inject_engine=False, name="ro.db")
    response = await harness.client.get("/api/health", headers=auth)
    assert response.status_code == 200
    body = response.json()
    assert body["total_messages"] == 1
    assert body["db_size_mb"] > 0


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+asyncpg://foresight:foresight@localhost:5432/foresight",
        "mysql+asyncmy://foresight:foresight@localhost:3306/foresight",
    ],
)
async def test_the_health_endpoint_reports_no_size_for_a_server_database(
    tmp_path, monkeypatch, url
):
    """PostgreSQL and MySQL have no file to measure.

    The name of the database on the server is not a path: a file that
    happens to carry that name must not be reported as the size of the
    dump, and the endpoint must still answer.
    """
    db_path = tmp_path / "m.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    await init_db(engine)
    repo = Repository(engine)
    await repo.apply_batch([WriteOp(OpKind.MESSAGE, _message())])
    # A file that carries the name of the database of the URL. The
    # endpoint must not measure it.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "foresight").write_bytes(b"x" * 4096)

    app = create_app(api_config(db_path), engine=_ServerEngine(engine, url))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["total_messages"] == 1
    assert body["db_size_mb"] == 0.0
    await engine.dispose()


async def test_the_liveness_route_answers_without_a_token(api_client):
    """The probe of the container carries no token.

    The liveness route sits outside `/api`, so the bearer token of the
    API does not apply to it: a probe has no token, and it must not need
    one.
    """
    response = await api_client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_the_liveness_route_says_nothing_about_the_dump(api_client):
    """A liveness probe is a probe and not a question about the archive.

    The route answers with the state of the service and with nothing
    else: no counts, no size, no timestamp. A stranger that finds the
    port learns nothing about the dump.
    """
    response = await api_client.get("/healthz")
    body = response.json()
    assert set(body) == {"status"}
    assert "total_messages" not in body
    assert "db_size_mb" not in body


async def test_the_liveness_route_answers_with_a_metrics_token(api_harness):
    """The token of the metrics does not guard the liveness route.

    The metrics token is a separate thing: a deployment that guards
    `/metrics` must still be able to probe the service.
    """
    harness = await api_harness([], token="secret-token")
    harness.app.state.config.api.metrics_token = "metrics-secret"
    response = await harness.client.get("/healthz")
    assert response.status_code == 200
