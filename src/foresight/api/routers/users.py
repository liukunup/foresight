"""Speaker endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from foresight.api.deps import get_engine

# The pagination rules live with the router that established them, so every
# list endpoint of the API pages the same way.
from foresight.api.routers.messages import DEFAULT_LIMIT, MAX_LIMIT
from foresight.api.schemas import Page, UserOut
from foresight.storage.tables import messages, users

router = APIRouter()


@router.get("/users", response_model=Page[UserOut])
async def list_users(
    channel_id: int | None = None,
    limit: int = Query(DEFAULT_LIMIT, ge=1),
    offset: int = Query(0, ge=0),
    engine: AsyncEngine = Depends(get_engine),
) -> Page[UserOut]:
    """List the speakers of the dump, the busiest one first.

    A speaker is an author that wrote at least one message: the counting
    happens over the messages table, so an author that never spoke is not
    listed. The usernames are read in a second query, because joining the
    users table into the grouped statement would change what the grouping
    is over.
    """
    # A request for more than the maximum is clamped instead of rejected:
    # the caller asked for a page, not for an error.
    limit = min(limit, MAX_LIMIT)

    clauses = []
    if channel_id is not None:
        clauses.append(messages.c.channel_id == channel_id)

    total_stmt = (
        select(func.count(distinct(messages.c.author_id)))
        .select_from(messages)
        .where(*clauses)
    )
    stmt = (
        select(
            messages.c.author_id,
            func.count().label("message_count"),
            func.min(messages.c.created_at).label("first_message_at"),
            func.max(messages.c.created_at).label("last_message_at"),
            func.avg(messages.c.char_count).label("avg_chars"),
        )
        .where(*clauses)
        .group_by(messages.c.author_id)
        # The author id breaks the tie between two authors that wrote the
        # same number of messages: without it the order of a page would
        # depend on the order the database returned the rows in.
        .order_by(func.count().desc(), messages.c.author_id)
        .limit(limit)
        .offset(offset)
    )
    async with engine.connect() as conn:
        total = int((await conn.execute(total_stmt)).scalar_one())
        rows = (await conn.execute(stmt)).all()
        names = {}
        if rows:
            ids = [row.author_id for row in rows]
            name_rows = (
                await conn.execute(
                    select(users.c.user_id, users.c.username).where(
                        users.c.user_id.in_(ids)
                    )
                )
            ).all()
            names = {row.user_id: row.username for row in name_rows}
    items = [
        UserOut(
            author_id=row.author_id,
            username=names.get(row.author_id),
            message_count=int(row.message_count),
            first_message_at=row.first_message_at,
            last_message_at=row.last_message_at,
            avg_chars=round(float(row.avg_chars or 0), 2),
        )
        for row in rows
    ]
    return Page[UserOut](
        total=total,
        limit=limit,
        offset=offset,
        has_more=offset + len(items) < total,
        items=items,
    )


@router.get("/users/{author_id}", response_model=UserOut)
async def get_user(
    author_id: int, engine: AsyncEngine = Depends(get_engine)
) -> UserOut:
    """Return one speaker, or 404 when the author wrote no message.

    The author is counted over the messages table, so an author that never
    spoke has no speaker to report.
    """
    stmt = (
        select(
            messages.c.author_id,
            func.count().label("message_count"),
            func.min(messages.c.created_at).label("first_message_at"),
            func.max(messages.c.created_at).label("last_message_at"),
            func.avg(messages.c.char_count).label("avg_chars"),
        )
        .where(messages.c.author_id == author_id)
        .group_by(messages.c.author_id)
    )
    async with engine.connect() as conn:
        row = (await conn.execute(stmt)).first()
        if row is None:
            raise HTTPException(status_code=404, detail="user not found")
        username = (
            await conn.execute(
                select(users.c.username).where(users.c.user_id == author_id)
            )
        ).first()
    return UserOut(
        author_id=row.author_id,
        username=username[0] if username is not None else None,
        message_count=int(row.message_count),
        first_message_at=row.first_message_at,
        last_message_at=row.last_message_at,
        avg_chars=round(float(row.avg_chars or 0), 2),
    )
