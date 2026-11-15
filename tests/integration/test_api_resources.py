"""Tests for the user, channel and reaction endpoints.

The endpoints are exercised through the ASGI transport, so the request and
the database engine live in the same event loop.
"""

from __future__ import annotations

from foresight.domain.models import (
    ChannelSnapshot,
    MessageRecord,
    OpKind,
    ReactionRecord,
    UserSnapshot,
    WriteOp,
)
from foresight.storage.repository import Repository


def message(
    message_id: int,
    *,
    channel_id: int = 20,
    author_id: int = 30,
    guild_id: int = 10,
    content: str = "hello",
    created_at: str = "2026-11-11T10:00:00+00:00",
) -> MessageRecord:
    """A message record for the resource tests."""
    return MessageRecord(
        message_id=message_id,
        guild_id=guild_id,
        channel_id=channel_id,
        author_id=author_id,
        content=content,
        message_type=0,
        created_at=created_at,
        captured_at="2026-11-11T10:00:00+00:00",
        char_count=len(content),
        word_count=len(content.split()),
    )


async def test_users_endpoint_lists_speakers(api_client, auth):
    response = await api_client.get("/api/users", headers=auth)
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["author_id"] == 30
    assert body["items"][0]["message_count"] == 4


async def test_user_detail(api_client, auth):
    response = await api_client.get("/api/users/30", headers=auth)
    assert response.status_code == 200
    assert response.json()["author_id"] == 30


async def test_user_detail_not_found(api_client, auth):
    response = await api_client.get("/api/users/999", headers=auth)
    assert response.status_code == 404


async def test_channels_endpoint(api_client, auth):
    response = await api_client.get("/api/channels", headers=auth)
    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["channel_id"] == 20


async def test_reactions_endpoint_is_empty(api_client, auth):
    response = await api_client.get("/api/reactions", headers=auth)
    assert response.status_code == 200
    assert response.json()["total"] == 0


async def test_the_resource_endpoints_require_a_token(api_client):
    """The protection lives at the mount site, not on the routes.

    The routers declare no dependency of their own, so the mount site is
    the only thing that keeps them closed.
    """
    for path in ("/api/users", "/api/users/30", "/api/channels", "/api/reactions"):
        response = await api_client.get(path)
        assert response.status_code == 401, path


async def test_users_endpoint_resolves_the_username(api_harness, auth):
    """The username comes from the users table, not from the messages.

    The speakers are counted over the messages table, so the name of a
    speaker has to be looked up in a second query.
    """
    harness = await api_harness([message(1)])
    repo = Repository(harness.engine)
    await repo.apply_batch(
        [
            WriteOp(
                OpKind.USER,
                UserSnapshot(
                    user_id=30,
                    username="ada",
                    global_name="Ada",
                    display_name="Ada L.",
                    discriminator="0",
                    avatar_url=None,
                    is_bot=False,
                    account_created_at=None,
                    guild_joined_at=None,
                    roles_json="[]",
                    updated_at="2026-11-11T10:00:00+00:00",
                ),
            )
        ]
    )

    response = await harness.client.get("/api/users", headers=auth)
    assert response.status_code == 200
    body = response.json()
    assert body["items"][0]["username"] == "ada"


