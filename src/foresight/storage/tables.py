"""SQLAlchemy Core table definitions.

JSON payloads are stored as TEXT so that SQLite, PostgreSQL and MySQL all
behave identically. `reactions` and `attachments` deliberately declare no
foreign key to `messages`: a reaction may arrive for a message that was sent
before the service started and therefore has no row in `messages`.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Float,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
)

metadata = MetaData()


def _pk_type() -> BigInteger:
    """Auto-increment primary key type (SQLite needs INTEGER for rowid alias)."""
    return BigInteger().with_variant(Integer, "sqlite")


guilds = Table(
    "guilds",
    metadata,
    Column("guild_id", BigInteger, primary_key=True),
    Column("name", Text),
    Column("member_count", Integer),
    Column("captured_at", Text, nullable=False),
)

channels = Table(
    "channels",
    metadata,
    Column("channel_id", BigInteger, primary_key=True),
    Column("guild_id", BigInteger),
    Column("name", Text),
    Column("topic", Text),
    Column("channel_type", Text),
    Column("position", Integer),
    Column("captured_at", Text, nullable=False),
)

users = Table(
    "users",
    metadata,
    Column("user_id", BigInteger, primary_key=True),
    Column("username", Text, nullable=False),
    Column("global_name", Text),
    Column("display_name", Text),
    Column("discriminator", Text),
    Column("avatar_url", Text),
    Column("is_bot", Boolean, nullable=False, default=False),
    Column("account_created_at", Text),
    Column("guild_joined_at", Text),
    Column("roles_json", Text, nullable=False, default="[]"),
    Column("updated_at", Text, nullable=False),
)

messages = Table(
    "messages",
    metadata,
    Column("message_id", BigInteger, primary_key=True),
    Column("guild_id", BigInteger),
    Column("channel_id", BigInteger, nullable=False),
    Column("author_id", BigInteger, nullable=False),
    Column("content", Text, nullable=False, default=""),
    Column("message_type", Integer, nullable=False, default=0),
    Column("created_at", Text, nullable=False),
    Column("captured_at", Text, nullable=False),
    Column("edited_at", Text),
    Column("edit_count", Integer, nullable=False, default=0),
    Column("deleted_at", Text),
    Column("reply_to_message_id", BigInteger),
    Column("mentions_count", Integer, nullable=False, default=0),
    Column("char_count", Integer, nullable=False, default=0),
    Column("word_count", Integer, nullable=False, default=0),
    Column("attachment_count", Integer, nullable=False, default=0),
    Column("attachments_json", Text),
    Column("embeds_json", Text),
    Column("is_bot", Boolean, nullable=False, default=False),
    Column("analysis_status", Text, nullable=False, default="pending"),
)

reactions = Table(
    "reactions",
    metadata,
    Column("id", _pk_type(), primary_key=True, autoincrement=True),
    Column("message_id", BigInteger, nullable=False),
    Column("user_id", BigInteger, nullable=False),
    Column("emoji", Text, nullable=False),
    Column("emoji_id", BigInteger),
    Column("is_custom", Boolean, nullable=False, default=False),
    Column("added_at", Text, nullable=False),
    Column("removed_at", Text),
    UniqueConstraint("message_id", "user_id", "emoji", name="uq_reactions_target"),
)

attachments = Table(
    "attachments",
    metadata,
    Column("id", _pk_type(), primary_key=True, autoincrement=True),
    Column("message_id", BigInteger, nullable=False),
    Column("attachment_id", BigInteger),
    Column("filename", Text),
    Column("content_type", Text),
    Column("size", BigInteger),
    Column("url", Text, nullable=False),
    Column("sha256", Text),
    Column("storage_backend", Text, nullable=False, default="local"),
    Column("storage_key", Text),
    Column("storage_uri", Text),
    Column("status", Text, nullable=False, default="pending"),
    Column("error", Text),
    Column("captured_at", Text, nullable=False),
    Column("stored_at", Text),
)

message_analysis = Table(
    "message_analysis",
    metadata,
    Column("message_id", BigInteger, primary_key=True),
    Column("translated_text", Text),
    Column("sentiment", Text),
    Column("sentiment_score", Float),
    Column("issues_json", Text),
    Column("model", Text),
    Column("prompt_version", Text),
    Column("latency_ms", Integer),
    Column("error", Text),
    Column("analyzed_at", Text, nullable=False),
)

Index("ix_messages_channel_created", messages.c.channel_id, messages.c.created_at)
Index("ix_messages_author_created", messages.c.author_id, messages.c.created_at)
Index("ix_messages_guild_created", messages.c.guild_id, messages.c.created_at)
Index("ix_messages_created", messages.c.created_at)
Index("ix_messages_analysis_status", messages.c.analysis_status)
Index("ix_reactions_message", reactions.c.message_id)
Index("ix_attachments_message", attachments.c.message_id)
Index("ix_attachments_status", attachments.c.status)
