"""Tests for the historical backfill.

The backfill pulls the history of a channel through the Discord API and
feeds it into the very same write path the live collector uses. The two
properties that matter are pinned here: the history is written with the
`insert_only` strategy, so that it never overwrites what the collector
already wrote, and the backfill does not run the AI.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from foresight.backfill import BackfillRunner, parse_window
from foresight.domain.models import OpKind
from foresight.metrics import REGISTRY


def test_parse_window_defaults_to_seven_days():
    start, end = parse_window(None, None)
    assert (end - start).days == 7
    assert end > start


def test_parse_window_uses_days():
    start, end = parse_window(3, None)
    assert (end - start).days == 3


def test_parse_window_uses_since():
    start, end = parse_window(None, "2026-11-01")
    assert start == datetime(2026, 11, 1, tzinfo=timezone.utc)
    assert end > start


def test_parse_window_rejects_both():
    with pytest.raises(ValueError):
        parse_window(3, "2026-11-01")


def test_parse_window_rejects_neither_when_explicit():
    start, end = parse_window(0, None)
    assert start <= end


def _config() -> SimpleNamespace:
    return SimpleNamespace(
        discord=SimpleNamespace(channel_ids=(20,)),
        media=SimpleNamespace(backend="local"),
    )


def _message(
    message_id: int = 1, created_at: datetime | None = None
) -> SimpleNamespace:
    return SimpleNamespace(
        id=message_id,
        content=f"message {message_id}",
        type=0,
        created_at=created_at or datetime(2026, 11, 10, tzinfo=timezone.utc),
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


async def _aiter(items: list[Any]):
    """An async iterator over the given items."""
    for item in items:
        yield item


class _FakeChannel:
    """A channel whose history is a fixed list of messages."""

    def __init__(self, messages: list[SimpleNamespace]) -> None:
        # Discord hands the newest messages of a page first.
        self._messages = sorted(messages, key=lambda message: message.id, reverse=True)
        self.calls: list[dict[str, Any]] = []

    def history(self, **kwargs: Any):
        self.calls.append(kwargs)
        limit = kwargs.get("limit", 100)
        before = kwargs.get("before")
        selected = [
            message
            for message in self._messages
            if before is None or message.id < before
        ]
        return _aiter(selected[:limit])


class _FakeClient:
    """A Discord client that never talks to Discord."""

    def __init__(self, channels: dict[int, _FakeChannel]) -> None:
        self._channels = channels
        self.token: str | None = None
        self.closed = False

    async def start(self, token: str) -> None:
        self.token = token

    async def wait_until_ready(self) -> None:
        return None

    async def close(self) -> None:
        self.closed = True

    def get_channel(self, channel_id: int) -> _FakeChannel | None:
        return self._channels.get(channel_id)


class _FakeWriter:
    """A writer that keeps the operations instead of writing them."""

    def __init__(self) -> None:
        self.ops: list[Any] = []

    def submit(self, op: Any) -> bool:
        self.ops.append(op)
        return True

    def kinds(self) -> list[str]:
        return [op.kind for op in self.ops]


def _window() -> tuple[datetime, datetime]:
    return (
        datetime(2026, 11, 1, tzinfo=timezone.utc),
        datetime(2026, 11, 12, tzinfo=timezone.utc),
    )


def _sample(name: str, labels: dict[str, str] | None = None) -> float:
    """The current value of a sample of the process wide metric registry."""
    value = REGISTRY.get_sample_value(name, labels or {})
    return 0.0 if value is None else value


async def test_backfill_writes_the_history_with_the_insert_only_strategy(
    monkeypatch: pytest.MonkeyPatch,
):
    """The backfill must never overwrite what the collector already wrote."""
    monkeypatch.setenv("DISCORD_TOKEN", "token")
    writer = _FakeWriter()
    client = _FakeClient({20: _FakeChannel([_message(1), _message(2)])})

    runner = BackfillRunner(_config(), writer, client_factory=lambda: client)
    counts = await runner.run([20], _window())

    assert counts == {"messages": 2, "channels": 1}
    assert writer.kinds()[0] == OpKind.MESSAGE_INSERT_ONLY
    # The user, the channel and the guild are written as well.
    assert OpKind.USER in writer.kinds()
    assert OpKind.CHANNEL in writer.kinds()
    assert OpKind.GUILD in writer.kinds()
    # The backfill does not run the AI.
    records = [op.payload for op in writer.ops if op.kind == OpKind.MESSAGE_INSERT_ONLY]
    assert [record.analysis_status for record in records] == ["skipped", "skipped"]


async def test_backfill_leaves_the_messages_outside_the_window_alone(
    monkeypatch: pytest.MonkeyPatch,
):
    """Only the messages of the window are imported."""
    monkeypatch.setenv("DISCORD_TOKEN", "token")
    writer = _FakeWriter()
    old = _message(1, created_at=datetime(2026, 10, 1, tzinfo=timezone.utc))
    inside = _message(2)
    client = _FakeClient({20: _FakeChannel([old, inside])})

    runner = BackfillRunner(_config(), writer, client_factory=lambda: client)
    counts = await runner.run([20], _window())

    assert counts["messages"] == 1
    records = [op.payload for op in writer.ops if op.kind == OpKind.MESSAGE_INSERT_ONLY]
    assert [record.message_id for record in records] == [2]


async def test_backfill_reports_a_channel_the_bot_cannot_see(
    monkeypatch: pytest.MonkeyPatch,
):
    """A channel that is not visible to the bot is not an error."""
    monkeypatch.setenv("DISCORD_TOKEN", "token")
    writer = _FakeWriter()
    client = _FakeClient({})

    runner = BackfillRunner(_config(), writer, client_factory=lambda: client)
    counts = await runner.run([20], _window())

    assert counts == {"messages": 0, "channels": 0}
    assert writer.ops == []


async def test_backfill_counts_the_imported_messages(
    monkeypatch: pytest.MonkeyPatch,
):
    """The operator sees how much history the backfill brought in."""
    monkeypatch.setenv("DISCORD_TOKEN", "token")
    writer = _FakeWriter()
    client = _FakeClient({20: _FakeChannel([_message(1), _message(2)])})
    before = _sample("foresight_backfill_messages_total", {"channel_id": "20"})

    runner = BackfillRunner(_config(), writer, client_factory=lambda: client)
    await runner.run([20], _window())

    assert (
        _sample("foresight_backfill_messages_total", {"channel_id": "20"}) == before + 2
    )


async def test_backfill_paginates_until_the_history_is_exhausted(
    monkeypatch: pytest.MonkeyPatch,
):
    """A channel with more messages than one page is read page by page."""
    monkeypatch.setenv("DISCORD_TOKEN", "token")
    writer = _FakeWriter()
    messages = [_message(index) for index in range(1, 151)]
    channel = _FakeChannel(messages)
    client = _FakeClient({20: channel})

    runner = BackfillRunner(_config(), writer, client_factory=lambda: client)
    counts = await runner.run([20], _window(), limit=150)

    assert counts["messages"] == 150
    # The first page asks for the newest messages, the second one for the
    # messages that are older than the oldest of the first page.
    assert channel.calls[0] == {"limit": 100}
    assert channel.calls[1]["before"] == 51


async def test_backfill_closes_the_client_when_it_is_done(
    monkeypatch: pytest.MonkeyPatch,
):
    """The client is a resource of the command, so it is released."""
    monkeypatch.setenv("DISCORD_TOKEN", "token")
    client = _FakeClient({20: _FakeChannel([_message(1)])})

    runner = BackfillRunner(_config(), _FakeWriter(), client_factory=lambda: client)
    await runner.run([20], _window())

    assert client.closed
