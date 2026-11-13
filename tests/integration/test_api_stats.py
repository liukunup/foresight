"""Tests for the analytics endpoints.

The endpoints are exercised through the ASGI transport, so the request and
the database engine live in the same event loop.
"""

from __future__ import annotations

from foresight.domain.models import MessageRecord


def message(
    message_id: int,
    content: str = "hello",
    created_at: str = "2026-11-11T10:00:00+00:00",
    channel_id: int = 20,
    author_id: int = 30,
) -> MessageRecord:
    """A message record for the stats tests."""
    return MessageRecord(
        message_id=message_id,
        guild_id=10,
        channel_id=channel_id,
        author_id=author_id,
        content=content,
        message_type=0,
        created_at=created_at,
        captured_at="2026-11-11T10:00:00+00:00",
        char_count=len(content),
        word_count=len(content.split()),
    )


async def test_stats_overview(api_client, auth):
    response = await api_client.get("/api/stats/overview", headers=auth)
    assert response.status_code == 200
    body = response.json()
    assert body["total_messages"] == 4
    assert body["unique_authors"] == 1


async def test_stats_top_talkers(api_client, auth):
    response = await api_client.get("/api/stats/top-talkers", headers=auth)
    assert response.status_code == 200
    assert response.json()["items"][0]["author_id"] == 30


async def test_stats_activity(api_client, auth):
    response = await api_client.get("/api/stats/activity", headers=auth)
    assert response.status_code == 200
    assert len(response.json()["items"]) == 4


async def test_stats_topics(api_client, auth):
    response = await api_client.get("/api/stats/topics", headers=auth)
    assert response.status_code == 200
    assert "items" in response.json()


async def test_the_stats_endpoints_require_a_token(api_client):
    """The protection lives at the mount site, not on the routes."""
    for path in (
        "/api/stats/overview",
        "/api/stats/top-talkers",
        "/api/stats/activity",
        "/api/stats/topics",
    ):
        response = await api_client.get(path)
        assert response.status_code == 401, path


async def test_stats_overview_counts_the_whole_dump(api_client, auth):
    """The overview reports the totals of the filtered messages."""
    response = await api_client.get("/api/stats/overview", headers=auth)
    assert response.status_code == 200
    body = response.json()
    assert body["total_messages"] == 4
    assert body["unique_authors"] == 1
    assert body["deleted_messages"] == 0
    assert body["edited_messages"] == 0
    assert body["avg_chars"] == 16.0


async def test_stats_overview_filters_by_channel(api_client, auth):
    response = await api_client.get(
        "/api/stats/overview", headers=auth, params={"channel_id": 999}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total_messages"] == 0
    assert body["unique_authors"] == 0


async def test_stats_overview_filters_by_time(api_harness, auth):
    """`since` and `until` bound the messages the overview counts."""
    records = [
        message(1, created_at="2026-11-11T10:00:00+00:00"),
        message(2, created_at="2026-11-12T10:00:00+00:00"),
        message(3, created_at="2026-11-13T10:00:00+00:00"),
    ]
    harness = await api_harness(records)

    response = await harness.client.get(
        "/api/stats/overview",
        headers=auth,
        params={
            "since": "2026-11-12T00:00:00+00:00",
            "until": "2026-11-12T23:59:59+00:00",
        },
    )
    assert response.status_code == 200
    assert response.json()["total_messages"] == 1


async def test_stats_top_talkers_returns_the_envelope(api_client, auth):
    """The list endpoints answer with the pagination envelope."""
    response = await api_client.get("/api/stats/top-talkers", headers=auth)
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["offset"] == 0
    assert body["has_more"] is False
    assert body["items"][0]["message_count"] == 4


async def test_stats_top_talkers_orders_by_message_count(api_harness, auth):
    records = [
        message(1, author_id=30),
        message(2, author_id=30),
        message(3, author_id=31),
    ]
    harness = await api_harness(records)

    response = await harness.client.get("/api/stats/top-talkers", headers=auth)
    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["author_id"] for item in items] == [30, 31]
    assert items[0]["message_count"] == 2


async def test_stats_limit_is_capped(api_client, auth):
    """A page bigger than the maximum is clamped, not rejected."""
    response = await api_client.get(
        "/api/stats/top-talkers", headers=auth, params={"limit": 5000}
    )
    assert response.status_code == 200
    assert response.json()["limit"] == 500


async def test_stats_activity_buckets_by_hour(api_harness, auth):
    """The hour bucket splits a day into hours."""
    records = [
        message(1, created_at="2026-11-11T10:00:00+00:00"),
        message(2, created_at="2026-11-11T11:00:00+00:00"),
        message(3, created_at="2026-11-12T10:00:00+00:00"),
    ]
    harness = await api_harness(records)

    days = await harness.client.get(
        "/api/stats/activity", headers=auth, params={"bucket": "day"}
    )
    hours = await harness.client.get(
        "/api/stats/activity", headers=auth, params={"bucket": "hour"}
    )
    assert days.status_code == 200
    assert hours.status_code == 200
    assert len(days.json()["items"]) == 2
    assert len(hours.json()["items"]) == 3
    assert days.json()["items"][0] == {"bucket": "2026-11-11", "message_count": 2}
    assert hours.json()["items"][0] == {
        "bucket": "2026-11-11T10",
        "message_count": 1,
    }


async def test_stats_activity_rejects_an_unknown_bucket(api_client, auth):
    """An unknown bucket is an error, not a silent day bucket."""
    response = await api_client.get(
        "/api/stats/activity", headers=auth, params={"bucket": "week"}
    )
    assert response.status_code == 422


async def test_stats_topics_falls_back_to_frequent_words(api_client, auth):
    """Without an analysis the topics are the frequent words."""
    response = await api_client.get("/api/stats/topics", headers=auth)
    assert response.status_code == 200
    body = response.json()
    assert body["items"][0] == {"topic": "message", "count": 4}


async def test_stats_endpoints_are_empty_without_messages(api_harness, auth):
    """An empty database answers with zeros and empty lists."""
    harness = await api_harness()

    overview = await harness.client.get("/api/stats/overview", headers=auth)
    assert overview.status_code == 200
    assert overview.json()["total_messages"] == 0
    assert overview.json()["unique_authors"] == 0

    for path in (
        "/api/stats/top-talkers",
        "/api/stats/activity",
        "/api/stats/topics",
    ):
        response = await harness.client.get(path, headers=auth)
        assert response.status_code == 200, path
        body = response.json()
        assert body["items"] == [], path
        assert body["total"] == 0, path
