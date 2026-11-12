"""Tests for the Discord event handler.

The handler is the only place that decides what a Discord event means for
the database, so these tests pin the rules the rest of the service relies
on: only whitelisted channels are captured, the bot's own messages are
skipped, every event becomes the write operation the repository expects,
and a malformed event can never take the bot down.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from foresight.domain.models import OpKind
from foresight.events import EventHandler


class RecordingWriter:
    """A writer that keeps the operations instead of writing them."""

    def __init__(self):
        self.ops = []

    def submit(self, op):
        self.ops.append(op)
        return True

    def kinds(self):
        return [op.kind for op in self.ops]


def _config(channel_ids=(20,)):
    return SimpleNamespace(discord=SimpleNamespace(channel_ids=tuple(channel_ids)),
                           media=SimpleNamespace(backend="local"))


def _author(user_id=30):
    return SimpleNamespace(
        id=user_id,
        name="alice",
        global_name="Alice",
        display_name="alice",
        discriminator="0",
        bot=False,
        avatar=None,
        created_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        joined_at=None,
        roles=[],
    )


def _message(channel_id=20, message_id=100, content="hello"):
    return SimpleNamespace(
        id=message_id,
        content=content,
        type=0,
        created_at=datetime(2026, 11, 11, tzinfo=timezone.utc),
        edited_at=None,
        author=_author(),
        channel=SimpleNamespace(
            id=channel_id,
            name="general",
            topic=None,
            type="text",
            position=1,
            guild=SimpleNamespace(id=10, name="Foresight", member_count=1),
        ),
        guild=SimpleNamespace(id=10, name="Foresight", member_count=1),
        reference=None,
        mentions=[],
        attachments=[],
        embeds=[],
    )


def _raw_payload(channel_id=20, message_id=100, user_id=31, emoji="🔥"):
    """A payload shaped like the raw events discord.py delivers."""
    return SimpleNamespace(
        channel_id=channel_id,
        message_id=message_id,
        user_id=user_id,
        emoji=SimpleNamespace(name=emoji, id=None, is_custom_emoji=lambda: False),
        data={"content": "edited", "edited_timestamp": "2026-11-11T01:00:00+00:00"},
        cached_message=None,
    )


class ExplodingAuthorMessage:
    """A message whose ``author`` raises as soon as the handler reads it."""

    guild = None
    channel = SimpleNamespace(id=20)

    @property
    def author(self):
        raise AttributeError("malformed message: author is not readable")


class ExplodingChannelMessage:
    """A message whose ``channel`` raises as soon as the handler reads it."""

    guild = None

    @property
    def channel(self):
        raise AttributeError("malformed message: channel is not readable")


def test_message_outside_whitelist_is_ignored():
    writer = RecordingWriter()
    handler = EventHandler(_config(), writer)
    handler.handle_message(_message(channel_id=999))
    assert writer.ops == []


def test_message_inside_whitelist_is_captured():
    writer = RecordingWriter()
    handler = EventHandler(_config(), writer)
    handler.handle_message(_message())

    assert writer.kinds() == [OpKind.MESSAGE, OpKind.USER, OpKind.CHANNEL, OpKind.GUILD]

    record = writer.ops[0].payload
    assert record.content == "hello"
    assert record.channel_id == 20
    assert record.author_id == 30


def test_own_messages_are_skipped():
    writer = RecordingWriter()
    handler = EventHandler(_config(), writer, bot_user_id=30)
    handler.handle_message(_message())
    assert writer.ops == []


@pytest.mark.parametrize(
    "message",
    [ExplodingAuthorMessage(), ExplodingChannelMessage()],
    ids=["author", "channel"],
)
def test_malformed_message_does_not_escape_the_handler(message, caplog):
    """A malformed message is logged, never raised and never written."""
    writer = RecordingWriter()
    handler = EventHandler(_config(), writer)
    caplog.set_level(logging.ERROR)

    handler.handle_message(message)

    assert writer.ops == []
    assert "failed to handle message" in caplog.text


def test_raw_edit_queues_edit_op():
    writer = RecordingWriter()
    handler = EventHandler(_config(), writer)
    payload = SimpleNamespace(
        channel_id=20,
        message_id=100,
        data={"content": "edited", "edited_timestamp": "2026-11-11T01:00:00+00:00"},
    )
    handler.handle_raw_edit(payload)
    op = writer.ops[0]
    assert op.kind == OpKind.MESSAGE_EDIT
    assert op.payload.content == "edited"


def test_raw_edit_without_content_is_skipped():
    """An embed-only update carries no body, so nothing is written."""
    writer = RecordingWriter()
    handler = EventHandler(_config(), writer)
    payload = SimpleNamespace(
        channel_id=20,
        message_id=100,
        data={"edited_timestamp": "2026-11-11T01:00:00+00:00"},
    )
    handler.handle_raw_edit(payload)
    assert writer.ops == []


def test_raw_edit_outside_whitelist_is_ignored():
    writer = RecordingWriter()
    handler = EventHandler(_config(), writer)
    payload = SimpleNamespace(
        channel_id=999,
        message_id=100,
        data={"content": "edited", "edited_timestamp": "2026-11-11T01:00:00+00:00"},
    )
    handler.handle_raw_edit(payload)
    assert writer.ops == []


def test_raw_delete_queues_delete_op():
    writer = RecordingWriter()
    handler = EventHandler(_config(), writer)
    payload = SimpleNamespace(channel_id=20, message_id=100, cached_message=None)
    handler.handle_raw_delete(payload)
    assert writer.ops[0].kind == OpKind.MESSAGE_DELETE


@pytest.mark.parametrize(
    "handler_name",
    ["handle_raw_delete", "handle_raw_reaction_add", "handle_raw_reaction_remove"],
)
def test_raw_handlers_outside_whitelist_are_ignored(handler_name):
    writer = RecordingWriter()
    handler = EventHandler(_config(), writer)
    payload = _raw_payload(channel_id=999)
    getattr(handler, handler_name)(payload)
    assert writer.ops == []


def test_reaction_add_and_remove():
    writer = RecordingWriter()
    handler = EventHandler(_config(), writer)
    payload = _raw_payload()

    handler.handle_raw_reaction_add(payload)
    handler.handle_raw_reaction_remove(payload)

    assert writer.kinds() == [OpKind.REACTION, OpKind.REACTION_REMOVE]

    added, removed = writer.ops
    assert added.payload.emoji == "🔥"
    assert added.payload.removed_at is None
    assert removed.payload.emoji == "🔥"
    assert removed.payload.removed_at is not None
