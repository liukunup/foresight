"""Tests for the async engine and the batched repository.

The repository is the only component that talks to the database, so these
tests pin the two guarantees the rest of the service relies on: every write
is idempotent, and a batch of operations is applied as one transaction with
one statement per operation kind.
"""

from __future__ import annotations

from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from foresight.config import DatabaseConfig
from foresight.domain.models import (
    AttachmentRecord,
    MessageDelete,
    MessageEdit,
    MessageRecord,
    OpKind,
    ReactionRecord,
    UserSnapshot,
    WriteOp,
)
from foresight.storage.db import create_engine, init_db
from foresight.storage.repository import Repository
from foresight.storage.tables import attachments, messages, reactions, users


async def _make_repo(tmp_path) -> tuple[Repository, AsyncEngine]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    await init_db(engine)
    return Repository(engine), engine


def _message(content: str = "hello", message_id: int = 1) -> MessageRecord:
    return MessageRecord(
        message_id=message_id,
        guild_id=10,
        channel_id=20,
        author_id=30,
        content=content,
        message_type=0,
        created_at="2026-11-11T00:00:00+00:00",
        captured_at="2026-11-11T00:00:00+00:00",
    )


def _attachment(message_id: int = 1, attachment_id: int = 555) -> AttachmentRecord:
    return AttachmentRecord(
        message_id=message_id,
        attachment_id=attachment_id,
        filename="a.png",
        content_type="image/png",
        size=12,
        url="https://cdn.example/a.png",
        storage_backend="local",
        status="pending",
        captured_at="2026-11-11T00:00:00+00:00",
    )


