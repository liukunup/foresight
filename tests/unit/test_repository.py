"""Tests for the async engine and the batched repository.

The repository is the only component that talks to the database, so these
tests pin the two guarantees the rest of the service relies on: every write
is idempotent, and a batch of operations is applied as one transaction with
one statement per operation kind.
"""

from __future__ import annotations

import re
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


def _channel(name: str = "general", channel_id: int = 20) -> ChannelSnapshot:
    return ChannelSnapshot(
        channel_id=channel_id,
        guild_id=10,
        name=name,
        topic="chat",
        channel_type="text",
        position=1,
        captured_at="2026-11-11T00:00:00+00:00",
    )


def _user(username: str = "alice", user_id: int = 30) -> UserSnapshot:
    return UserSnapshot(
        user_id=user_id,
        username=username,
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


def _insert_rows(sql: str) -> int:
    """The number of rows a multi-row insert statement carries.

    The repository writes a group of one kind with a single statement, so
    the number of its rows is the number of the records of the group. A
    group that carries the same conflict key twice is a group PostgreSQL
    refuses — "ON CONFLICT DO UPDATE command cannot affect row a second
    time" — so the group must be reduced before the statement is built.
    """
    values = sql.split("VALUES", 1)[1]
    return values.count("), (") + 1


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


async def test_the_removal_of_a_record_does_not_take_away_a_live_row(
    repo_and_engine,
):
    """The removal of a reaction is written by its own operation.

    The record of a reaction describes an addition, so a record that is
    written again over a row that is alive must not take the row away.
    """
    repo, engine = repo_and_engine
    reaction = ReactionRecord(1, 30, "🔥", None, False, "2026-11-11T00:00:00+00:00")
    await repo.apply_batch([WriteOp(OpKind.REACTION, reaction)])
    removed = ReactionRecord(1, 30, "🔥", None, False, "2026-11-11T00:00:00+00:00")
    removed.removed_at = "2026-11-11T04:00:00+00:00"
    await repo.apply_batch([WriteOp(OpKind.REACTION, removed)])
    async with engine.connect() as conn:
        rows = (await conn.execute(select(reactions))).all()
    assert len(rows) == 1
    assert rows[0].removed_at is None


async def test_user_snapshot_is_upserted(repo_and_engine):
    repo, engine = repo_and_engine
    snapshot = _user()
    await repo.apply_batch([WriteOp(OpKind.USER, snapshot)])
    renamed = _user(username="alice2")
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


async def test_pending_attachments_are_the_ones_that_wait_for_their_download(
    repo_and_engine,
):
    """Only the attachments whose status is `pending` are still to be fetched.

    A download that never happened — because the worker was down, or
    because its status was lost — leaves the row in `pending`. The
    collector queues these attachments again when it starts.
    """
    repo, engine = repo_and_engine
    await repo.apply_batch([WriteOp(OpKind.ATTACHMENT, _attachment())])
    await repo.apply_batch(
        [
            WriteOp(
                OpKind.ATTACHMENT_STATUS,
                AttachmentStatus(1, 556, "stored", None, "abc", "key", "uri", None),
            )
        ]
    )

    pending = await repo.pending_attachments()

    assert [record.attachment_id for record in pending] == [555]
    assert pending[0].url == "https://cdn.example/a.png"


async def test_pending_attachments_can_be_bounded(repo_and_engine):
    """The queue of the media worker is bounded, so the list is too."""
    repo, engine = repo_and_engine
    await repo.apply_batch(
        [
            WriteOp(OpKind.ATTACHMENT, _attachment(attachment_id=index + 1))
            for index in range(3)
        ]
    )

    assert len(await repo.pending_attachments(limit=2)) == 2
    assert await repo.pending_attachments(limit=0) == []


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


async def test_mysql_insert_only_only_sets_the_last_insert_id_for_auto_increment_keys():
    """Only an auto-increment key may use the `LAST_INSERT_ID` idiom.

    `attachments.id` is an auto-increment surrogate the database allocates,
    so the no-op update reads `LAST_INSERT_ID(id)` and the connection's last
    insert id keeps pointing at the existing row. `messages.message_id` is a
    snowflake the application generates: `LAST_INSERT_ID(message_id)` would
    set the session's last insert id to a key the database never allocated,
    and every caller reading `cursor.lastrowid` would misread it. That table
    therefore gets a plain self-assignment, the true no-op.
    """
    attachment_sql = await _compiled_for(
        "mysql",
        lambda repo: repo._insert_only(attachments, [_ATTACHMENT_VALUES]),
    )
    message_sql = await _compiled_for(
        "mysql",
        lambda repo: repo._insert_only(messages, [_message_values()]),
    )
    assert "last_insert_id" in attachment_sql.lower()
    assert "last_insert_id" not in message_sql.lower()
    assert "message_id = messages.message_id" in message_sql
    set_clause = message_sql.lower().split("on duplicate key update", 1)[1]
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
    package_dir = Path(module_file).parent.parent
    offenders = [
        str(path.relative_to(package_dir))
        for path in sorted(package_dir.rglob("*.py"))
        if "insert_attachment" in path.read_text(encoding="utf-8")
    ]
    assert offenders == [], (
        "these modules still reference the removed insert_attachment: "
        + ", ".join(offenders)
    )


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
    assert "'not_a_kind'" in message


async def test_unknown_op_kind_names_the_full_valid_kind_set(repo_and_engine):
    """The error names the offending kind and every valid kind.

    A substring check would pass on a truncated list of valid kinds, so the
    kinds are read back as quoted tokens: the message must name the
    offending kind and exactly the kinds the repository accepts.
    """
    repo, _ = repo_and_engine
    with pytest.raises(ValueError) as excinfo:
        await repo.apply_batch(
            [
                WriteOp(OpKind.MESSAGE, _message()),
                WriteOp("not_a_kind", _message()),
            ]
        )
    message = str(excinfo.value)
    assert "'not_a_kind'" in message
    named_kinds = set(re.findall(r"'([^']*)'", message))
    assert named_kinds == {"not_a_kind", *OP_ORDER}


async def test_unknown_op_kind_writes_nothing_of_the_batch(repo_and_engine):
    """A batch with an unknown kind is rejected before anything is written."""
    repo, engine = repo_and_engine
    with pytest.raises(ValueError):
        await repo.apply_batch(
            [
                WriteOp(OpKind.MESSAGE, _message()),
                WriteOp(OpKind.USER, _user()),
                WriteOp("not_a_kind", _message()),
            ]
        )
    async with engine.connect() as conn:
        message_rows = (await conn.execute(select(messages))).all()
        user_rows = (await conn.execute(select(users))).all()
    assert message_rows == []
    assert user_rows == []


async def test_pending_messages_are_the_ones_that_wait_for_their_analysis(
    repo_and_engine,
):
    """Only the messages whose status is `pending` are still to be analysed."""
    repo, engine = repo_and_engine
    await repo.apply_batch(
        [
            WriteOp(OpKind.MESSAGE, _message(content="waiting", message_id=1)),
            WriteOp(OpKind.MESSAGE, _message(content="done", message_id=2)),
        ]
    )
    await repo.apply_batch([WriteOp(OpKind.ANALYSIS_STATUS, (2, "done"))])

    pending = await repo.pending_messages()

    assert pending == [(1, "waiting")]


async def test_pending_messages_are_the_oldest_first(repo_and_engine):
    """The oldest message waits the shortest time."""
    repo, engine = repo_and_engine
    for index, created in enumerate(
        ["2026-11-12T00:00:00+00:00", "2026-11-10T00:00:00+00:00"]
    ):
        record = _message(content=f"message {index}", message_id=index + 1)
        record.created_at = created
        await repo.apply_batch([WriteOp(OpKind.MESSAGE, record)])

    pending = await repo.pending_messages()

    assert [content for _, content in pending] == ["message 1", "message 0"]


async def test_pending_messages_can_be_bounded(repo_and_engine):
    """The queue of the analysis worker is bounded, so the list is too."""
    repo, engine = repo_and_engine
    for index in range(3):
        record = _message(content=f"message {index}", message_id=index + 1)
        record.created_at = f"2026-11-1{index}T00:00:00+00:00"
        await repo.apply_batch([WriteOp(OpKind.MESSAGE, record)])

    assert await repo.pending_messages(limit=2) == [
        (1, "message 0"),
        (2, "message 1"),
    ]
    assert await repo.pending_messages(limit=0) == []


async def test_pending_messages_can_be_bounded_by_time(repo_and_engine):
    """The lower bound is the creation of the message."""
    repo, engine = repo_and_engine
    for index, created in enumerate(
        ["2026-11-01T00:00:00+00:00", "2026-11-10T00:00:00+00:00"]
    ):
        record = _message(content=f"message {index}", message_id=index + 1)
        record.created_at = created
        await repo.apply_batch([WriteOp(OpKind.MESSAGE, record)])

    pending = await repo.pending_messages(since="2026-11-05T00:00:00+00:00")

    assert pending == [(2, "message 1")]


# -- a group that carries the same conflict key twice --------------------
#
# The collector writes a snapshot of the author, the channel and the guild
# with every message, so two messages of the same author inside one flush
# window carry the same key. PostgreSQL refuses such a statement and MySQL
# refuses the insert-only form of it, so the group is reduced to one row
# per key before the statement is built.


async def test_a_batch_with_two_users_of_the_same_id_keeps_the_last_one(
    repo_and_engine,
):
    repo, engine = repo_and_engine
    await repo.apply_batch(
        [
            WriteOp(OpKind.USER, _user("alice")),
            WriteOp(OpKind.USER, _user("alice2")),
        ]
    )
    async with engine.connect() as conn:
        rows = (await conn.execute(select(users))).all()
    assert len(rows) == 1
    assert rows[0].username == "alice2"


async def test_a_batch_with_two_channels_of_the_same_id_keeps_the_last_one(
    repo_and_engine,
):
    repo, engine = repo_and_engine
    await repo.apply_batch(
        [
            WriteOp(OpKind.CHANNEL, _channel("general")),
            WriteOp(OpKind.CHANNEL, _channel("general-v2")),
        ]
    )
    async with engine.connect() as conn:
        rows = (await conn.execute(select(channels))).all()
    assert len(rows) == 1
    assert rows[0].name == "general-v2"


async def test_a_batch_with_two_messages_of_the_same_id_keeps_the_last_one(
    repo_and_engine,
):
    repo, engine = repo_and_engine
    await repo.apply_batch(
        [
            WriteOp(OpKind.MESSAGE, _message("v1")),
            WriteOp(OpKind.MESSAGE, _message("v2")),
        ]
    )
    async with engine.connect() as conn:
        rows = (await conn.execute(select(messages))).all()
    assert len(rows) == 1
    assert rows[0].content == "v2"


async def test_a_group_with_the_same_conflict_key_is_written_as_one_row(
    repo_and_engine,
):
    """The statement of a group must carry one row per conflict key.

    PostgreSQL refuses a statement that would touch the same row twice,
    so the group is reduced before the statement is built.
    """
    repo, engine = repo_and_engine
    statements = _record_statements(engine)
    await repo.apply_batch(
        [
            WriteOp(OpKind.USER, _user("alice")),
            WriteOp(OpKind.USER, _user("alice2")),
        ]
    )
    assert len(statements) == 1
    assert _insert_rows(statements[0]) == 1


async def test_a_group_of_different_keys_is_still_written_as_one_statement(
    repo_and_engine,
):
    """The reduction must not split a group into one statement per row."""
    repo, engine = repo_and_engine
    statements = _record_statements(engine)
    await repo.apply_batch(
        [
            WriteOp(OpKind.USER, _user("alice")),
            WriteOp(OpKind.USER, _user("bob", user_id=31)),
        ]
    )
    assert len(statements) == 1
    assert _insert_rows(statements[0]) == 2


# -- the soft state survives a redelivery --------------------------------


async def test_the_realtime_upsert_keeps_the_deleted_at(repo_and_engine):
    """A message that was deleted stays deleted when it is written again.

    The collector writes the whole message again when the gateway
    redelivers it, and the record carries no deletion: the soft delete
    must survive it, or the deleted ratio stops being analysable.
    """
    repo, engine = repo_and_engine
    await repo.apply_batch([WriteOp(OpKind.MESSAGE, _message("keep me"))])
    await repo.apply_batch(
        [WriteOp(OpKind.MESSAGE_DELETE, MessageDelete(1, "2026-11-11T03:00:00+00:00"))]
    )
    await repo.apply_batch([WriteOp(OpKind.MESSAGE, _message("redelivered"))])
    async with engine.connect() as conn:
        row = (
            await conn.execute(select(messages).where(messages.c.message_id == 1))
        ).one()
    assert row.deleted_at == "2026-11-11T03:00:00+00:00"
    assert row.content == "redelivered"


async def test_the_realtime_upsert_keeps_the_edit_count(repo_and_engine):
    """The counter of the edits must not go down when a message is written."""
    repo, engine = repo_and_engine
    await repo.apply_batch([WriteOp(OpKind.MESSAGE, _message())])
    await repo.apply_batch(
        [WriteOp(OpKind.MESSAGE_EDIT, MessageEdit(1, "edited", "2026-11-11T01:00:00+00:00"))]
    )
    await repo.apply_batch([WriteOp(OpKind.MESSAGE, _message("redelivered"))])
    async with engine.connect() as conn:
        row = (
            await conn.execute(select(messages).where(messages.c.message_id == 1))
        ).one()
    assert row.edit_count == 1


async def test_the_realtime_upsert_keeps_the_edit_marker(repo_and_engine):
    """The marker of the last edit must survive a redelivery.

    The marker is what tells a replayed edit from a new one, so it must
    not be cleared by a message that carries none.
    """
    repo, engine = repo_and_engine
    await repo.apply_batch([WriteOp(OpKind.MESSAGE, _message())])
    await repo.apply_batch(
        [WriteOp(OpKind.MESSAGE_EDIT, MessageEdit(1, "edited", "2026-11-11T01:00:00+00:00"))]
    )
    await repo.apply_batch([WriteOp(OpKind.MESSAGE, _message("redelivered"))])
    async with engine.connect() as conn:
        row = (
            await conn.execute(select(messages).where(messages.c.message_id == 1))
        ).one()
    assert row.edited_at == "2026-11-11T01:00:00+00:00"


# -- a replayed write changes nothing ------------------------------------


async def test_a_replayed_edit_does_not_count_twice(repo_and_engine):
    """The failure replay can apply an edit that already reached the row.

    The counter of the edits is a relative value, so an edit that is
    applied twice counts twice. An edit that is not newer than the one
    the row already carries is a replay, and it changes nothing.
    """
    repo, engine = repo_and_engine
    await repo.apply_batch([WriteOp(OpKind.MESSAGE, _message())])
    edit = MessageEdit(1, "edited", "2026-11-11T01:00:00+00:00")
    await repo.apply_batch([WriteOp(OpKind.MESSAGE_EDIT, edit)])
    await repo.apply_batch([WriteOp(OpKind.MESSAGE_EDIT, edit)])
    async with engine.connect() as conn:
        row = (
            await conn.execute(select(messages).where(messages.c.message_id == 1))
        ).one()
    assert row.edit_count == 1


async def test_a_replayed_batch_of_edits_does_not_count_twice(repo_and_engine):
    """The whole batch is replayed, not only its last edit."""
    repo, engine = repo_and_engine
    await repo.apply_batch([WriteOp(OpKind.MESSAGE, _message())])
    edits = [
        WriteOp(OpKind.MESSAGE_EDIT, MessageEdit(1, "v1", "2026-11-11T01:00:00+00:00")),
        WriteOp(OpKind.MESSAGE_EDIT, MessageEdit(1, "v2", "2026-11-11T02:00:00+00:00")),
    ]
    await repo.apply_batch(edits)
    await repo.apply_batch(edits)
    async with engine.connect() as conn:
        row = (
            await conn.execute(select(messages).where(messages.c.message_id == 1))
        ).one()
    assert row.edit_count == 2
    assert row.content == "v2"
    assert row.edited_at == "2026-11-11T02:00:00+00:00"


async def test_a_replayed_edit_does_not_overwrite_a_newer_content(repo_and_engine):
    """An edit that is older than the recorded one must not win."""
    repo, engine = repo_and_engine
    await repo.apply_batch([WriteOp(OpKind.MESSAGE, _message())])
    await repo.apply_batch(
        [WriteOp(OpKind.MESSAGE_EDIT, MessageEdit(1, "newer", "2026-11-11T02:00:00+00:00"))]
    )
    await repo.apply_batch(
        [WriteOp(OpKind.MESSAGE_EDIT, MessageEdit(1, "older", "2026-11-11T01:00:00+00:00"))]
    )
    async with engine.connect() as conn:
        row = (
            await conn.execute(select(messages).where(messages.c.message_id == 1))
        ).one()
    assert row.content == "newer"
    assert row.edited_at == "2026-11-11T02:00:00+00:00"
    assert row.edit_count == 1


async def test_a_replayed_reaction_does_not_resurrect_it(repo_and_engine):
    """A reaction that was taken back must stay taken back."""
    repo, engine = repo_and_engine
    reaction = ReactionRecord(1, 30, "🔥", None, False, "2026-11-11T00:00:00+00:00")
    await repo.apply_batch([WriteOp(OpKind.REACTION, reaction)])
    await repo.apply_batch(
        [
            WriteOp(
                OpKind.REACTION_REMOVE,
                ReactionRemove(1, 30, "🔥", "2026-11-11T04:00:00+00:00"),
            )
        ]
    )
    await repo.apply_batch([WriteOp(OpKind.REACTION, reaction)])
    async with engine.connect() as conn:
        rows = (await conn.execute(select(reactions))).all()
    assert len(rows) == 1
    assert rows[0].removed_at == "2026-11-11T04:00:00+00:00"


# -- the life cycle of a reaction ----------------------------------------
#
# Spec §5.5: the removal of a reaction is a soft removal — the row stays
# and its `removed_at` is stamped. A reaction that is added again after
# the removal is alive again, so the stamp is cleared. A record that
# predates the removal is a replay of an old add: it must not revive the
# reaction.


async def test_a_reaction_is_written_alive(repo_and_engine):
    """A reaction that was just added carries no removal."""
    repo, engine = repo_and_engine
    await repo.apply_batch(
        [
            WriteOp(
                OpKind.REACTION,
                ReactionRecord(1, 30, "🔥", None, False, "2026-11-11T00:00:00+00:00"),
            )
        ]
    )
    async with engine.connect() as conn:
        row = (await conn.execute(select(reactions))).one()
    assert row.removed_at is None


async def test_a_re_add_brings_the_reaction_back(repo_and_engine):
    """A reaction that was taken back and added again is alive again.

    The row carries the time of the removal and the record carries the
    time of the add. An add that is newer than the removal is a genuine
    re-add, so the removal is cleared.
    """
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
                ReactionRemove(1, 30, "🔥", "2026-11-11T04:00:00+00:00"),
            )
        ]
    )
    await repo.apply_batch(
        [
            WriteOp(
                OpKind.REACTION,
                ReactionRecord(1, 30, "🔥", None, False, "2026-11-11T06:00:00+00:00"),
            )
        ]
    )
    async with engine.connect() as conn:
        row = (await conn.execute(select(reactions))).one()
    assert row.removed_at is None
    assert row.added_at == "2026-11-11T06:00:00+00:00"


