"""Health endpoint."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from foresight.api.deps import get_engine
from foresight.api.metrics import db_attachments_total, db_messages_total
from foresight.api.schemas import HealthOut
from foresight.storage.tables import attachments, messages

router = APIRouter()


async def _counts(conn: AsyncConnection) -> tuple[int, int]:
    """The number of messages and the number of attachments in the dump."""
    total = int(
        (await conn.execute(select(func.count()).select_from(messages))).scalar_one()
    )
    media = int(
        (await conn.execute(select(func.count()).select_from(attachments))).scalar_one()
    )
    return total, media


async def publish_counts(engine: AsyncEngine) -> tuple[int, int]:
    """Read the counts of the dump and publish them as the gauges.

    The gauges are published when the metrics are scraped and not when
    the health endpoint is called: the health endpoint sits behind the
    bearer token, so a probe that has no token would never reach it and
    the gauges would stay at zero while the dump holds rows.
    """
    async with engine.connect() as conn:
        total, media = await _counts(conn)
    db_messages_total.set(total)
    db_attachments_total.set(media)
    return total, media


def _database_size_mb(url: URL) -> float:
    """The size of the database file, in megabytes.

    Only a file-backed database has a file to measure. On PostgreSQL and
    on MySQL the name of the database is the name of the database on the
    server — not a path — and the dump is spread over the files of the
    server, so there is no single file to measure and the size of the
    dump is reported as zero.
    """
    if url.get_backend_name() != "sqlite":
        return 0.0
    raw = url.database
    if not raw:
        return 0.0
    if raw.startswith("file:"):
        # A read-only SQLite connection is opened through a `file:` URI,
        # so the path of the database file carries that prefix — and the
        # path is escaped once more, because the driver unescapes it.
        raw = unquote(raw.removeprefix("file:"))
    db_path = Path(raw)
    if not db_path.exists():
        return 0.0
    return round(db_path.stat().st_size / (1024 * 1024), 3)


@router.get("/health", response_model=HealthOut)
async def health(engine: AsyncEngine = Depends(get_engine)) -> HealthOut:
    """Report the state of the dump.

    The counts come from the database itself, so the endpoint answers the
    question the caller really has: how much data has been dumped and when
    was the last row written.

    The size of the database is only a number for SQLite, the one backend
    that stores the dump in a file: PostgreSQL and MySQL spread the dump
    over the files of the server, so the size is reported as zero.
    """
    async with engine.connect() as conn:
        total, media = await _counts(conn)
        # `captured_at` is the moment the row was written, which is what a
        # health check reports: the newest message creation time would say
        # nothing about whether the collector still runs.
        last = (
            await conn.execute(
                select(messages.c.captured_at)
                .order_by(messages.c.captured_at.desc())
                .limit(1)
            )
        ).first()
    return HealthOut(
        status="ok",
        total_messages=total,
        total_attachments=media,
        db_size_mb=_database_size_mb(engine.url),
        last_captured_at=last[0] if last else None,
    )
