from datetime import datetime, timezone
from types import SimpleNamespace

from foresight.events import EventHandler


class RecordingWriter:
    def __init__(self):
        self.ops = []

    def submit(self, op):
        self.ops.append(op)
        return True


def _config():
    return SimpleNamespace(
        discord=SimpleNamespace(channel_ids=(20,)),
        media=SimpleNamespace(backend="local"),
    )


def _message(content="hello"):
    return SimpleNamespace(
        id=100,
        content=content,
        type=0,
        created_at=datetime(2026, 11, 11, tzinfo=timezone.utc),
        edited_at=None,
        author=SimpleNamespace(
            id=30,
            name="alice",
            global_name="Alice",
            display_name="alice",
            discriminator="0",
            bot=False,
            avatar=None,
            created_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
            joined_at=None,
            roles=[],
        ),
        channel=SimpleNamespace(
            id=20,
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


def test_message_is_handed_to_the_analysis_sink():
    seen = []
    writer = RecordingWriter()
    handler = EventHandler(_config(), writer, analysis_sink=lambda mid, content: seen.append((mid, content)))
    handler.handle_message(_message("please add export"))
    assert seen == [(100, "please add export")]


def test_message_without_analysis_sink_still_works():
    writer = RecordingWriter()
    handler = EventHandler(_config(), writer)
    handler.handle_message(_message())
    assert writer.ops


def test_a_message_without_content_is_analysed():
    """The worker decides what is worth analysing.

    The handler hands over every message that reached the writer: a
    message that carries no text is skipped by the worker, which marks
    it as skipped — a message that stays `pending` is queued again on
    every restart.
    """
    seen = []
    writer = RecordingWriter()
    handler = EventHandler(
        _config(), writer, analysis_sink=lambda mid, content: seen.append((mid, content))
    )
    handler.handle_message(_message("   "))
    assert seen == [(100, "   ")]


def test_a_message_the_writer_refuses_is_not_analysed():
    """A message that was not captured has no row to be analysed in."""

    class RefusingWriter:
        def submit(self, op):
            return False

    seen = []
    handler = EventHandler(
        _config(),
        RefusingWriter(),
        analysis_sink=lambda mid, content: seen.append((mid, content)),
    )
    handler.handle_message(_message("please add export"))
    assert seen == []
