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

from .conftest import api_message


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


async def test_messages_ordering_is_stable_for_equal_timestamps(api_harness, auth):
    """The message id breaks the tie between two equal timestamps.

    Without the tie-breaker a page could show the same row twice or skip
    one, so the two pages together must be exactly the seeded rows. The
    messages are seeded out of order on purpose: the row order of the
    database must not decide the order of the pages.
    """
    records = [
        api_message(2, "second", "2026-11-11T10:00:00+00:00"),
        api_message(1, "first", "2026-11-11T10:00:00+00:00"),
        api_message(4, "fourth", "2026-11-12T10:00:00+00:00"),
        api_message(3, "third", "2026-11-12T10:00:00+00:00"),
    ]
    harness = await api_harness(records)

    first = await harness.client.get(
        "/api/messages", headers=auth, params={"limit": 2, "offset": 0}
    )
    second = await harness.client.get(
        "/api/messages", headers=auth, params={"limit": 2, "offset": 2}
    )
    assert first.status_code == 200
    assert second.status_code == 200
    first_ids = [item["message_id"] for item in first.json()["items"]]
    second_ids = [item["message_id"] for item in second.json()["items"]]

    assert set(first_ids).isdisjoint(second_ids)
    assert sorted(first_ids + second_ids) == [1, 2, 3, 4]
    # Both sort keys run in the requested direction.
    assert first_ids + second_ids == [4, 3, 2, 1]


async def test_messages_ordering_is_stable_for_equal_timestamps_asc(api_harness, auth):
    """The tie is broken in the requested direction."""
    records = [
        api_message(2, "second", "2026-11-11T10:00:00+00:00"),
        api_message(1, "first", "2026-11-11T10:00:00+00:00"),
        api_message(4, "fourth", "2026-11-12T10:00:00+00:00"),
        api_message(3, "third", "2026-11-12T10:00:00+00:00"),
    ]
    harness = await api_harness(records)

    first = await harness.client.get(
        "/api/messages", headers=auth, params={"limit": 2, "offset": 0, "order": "asc"}
    )
    second = await harness.client.get(
        "/api/messages", headers=auth, params={"limit": 2, "offset": 2, "order": "asc"}
    )
    first_ids = [item["message_id"] for item in first.json()["items"]]
    second_ids = [item["message_id"] for item in second.json()["items"]]

    assert set(first_ids).isdisjoint(second_ids)
    assert sorted(first_ids + second_ids) == [1, 2, 3, 4]
    assert first_ids + second_ids == [1, 2, 3, 4]


async def test_messages_search_treats_the_wildcards_literally(api_harness, auth):
    """`q` is a substring, not a pattern.

    A search for `%` looks for a percent sign and a search for `a_b`
    looks for an underscore, not for any character.
    """
    records = [
        api_message(1, "a_b"),
        api_message(2, "axb"),
        api_message(3, "a%b"),
        api_message(4, "plain"),
        api_message(5, "back\\slash"),
    ]
    harness = await api_harness(records)

    underscore = await harness.client.get(
        "/api/messages", headers=auth, params={"q": "a_b"}
    )
    assert underscore.status_code == 200
    assert underscore.json()["total"] == 1
    assert [item["message_id"] for item in underscore.json()["items"]] == [1]

    percent = await harness.client.get("/api/messages", headers=auth, params={"q": "%"})
    assert percent.status_code == 200
    assert percent.json()["total"] == 1
    assert [item["message_id"] for item in percent.json()["items"]] == [3]

    # The backslash is escaped as well, so it still matches itself.
    backslash = await harness.client.get(
        "/api/messages", headers=auth, params={"q": "back\\slash"}
    )
    assert backslash.status_code == 200
    assert backslash.json()["total"] == 1
    assert [item["message_id"] for item in backslash.json()["items"]] == [5]


async def test_messages_search_matches_a_non_ascii_needle(api_harness, auth):
    """The needle is lowercased by the database, not by Python.

    SQLite's `lower()` only folds ASCII while Python's `lower()` folds
    every script: when the needle is lowered in Python and the column in
    SQL, the two sides disagree and the match is lost.
    """
    records = [api_message(1, "ÉCOLE 42")]
    harness = await api_harness(records)

    response = await harness.client.get(
        "/api/messages", headers=auth, params={"q": "École 42"}
    )
    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["message_id"] == 1
