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
from foresight.metrics import (
    messages_captured,
    messages_deleted,
    messages_edited,
    reactions_total,
)

logger = logging.getLogger(__name__)


def _label(value: Any) -> str:
    """Turn an id into the value of a metric label.

    A label is a string, and an id that is not there — a message of a
    direct message has no guild — is an empty label and not a guess.
    """
    if value is None:
        return ""
    return str(value)


def _edit_timestamp(raw: Any) -> str:
    """The moment of an edit, in the canonical form of the service.

    Discord sends the moment of the last edit as an ISO-8601 string, and
    the database compares the timestamps of the edits as strings. The
    value is therefore rendered the way the mapper renders every other
    timestamp: in UTC, without a fraction when there is none. Two
    spellings of the same moment would compare as two different edits, and
    the replay of an edit would count as a new one.
    """
    if isinstance(raw, str):
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            logger.warning("unreadable edit timestamp: %r", raw)
        else:
            return iso(parsed) or ""
    return iso(datetime.now(timezone.utc)) or ""


class EventHandler:
    """Turn Discord events into write operations."""

    def __init__(
        self,
        config: Any,
        writer: Any,
        media_sink: Callable[[AttachmentRecord], None] | None = None,
        analysis_sink: Callable[[int, str], None] | None = None,
        bot_user_id: int | None = None,
    ) -> None:
        self._whitelist = set(config.discord.channel_ids)
        self._writer = writer
        self._media_sink = media_sink
        self._analysis_sink = analysis_sink
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
            if self._writer.submit(WriteOp(OpKind.MESSAGE, record)):
                # The message is counted when it really reached the writer:
                # a message the queue refused was not captured at all.
                messages_captured.labels(
                    channel_id=_label(record.channel_id),
                    guild_id=_label(record.guild_id),
                ).inc()
                if self._analysis_sink is not None:
                    # Only a message that reached the writer is analysed: a
                    # message the queue refused has no row in the database,
                    # so its analysis would be an analysis of nothing.
                    # Every message is handed over, even an empty one: the
                    # worker decides what is worth analysing, and it marks
                    # the rest as skipped.
                    self._analysis_sink(record.message_id, record.content)
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
                # Only an attachment that reached the writer is downloaded:
                # a row the queue refused has no place in the database, so
                # the file would be an orphan and the status update that
                # follows would match no row at all.
                if self._writer.submit(WriteOp(OpKind.ATTACHMENT, attachment)):
                    if self._media_sink is not None:
                        self._media_sink(attachment)
        except Exception:
            logger.exception("failed to handle message")

    def handle_raw_edit(self, payload: Any) -> None:
        try:
            channel_id = getattr(payload, "channel_id", None)
            if not self._allowed(channel_id):
                return
            data = getattr(payload, "data", None) or {}
            content = data.get("content")
            if content is None:
                return
            edited_at = _edit_timestamp(data.get("edited_timestamp"))
            if self._writer.submit(
                WriteOp(
                    OpKind.MESSAGE_EDIT,
                    MessageEdit(payload.message_id, content, edited_at),
                )
            ):
                messages_edited.labels(channel_id=_label(channel_id)).inc()
        except Exception:
            logger.exception("failed to handle message edit")

    def handle_raw_delete(self, payload: Any) -> None:
        try:
            channel_id = getattr(payload, "channel_id", None)
            if not self._allowed(channel_id):
                return
            deleted_at = iso(datetime.now(timezone.utc))
            if self._writer.submit(
                WriteOp(OpKind.MESSAGE_DELETE, MessageDelete(payload.message_id, deleted_at))
            ):
                messages_deleted.labels(channel_id=_label(channel_id)).inc()
        except Exception:
            logger.exception("failed to handle message delete")

    def handle_raw_reaction_add(self, payload: Any) -> None:
        try:
            channel_id = getattr(payload, "channel_id", None)
            if not self._allowed(channel_id):
                return
            if self._writer.submit(
                WriteOp(OpKind.REACTION, map_reaction(payload))
            ):
                # Only a reaction that arrives is a reaction that was
                # received: one that is taken back again is not counted.
                reactions_total.labels(channel_id=_label(channel_id)).inc()
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
