"""The only module that knows SQL."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Callable, Mapping, Sequence

from sqlalchemy import Table, bindparam, func, literal_column, update
from sqlalchemy.dialects.mysql import Insert as MySQLInsert
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.postgresql import Insert as PGInsert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import Insert as SQLiteInsert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from foresight.domain.models import (
    AttachmentRecord,
    AttachmentStatus,
    ChannelSnapshot,
    GuildSnapshot,
    MessageAnalysis,
    MessageDelete,
    MessageEdit,
    MessageRecord,
    OpKind,
    ReactionRecord,
    ReactionRemove,
    UserSnapshot,
    WriteOp,
)
from foresight.storage.tables import (
    attachments,
    channels,
    guilds,
    message_analysis,
    messages,
    reactions,
    users,
)

# Spec §7.1: the realtime path may only refresh these four columns. It must
# never touch `analysis_status` (owned by the AI worker) or `captured_at`
# (records when the message was first dumped), and it must not overwrite the
# counters a fresh record happens to carry.
MESSAGE_UPDATABLE = (
    "content",
    "edited_at",
    "edit_count",
    "deleted_at",
)

# The order in which the groups of a batch are applied. A batch can report a
# message, an edit and a delete for the same `message_id`; the message is
# written first, then its edit, then its delete, so the freshest information
# wins no matter in which order the operations arrived. Every kind the
# repository handles must appear here.
OP_ORDER: tuple[str, ...] = (
    OpKind.MESSAGE,
    OpKind.MESSAGE_INSERT_ONLY,
    OpKind.MESSAGE_EDIT,
    OpKind.MESSAGE_DELETE,
    OpKind.REACTION,
    OpKind.REACTION_REMOVE,
    OpKind.USER,
    OpKind.CHANNEL,
    OpKind.GUILD,
    OpKind.ATTACHMENT,
    OpKind.ATTACHMENT_STATUS,
    OpKind.ANALYSIS_STATUS,
    OpKind.ANALYSIS,
)


class Repository:
    """Batched, dialect-aware write access."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._dialect = engine.dialect.name
        self._handlers: dict[str, Callable[[AsyncConnection, list[Any]], Any]] = {
            OpKind.MESSAGE: self._handle_message,
            OpKind.MESSAGE_INSERT_ONLY: self._handle_message_insert_only,
            OpKind.MESSAGE_EDIT: self._handle_message_edit,
            OpKind.MESSAGE_DELETE: self._handle_message_delete,
            OpKind.REACTION: self._handle_reaction,
            OpKind.REACTION_REMOVE: self._handle_reaction_remove,
            OpKind.USER: self._handle_user,
            OpKind.CHANNEL: self._handle_channel,
            OpKind.GUILD: self._handle_guild,
            OpKind.ATTACHMENT: self._handle_attachment,
            OpKind.ATTACHMENT_STATUS: self._handle_attachment_status,
            OpKind.ANALYSIS_STATUS: self._handle_analysis_status,
            OpKind.ANALYSIS: self._handle_analysis,
        }

    async def apply_batch(self, ops: Sequence[WriteOp]) -> None:
        """Apply a batch of operations inside a single transaction.

        The operations are grouped by kind and every group is applied with a
        single statement. The groups are applied in the order given by
        `OP_ORDER`: a message is written before its edit and before its
        delete, so a batch that reports the same `message_id` more than once
        ends with the freshest information. The result therefore does not
        depend on the order in which the operations arrived.
        """
        if not ops:
            return
        groups: dict[str, list[Any]] = defaultdict(list)
        for op in ops:
            if op.kind not in self._handlers:
                raise ValueError(
                    f"unknown op kind {op.kind!r}; "
                    f"valid kinds are: {', '.join(sorted(self._handlers))}"
                )
            groups[op.kind].append(op.payload)
        async with self._engine.begin() as conn:
            for kind in self._ordered_kinds(groups):
                await self._handlers[kind](conn, groups[kind])

    @staticmethod
    def _ordered_kinds(groups: Mapping[str, list[Any]]) -> list[str]:
        """The kinds of a batch, in the order in which they are applied.

        `OP_ORDER` is the canonical order. A kind that is missing from it
        (a programming error, guarded by a test) is applied last, so that
        no operation of a batch is ever silently dropped.
        """
        ordered = [kind for kind in OP_ORDER if kind in groups]
        return ordered + [kind for kind in groups if kind not in OP_ORDER]

    # -- insert helpers -------------------------------------------------

    def _insert(self, table: Table) -> SQLiteInsert | PGInsert | MySQLInsert:
        """Return the insert statement builder of the current dialect.

        The concrete class carries the dialect specific API: MySQL offers
        `on_duplicate_key_update` and `inserted`, SQLite and PostgreSQL offer
        `on_conflict_do_update`, `on_conflict_do_nothing` and `excluded`.
        """
        if self._dialect == "postgresql":
            return pg_insert(table)
        if self._dialect == "mysql":
            return mysql_insert(table)
        return sqlite_insert(table)

    def _upsert(
        self,
        table: Table,
        values: list[dict[str, Any]],
        conflict_columns: list[str],
        update_columns: tuple[str, ...],
    ) -> SQLiteInsert | PGInsert | MySQLInsert:
        """Build an upsert that refreshes `update_columns` on conflict."""
        stmt = self._insert(table).values(values)
        if isinstance(stmt, MySQLInsert):
            # MySQL names the row that would have been inserted with
            # VALUES(col), SQLite and PostgreSQL with the excluded table.
            return stmt.on_duplicate_key_update(
                **{col: stmt.inserted[col] for col in update_columns}
            )
        return stmt.on_conflict_do_update(
            index_elements=conflict_columns,
            set_={col: stmt.excluded[col] for col in update_columns},
        )

    def _insert_only(
        self, table: Table, values: list[dict[str, Any]]
    ) -> SQLiteInsert | PGInsert | MySQLInsert:
        """Build an insert that keeps rows which already exist.

        On MySQL this is `ON DUPLICATE KEY UPDATE id = LAST_INSERT_ID(id)` —
        a true no-op: the conflicting row keeps its surrogate key and the
        connection's last insert id stays usable. Writing `id = VALUES(id)`
        instead would copy the freshly allocated auto-increment value onto
        the existing row and rewrite its key.
        """
        stmt = self._insert(table).values(values)
        if isinstance(stmt, MySQLInsert):
            # MySQL has no "do nothing": a no-op update on the primary key
            # leaves the conflicting row as it is.
            primary_key = list(table.primary_key.columns)[0]
            return stmt.on_duplicate_key_update(
                **{
                    primary_key.name: func.last_insert_id(
                        literal_column(primary_key.name)
                    )
                }
            )
        return stmt.on_conflict_do_nothing()

    # -- handlers -------------------------------------------------------
    # Update statements bind their parameters under a `b_` prefixed name:
    # SQLAlchemy reserves the plain column names for the parameters of an
    # UPDATE and would reject them as duplicate bind parameters.

    async def _handle_message(self, conn: AsyncConnection, records: list[MessageRecord]) -> None:
        values = [self._message_values(r) for r in records]
        stmt = self._upsert(
            messages,
            values,
            ["message_id"],
            MESSAGE_UPDATABLE,
        )
        await conn.execute(stmt)

    async def _handle_message_insert_only(
        self, conn: AsyncConnection, records: list[MessageRecord]
    ) -> None:
        values = [self._message_values(r) for r in records]
        await conn.execute(self._insert_only(messages, values))

    async def _handle_message_edit(self, conn: AsyncConnection, edits: list[MessageEdit]) -> None:
        stmt = (
            update(messages)
            .where(messages.c.message_id == bindparam("b_message_id"))
            .values(
                content=bindparam("b_content"),
                edited_at=bindparam("b_edited_at"),
                edit_count=messages.c.edit_count + 1,
            )
        )
        await conn.execute(
            stmt,
            [
                {
                    "b_message_id": edit.message_id,
                    "b_content": edit.content,
                    "b_edited_at": edit.edited_at,
                }
                for edit in edits
            ],
        )

    async def _handle_message_delete(
        self, conn: AsyncConnection, deletes: list[MessageDelete]
    ) -> None:
        stmt = (
            update(messages)
            .where(messages.c.message_id == bindparam("b_message_id"))
            .values(deleted_at=bindparam("b_deleted_at"))
        )
        await conn.execute(
            stmt,
            [
                {
                    "b_message_id": deletion.message_id,
                    "b_deleted_at": deletion.deleted_at,
                }
                for deletion in deletes
            ],
        )

    async def _handle_reaction(
        self, conn: AsyncConnection, records: list[ReactionRecord]
    ) -> None:
        values = [
            {
                "message_id": r.message_id,
                "user_id": r.user_id,
                "emoji": r.emoji,
                "emoji_id": r.emoji_id,
                "is_custom": r.is_custom,
                "added_at": r.added_at,
                "removed_at": r.removed_at,
            }
            for r in records
        ]
        stmt = self._upsert(
            reactions,
            values,
            ["message_id", "user_id", "emoji"],
            ("emoji_id", "is_custom", "added_at", "removed_at"),
        )
        await conn.execute(stmt)

    async def _handle_reaction_remove(
        self, conn: AsyncConnection, removes: list[ReactionRemove]
    ) -> None:
        stmt = (
            update(reactions)
            .where(
                (reactions.c.message_id == bindparam("b_message_id"))
                & (reactions.c.user_id == bindparam("b_user_id"))
                & (reactions.c.emoji == bindparam("b_emoji"))
            )
            .values(removed_at=bindparam("b_removed_at"))
        )
        await conn.execute(
            stmt,
            [
                {
                    "b_message_id": remove.message_id,
                    "b_user_id": remove.user_id,
                    "b_emoji": remove.emoji,
                    "b_removed_at": remove.removed_at,
                }
                for remove in removes
            ],
        )

    async def _handle_user(self, conn: AsyncConnection, records: list[UserSnapshot]) -> None:
        values = [
            {
                "user_id": r.user_id,
                "username": r.username,
                "global_name": r.global_name,
                "display_name": r.display_name,
                "discriminator": r.discriminator,
                "avatar_url": r.avatar_url,
                "is_bot": r.is_bot,
                "account_created_at": r.account_created_at,
                "guild_joined_at": r.guild_joined_at,
                "roles_json": r.roles_json,
                "updated_at": r.updated_at,
            }
            for r in records
        ]
        stmt = self._upsert(
            users,
            values,
            ["user_id"],
            (
                "username",
                "global_name",
                "display_name",
                "discriminator",
                "avatar_url",
                "is_bot",
                "account_created_at",
                "guild_joined_at",
                "roles_json",
                "updated_at",
            ),
        )
        await conn.execute(stmt)

    async def _handle_channel(
        self, conn: AsyncConnection, records: list[ChannelSnapshot]
    ) -> None:
        values = [
            {
                "channel_id": r.channel_id,
                "guild_id": r.guild_id,
                "name": r.name,
                "topic": r.topic,
                "channel_type": r.channel_type,
                "position": r.position,
                "captured_at": r.captured_at,
            }
            for r in records
        ]
        stmt = self._upsert(
            channels,
            values,
            ["channel_id"],
            ("guild_id", "name", "topic", "channel_type", "position", "captured_at"),
        )
        await conn.execute(stmt)

    async def _handle_guild(
        self, conn: AsyncConnection, records: list[GuildSnapshot]
    ) -> None:
        values = [
            {
                "guild_id": r.guild_id,
                "name": r.name,
                "member_count": r.member_count,
                "captured_at": r.captured_at,
            }
            for r in records
        ]
        stmt = self._upsert(
            guilds,
            values,
            ["guild_id"],
            ("name", "member_count", "captured_at"),
        )
        await conn.execute(stmt)

    async def _handle_attachment(
        self, conn: AsyncConnection, records: list[AttachmentRecord]
    ) -> None:
        values = [
            {
                "message_id": r.message_id,
                "attachment_id": r.attachment_id,
                "filename": r.filename,
                "content_type": r.content_type,
                "size": r.size,
                "url": r.url,
                "storage_backend": r.storage_backend,
                "status": r.status,
                "captured_at": r.captured_at,
            }
            for r in records
        ]
        await conn.execute(self._insert_only(attachments, values))

    async def _handle_attachment_status(
        self, conn: AsyncConnection, records: list[AttachmentStatus]
    ) -> None:
        """Write a complete status snapshot to the matching attachment row.

        `AttachmentStatus` is a snapshot of the status of one attachment:
        every status column is written unconditionally, so a payload that
        omits a field clears it. That is deliberate — an attachment row is
        either stored or failed, never both — so a row that ends up
        `skipped` must not keep the file columns of an earlier attempt.
        """
        stmt = (
            update(attachments)
            .where(
                (attachments.c.message_id == bindparam("b_message_id"))
                & (attachments.c.attachment_id == bindparam("b_attachment_id"))
            )
            .values(
                status=bindparam("b_status"),
                error=bindparam("b_error"),
                sha256=bindparam("b_sha256"),
                storage_key=bindparam("b_storage_key"),
                storage_uri=bindparam("b_storage_uri"),
                stored_at=bindparam("b_stored_at"),
            )
        )
        await conn.execute(
            stmt,
            [
                {
                    "b_message_id": record.message_id,
                    "b_attachment_id": record.attachment_id,
                    "b_status": record.status,
                    "b_error": record.error,
                    "b_sha256": record.sha256,
                    "b_storage_key": record.storage_key,
                    "b_storage_uri": record.storage_uri,
                    "b_stored_at": record.stored_at,
                }
                for record in records
            ],
        )

    async def _handle_analysis_status(
        self, conn: AsyncConnection, records: list[tuple[int, str]]
    ) -> None:
        stmt = (
            update(messages)
            .where(messages.c.message_id == bindparam("b_message_id"))
            .values(analysis_status=bindparam("b_status"))
        )
        await conn.execute(
            stmt,
            [
                {"b_message_id": message_id, "b_status": status}
                for message_id, status in records
            ],
        )

    async def _handle_analysis(
        self, conn: AsyncConnection, records: list[MessageAnalysis]
    ) -> None:
        values = [
            {
                "message_id": r.message_id,
                "translated_text": r.translated_text,
                "sentiment": r.sentiment,
                "sentiment_score": r.sentiment_score,
                "issues_json": r.issues_json,
                "model": r.model,
                "prompt_version": r.prompt_version,
                "latency_ms": r.latency_ms,
                "error": r.error,
                "analyzed_at": r.analyzed_at,
            }
            for r in records
        ]
        stmt = self._upsert(
            message_analysis,
            values,
            ["message_id"],
            (
                "translated_text",
                "sentiment",
                "sentiment_score",
                "issues_json",
                "model",
                "prompt_version",
                "latency_ms",
                "error",
                "analyzed_at",
            ),
        )
        await conn.execute(stmt)

    @staticmethod
    def _message_values(record: MessageRecord) -> dict[str, Any]:
        return {
            "message_id": record.message_id,
            "guild_id": record.guild_id,
            "channel_id": record.channel_id,
            "author_id": record.author_id,
            "content": record.content,
            "message_type": record.message_type,
            "created_at": record.created_at,
            "captured_at": record.captured_at,
            "edited_at": record.edited_at,
            "edit_count": record.edit_count,
            "deleted_at": record.deleted_at,
            "reply_to_message_id": record.reply_to_message_id,
            "mentions_count": record.mentions_count,
            "char_count": record.char_count,
            "word_count": record.word_count,
            "attachment_count": record.attachment_count,
            "attachments_json": record.attachments_json,
            "embeds_json": record.embeds_json,
            "is_bot": record.is_bot,
            "analysis_status": record.analysis_status,
        }