async def test_users_endpoint_filters_by_channel(api_harness, auth):
    """Only the speakers of the requested channel are listed."""
    records = [
        message(1, channel_id=20, author_id=30),
        message(2, channel_id=20, author_id=30),
        message(3, channel_id=21, author_id=31),
    ]
    harness = await api_harness(records)

    response = await harness.client.get(
        "/api/users", headers=auth, params={"channel_id": 21}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["author_id"] == 31
    assert body["items"][0]["message_count"] == 1


async def test_users_endpoint_orders_by_message_count(api_harness, auth):
    """The busiest speaker comes first, the id breaks a tie."""
    records = [
        message(1, author_id=30),
        message(2, author_id=30),
        message(3, author_id=31),
        message(4, author_id=32),
        message(5, author_id=32),
    ]
    harness = await api_harness(records)

    response = await harness.client.get("/api/users", headers=auth)
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 3
    assert [item["author_id"] for item in body["items"]] == [30, 32, 31]


async def test_users_endpoint_limit_is_capped(api_harness, auth):
    """A page bigger than the maximum is clamped, not rejected."""
    harness = await api_harness([message(1)])

    response = await harness.client.get(
        "/api/users", headers=auth, params={"limit": 5000}
    )
    assert response.status_code == 200
    assert response.json()["limit"] == 500


async def test_users_endpoint_paginates_over_the_speakers(api_harness, auth):
    """The envelope pages over the speakers, not over the messages.

    A speaker that wrote four messages is one row of the list, so the
    total is the number of speakers and the offset moves from speaker to
    speaker.
    """
    records = [
        message(1, author_id=30),
        message(2, author_id=30),
        message(3, author_id=31),
        message(4, author_id=32),
    ]
    harness = await api_harness(records)

    first = await harness.client.get(
        "/api/users", headers=auth, params={"limit": 2, "offset": 0}
    )
    second = await harness.client.get(
        "/api/users", headers=auth, params={"limit": 2, "offset": 2}
    )
    assert first.status_code == 200
    assert second.status_code == 200
    first_body = first.json()
    second_body = second.json()
    assert first_body["total"] == 3
    assert first_body["limit"] == 2
    assert first_body["offset"] == 0
    assert first_body["has_more"] is True
    assert second_body["offset"] == 2
    assert second_body["has_more"] is False
    assert [item["author_id"] for item in first_body["items"]] == [30, 31]
    assert [item["author_id"] for item in second_body["items"]] == [32]


async def test_users_endpoint_is_empty_without_messages(api_harness, auth):
    """An empty database answers with zeros, not with an error."""
    harness = await api_harness()

    response = await harness.client.get("/api/users", headers=auth)
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 0
    assert body["items"] == []


async def test_user_detail_is_not_found_without_messages(api_harness, auth):
    """An author that never spoke is not a speaker."""
    harness = await api_harness([message(1, author_id=30)])

    response = await harness.client.get("/api/users/999", headers=auth)
    assert response.status_code == 404


async def test_channels_endpoint_resolves_the_name(api_harness, auth):
    """The name comes from the channels table."""
    harness = await api_harness([message(1, channel_id=20)])
    repo = Repository(harness.engine)
    await repo.apply_batch(
        [
            WriteOp(
                OpKind.CHANNEL,
                ChannelSnapshot(
                    channel_id=20,
                    guild_id=10,
                    name="general",
                    topic=None,
                    channel_type="text",
                    position=None,
                    captured_at="2026-11-11T10:00:00+00:00",
                ),
            )
        ]
    )

    response = await harness.client.get("/api/channels", headers=auth)
    assert response.status_code == 200
    body = response.json()
    assert body["items"][0]["name"] == "general"
    assert body["items"][0]["guild_id"] == 10


async def test_channels_endpoint_filters_by_guild(api_harness, auth):
    """Only the channels of the requested guild are listed."""
    records = [
        message(1, channel_id=20, guild_id=10),
        message(2, channel_id=21, guild_id=11),
    ]
    harness = await api_harness(records)

    response = await harness.client.get(
        "/api/channels", headers=auth, params={"guild_id": 11}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["channel_id"] == 21


async def test_channels_endpoint_reports_the_guild_of_the_messages(api_harness, auth):
    """The guild of a channel is the guild of its messages.

    The endpoint filters over the messages table, so the guild it reports
    has to come from the same table: a snapshot that disagrees with the
    messages must not make the response describe another guild than the
    one that was filtered.
    """
    harness = await api_harness([message(1, channel_id=20, guild_id=10)])
    repo = Repository(harness.engine)
    await repo.apply_batch(
        [
            WriteOp(
                OpKind.CHANNEL,
                ChannelSnapshot(
                    channel_id=20,
                    guild_id=999,
                    name="general",
                    topic=None,
                    channel_type="text",
                    position=None,
                    captured_at="2026-11-11T10:00:00+00:00",
                ),
            )
        ]
    )

    response = await harness.client.get("/api/channels", headers=auth)
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["guild_id"] == 10
    assert item["name"] == "general"


async def test_channels_endpoint_is_empty_without_messages(api_harness, auth):
    harness = await api_harness()

    response = await harness.client.get("/api/channels", headers=auth)
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 0
    assert body["items"] == []


async def test_reactions_endpoint_lists_the_reactions(api_harness, auth):
    """A reaction row is reported as it is stored."""
    harness = await api_harness([message(1)])
    repo = Repository(harness.engine)
    await repo.apply_batch(
        [
            WriteOp(
                OpKind.REACTION,
                ReactionRecord(
                    message_id=1,
                    user_id=30,
                    emoji="👍",
                    emoji_id=None,
                    is_custom=False,
                    added_at="2026-11-11T10:01:00+00:00",
                ),
            )
        ]
    )

    response = await harness.client.get("/api/reactions", headers=auth)
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["message_id"] == 1
    assert item["user_id"] == 30
    assert item["emoji"] == "👍"
    assert item["is_custom"] is False
    assert item["added_at"] == "2026-11-11T10:01:00+00:00"
    assert item["removed_at"] is None


async def test_reactions_endpoint_only_active_hides_removed_reactions(
    api_harness, auth
):
    """A removed reaction is still stored, but it no longer counts."""
    harness = await api_harness([message(1)])
    repo = Repository(harness.engine)
    await repo.apply_batch(
        [
            WriteOp(
                OpKind.REACTION,
                ReactionRecord(
                    message_id=1,
                    user_id=30,
                    emoji="👍",
                    emoji_id=None,
                    is_custom=False,
                    added_at="2026-11-11T10:01:00+00:00",
                ),
            ),
            WriteOp(
                OpKind.REACTION,
                ReactionRecord(
                    message_id=1,
                    user_id=31,
                    emoji="🎉",
                    emoji_id=None,
                    is_custom=False,
                    added_at="2026-11-11T10:02:00+00:00",
                    removed_at="2026-11-11T11:00:00+00:00",
                ),
            ),
        ]
    )

    every = await harness.client.get("/api/reactions", headers=auth)
    assert every.status_code == 200
    assert every.json()["total"] == 2

    active = await harness.client.get(
        "/api/reactions", headers=auth, params={"only_active": True}
    )
    assert active.status_code == 200
    body = active.json()
    assert body["total"] == 1
    assert body["items"][0]["emoji"] == "👍"


async def test_reactions_endpoint_filters_by_message(api_harness, auth):
    harness = await api_harness([message(1), message(2)])
    repo = Repository(harness.engine)
    await repo.apply_batch(
        [
            WriteOp(
                OpKind.REACTION,
                ReactionRecord(
                    message_id=message_id,
                    user_id=30,
                    emoji="👍",
                    emoji_id=None,
                    is_custom=False,
                    added_at="2026-11-11T10:01:00+00:00",
                ),
            )
            for message_id in (1, 2)
        ]
    )

    response = await harness.client.get(
        "/api/reactions", headers=auth, params={"message_id": 2}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["message_id"] == 2


async def test_reactions_endpoint_is_empty_without_reactions(api_harness, auth):
    harness = await api_harness([message(1)])

    response = await harness.client.get("/api/reactions", headers=auth)
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 0
    assert body["items"] == []
