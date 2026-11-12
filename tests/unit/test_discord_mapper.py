from datetime import datetime, timezone
from types import SimpleNamespace

from foresight.adapters.discord_mapper import (
    count_words,
    iso,
    map_attachments,
    map_channel,
    map_guild,
    map_message,
    map_reaction,
    map_user,
)


def _author(user_id: int = 30, bot: bool = False):
    return SimpleNamespace(
        id=user_id,
        name="alice",
        global_name="Alice",
        display_name="alice!",
        discriminator="0",
        bot=bot,
        avatar=SimpleNamespace(url="https://cdn/avatar.png"),
        created_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        joined_at=datetime(2021, 1, 1, tzinfo=timezone.utc),
        roles=[SimpleNamespace(id=5), SimpleNamespace(id=6)],
    )


def _channel(channel_id: int = 20):
    return SimpleNamespace(
        id=channel_id,
        name="general",
        topic="hello",
        type="text",
        position=3,
        guild=SimpleNamespace(id=10, name="Foresight", member_count=42),
    )


def _message(content: str = "hello", edited_at=None):
    return SimpleNamespace(
        id=100,
        content=content,
        type=0,
        created_at=datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc),
        edited_at=edited_at,
        author=_author(),
        channel=_channel(),
        guild=SimpleNamespace(id=10, name="Foresight", member_count=42),
        reference=SimpleNamespace(message_id=99),
        mentions=[SimpleNamespace(id=31)],
        attachments=[],
        embeds=[],
    )


def test_map_message_extracts_fields():
    record = map_message(_message())
    assert record.message_id == 100
    assert record.guild_id == 10
    assert record.channel_id == 20
    assert record.author_id == 30
    assert record.content == "hello"
    assert record.reply_to_message_id == 99
    assert record.mentions_count == 1
    assert record.char_count == 5
    assert record.is_bot is False
    assert record.analysis_status == "pending"


def test_map_message_without_reference_has_no_reply():
    message = _message()
    message.reference = None
    assert map_message(message).reply_to_message_id is None


def test_map_message_counts_cjk_words():
    record = map_message(_message("你好世界"))
    assert record.char_count == 4
    assert record.word_count == 4


def test_map_user_snapshot():
    snapshot = map_user(_author())
    assert snapshot.user_id == 30
    assert snapshot.username == "alice"
    assert snapshot.global_name == "Alice"
    assert snapshot.avatar_url == "https://cdn/avatar.png"
    assert snapshot.guild_joined_at == "2021-01-01T00:00:00+00:00"
    assert '"5"' in snapshot.roles_json


def test_map_channel_and_guild():
    channel = map_channel(_channel())
    assert channel.channel_id == 20
    assert channel.guild_id == 10
    assert channel.name == "general"
    guild = map_guild(SimpleNamespace(id=10, name="Foresight", member_count=42))
    assert guild.guild_id == 10
    assert guild.member_count == 42


def test_map_reaction_unicode_and_custom():
    payload = SimpleNamespace(
        message_id=100,
        user_id=31,
        emoji=SimpleNamespace(name="🔥", id=None, is_custom_emoji=lambda: False),
    )
    record = map_reaction(payload)
    assert record.emoji == "🔥"
    assert record.emoji_id is None
    assert record.is_custom is False

    custom = SimpleNamespace(
        message_id=100,
        user_id=31,
        emoji=SimpleNamespace(name="peepo", id=555, is_custom_emoji=lambda: True),
    )
    record = map_reaction(custom)
    assert record.emoji == "peepo"
    assert record.emoji_id == 555
    assert record.is_custom is True


def test_map_attachments():
    message = _message()
    message.attachments = [
        SimpleNamespace(
            id=1,
            filename="a.png",
            content_type="image/png",
            size=100,
            url="https://cdn/a.png",
        )
    ]
    records = map_attachments(message)
    assert len(records) == 1
    assert records[0].attachment_id == 1
    assert records[0].filename == "a.png"
    assert records[0].status == "pending"


def test_count_words_mixed_language():
    assert count_words("hello world") == 2
    assert count_words("你好世界") == 4
    assert count_words("hello 世界") == 3
    assert count_words("") == 0


def test_iso_handles_naive_datetime():
    assert iso(datetime(2026, 1, 1, 0, 0)) == "2026-01-01T00:00:00+00:00"
    assert iso(None) is None
