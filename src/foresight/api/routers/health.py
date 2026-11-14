"""Health endpoint."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
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


@router.get("/health", response_model=HealthOut)
async def health(engine: AsyncEngine = Depends(get_engine)) -> HealthOut:
    """Report the state of the dump.

    The counts come from the database itself, so the endpoint answers the
    question the caller really has: how much data has been dumped and when
    was the last row written.
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
    size_mb = 0.0
    path = getattr(engine.url, "database", None)
    if path:
        # A read-only SQLite connection is opened through a `file:` URI, so
        # the path of the database file carries that prefix.
        raw = str(path)
        if raw.startswith("file:"):
            raw = raw[len("file:") :]
        db_path = Path(raw)
        if db_path.exists():
            size_mb = round(db_path.stat().st_size / (1024 * 1024), 3)
    return HealthOut(
        status="ok",
        total_messages=total,
        total_attachments=media,
        db_size_mb=size_mb,
        last_captured_at=last[0] if last else None,
    )
