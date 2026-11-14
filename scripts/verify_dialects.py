#!/usr/bin/env python3
"""Run the write paths of the repository against real database servers.

The test suite of the project runs on SQLite: it never contacts a
PostgreSQL or a MySQL server, so the dialect specific statements are
never executed there. This script runs the same write paths against the
servers that are given on the command line, so that the statements are
executed by the database they were written for.

    docker run -d --name foresight-pg -e POSTGRES_USER=foresight \\
        -e POSTGRES_PASSWORD=foresight -e POSTGRES_DB=foresight \\
        -p 15432:5432 postgres:16-alpine
    docker run -d --name foresight-mysql -e MYSQL_ROOT_PASSWORD=root \\
        -e MYSQL_DATABASE=foresight -e MYSQL_USER=foresight \\
        -e MYSQL_PASSWORD=foresight -p 13306:3306 mysql:8.0.46

    uv run python scripts/verify_dialects.py \\
        postgresql://foresight:foresight@127.0.0.1:15432/foresight \\
        mysql://foresight:foresight@127.0.0.1:13306/foresight

Every check is reported with its name, and the script exits with a
non-zero status when a check fails.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Callable

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine  # noqa: E402

from foresight.domain.models import (  # noqa: E402
    AttachmentRecord,
    AttachmentStatus,
    ChannelSnapshot,
    GuildSnapshot,
    MessageDelete,
    MessageEdit,
    MessageRecord,
    OpKind,
    ReactionRecord,
    ReactionRemove,
    UserSnapshot,
    WriteOp,
)
from foresight.storage.db import init_db  # noqa: E402
from foresight.storage.repository import Repository  # noqa: E402
from foresight.storage.tables import (  # noqa: E402
    attachments,
    channels,
    guilds,
    messages,
    reactions,
    users,
)

SQLITE_URL = "sqlite+aiosqlite:///:memory:"


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


def _guild(name: str = "foresight", guild_id: int = 10) -> GuildSnapshot:
    return GuildSnapshot(
        guild_id=guild_id,
        name=name,
        member_count=3,
        captured_at="2026-11-11T00:00:00+00:00",
    )


def _reaction(emoji: str = "🔥") -> ReactionRecord:
    return ReactionRecord(1, 30, emoji, None, False, "2026-11-11T00:00:00+00:00")


def _attachment(attachment_id: int = 555) -> AttachmentRecord:
    return AttachmentRecord(
        message_id=1,
        attachment_id=attachment_id,
        filename="a.png",
        content_type="image/png",
        size=12,
        url="https://cdn.example/a.png",
        storage_backend="local",
        status="pending",
        captured_at="2026-11-11T00:00:00+00:00",
    )


async def _count(conn: Any, table: Any) -> int:
    rows = (await conn.execute(select(table))).all()
    return len(rows)


async def check_two_users_of_the_same_id(repo: Repository, engine: AsyncEngine) -> None:
    await repo.apply_batch(
        [WriteOp(OpKind.USER, _user("alice")), WriteOp(OpKind.USER, _user("alice2"))]
    )
    async with engine.connect() as conn:
        rows = (await conn.execute(select(users))).all()
    assert len(rows) == 1, f"expected one row, got {len(rows)}"
    assert rows[0].username == "alice2", f"got {rows[0].username!r}"


async def check_two_channels_of_the_same_id(
    repo: Repository, engine: AsyncEngine
) -> None:
    await repo.apply_batch(
        [
            WriteOp(OpKind.CHANNEL, _channel("general")),
            WriteOp(OpKind.CHANNEL, _channel("general-v2")),
        ]
    )
    async with engine.connect() as conn:
        rows = (await conn.execute(select(channels))).all()
    assert len(rows) == 1, f"expected one row, got {len(rows)}"
    assert rows[0].name == "general-v2", f"got {rows[0].name!r}"


async def check_two_guilds_of_the_same_id(repo: Repository, engine: AsyncEngine) -> None:
    await repo.apply_batch(
        [
            WriteOp(OpKind.GUILD, _guild("foresight")),
            WriteOp(OpKind.GUILD, _guild("foresight-v2")),
        ]
    )
    async with engine.connect() as conn:
        rows = (await conn.execute(select(guilds))).all()
    assert len(rows) == 1, f"expected one row, got {len(rows)}"
    assert rows[0].name == "foresight-v2", f"got {rows[0].name!r}"


async def check_two_messages_of_the_same_id(
    repo: Repository, engine: AsyncEngine
) -> None:
    await repo.apply_batch(
        [
            WriteOp(OpKind.MESSAGE, _message("v1")),
            WriteOp(OpKind.MESSAGE, _message("v2")),
        ]
    )
    async with engine.connect() as conn:
        rows = (await conn.execute(select(messages))).all()
    assert len(rows) == 1, f"expected one row, got {len(rows)}"
    assert rows[0].content == "v2", f"got {rows[0].content!r}"


async def check_two_reactions_of_the_same_target(
    repo: Repository, engine: AsyncEngine
) -> None:
    await repo.apply_batch(
        [WriteOp(OpKind.REACTION, _reaction()), WriteOp(OpKind.REACTION, _reaction())]
    )
    async with engine.connect() as conn:
        rows = (await conn.execute(select(reactions))).all()
    assert len(rows) == 1, f"expected one row, got {len(rows)}"


async def check_two_attachments_of_the_same_target(
    repo: Repository, engine: AsyncEngine
) -> None:
    await repo.apply_batch(
        [
            WriteOp(OpKind.ATTACHMENT, _attachment()),
            WriteOp(OpKind.ATTACHMENT, _attachment()),
        ]
    )
    async with engine.connect() as conn:
        rows = (await conn.execute(select(attachments))).all()
    assert len(rows) == 1, f"expected one row, got {len(rows)}"


async def check_the_insert_only_path(repo: Repository, engine: AsyncEngine) -> None:
    """The insert-only path must not overwrite a row of the collector."""
    await repo.apply_batch([WriteOp(OpKind.MESSAGE, _message("live"))])
    await repo.apply_batch(
        [WriteOp(OpKind.MESSAGE_INSERT_ONLY, _message("backfill"))]
    )
    async with engine.connect() as conn:
        row = (
            await conn.execute(select(messages).where(messages.c.message_id == 1))
        ).one()
    assert row.content == "live", f"got {row.content!r}"


async def check_the_soft_state_survives_a_redelivery(
    repo: Repository, engine: AsyncEngine
) -> None:
    await repo.apply_batch([WriteOp(OpKind.MESSAGE, _message("keep me"))])
    await repo.apply_batch(
        [
            WriteOp(
                OpKind.MESSAGE_DELETE, MessageDelete(1, "2026-11-11T03:00:00+00:00")
            )
        ]
    )
    await repo.apply_batch(
        [
            WriteOp(
                OpKind.MESSAGE_EDIT, MessageEdit(1, "edited", "2026-11-11T01:00:00+00:00")
            )
        ]
    )
    await repo.apply_batch([WriteOp(OpKind.MESSAGE, _message("redelivered"))])
    async with engine.connect() as conn:
        row = (
            await conn.execute(select(messages).where(messages.c.message_id == 1))
        ).one()
    assert row.deleted_at == "2026-11-11T03:00:00+00:00", f"got {row.deleted_at!r}"
    assert row.edit_count == 1, f"got {row.edit_count!r}"


async def check_a_replayed_edit(repo: Repository, engine: AsyncEngine) -> None:
    await repo.apply_batch([WriteOp(OpKind.MESSAGE, _message())])
    edit = WriteOp(OpKind.MESSAGE_EDIT, MessageEdit(1, "edited", "2026-11-11T01:00:00+00:00"))
    await repo.apply_batch([edit])
    await repo.apply_batch([edit])
    async with engine.connect() as conn:
        row = (
            await conn.execute(select(messages).where(messages.c.message_id == 1))
        ).one()
    assert row.edit_count == 1, f"got {row.edit_count!r}"
    assert row.content == "edited", f"got {row.content!r}"


async def check_a_replayed_reaction(repo: Repository, engine: AsyncEngine) -> None:
    reaction = WriteOp(OpKind.REACTION, _reaction())
    await repo.apply_batch([reaction])
    await repo.apply_batch(
        [
            WriteOp(
                OpKind.REACTION_REMOVE,
                ReactionRemove(1, 30, "🔥", "2026-11-11T04:00:00+00:00"),
            )
        ]
    )
    await repo.apply_batch([reaction])
    async with engine.connect() as conn:
        row = (await conn.execute(select(reactions))).one()
    assert row.removed_at == "2026-11-11T04:00:00+00:00", f"got {row.removed_at!r}"


async def check_the_attachment_status(repo: Repository, engine: AsyncEngine) -> None:
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
    async with engine.connect() as conn:
        row = (
            await conn.execute(select(attachments).where(attachments.c.attachment_id == 555))
        ).one()
    assert row.status == "stored", f"got {row.status!r}"


CHECKS: list[tuple[str, Callable[[Repository, AsyncEngine], Any]]] = [
    ("two users of the same id", check_two_users_of_the_same_id),
    ("two channels of the same id", check_two_channels_of_the_same_id),
    ("two guilds of the same id", check_two_guilds_of_the_same_id),
    ("two messages of the same id", check_two_messages_of_the_same_id),
    ("two reactions of the same target", check_two_reactions_of_the_same_target),
    ("two attachments of the same target", check_two_attachments_of_the_same_target),
    ("the insert-only path", check_the_insert_only_path),
    ("the soft state survives a redelivery", check_the_soft_state_survives_a_redelivery),
    ("a replayed edit", check_a_replayed_edit),
    ("a replayed reaction", check_a_replayed_reaction),
    ("the attachment status", check_the_attachment_status),
]


async def run_backend(name: str, url: str) -> list[tuple[str, bool, str]]:
    """Run every check on one backend and report the outcome of each."""
    results: list[tuple[str, bool, str]] = []
    for check_name, check in CHECKS:
        engine = create_async_engine(url, future=True, pool_pre_ping=True)
        try:
            await init_db(engine)
            repo = Repository(engine)
            async with engine.begin() as conn:
                for table in (messages, users, channels, guilds, reactions, attachments):
                    await conn.execute(table.delete())
            await check(repo, engine)
            results.append((check_name, True, ""))
        except Exception as exc:  # noqa: BLE001
            results.append((check_name, False, f"{type(exc).__name__}: {exc}"))
        finally:
            await engine.dispose()
    return results


async def main(argv: list[str]) -> int:
    backends: list[tuple[str, str]] = [("sqlite", SQLITE_URL)]
    for url in argv:
        if url.startswith("postgresql"):
            backends.append(("postgresql", url.replace("postgresql://", "postgresql+asyncpg://", 1)))
        elif url.startswith("mysql"):
            backends.append(("mysql", url.replace("mysql://", "mysql+asyncmy://", 1)))
        else:
            print(f"unknown backend: {url}", file=sys.stderr)
            return 2
    failures = 0
    for name, url in backends:
        print(f"\n=== {name} ===")
        for check_name, ok, detail in await run_backend(name, url):
            if ok:
                print(f"  ok    {check_name}")
            else:
                failures += 1
                print(f"  FAIL  {check_name}: {detail}")
    print()
    if failures:
        print(f"{failures} check(s) failed")
        return 1
    print("every check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
