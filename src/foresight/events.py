"""Discord event handling.

All Discord-specific branching lives here so that the bot object stays a
thin registration layer and the logic stays unit testable.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable

from foresight.adapters.discord_mapper import (
    iso,
    map_attachments,
    map_channel,
    map_guild,
    map_message,
    map_reaction,
    map_reaction_remove,
    map_user,
)
from foresight.domain.models import (
    AttachmentRecord,
    MessageDelete,
    MessageEdit,
    OpKind,
    WriteOp,
)

logger = logging.getLogger(__name__)


class EventHandler:
    """Turn Discord events into write operations."""

    def __init__(
        self,
        config: Any,
        writer: Any,
        media_sink: Callable[[AttachmentRecord], None] | None = None,
        bot_user_id: int | None = None,
    ) -> None:
        self._whitelist = set(config.discord.channel_ids)
        self._writer = writer
        self._media_sink = media_sink
        self._bot_user_id = bot_user_id
        self._media_backend = getattr(getattr(config, "media", None), "backend", "local")

    def _allowed(self, channel_id: int | None) -> bool:
        return channel_id is not None and channel_id in self._whitelist

    def handle_message(self, message: Any) -> None:
        try:
            channel_id = getattr(message.channel, "id", None)
            if not self._allowed(channel_id):
                return
            author_id = getattr(message.author, "id", None)
            if self._bot_user_id is not None and author_id == self._bot_user_id:
                return
            captured_at = datetime.now(timezone.utc)
            record = map_message(message, captured_at=captured_at)
            self._writer.submit(WriteOp(OpKind.MESSAGE, record))
            self._writer.submit(
                WriteOp(OpKind.USER, map_user(message.author, updated_at=captured_at))
            )
            self._writer.submit(
                WriteOp(OpKind.CHANNEL, map_channel(message.channel, captured_at=captured_at))
            )
            guild = getattr(message, "guild", None)
            if guild is not None:
                self._writer.submit(
                    WriteOp(OpKind.GUILD, map_guild(guild, captured_at=captured_at))
                )
            for attachment in map_attachments(
                message,
                captured_at=captured_at,
                storage_backend=self._media_backend,
            ):
                self._writer.submit(WriteOp(OpKind.ATTACHMENT, attachment))
                if self._media_sink is not None:
                    self._media_sink(attachment)
        except Exception:
            logger.exception("failed to handle message")

    def handle_raw_edit(self, payload: Any) -> None:
        try:
            if not self._allowed(getattr(payload, "channel_id", None)):
                return
            data = getattr(payload, "data", None) or {}
            content = data.get("content")
            if content is None:
                return
            edited_at = data.get("edited_timestamp") or iso(datetime.now(timezone.utc))
            self._writer.submit(
                WriteOp(
                    OpKind.MESSAGE_EDIT,
                    MessageEdit(payload.message_id, content, edited_at),
                )
            )
        except Exception:
            logger.exception("failed to handle message edit")

    def handle_raw_delete(self, payload: Any) -> None:
        try:
            if not self._allowed(getattr(payload, "channel_id", None)):
                return
            deleted_at = iso(datetime.now(timezone.utc))
            self._writer.submit(
                WriteOp(OpKind.MESSAGE_DELETE, MessageDelete(payload.message_id, deleted_at))
            )
        except Exception:
            logger.exception("failed to handle message delete")

    def handle_raw_reaction_add(self, payload: Any) -> None:
        try:
            if not self._allowed(getattr(payload, "channel_id", None)):
                return
            self._writer.submit(
                WriteOp(OpKind.REACTION, map_reaction(payload))
            )
        except Exception:
            logger.exception("failed to handle reaction add")

    def handle_raw_reaction_remove(self, payload: Any) -> None:
        try:
            if not self._allowed(getattr(payload, "channel_id", None)):
                return
            # REACTION_REMOVE 只更新 removed_at，不覆盖 added_at
            self._writer.submit(
                WriteOp(OpKind.REACTION_REMOVE, map_reaction_remove(payload))
            )
        except Exception:
            logger.exception("failed to handle reaction remove")
