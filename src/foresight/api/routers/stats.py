"""Analytics endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from foresight.api.deps import get_stats

# The pagination rules live with the router that established them, so every
# list endpoint of the API pages the same way.
from foresight.api.routers.messages import DEFAULT_LIMIT, MAX_LIMIT
from foresight.storage.stats import StatsService

router = APIRouter()


@router.get("/stats/overview")
async def stats_overview(
    since: str | None = None,
    until: str | None = None,
    channel_id: int | None = None,
    stats: StatsService = Depends(get_stats),
) -> dict:
    """Report the totals of the dump.

    The overview is a summary and not a list: it answers with the counters
    themselves instead of with an envelope.
    """
    return await stats.overview(since=since, until=until, channel_id=channel_id)


@router.get("/stats/top-talkers")
async def stats_top_talkers(
    since: str | None = None,
    until: str | None = None,
    channel_id: int | None = None,
    limit: int = Query(DEFAULT_LIMIT, ge=1),
    stats: StatsService = Depends(get_stats),
) -> dict:
    """Rank the authors by the number of messages they wrote."""
    # A request for more than the maximum is clamped instead of rejected:
    # the caller asked for a page, not for an error.
    limit = min(limit, MAX_LIMIT)
    rows = await stats.top_talkers(
        since=since, until=until, channel_id=channel_id, limit=limit
    )
    return {
        "total": len(rows),
        "limit": limit,
        "offset": 0,
        "has_more": False,
        "items": rows,
    }


@router.get("/stats/activity")
async def stats_activity(
    since: str | None = None,
    until: str | None = None,
    channel_id: int | None = None,
    bucket: str = Query("day", pattern="^(hour|day)$"),
    stats: StatsService = Depends(get_stats),
) -> dict:
    """Report how many messages were written per hour or per day.

    An unknown bucket is rejected: a bucket the API does not know would be
    answered with a day bucket the caller did not ask for.
    """
    rows = await stats.activity(
        since=since, until=until, bucket=bucket, channel_id=channel_id
    )
    return {
        "total": len(rows),
        "limit": len(rows),
        "offset": 0,
        "has_more": False,
        "items": rows,
    }


@router.get("/stats/topics")
async def stats_topics(
    since: str | None = None,
    until: str | None = None,
    channel_id: int | None = None,
    limit: int = Query(DEFAULT_LIMIT, ge=1),
    stats: StatsService = Depends(get_stats),
) -> dict:
    """Report the topics of the dump.

    The topics are the issues the AI analysis extracted; without an
    analysis they are the frequent words of the messages.
    """
    # A request for more than the maximum is clamped instead of rejected:
    # the caller asked for a page, not for an error.
    limit = min(limit, MAX_LIMIT)
    rows = await stats.topics(
        since=since, until=until, channel_id=channel_id, limit=limit
    )
    return {
        "total": len(rows),
        "limit": limit,
        "offset": 0,
        "has_more": False,
        "items": rows,
    }
