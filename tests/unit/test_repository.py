"""Tests for the async engine and the batched repository.

The repository is the only component that talks to the database, so these
tests pin the two guarantees the rest of the service relies on: every write
is idempotent, and a batch of operations is applied as one transaction with
one statement per operation kind.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event, select
from sqlalchemy.dialects import mysql, postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

import foresight.storage.repository as repository_module
from foresight.config import DatabaseConfig
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
from foresight.storage.db import create_engine, init_db
from foresight.storage.repository import OP_ORDER, Repository
from foresight.storage.tables import (
    attachments,
    channels,
    guilds,
    message_analysis,
    messages,
    reactions,
    users,
)

# The three backends the project supports. The engines are only used to read
# `engine.dialect.name`; no server is ever contacted.
_DIALECT_URLS = {
    "sqlite": "sqlite+aiosqlite://",
    "postgresql": "postgresql+asyncpg://user:pass@localhost/foresight",
    "mysql": "mysql+asyncmy://user:pass@localhost/foresight",
}

_SQLALCHEMY_DIALECTS = {
    "sqlite": sqlite.dialect,
    "postgresql": postgresql.dialect,
    "mysql": mysql.dialect,
}


@pytest.fixture
async def repo_and_engine(tmp_path) -> AsyncIterator[tuple[Repository, AsyncEngine]]:
    """A repository on a fresh SQLite database, disposed after the test.

    The engine must be disposed: an aiosqlite connection keeps a worker
    thread alive, and a thread that outlives the event loop of its test
    reports an error on the closed loop.
    """
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    await init_db(engine)
    try:
        yield Repository(engine), engine
    finally:
        await engine.dispose()


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
    def _record(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        statements.append(statement)

    return statements


async def _compiled_for(
    dialect: str, build: Callable[[Repository], Any]
) -> str:
    """Compile a statement for `dialect` without contacting a server."""
    engine = create_async_engine(_DIALECT_URLS[dialect])
    try:
        repository = Repository(engine)
        statement = build(repository)
        return str(statement.compile(dialect=_SQLALCHEMY_DIALECTS[dialect]()))
    finally:
        await engine.dispose()


def _message_values() -> dict[str, Any]:
    """The values the repository writes for one message."""
    return Repository._message_values(_message())


_ATTACHMENT_VALUES = {
    "message_id": 1,
    "attachment_id": 555,
    "filename": "a.png",
    "content_type": "image/png",
    "size": 12,
    "url": "https://cdn.example/a.png",
    "storage_backend": "local",
    "status": "pending",
    "captured_at": "2026-11-11T00:00:00+00:00",
}


async def test_apply_batch_inserts_message(repo_and_engine):
    repo, engine = repo_and_engine
    await repo.apply_batch([WriteOp(OpKind.MESSAGE, _message())])
    async with engine.connect() as conn:
        rows = (await conn.execute(select(messages))).all()
    assert len(rows) == 1
    assert rows[0].content == "hello"


async def test_insert_only_does_not_overwrite_existing(repo_and_engine):
    repo, engine = repo_and_engine
    await repo.apply_batch([WriteOp(OpKind.MESSAGE, _message("live"))])
    await repo.apply_batch([WriteOp(OpKind.MESSAGE_INSERT_ONLY, _message("backfill"))])
    async with engine.connect() as conn:
        row = (
            await conn.execute(select(messages).where(messages.c.message_id == 1))
        ).one()
    assert row.content == "live"


async def test_realtime_upsert_updates_content_but_keeps_analysis_status(repo_and_engine):
    repo, engine = repo_and_engine
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


async def test_message_edit_increments_edit_count(repo_and_engine):
    repo, engine = repo_and_engine
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


async def test_message_delete_is_soft(repo_and_engine):
    repo, engine = repo_and_engine
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


async def test_reaction_upsert_clears_removed_at(repo_and_engine):
    repo, engine = repo_and_engine
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


async def test_user_snapshot_is_upserted(repo_and_engine):
    repo, engine = repo_and_engine
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


async def test_attachment_insert_is_idempotent(repo_and_engine):
    repo, engine = repo_and_engine
    await repo.apply_batch([WriteOp(OpKind.ATTACHMENT, _attachment())])
    await repo.apply_batch([WriteOp(OpKind.ATTACHMENT, _attachment())])
    async with engine.connect() as conn:
        rows = (await conn.execute(select(attachments))).all()
    assert len(rows) == 1
    assert rows[0].url == "https://cdn.example/a.png"


async def test_analysis_status_op_updates_the_message(repo_and_engine):
    repo, engine = repo_and_engine
    await repo.apply_batch([WriteOp(OpKind.MESSAGE, _message())])
    await repo.apply_batch(
        [WriteOp(OpKind.ANALYSIS_STATUS, (1, "done"))]
    )
    async with engine.connect() as conn:
        row = (
            await conn.execute(select(messages).where(messages.c.message_id == 1))
        ).one()
    assert row.analysis_status == "done"


async def test_apply_batch_sends_one_statement_per_kind(repo_and_engine):
    repo, engine = repo_and_engine
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


async def test_apply_batch_sends_one_statement_for_a_group_of_edits(repo_and_engine):
    repo, engine = repo_and_engine
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


# -- dialect rendering ------------------------------------------------


async def test_upsert_renders_on_conflict_on_sqlite():
    sql = await _compiled_for(
        "sqlite",
        lambda repo: repo._upsert(
            messages, [_message_values()], ["message_id"], ("content",)
        ),
    )
    assert "ON CONFLICT (message_id) DO UPDATE" in sql


async def test_upsert_renders_on_conflict_on_postgresql():
    sql = await _compiled_for(
        "postgresql",
        lambda repo: repo._upsert(
            messages, [_message_values()], ["message_id"], ("content",)
        ),
    )
    assert "ON CONFLICT (message_id) DO UPDATE" in sql


async def test_upsert_renders_on_duplicate_key_on_mysql():
    sql = await _compiled_for(
        "mysql",
        lambda repo: repo._upsert(
            messages, [_message_values()], ["message_id"], ("content",)
        ),
    )
    assert "ON DUPLICATE KEY UPDATE" in sql


async def test_postgresql_upsert_reads_the_excluded_row():
    sql = await _compiled_for(
        "postgresql",
        lambda repo: repo._upsert(
            messages, [_message_values()], ["message_id"], ("content",)
        ),
    )
    assert "excluded.content" in sql
    assert "inserted.content" not in sql


async def test_insert_only_renders_do_nothing_on_sqlite():
    sql = await _compiled_for(
        "sqlite",
        lambda repo: repo._insert_only(messages, [_message_values()]),
    )
    assert "ON CONFLICT DO NOTHING" in sql


async def test_insert_only_renders_do_nothing_on_postgresql():
    sql = await _compiled_for(
        "postgresql",
        lambda repo: repo._insert_only(messages, [_message_values()]),
    )
    assert "ON CONFLICT DO NOTHING" in sql


async def test_mysql_insert_only_is_a_no_op():
    sql = await _compiled_for(
        "mysql",
        lambda repo: repo._insert_only(attachments, [_ATTACHMENT_VALUES]),
    )
    assert "last_insert_id" in sql.lower()
    assert "values(id)" not in sql.lower()
    set_clause = sql.lower().split("on duplicate key update", 1)[1]
    assert "values(" not in set_clause


# -- handler coverage -------------------------------------------------


async def test_reaction_remove_sets_removed_at_and_keeps_the_row(repo_and_engine):
    repo, engine = repo_and_engine
    await repo.apply_batch(
        [
            WriteOp(
                OpKind.REACTION,
                ReactionRecord(1, 30, "🔥", None, False, "2026-11-11T00:00:00+00:00"),
            )
        ]
    )
    await repo.apply_batch(
        [
            WriteOp(
                OpKind.REACTION_REMOVE,
                ReactionRemove(1, 30, "🔥", "2026-11-11T05:00:00+00:00"),
            )
        ]
    )
    async with engine.connect() as conn:
        rows = (await conn.execute(select(reactions))).all()
    assert len(rows) == 1
    assert rows[0].removed_at == "2026-11-11T05:00:00+00:00"


async def test_attachment_status_updates_only_the_matching_row(repo_and_engine):
    repo, engine = repo_and_engine
    await repo.apply_batch(
        [
            WriteOp(OpKind.ATTACHMENT, _attachment(message_id=1, attachment_id=555)),
            WriteOp(OpKind.ATTACHMENT, _attachment(message_id=2, attachment_id=556)),
        ]
    )
    await repo.apply_batch(
        [
            WriteOp(
                OpKind.ATTACHMENT_STATUS,
                AttachmentStatus(
                    1,
                    555,
                    "stored",
                    None,
                    "abc",
                    "key",
                    "uri",
                    "2026-11-11T06:00:00+00:00",
                ),
            )
        ]
    )
    async with engine.connect() as conn:
        rows = (await conn.execute(select(attachments))).all()
    by_attachment_id = {row.attachment_id: row for row in rows}
    assert by_attachment_id[555].status == "stored"
    assert by_attachment_id[555].sha256 == "abc"
    assert by_attachment_id[556].status == "pending"
    assert by_attachment_id[556].sha256 is None


async def test_attachment_status_is_a_complete_snapshot(repo_and_engine):
    repo, engine = repo_and_engine
    await repo.apply_batch([WriteOp(OpKind.ATTACHMENT, _attachment())])
    await repo.apply_batch(
        [
            WriteOp(
                OpKind.ATTACHMENT_STATUS,
                AttachmentStatus(
                    1,
                    555,
                    "stored",
                    None,
                    "abc",
                    "key",
                    "uri",
                    "2026-11-11T06:00:00+00:00",
                ),
            )
        ]
    )
    await repo.apply_batch(
        [WriteOp(OpKind.ATTACHMENT_STATUS, AttachmentStatus(1, 555, "skipped"))]
    )
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                select(attachments).where(attachments.c.attachment_id == 555)
            )
        ).one()
    assert row.status == "skipped"
    assert row.sha256 is None
    assert row.storage_key is None
    assert row.storage_uri is None
    assert row.stored_at is None


async def test_channel_snapshot_is_upserted(repo_and_engine):
    repo, engine = repo_and_engine
    snapshot = ChannelSnapshot(
        channel_id=20,
        guild_id=10,
        name="general",
        topic="chat",
        channel_type="text",
        position=1,
        captured_at="2026-11-11T00:00:00+00:00",
    )
    await repo.apply_batch([WriteOp(OpKind.CHANNEL, snapshot)])
    renamed = ChannelSnapshot(
        channel_id=20,
        guild_id=10,
        name="general-v2",
        topic="chat",
        channel_type="text",
        position=1,
        captured_at="2026-11-11T00:00:00+00:00",
    )
    await repo.apply_batch([WriteOp(OpKind.CHANNEL, renamed)])
    async with engine.connect() as conn:
        rows = (await conn.execute(select(channels))).all()
    assert len(rows) == 1
    assert rows[0].name == "general-v2"


async def test_guild_snapshot_is_upserted(repo_and_engine):
    repo, engine = repo_and_engine
    snapshot = GuildSnapshot(
        guild_id=10,
        name="foresight",
        member_count=3,
        captured_at="2026-11-11T00:00:00+00:00",
    )
    await repo.apply_batch([WriteOp(OpKind.GUILD, snapshot)])
    bigger = GuildSnapshot(
        guild_id=10,
        name="foresight",
        member_count=4,
        captured_at="2026-11-11T00:00:00+00:00",
    )
    await repo.apply_batch([WriteOp(OpKind.GUILD, bigger)])
    async with engine.connect() as conn:
        rows = (await conn.execute(select(guilds))).all()
    assert len(rows) == 1
    assert rows[0].member_count == 4


async def test_analysis_upserts_on_message_id(repo_and_engine):
    repo, engine = repo_and_engine
    await repo.apply_batch(
        [
            WriteOp(
                OpKind.ANALYSIS,
                MessageAnalysis(
                    1,
                    "translated",
                    "positive",
                    0.9,
                    "[]",
                    "model",
                    "v1",
                    10,
                    None,
                    "2026-11-11T00:00:00+00:00",
                ),
            )
        ]
    )
    await repo.apply_batch(
        [
            WriteOp(
                OpKind.ANALYSIS,
                MessageAnalysis(
                    1,
                    "translated again",
                    "negative",
                    0.1,
                    "[]",
                    "model",
                    "v1",
                    20,
                    None,
                    "2026-11-11T01:00:00+00:00",
                ),
            )
        ]
    )
    async with engine.connect() as conn:
        rows = (await conn.execute(select(message_analysis))).all()
    assert len(rows) == 1
    assert rows[0].translated_text == "translated again"
    assert rows[0].sentiment == "negative"


# -- repository guarantees --------------------------------------------


async def test_attachment_insert_is_idempotent_within_one_batch(repo_and_engine):
    repo, engine = repo_and_engine
    await repo.apply_batch(
        [
            WriteOp(OpKind.ATTACHMENT, _attachment()),
            WriteOp(OpKind.ATTACHMENT, _attachment()),
        ]
    )
    async with engine.connect() as conn:
        rows = (await conn.execute(select(attachments))).all()
    assert len(rows) == 1
    assert rows[0].url == "https://cdn.example/a.png"


def test_repository_has_no_insert_attachment():
    assert not hasattr(Repository, "insert_attachment")


def test_op_order_covers_every_kind():
    kinds = {
        value for name, value in vars(OpKind).items() if not name.startswith("_")
    }
    assert set(OP_ORDER) == kinds


def test_no_module_references_insert_attachment():
    module_file = repository_module.__file__
    assert module_file is not None
    package_dir = Path(module_file).parent
    offenders = [
        path.name
        for path in sorted(package_dir.rglob("*.py"))
        if "insert_attachment" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


async def test_realtime_upsert_keeps_the_char_count(repo_and_engine):
    repo, engine = repo_and_engine
    original = _message("v1")
    original.char_count = 5
    await repo.apply_batch([WriteOp(OpKind.MESSAGE, original)])
    changed = _message("v2")
    changed.char_count = 99
    await repo.apply_batch([WriteOp(OpKind.MESSAGE, changed)])
    async with engine.connect() as conn:
        row = (
            await conn.execute(select(messages).where(messages.c.message_id == 1))
        ).one()
    assert row.content == "v2"
    assert row.char_count == 5


async def test_apply_batch_applies_an_edit_after_the_message(repo_and_engine):
    repo, engine = repo_and_engine
    await repo.apply_batch(
        [
            WriteOp(
                OpKind.MESSAGE_EDIT,
                MessageEdit(1, "edited", "2026-11-11T01:00:00+00:00"),
            ),
            WriteOp(OpKind.MESSAGE, _message("original")),
        ]
    )
    async with engine.connect() as conn:
        row = (
            await conn.execute(select(messages).where(messages.c.message_id == 1))
        ).one()
    assert row.content == "edited"
    assert row.edit_count == 1


async def test_unknown_op_kind_raises_value_error(repo_and_engine):
    repo, _ = repo_and_engine
    with pytest.raises(ValueError) as excinfo:
        await repo.apply_batch([WriteOp("not_a_kind", _message())])
    message = str(excinfo.value)
    assert "not_a_kind" in message
    assert OpKind.MESSAGE in message

