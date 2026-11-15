"""Analytics endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from foresight.api.deps import TimeBounds, get_stats, time_bounds

# The pagination rules live with the router that established them, so every
# list endpoint of the API pages the same way.
from foresight.api.routers.messages import DEFAULT_LIMIT, MAX_LIMIT
from foresight.storage.stats import StatsService

router = APIRouter()


@router.get("/stats/overview")
async def stats_overview(
    channel_id: int | None = None,
    bounds: TimeBounds = Depends(time_bounds),
    stats: StatsService = Depends(get_stats),
) -> dict:
    """Report the totals of the dump.

    The overview is a summary and not a list: it answers with the counters
    themselves instead of with an envelope.

    The `since`/`until` bounds are normalized by the dependency, so a
    bound of the form `YYYY-MM-DD` names a whole day.
    """
    return await stats.overview(
        since=bounds.since, until=bounds.until, channel_id=channel_id
    )


@router.get("/stats/top-talkers")
async def stats_top_talkers(
    channel_id: int | None = None,
    limit: int = Query(DEFAULT_LIMIT, ge=1),
    bounds: TimeBounds = Depends(time_bounds),
    stats: StatsService = Depends(get_stats),
) -> dict:
    """Rank the authors by the number of messages they wrote.

    The envelope counts the rows of this response: `total` is the number of
    authors on this page, not the number of authors of the dump. `truncated`
    is true when the page reached `limit`, because there may then be more
    authors than the ones in this response, and `has_more` repeats it.
    """
    # A request for more than the maximum is clamped instead of rejected:
    # the caller asked for a page, not for an error.
    limit = min(limit, MAX_LIMIT)
    rows = await stats.top_talkers(
        since=bounds.since,
        until=bounds.until,
        channel_id=channel_id,
        limit=limit,
    )
    truncated = len(rows) >= limit
    return {
        "total": len(rows),
        "limit": limit,
        "offset": 0,
        "has_more": truncated,
        "truncated": truncated,
        "items": rows,
    }


@router.get("/stats/activity")
async def stats_activity(
    channel_id: int | None = None,
    bucket: str = Query("day", pattern="^(hour|day)$"),
    limit: int = Query(DEFAULT_LIMIT, ge=1),
    bounds: TimeBounds = Depends(time_bounds),
    stats: StatsService = Depends(get_stats),
) -> dict:
    """Report how many messages were written per hour or per day.

    An unknown bucket is rejected: a bucket the API does not know would be
    answered with a day bucket the caller did not ask for.

    The envelope counts the rows of this response: `total` is the number of
    buckets on this page, not the number of buckets of the dump. `truncated`
    is true when the page reached `limit`, because there may then be more
    buckets than the ones in this response, and `has_more` repeats it.
    """
    # A request for more than the maximum is clamped instead of rejected:
    # the caller asked for a page, not for an error.
    limit = min(limit, MAX_LIMIT)
    buckets = await stats.activity(
        since=bounds.since,
        until=bounds.until,
        bucket=bucket,
        channel_id=channel_id,
    )
    # The service returns the whole series and the page is cut here: the
    # query that buckets the messages lives in the storage layer, and the
    # routers do not build statements of their own.
    rows = buckets[:limit]
    truncated = len(rows) >= limit
    return {
        "total": len(rows),
        "limit": limit,
        "offset": 0,
        "has_more": truncated,
        "truncated": truncated,
        "items": rows,
    }


@router.get("/stats/topics")
async def stats_topics(
    channel_id: int | None = None,
    limit: int = Query(DEFAULT_LIMIT, ge=1),
    bounds: TimeBounds = Depends(time_bounds),
    stats: StatsService = Depends(get_stats),
) -> dict:
    """Report the topics of the dump.

    The topics are the issues the AI analysis extracted; without an
    analysis they are the frequent words of the messages.

    The envelope counts the rows of this response: `total` is the number of
    topics on this page, not the number of topics of the dump. `truncated`
    says that the list was cut at `limit`, and `has_more` repeats it: a
    caller that sees `truncated: true` knows that more topics exist than
    the ones it received.
    """
    # A request for more than the maximum is clamped instead of rejected:
    # the caller asked for a page, not for an error.
    limit = min(limit, MAX_LIMIT)
    rows = await stats.topics(
        since=bounds.since,
        until=bounds.until,
        channel_id=channel_id,
        limit=limit,
    )
    truncated = len(rows) >= limit
    return {
        "total": len(rows),
        "limit": limit,
        "offset": 0,
        "has_more": truncated,
        "truncated": truncated,
        "items": rows,
    }
