"""Message read endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from foresight.api.deps import get_engine, require_token
from foresight.api.schemas import AnalysisOut, MessageDetail, MessageOut, Page
from foresight.storage.tables import message_analysis, messages

router = APIRouter()

MAX_LIMIT = 500
DEFAULT_LIMIT = 50


@router.get(
    "/messages", response_model=Page[MessageOut], dependencies=[Depends(require_token)]
)
async def list_messages(
    channel_id: int | None = None,
    author_id: int | None = None,
    guild_id: int | None = None,
    since: str | None = None,
    until: str | None = None,
    q: str | None = None,
    min_len: int | None = None,
    has_attachment: bool | None = None,
    deleted: bool | None = None,
    edited: bool | None = None,
    order: str = Query("desc", pattern="^(asc|desc)$"),
    limit: int = Query(DEFAULT_LIMIT, ge=1),
    offset: int = Query(0, ge=0),
    engine: AsyncEngine = Depends(get_engine),
) -> Page[MessageOut]:
    """List the dumped messages, newest first unless `order=asc`.

    The timestamps are stored as fixed-width UTC ISO-8601 strings, so the
    `since`/`until` bounds are compared as strings and the interval is
    closed on both ends.
    """
    # A request for more than the maximum is clamped instead of rejected:
    # the caller asked for a page, not for an error.
    limit = min(limit, MAX_LIMIT)

    clauses = []
    if channel_id is not None:
        clauses.append(messages.c.channel_id == channel_id)
    if author_id is not None:
        clauses.append(messages.c.author_id == author_id)
    if guild_id is not None:
        clauses.append(messages.c.guild_id == guild_id)
    if since:
        clauses.append(messages.c.created_at >= since)
    if until:
        clauses.append(messages.c.created_at <= until)
    if min_len is not None:
        clauses.append(messages.c.char_count >= min_len)
    if has_attachment is True:
        clauses.append(messages.c.attachment_count > 0)
    if has_attachment is False:
        clauses.append(messages.c.attachment_count == 0)
    if deleted is True:
        clauses.append(messages.c.deleted_at.is_not(None))
    if deleted is False:
        clauses.append(messages.c.deleted_at.is_(None))
    if edited is True:
        clauses.append(messages.c.edited_at.is_not(None))
    if edited is False:
        clauses.append(messages.c.edited_at.is_(None))
    if q:
        # The pattern is lowercased in Python and the column is lowercased
        # in SQL: the match is case insensitive and behaves the same on
        # SQLite, PostgreSQL and MySQL.
        clauses.append(func.lower(messages.c.content).like(f"%{q.lower()}%"))

    total_stmt = select(func.count()).select_from(messages).where(*clauses)
    # The message id breaks the tie between two messages that share a
    # timestamp: without it a page could show the same row twice or skip
    # one.
    if order == "asc":
        order_column = messages.c.created_at.asc()
        tie_breaker = messages.c.message_id.asc()
    else:
        order_column = messages.c.created_at.desc()
        tie_breaker = messages.c.message_id.desc()
    data_stmt = (
        select(messages)
        .where(*clauses)
        .order_by(order_column, tie_breaker)
        .limit(limit)
        .offset(offset)
    )
    async with engine.connect() as conn:
        total = int((await conn.execute(total_stmt)).scalar_one())
        rows = (await conn.execute(data_stmt)).all()
    items = [MessageOut(**dict(row._mapping)) for row in rows]
    return Page[MessageOut](
        total=total,
        limit=limit,
        offset=offset,
        has_more=offset + len(items) < total,
        items=items,
    )


@router.get(
    "/messages/{message_id}",
    response_model=MessageDetail,
    dependencies=[Depends(require_token)],
)
async def get_message(
    message_id: int, engine: AsyncEngine = Depends(get_engine)
) -> MessageDetail:
    """Return one message together with its AI analysis.

    The analysis lives in its own table, so it is read separately: a
    message that was never analyzed simply has no analysis.
    """
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                select(messages).where(messages.c.message_id == message_id)
            )
        ).first()
        analysis = (
            await conn.execute(
                select(message_analysis).where(
                    message_analysis.c.message_id == message_id
                )
            )
        ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="message not found")
    payload = dict(row._mapping)
    payload["analysis"] = (
        AnalysisOut(**dict(analysis._mapping)) if analysis is not None else None
    )
    return MessageDetail(**payload)
