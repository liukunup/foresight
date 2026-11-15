"""Response models."""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    """The pagination envelope every list endpoint returns."""

    total: int
    limit: int
    offset: int
    has_more: bool
    items: list[T]


class MessageOut(BaseModel):
    message_id: int
    guild_id: int | None
    channel_id: int
    author_id: int
    content: str
    message_type: int
    created_at: str
    captured_at: str
    edited_at: str | None
    edit_count: int
    deleted_at: str | None
    reply_to_message_id: int | None
    mentions_count: int
    char_count: int
    word_count: int
    attachment_count: int
    attachments_json: str | None
    embeds_json: str | None
    is_bot: bool
    analysis_status: str


class AnalysisOut(BaseModel):
    message_id: int
    translated_text: str | None
    sentiment: str | None
    sentiment_score: float | None
    issues_json: str | None
    model: str | None
    prompt_version: str | None
    latency_ms: int | None
    error: str | None
    analyzed_at: str | None


class MessageDetail(MessageOut):
    """A message plus its AI analysis, if one exists."""

    analysis: AnalysisOut | None = None


class HealthOut(BaseModel):
    status: str
    total_messages: int
    total_attachments: int
    db_size_mb: float
    last_captured_at: str | None


class UserOut(BaseModel):
    """One speaker of the dump, counted over the messages table."""

    author_id: int
    username: str | None
    message_count: int
    first_message_at: str | None
    last_message_at: str | None
    avg_chars: float


class ChannelOut(BaseModel):
    """One channel of the dump, counted over the messages table."""

    channel_id: int
    guild_id: int | None
    name: str | None
    message_count: int
    last_message_at: str | None


class ReactionOut(BaseModel):
    """One reaction row, soft removals included."""

    id: int
    message_id: int
    user_id: int
    emoji: str
    is_custom: bool
    added_at: str
    removed_at: str | None
