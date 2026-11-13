"""Tests for the read-only HTTP API.

The API is exercised through the ASGI transport, so the request and the
database engine live in the same event loop.
"""

from types import SimpleNamespace

from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine

from foresight.api.app import create_app
from foresight.domain.models import MessageAnalysis, MessageRecord, OpKind, WriteOp
from foresight.storage.db import init_db
from foresight.storage.repository import Repository


async def test_health_requires_token(api_client):
    response = await api_client.get("/api/health")
    assert response.status_code == 401


async def test_health_returns_counts(api_client, auth):
    response = await api_client.get("/api/health", headers=auth)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["total_messages"] == 4


async def test_messages_are_paginated(api_client, auth):
    response = await api_client.get("/api/messages", headers=auth, params={"limit": 2})
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 4
    assert body["limit"] == 2
    assert body["has_more"] is True
    assert len(body["items"]) == 2


async def test_messages_limit_is_capped(api_client, auth):
    response = await api_client.get("/api/messages", headers=auth, params={"limit": 5000})
    assert response.status_code == 200
    assert response.json()["limit"] == 500


async def test_messages_search_is_case_insensitive(api_client, auth):
    response = await api_client.get(
        "/api/messages", headers=auth, params={"q": "MESSAGE NUMBER 1"}
    )
    assert response.status_code == 200
    assert response.json()["total"] == 1


async def test_messages_filter_by_channel(api_client, auth):
    response = await api_client.get(
        "/api/messages", headers=auth, params={"channel_id": 999}
    )
    assert response.json()["total"] == 0


async def test_message_detail(api_client, auth):
    response = await api_client.get("/api/messages/2", headers=auth)
    assert response.status_code == 200
    body = response.json()
    assert body["message_id"] == 2
    assert body["analysis"] is None


async def test_message_detail_not_found(api_client, auth):
    response = await api_client.get("/api/messages/9999", headers=auth)
    assert response.status_code == 404


async def test_message_detail_carries_the_analysis(tmp_path, monkeypatch):
    """The detail endpoint joins the analysis of the message.

    The analysis lives in its own table, so a message that was analyzed
    must report the analysis next to its content.
    """
    monkeypatch.setenv("FORESIGHT_API_TOKEN", "secret-token")
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'analysis.db'}")
    await init_db(engine)
    repo = Repository(engine)
    await repo.apply_batch(
        [
            WriteOp(
                OpKind.MESSAGE,
                MessageRecord(
                    message_id=1,
                    guild_id=10,
                    channel_id=20,
                    author_id=30,
                    content="hello",
                    message_type=0,
                    created_at="2026-11-11T10:00:00+00:00",
                    captured_at="2026-11-11T10:00:00+00:00",
                    char_count=5,
                    word_count=1,
                ),
            ),
            WriteOp(
                OpKind.ANALYSIS,
                MessageAnalysis(
                    message_id=1,
                    translated_text="你好",
                    sentiment="positive",
                    sentiment_score=0.9,
                    issues_json='["a greeting"]',
                    model="gpt-4o-mini",
                    prompt_version="v1",
                    latency_ms=120,
                    error=None,
                    analyzed_at="2026-11-11T10:05:00+00:00",
                ),
            ),
        ]
    )
    config = SimpleNamespace(
        api=SimpleNamespace(host="127.0.0.1", port=8000, metrics_token=""),
        database=SimpleNamespace(
            backend="sqlite",
            path=str(tmp_path / "analysis.db"),
            sqlalchemy_url=f"sqlite+aiosqlite:///{tmp_path / 'analysis.db'}",
            readonly_sqlalchemy_url=(
                f"sqlite+aiosqlite:///file:{tmp_path / 'analysis.db'}?mode=ro&uri=true"
            ),
        ),
        media=SimpleNamespace(backend="local"),
    )
    app = create_app(config, engine=engine)
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            response = await client.get(
                "/api/messages/1",
                headers={"Authorization": "Bearer secret-token"},
            )
    finally:
        await engine.dispose()
    assert response.status_code == 200
    body = response.json()
    assert body["message_id"] == 1
    assert body["analysis"]["translated_text"] == "你好"
    assert body["analysis"]["sentiment"] == "positive"
    assert body["analysis"]["sentiment_score"] == 0.9
