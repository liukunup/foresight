"""Reaction endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from foresight.api.deps import get_engine

# The pagination rules live with the router that established them, so every
# list endpoint of the API pages the same way.
from foresight.api.routers.messages import DEFAULT_LIMIT, MAX_LIMIT
from foresight.api.schemas import Page, ReactionOut
from foresight.storage.tables import reactions

router = APIRouter()


@router.get("/reactions", response_model=Page[ReactionOut])
async def list_reactions(
    message_id: int | None = None,
    only_active: bool = False,
    limit: int = Query(DEFAULT_LIMIT, ge=1),
    offset: int = Query(0, ge=0),
    engine: AsyncEngine = Depends(get_engine),
) -> Page[ReactionOut]:
    """List the reactions of the dump, the newest one first.

    A removed reaction stays in the table: it is the row that says the
    reaction was there and that it is gone. `only_active` hides those rows.
    """
    # A request for more than the maximum is clamped instead of rejected:
    # the caller asked for a page, not for an error.
    limit = min(limit, MAX_LIMIT)

    clauses = []
    if message_id is not None:
        clauses.append(reactions.c.message_id == message_id)
    if only_active:
        clauses.append(reactions.c.removed_at.is_(None))

    total_stmt = select(func.count()).select_from(reactions).where(*clauses)
    stmt = (
        select(reactions)
        .where(*clauses)
        # The row id breaks the tie between two reactions that were added
        # at the same time: without it the order of a page would depend on
        # the order the database returned the rows in.
        .order_by(reactions.c.added_at.desc(), reactions.c.id.desc())
        .limit(limit)
        .offset(offset)
    )
    async with engine.connect() as conn:
        total = int((await conn.execute(total_stmt)).scalar_one())
        rows = (await conn.execute(stmt)).all()
    items = [ReactionOut(**dict(row._mapping)) for row in rows]
    return Page[ReactionOut](
        total=total,
        limit=limit,
        offset=offset,
        has_more=offset + len(items) < total,
        items=items,
    )