async def test_a_stale_re_add_does_not_bring_the_reaction_back(repo_and_engine):
    """An add that predates the removal is a replay and revives nothing.

    The add is older than the removal, so it is the record of an add that
    was already taken back. The row keeps its removal.
    """
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
                ReactionRemove(1, 30, "🔥", "2026-11-11T04:00:00+00:00"),
            )
        ]
    )
    await repo.apply_batch(
        [
            WriteOp(
                OpKind.REACTION,
                ReactionRecord(1, 30, "🔥", None, False, "2026-11-11T00:00:00+00:00"),
            )
        ]
    )
    async with engine.connect() as conn:
        row = (await conn.execute(select(reactions))).one()
    assert row.removed_at == "2026-11-11T04:00:00+00:00"


# -- the dialect specific rendering --------------------------------------


async def test_mysql_insert_only_qualifies_the_column():
    """MySQL refuses an unqualified column in the no-op update.

    `INSERT INTO messages (...) VALUES (...) ON DUPLICATE KEY UPDATE
    message_id = message_id` is rejected with "Column 'message_id' in
    field list is ambiguous": the name stands for the row of the table and
    for the row that would have been inserted. The reference has to name
    the table.
    """
    sql = await _compiled_for(
        "mysql",
        lambda repo: repo._insert_only(messages, [_message_values()]),
    )
    set_clause = sql.lower().split("on duplicate key update", 1)[1]
    assert "message_id = messages.message_id" in set_clause
    assert "message_id = message_id" not in set_clause