def _record_statements(engine: AsyncEngine) -> list[str]:
    """Collect every statement the engine sends to the database."""
    statements: list[str] = []

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _record(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        statements.append(statement)

    return statements


async def test_apply_batch_inserts_message(tmp_path):
    repo, engine = await _make_repo(tmp_path)
    await repo.apply_batch([WriteOp(OpKind.MESSAGE, _message())])
    async with engine.connect() as conn:
        rows = (await conn.execute(select(messages))).all()
    assert len(rows) == 1
    assert rows[0].content == "hello"


async def test_insert_only_does_not_overwrite_existing(tmp_path):
    repo, engine = await _make_repo(tmp_path)
    await repo.apply_batch([WriteOp(OpKind.MESSAGE, _message("live"))])
    await repo.apply_batch([WriteOp(OpKind.MESSAGE_INSERT_ONLY, _message("backfill"))])
    async with engine.connect() as conn:
        row = (
            await conn.execute(select(messages).where(messages.c.message_id == 1))
        ).one()
    assert row.content == "live"


async def test_realtime_upsert_updates_content_but_keeps_analysis_status(tmp_path):
    repo, engine = await _make_repo(tmp_path)
    await repo.apply_batch([WriteOp(OpKind.MESSAGE, _message("v1"))])
    updated = _message("v2")
    updated.analysis_status = "done"
    await repo.apply_batch([WriteOp(OpKind.MESSAGE, updated)])
    async with engine.connect() as conn:
        row = (
            await conn.execute(select(messages).where(messages.c.message_id == 1))
        ).one()
    assert row.content == "v2"
    assert row.analysis_status == "pending"


async def test_message_edit_increments_edit_count(tmp_path):
    repo, engine = await _make_repo(tmp_path)
    await repo.apply_batch([WriteOp(OpKind.MESSAGE, _message())])
    await repo.apply_batch(
        [
            WriteOp(OpKind.MESSAGE_EDIT, MessageEdit(1, "edited", "2026-11-11T01:00:00+00:00")),
            WriteOp(OpKind.MESSAGE_EDIT, MessageEdit(1, "edited2", "2026-11-11T02:00:00+00:00")),
        ]
    )
    async with engine.connect() as conn:
        row = (
            await conn.execute(select(messages).where(messages.c.message_id == 1))
        ).one()
    assert row.content == "edited2"
    assert row.edit_count == 2
    assert row.edited_at == "2026-11-11T02:00:00+00:00"


async def test_message_delete_is_soft(tmp_path):
    repo, engine = await _make_repo(tmp_path)
    await repo.apply_batch([WriteOp(OpKind.MESSAGE, _message("keep me"))])
    await repo.apply_batch(
        [WriteOp(OpKind.MESSAGE_DELETE, MessageDelete(1, "2026-11-11T03:00:00+00:00"))]
    )
    async with engine.connect() as conn:
        row = (
            await conn.execute(select(messages).where(messages.c.message_id == 1))
        ).one()
    assert row.deleted_at == "2026-11-11T03:00:00+00:00"
    assert row.content == "keep me"


async def test_reaction_upsert_clears_removed_at(tmp_path):
    repo, engine = await _make_repo(tmp_path)
    reaction = ReactionRecord(1, 30, "🔥", None, False, "2026-11-11T00:00:00+00:00")
    await repo.apply_batch([WriteOp(OpKind.REACTION, reaction)])
    removed = ReactionRecord(1, 30, "🔥", None, False, "2026-11-11T00:00:00+00:00")
    removed.removed_at = "2026-11-11T04:00:00+00:00"
    await repo.apply_batch([WriteOp(OpKind.REACTION, removed)])
    await repo.apply_batch(
        [WriteOp(OpKind.REACTION, reaction)]
    )
    async with engine.connect() as conn:
        rows = (await conn.execute(select(reactions))).all()
    assert len(rows) == 1
    assert rows[0].removed_at is None


async def test_user_snapshot_is_upserted(tmp_path):
    repo, engine = await _make_repo(tmp_path)
    snapshot = UserSnapshot(
        user_id=30,
        username="alice",
        global_name="Alice",
        display_name="alice#1",
        discriminator="0",
        avatar_url=None,
        is_bot=False,
        account_created_at=None,
        guild_joined_at=None,
        roles_json="[]",
        updated_at="2026-11-11T00:00:00+00:00",
    )
    await repo.apply_batch([WriteOp(OpKind.USER, snapshot)])
    renamed = UserSnapshot(**{**snapshot.__dict__, "username": "alice2"})
    await repo.apply_batch([WriteOp(OpKind.USER, renamed)])
    async with engine.connect() as conn:
        rows = (await conn.execute(select(users))).all()
    assert len(rows) == 1
    assert rows[0].username == "alice2"


async def test_attachment_insert_is_idempotent(tmp_path):
    repo, engine = await _make_repo(tmp_path)
    await repo.apply_batch([WriteOp(OpKind.ATTACHMENT, _attachment())])
    await repo.apply_batch([WriteOp(OpKind.ATTACHMENT, _attachment())])
    async with engine.connect() as conn:
        rows = (await conn.execute(select(attachments))).all()
    assert len(rows) == 1
    assert rows[0].url == "https://cdn.example/a.png"


async def test_analysis_status_op_updates_the_message(tmp_path):
    repo, engine = await _make_repo(tmp_path)
    await repo.apply_batch([WriteOp(OpKind.MESSAGE, _message())])
    await repo.apply_batch(
        [WriteOp(OpKind.ANALYSIS_STATUS, (1, "done"))]
    )
    async with engine.connect() as conn:
        row = (
            await conn.execute(select(messages).where(messages.c.message_id == 1))
        ).one()
    assert row.analysis_status == "done"


async def test_apply_batch_sends_one_statement_per_kind(tmp_path):
    repo, engine = await _make_repo(tmp_path)
    statements = _record_statements(engine)
    await repo.apply_batch(
        [
            WriteOp(OpKind.MESSAGE, _message(message_id=1)),
            WriteOp(OpKind.MESSAGE, _message(message_id=2)),
            WriteOp(OpKind.MESSAGE, _message(message_id=3)),
        ]
    )
    assert len(statements) == 1
    assert "INSERT INTO messages" in statements[0]


async def test_apply_batch_sends_one_statement_for_a_group_of_edits(tmp_path):
    repo, engine = await _make_repo(tmp_path)
    await repo.apply_batch([WriteOp(OpKind.MESSAGE, _message())])
    statements = _record_statements(engine)
    await repo.apply_batch(
        [
            WriteOp(OpKind.MESSAGE_EDIT, MessageEdit(1, "edited", "2026-11-11T01:00:00+00:00")),
            WriteOp(OpKind.MESSAGE_EDIT, MessageEdit(1, "edited2", "2026-11-11T02:00:00+00:00")),
        ]
    )
    assert len(statements) == 1
    assert "UPDATE messages" in statements[0]


async def test_create_engine_enables_wal_mode(tmp_path):
    engine = create_engine(
        DatabaseConfig(backend="sqlite", path=str(tmp_path / "wal.db"))
    )
    try:
        async with engine.connect() as conn:
            result = await conn.exec_driver_sql("PRAGMA journal_mode")
            mode = result.scalar()
    finally:
        await engine.dispose()
    assert mode == "wal"


async def test_create_engine_creates_the_parent_directory(tmp_path):
    target = tmp_path / "nested" / "dir" / "foresight.db"
    engine = create_engine(DatabaseConfig(backend="sqlite", path=str(target)))
    await engine.dispose()
    assert target.parent.is_dir()
