"""Translate Discord SDK objects into domain records.

This is the only module that may mention Discord field names.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from foresight.domain.models import (
    AttachmentRecord,
    ChannelSnapshot,
    GuildSnapshot,
    MessageRecord,
    ReactionRecord,
    ReactionRemove,
    UserSnapshot,
)

_CJK_PATTERN = re.compile(
    r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]"
)


def iso(dt: datetime | None) -> str | None:
    """Render a datetime as a UTC ISO-8601 string."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def count_words(text: str) -> int:
    """Count words: CJK characters count individually, other runs count per word."""
    if not text:
        return 0
    cjk = len(_CJK_PATTERN.findall(text))
    rest = _CJK_PATTERN.sub(" ", text)
    latin = len([chunk for chunk in re.split(r"\s+", rest) if chunk])
    return cjk + latin


def _avatar_url(author: Any) -> str | None:
    avatar = getattr(author, "avatar", None)
    if avatar is None:
        return None
    url = getattr(avatar, "url", None)
    return str(url) if url else None


def _display_name(author: Any) -> str | None:
    name = getattr(author, "display_name", None)
    if name:
        return str(name)
    raw = getattr(author, "name", None)
    return str(raw) if raw else None


def map_user(author: Any, updated_at: datetime | None = None) -> UserSnapshot:
    now = updated_at or _now()
    roles = [str(getattr(role, "id")) for role in (getattr(author, "roles", None) or [])]
    return UserSnapshot(
        user_id=author.id,
        username=str(getattr(author, "name", "") or ""),
        global_name=getattr(author, "global_name", None),
        display_name=_display_name(author),
        discriminator=getattr(author, "discriminator", None),
        avatar_url=_avatar_url(author),
        is_bot=bool(getattr(author, "bot", False)),
        account_created_at=iso(getattr(author, "created_at", None)),
        guild_joined_at=iso(getattr(author, "joined_at", None)),
        roles_json=json.dumps(roles),
        updated_at=iso(now),
    )


def map_channel(channel: Any, captured_at: datetime | None = None) -> ChannelSnapshot:
    now = captured_at or _now()
    guild = getattr(channel, "guild", None)
    channel_type = getattr(channel, "type", None)
    return ChannelSnapshot(
        channel_id=channel.id,
        guild_id=getattr(guild, "id", None),
        name=getattr(channel, "name", None),
        topic=getattr(channel, "topic", None),
        channel_type=str(channel_type) if channel_type is not None else None,
        position=getattr(channel, "position", None),
        captured_at=iso(now),
    )


def map_guild(guild: Any, captured_at: datetime | None = None) -> GuildSnapshot:
    now = captured_at or _now()
    return GuildSnapshot(
        guild_id=guild.id,
        name=getattr(guild, "name", None),
        member_count=getattr(guild, "member_count", None),
        captured_at=iso(now),
    )


def _attachment_summary(attachment: Any) -> dict[str, Any]:
    return {
        "id": getattr(attachment, "id", None),
        "filename": getattr(attachment, "filename", None),
        "content_type": getattr(attachment, "content_type", None),
        "size": getattr(attachment, "size", None),
        "url": getattr(attachment, "url", None),
    }


def map_message(message: Any, captured_at: datetime | None = None) -> MessageRecord:
    now = captured_at or _now()
    content = message.content or ""
    author = message.author
    channel = message.channel
    guild = getattr(message, "guild", None)
    reference = getattr(message, "reference", None)
    attachments = list(getattr(message, "attachments", None) or [])
    embeds = list(getattr(message, "embeds", None) or [])
    message_type = getattr(message, "type", 0)
    return MessageRecord(
        message_id=message.id,
        guild_id=getattr(guild, "id", None),
        channel_id=channel.id,
        author_id=author.id,
        content=content,
        message_type=int(getattr(message_type, "value", message_type) or 0),
        created_at=iso(message.created_at),
        captured_at=iso(now),
        edited_at=iso(getattr(message, "edited_at", None)),
        edit_count=0,
        deleted_at=None,
        reply_to_message_id=getattr(reference, "message_id", None),
        mentions_count=len(list(getattr(message, "mentions", None) or [])),
        char_count=len(content),
        word_count=count_words(content),
        attachment_count=len(attachments),
        attachments_json=json.dumps(
            [_attachment_summary(a) for a in attachments], ensure_ascii=False
        ),
        embeds_json=json.dumps(
            [embed.to_dict() for embed in embeds], ensure_ascii=False
        ),
        is_bot=bool(getattr(author, "bot", False)),
        analysis_status="pending",
    )


def map_attachments(
    message: Any, captured_at: datetime | None = None, storage_backend: str = "local"
) -> list[AttachmentRecord]:
    now = captured_at or _now()
    records: list[AttachmentRecord] = []
    for attachment in list(getattr(message, "attachments", None) or []):
        records.append(
            AttachmentRecord(
                message_id=message.id,
                attachment_id=getattr(attachment, "id", None),
                filename=getattr(attachment, "filename", None),
                content_type=getattr(attachment, "content_type", None),
                size=getattr(attachment, "size", None),
                url=str(getattr(attachment, "url", "") or ""),
                storage_backend=storage_backend,
                status="pending",
                captured_at=iso(now),
            )
        )
    return records


def _emoji_info(emoji: Any) -> tuple[str, int | None, bool]:
    name = getattr(emoji, "name", "") or ""
    emoji_id = getattr(emoji, "id", None)
    is_custom = False
    checker = getattr(emoji, "is_custom_emoji", None)
    if callable(checker):
        is_custom = bool(checker())
    if emoji_id is not None:
        is_custom = True
    return str(name), emoji_id, is_custom


def map_reaction(payload: Any) -> ReactionRecord:
    """Map a reaction *add* event for the ``OpKind.REACTION`` upsert.

    The upsert's conflict-update columns include ``added_at``, so this function
    must only ever describe an addition: ``added_at`` is stamped now and
    ``removed_at`` stays ``None``. Removals go through :func:`map_reaction_remove`.
    """
    name, emoji_id, is_custom = _emoji_info(payload.emoji)
    now = _now()
    return ReactionRecord(
        message_id=payload.message_id,
        user_id=payload.user_id,
        emoji=name,
        emoji_id=emoji_id,
        is_custom=is_custom,
        added_at=iso(now),
        removed_at=None,
    )


def map_reaction_remove(payload: Any) -> ReactionRemove:
    """Map a reaction *removal* event for the ``OpKind.REACTION_REMOVE`` op.

    That op updates ``removed_at`` only, so the ``added_at`` written by
    :func:`map_reaction` survives the removal instead of being overwritten.
    """
    name, _emoji_id, _is_custom = _emoji_info(payload.emoji)
    now = _now()
    return ReactionRemove(
        message_id=payload.message_id,
        user_id=payload.user_id,
        emoji=name,
        removed_at=iso(now),
    )
