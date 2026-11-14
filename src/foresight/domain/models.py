"""Plain dataclasses shared by every layer.

This module must stay dependency free: no discord, no sqlalchemy, no fastapi.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class OpKind:
    """Kinds of write operations understood by the repository."""

    MESSAGE = "message"
    MESSAGE_INSERT_ONLY = "message_insert_only"
    MESSAGE_EDIT = "message_edit"
    MESSAGE_DELETE = "message_delete"
    REACTION = "reaction"
    REACTION_REMOVE = "reaction_remove"
    USER = "user"
    CHANNEL = "channel"
    GUILD = "guild"
    ATTACHMENT = "attachment"
    ATTACHMENT_STATUS = "attachment_status"
    ANALYSIS_STATUS = "analysis_status"
    ANALYSIS = "analysis"


@dataclass
class WriteOp:
    """A single unit of work handed to the repository."""

    kind: str
    payload: Any


@dataclass
class MessageRecord:
    message_id: int
    guild_id: int | None
    channel_id: int
    author_id: int
    content: str
    message_type: int
    created_at: str
    captured_at: str
    edited_at: str | None = None
    edit_count: int = 0
    deleted_at: str | None = None
    reply_to_message_id: int | None = None
    mentions_count: int = 0
    char_count: int = 0
    word_count: int = 0
    attachment_count: int = 0
    attachments_json: str | None = None
    embeds_json: str | None = None
    is_bot: bool = False
    analysis_status: str = "pending"


@dataclass
class MessageEdit:
    message_id: int
    content: str
    edited_at: str


@dataclass
class MessageDelete:
    message_id: int
    deleted_at: str


@dataclass
class ReactionRecord:
    message_id: int
    user_id: int
    emoji: str
    emoji_id: int | None
    is_custom: bool
    added_at: str
    removed_at: str | None = None


@dataclass
class ReactionRemove:
    message_id: int
    user_id: int
    emoji: str
    removed_at: str


@dataclass
class UserSnapshot:
    user_id: int
    username: str
    global_name: str | None
    display_name: str | None
    discriminator: str | None
    avatar_url: str | None
    is_bot: bool
    account_created_at: str | None
    guild_joined_at: str | None
    roles_json: str
    updated_at: str


@dataclass
class ChannelSnapshot:
    channel_id: int
    guild_id: int | None
    name: str | None
    topic: str | None
    channel_type: str | None
    position: int | None
    captured_at: str


@dataclass
class GuildSnapshot:
    guild_id: int
    name: str | None
    member_count: int | None
    captured_at: str


@dataclass
class AttachmentRecord:
    message_id: int
    attachment_id: int | None
    filename: str | None
    content_type: str | None
    size: int | None
    url: str
    storage_backend: str
    status: str
    captured_at: str


@dataclass
class AttachmentStatus:
    """Terminal status update for one attachment row."""

    message_id: int
    attachment_id: int | None
    status: str
    error: str | None = None
    sha256: str | None = None
    storage_key: str | None = None
    storage_uri: str | None = None
    stored_at: str | None = None


@dataclass
class MessageAnalysis:
    message_id: int
    translated_text: str | None
    sentiment: str | None
    sentiment_score: float | None
    issues_json: str | None
    model: str
    prompt_version: str
    latency_ms: int
    error: str | None
    analyzed_at: str
