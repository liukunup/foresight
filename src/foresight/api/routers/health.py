"""Health endpoint."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from foresight.api.deps import get_engine, require_token
from foresight.api.schemas import HealthOut
from foresight.storage.tables import attachments, messages

router = APIRouter()


@router.get("/health", response_model=HealthOut, dependencies=[Depends(require_token)])
async def health(engine: AsyncEngine = Depends(get_engine)) -> HealthOut:
    """Report the state of the dump.

    The counts come from the database itself, so the endpoint answers the
    question the caller really has: how much data has been dumped and when
    was the last row written.
    """
    async with engine.connect() as conn:
        total = int(
            (await conn.execute(select(func.count()).select_from(messages))).scalar_one()
        )
        media = int(
            (await conn.execute(select(func.count()).select_from(attachments))).scalar_one()
        )
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
