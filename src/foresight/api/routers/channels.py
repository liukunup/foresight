"""Channel endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from foresight.api.deps import get_engine

# The pagination rules live with the router that established them, so every
# list endpoint of the API pages the same way.
from foresight.api.routers.messages import DEFAULT_LIMIT, MAX_LIMIT
from foresight.api.schemas import ChannelOut, Page
from foresight.storage.tables import channels, messages

router = APIRouter()


@router.get("/channels", response_model=Page[ChannelOut])
async def list_channels(
    guild_id: int | None = None,
    limit: int = Query(DEFAULT_LIMIT, ge=1),
    offset: int = Query(0, ge=0),
    engine: AsyncEngine = Depends(get_engine),
) -> Page[ChannelOut]:
    """List the channels of the dump, the busiest one first.

    A channel is a channel that carries at least one message: the counting
    happens over the messages table. The name of a channel is read in a
    second query, because joining the channels table into the grouped
    statement would change what the grouping is over.

    The guild of a channel is the guild of its messages, so it comes from
    the same table the endpoint filters over: a snapshot that disagrees
    with the messages must not make the response describe another guild
    than the one that was filtered.

    The envelope counts the groups and not the messages: `total` is the
    number of channels that match the filter and `items` holds one row per
    channel, however many messages that channel carries.
    """
    # A request for more than the maximum is clamped instead of rejected:
    # the caller asked for a page, not for an error.
    limit = min(limit, MAX_LIMIT)

    clauses = []
    if guild_id is not None:
        clauses.append(messages.c.guild_id == guild_id)

    total_stmt = (
        select(func.count(distinct(messages.c.channel_id)))
        .select_from(messages)
        .where(*clauses)
    )
    stmt = (
        select(
            messages.c.channel_id,
            # The guild of the messages, not the guild of the snapshot. A
            # channel belongs to one guild; when a dump carries messages
            # of two guilds for one channel, the larger id is reported, so
            # that the answer does not depend on the order of the rows.
            func.max(messages.c.guild_id).label("guild_id"),
            func.count().label("message_count"),
            func.max(messages.c.created_at).label("last_message_at"),
        )
        .where(*clauses)
        .group_by(messages.c.channel_id)
        # The channel id breaks the tie between two channels that carry the
        # same number of messages: without it the order of a page would
        # depend on the order the database returned the rows in.
        .order_by(func.count().desc(), messages.c.channel_id)
        .limit(limit)
        .offset(offset)
    )
    async with engine.connect() as conn:
        total = int((await conn.execute(total_stmt)).scalar_one())
        rows = (await conn.execute(stmt)).all()
        meta_by_channel = {}
        if rows:
            ids = [row.channel_id for row in rows]
            meta_rows = (
                await conn.execute(
                    select(
                        channels.c.channel_id,
                        channels.c.name,
                    ).where(channels.c.channel_id.in_(ids))
                )
            ).all()
            meta_by_channel = {row.channel_id: row for row in meta_rows}
    items = []
    for row in rows:
        meta = meta_by_channel.get(row.channel_id)
        items.append(
            ChannelOut(
                channel_id=row.channel_id,
                guild_id=row.guild_id,
                name=meta.name if meta is not None else None,
                message_count=int(row.message_count),
                last_message_at=row.last_message_at,
            )
        )
    return Page[ChannelOut](
        total=total,
        limit=limit,
        offset=offset,
        has_more=offset + len(items) < total,
        items=items,
    )
