"""Tests for the Prometheus endpoint.

The metrics are the ones an operator watches: how much was captured, how
fast the API answers, how much the database holds. The endpoint itself is
outside `/api`, so Prometheus can scrape it without a bearer token — unless
a metrics token is configured, which then guards it.
"""

from types import SimpleNamespace

from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine

from foresight.api.app import create_app
from foresight.storage.db import init_db


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


async def test_health_updates_the_database_gauges(api_client, auth):
    await api_client.get("/api/health", headers=auth)
    response = await api_client.get("/metrics")
    # The counts are known where they are read, so they are published
    # there — the scrape does not query the database on its own.
    assert "foresight_db_messages_total 4.0" in response.text
    assert "foresight_db_attachments_total 0.0" in response.text


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
