"""Tests for the Prometheus endpoint.

The metrics are the ones an operator watches: how much was captured, how
fast the API answers, how much the database holds. The endpoint itself is
outside `/api`, so Prometheus can scrape it without a bearer token — unless
a metrics token is configured, which then guards it.
"""

from datetime import datetime, timezone
from types import SimpleNamespace

from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine

from foresight.api.app import create_app
from foresight.domain.models import MessageRecord, OpKind, WriteOp
from foresight.events import EventHandler
from foresight.storage.db import init_db
from foresight.storage.repository import Repository
from foresight.writer import Writer


def _stored_message(message_id: int) -> MessageRecord:
    """A message record for the database of a test."""
    return MessageRecord(
        message_id=message_id,
        guild_id=10,
        channel_id=20,
        author_id=30,
        content=f"message number {message_id}",
        message_type=0,
        created_at="2026-11-11T10:00:00+00:00",
        captured_at="2026-11-11T10:00:00+00:00",
    )


async def test_metrics_endpoint_exposes_foresight_metrics(api_client):
    response = await api_client.get("/metrics")
    assert response.status_code == 200
    assert "foresight_api_requests_total" in response.text


async def test_metrics_records_api_calls(api_client, auth):
    await api_client.get("/api/health", headers=auth)
    response = await api_client.get("/metrics")
    assert (
        'foresight_api_requests_total{method="GET",path="/api/health",status="200"}'
        in response.text
    )


async def test_metrics_records_refused_requests(api_client):
    # The counter is raised by the middleware, which sits outside the
    # routers: a request that is refused is a request that was made.
    await api_client.get("/api/health")
    response = await api_client.get("/metrics")
    assert (
        'foresight_api_requests_total{method="GET",path="/api/health",status="401"}'
        in response.text
    )


async def test_metrics_label_a_path_with_the_route_and_not_with_the_id(
    api_client, auth
):
    # The label of a route with a parameter is the template: one time
    # series per message id would melt Prometheus.
    await api_client.get("/api/messages/1", headers=auth)
    response = await api_client.get("/metrics")
    assert 'path="/api/messages/{message_id}"' in response.text
    assert 'path="/api/messages/1"' not in response.text


async def test_metrics_label_an_unmatched_path_without_its_value(api_client):
    # A path that matches no route carries no bound, so it is not a label.
    await api_client.get("/api/nothing-here")
    response = await api_client.get("/metrics")
    assert 'path="unmatched"' in response.text
    assert 'path="/api/nothing-here"' not in response.text


async def test_metrics_records_a_request_that_failed(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'm.db'}")
    await init_db(engine)
    config = SimpleNamespace(
        api=SimpleNamespace(host="127.0.0.1", port=8000, metrics_token=""),
        database=SimpleNamespace(
            backend="sqlite",
            path=str(tmp_path / "m.db"),
            sqlalchemy_url=f"sqlite+aiosqlite:///{tmp_path / 'm.db'}",
            readonly_sqlalchemy_url=(
                f"sqlite+aiosqlite:///file:{tmp_path / 'm.db'}?mode=ro&uri=true"
            ),
        ),
        media=SimpleNamespace(backend="local"),
    )
    app = create_app(config, engine=engine)

    @app.get("/api/boom")
    async def boom() -> None:
        raise RuntimeError("the endpoint failed")

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        failed = await client.get("/api/boom")
        response = await client.get("/metrics")
    assert failed.status_code == 500
    # A request that failed is a request that was made.
    assert (
        'foresight_api_requests_total{method="GET",path="/api/boom",status="500"}'
        in response.text
    )
    await engine.dispose()


async def test_the_database_gauges_show_the_counts_of_the_dump(api_client, auth):
    await api_client.get("/api/health", headers=auth)
    response = await api_client.get("/metrics")
    # The scrape publishes the counts of the dump: the health call in
    # between does not matter, the gauge is true either way.
    assert "foresight_db_messages_total 4.0" in response.text
    assert "foresight_db_attachments_total 0.0" in response.text


async def test_a_scrape_publishes_the_database_gauges_without_a_health_call(
    tmp_path,
):
    """The gauges must be true without a health call in between.

    The health endpoint sits behind the bearer token, so a probe that has
    no token never reaches it — the gauges would stay at zero for ever.
    """
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'm.db'}")
    await init_db(engine)
    repo = Repository(engine)
    await repo.apply_batch(
        [
            WriteOp(OpKind.MESSAGE, _stored_message(index + 1))
            for index in range(3)
        ]
    )
    config = SimpleNamespace(
        api=SimpleNamespace(host="127.0.0.1", port=8000, metrics_token=""),
        database=SimpleNamespace(
            backend="sqlite",
            path=str(tmp_path / "m.db"),
            sqlalchemy_url=f"sqlite+aiosqlite:///{tmp_path / 'm.db'}",
            readonly_sqlalchemy_url=(
                f"sqlite+aiosqlite:///file:{tmp_path / 'm.db'}?mode=ro&uri=true"
            ),
        ),
        media=SimpleNamespace(backend="local"),
    )
    app = create_app(config, engine=engine)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/metrics")
    assert response.status_code == 200
    # The database holds three messages, and the gauge says so.
    assert "foresight_db_messages_total 3.0" in response.text
    assert "foresight_db_attachments_total 0.0" in response.text
    await engine.dispose()