async def test_mysql_insert_only_of_an_attachment_keeps_the_last_insert_id():
    """The surrogate key of the database may use the LAST_INSERT_ID idiom."""
    sql = await _compiled_for(
        "mysql",
        lambda repo: repo._insert_only(attachments, [_ATTACHMENT_VALUES]),
    )
    assert "last_insert_id" in sql.lower()


@pytest.mark.parametrize("dialect", ["sqlite", "postgresql", "mysql"])
async def test_the_realtime_upsert_keeps_the_soft_state(dialect):
    """The soft state of a message survives a redelivery on every backend.

    The conflict update keeps the deletion of the row and never lowers the
    counter of the edits.
    """
    sql = await _compiled_for(
        dialect,
        lambda repo: repo._message_upsert([_message_values()]),
    )
    assert "coalesce" in sql.lower()
    assert "deleted_at" in sql.lower()
    assert "edit_count" in sql.lower()
    # The counter never goes down: the larger of the two wins.
    assert "greatest" in sql.lower() or "max(" in sql.lower()


@pytest.mark.parametrize("dialect", ["sqlite", "postgresql", "mysql"])
async def test_the_reaction_upsert_compares_the_timestamps(dialect):
    """The removal is only cleared by an add that is newer than it.

    The conflict update compares the time of the incoming add with the
    time of the removal of the row, so the statement must name both of
    them on every backend.
    """
    sql = await _compiled_for(
        dialect,
        lambda repo: repo._reaction_upsert(
            [
                {
                    "message_id": 1,
                    "user_id": 30,
                    "emoji": "🔥",
                    "emoji_id": None,
                    "is_custom": False,
                    "added_at": "2026-11-11T00:00:00+00:00",
                    "removed_at": None,
                }
            ]
        ),
    )
    assert "case" in sql.lower()
    assert "removed_at" in sql.lower()
    assert "added_at" in sql.lower()
