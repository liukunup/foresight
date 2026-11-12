"""Tests for the Discord event handler.

The handler is the only place that decides what a Discord event means for
the database, so these tests pin the rules the rest of the service relies
on: only whitelisted channels are captured, the bot's own messages are
skipped, and every event becomes the write operation the repository
expects.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

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


def test_message_outside_whitelist_is_ignored():
    writer = RecordingWriter()
    handler = EventHandler(_config(), writer)
    handler.handle_message(_message(channel_id=999))
    assert writer.ops == []


def test_message_inside_whitelist_is_captured():
    writer = RecordingWriter()
    handler = EventHandler(_config(), writer)
    handler.handle_message(_message())
    kinds = writer.kinds()
    assert OpKind.MESSAGE in kinds
    assert OpKind.USER in kinds
    assert OpKind.CHANNEL in kinds
    assert OpKind.GUILD in kinds


def test_own_messages_are_skipped():
    writer = RecordingWriter()
    handler = EventHandler(_config(), writer, bot_user_id=30)
    handler.handle_message(_message())
    assert writer.ops == []


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


def test_reaction_add_and_remove():
    writer = RecordingWriter()
    handler = EventHandler(_config(), writer)
    payload = SimpleNamespace(
        message_id=100,
        user_id=31,
        channel_id=20,
        emoji=SimpleNamespace(name="🔥", id=None, is_custom_emoji=lambda: False),
    )
    handler.handle_raw_reaction_add(payload)
    handler.handle_raw_reaction_remove(payload)
    assert writer.kinds() == [OpKind.REACTION, OpKind.REACTION_REMOVE]