def _discord_message(channel_id: int = 20, guild_id: int = 10, message_id: int = 100):
    """A message shaped like the one discord.py delivers."""
    author = SimpleNamespace(
        id=30,
        name="alice",
        global_name="Alice",
        display_name="alice",
        discriminator="0",
        bot=False,
        avatar=None,
        created_at=datetime(2026, 11, 11, tzinfo=timezone.utc),
        joined_at=None,
        roles=[],
    )
    return SimpleNamespace(
        id=message_id,
        content="hello",
        type=0,
        created_at=datetime(2026, 11, 11, tzinfo=timezone.utc),
        edited_at=None,
        author=author,
        channel=SimpleNamespace(
            id=channel_id,
            name="general",
            topic=None,
            type="text",
            position=1,
            guild=None,
        ),
        guild=SimpleNamespace(id=guild_id, name="Foresight", member_count=1),
        reference=None,
        mentions=[],
        attachments=[],
        embeds=[],
    )


def _raw_payload(channel_id: int = 20, message_id: int = 100):
    """A payload shaped like the raw events discord.py delivers."""
    return SimpleNamespace(
        channel_id=channel_id,
        message_id=message_id,
        user_id=31,
        emoji=SimpleNamespace(name="🔥", id=None, is_custom_emoji=lambda: False),
        data={"content": "edited", "edited_timestamp": "2026-11-11T01:00:00+00:00"},
        cached_message=None,
    )


class _RecordingRepository:
    """A repository that accepts everything and writes nothing."""

    async def apply_batch(self, ops):
        return None


async def test_the_collector_metrics_are_published(api_client):
    """The metrics of the collector must show up in the exposition.

    The names are the ones the documentation promises, so a rename of any
    of them has to fail this test.
    """
    config = SimpleNamespace(
        discord=SimpleNamespace(channel_ids=(20,)),
        media=SimpleNamespace(backend="local"),
    )
    writer = Writer(_RecordingRepository(), batch_size=10, flush_interval_ms=10)
    await writer.start()
    handler = EventHandler(config, writer)

    handler.handle_message(_discord_message())
    handler.handle_raw_edit(_raw_payload())
    handler.handle_raw_delete(_raw_payload())
    handler.handle_raw_reaction_add(_raw_payload())
    writer.submit(WriteOp(OpKind.MESSAGE, _stored_message(101)))
    await writer.stop()

    response = await api_client.get("/metrics")
    assert response.status_code == 200
    # The names are the ones the documentation promises, so a rename of
    # any of them has to fail this test.
    for name in (
        "foresight_messages_captured_total",
        "foresight_messages_deleted_total",
        "foresight_messages_edited_total",
        "foresight_reactions_total",
        "foresight_writer_queue_depth",
        "foresight_writer_batches_total",
        "foresight_writer_batch_size",
    ):
        assert name in response.text
    # A counter without a child publishes no series at all, so the series
    # themselves are checked as well: the events really reached the
    # metrics.
    for series in (
        'foresight_messages_captured_total{channel_id="20",guild_id="10"}',
        'foresight_messages_deleted_total{channel_id="20"}',
        'foresight_messages_edited_total{channel_id="20"}',
        'foresight_reactions_total{channel_id="20"}',
        'foresight_writer_batches_total{status="ok"}',
    ):
        assert series in response.text


async def test_metrics_token_is_enforced_when_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("FORESIGHT_API_TOKEN", "secret-token")
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'm.db'}")
    await init_db(engine)
    config = SimpleNamespace(
        api=SimpleNamespace(host="127.0.0.1", port=8000, metrics_token="metrics-secret"),
        database=SimpleNamespace(
            backend="sqlite",
            path=str(tmp_path / "m.db"),
            sqlalchemy_url=f"sqlite+aiosqlite:///{tmp_path / 'm.db'}",
            readonly_sqlalchemy_url=(
                f"sqlite+aiosqlite:///file:{tmp_path / 'm.db'}?mode=ro&uri=true"
            ),
        ),
        media=SimpleNamespace(backend="local"),
    )
    app = create_app(config, engine=engine)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        denied = await client.get("/metrics")
        allowed = await client.get(
            "/metrics", headers={"X-Metrics-Token": "metrics-secret"}
        )
    assert denied.status_code == 401
    assert allowed.status_code == 200
    await engine.dispose()
