"""Read-only analytics queries.

Timestamps are stored as fixed-width UTC ISO-8601 strings, so string
comparison equals chronological comparison and `substr` gives us portable
date bucketing without dialect specific date functions.

This module never writes: it only issues SELECT statements. It is the read
side of the storage layer and reads through its own queries instead of going
through the repository, which is the write side.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any, Sequence

from sqlalchemy import ColumnElement, distinct, func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from foresight.storage.tables import message_analysis, messages, users


def bucket_expr(column: Any, bucket: str) -> ColumnElement[Any]:
    """Return a portable expression that truncates an ISO timestamp.

    `substr(column, 1, 10)` truncates to the day, `substr(column, 1, 13)` to
    the hour. Every supported backend has `substr`, so the same expression
    works on SQLite, PostgreSQL and MySQL without a dialect branch.
    """
    if bucket == "hour":
        return func.substr(column, 1, 13)
    return func.substr(column, 1, 10)


def _ranked(counter: Counter[str], limit: int) -> list[tuple[str, int]]:
    """Order the counted topics deterministically.

    The count decides the order and the topic itself breaks a tie. A counter
    alone keeps the order in which the topics were first seen, which depends
    on the order the database returned the rows in: that order is the rowid
    order on SQLite and unspecified on PostgreSQL and MySQL.
    """
    if limit <= 0:
        return []
    return sorted(counter.items(), key=lambda item: (-item[1], item[0]))[:limit]


class StatsService:
    """Aggregate queries used by both the API and the CLI."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    def _filter(
        self,
        since: str | None,
        until: str | None,
        channel_id: int | None,
    ) -> list[Any]:
        clauses: list[Any] = []
        if since:
            clauses.append(messages.c.created_at >= since)
        if until:
            clauses.append(messages.c.created_at <= until)
        if channel_id is not None:
            clauses.append(messages.c.channel_id == channel_id)
        return clauses

    async def overview(
        self,
        since: str | None = None,
        until: str | None = None,
        channel_id: int | None = None,
    ) -> dict[str, Any]:
        clauses = self._filter(since, until, channel_id)
        total = select(func.count()).select_from(messages).where(*clauses)
        authors = (
            select(func.count(distinct(messages.c.author_id)))
            .select_from(messages)
            .where(*clauses)
        )
        deleted = select(func.count()).select_from(messages).where(
            *clauses, messages.c.deleted_at.is_not(None)
        )
        edited = select(func.count()).select_from(messages).where(
            *clauses, messages.c.edited_at.is_not(None)
        )
        avg_chars = (
            select(func.avg(messages.c.char_count)).select_from(messages).where(*clauses)
        )
        async with self._engine.connect() as conn:
            total_count = int((await conn.execute(total)).scalar_one())
            unique_authors = int((await conn.execute(authors)).scalar_one())
            deleted_count = int((await conn.execute(deleted)).scalar_one())
            edited_count = int((await conn.execute(edited)).scalar_one())
            average = (await conn.execute(avg_chars)).scalar_one()
        return {
            "total_messages": total_count,
            "unique_authors": unique_authors,
            "deleted_messages": deleted_count,
            "edited_messages": edited_count,
            "avg_chars": round(float(average or 0), 2),
        }

    async def top_talkers(
        self,
        since: str | None = None,
        until: str | None = None,
        channel_id: int | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        clauses = self._filter(since, until, channel_id)
        stmt = (
            select(
                messages.c.author_id,
                func.count().label("message_count"),
                func.sum(messages.c.char_count).label("total_chars"),
            )
            .select_from(messages)
            .where(*clauses)
            .group_by(messages.c.author_id)
            .order_by(func.count().desc(), messages.c.author_id)
            .limit(limit)
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        names = await self._usernames([row.author_id for row in rows])
        return [
            {
                "author_id": row.author_id,
                "username": names.get(row.author_id),
                "message_count": int(row.message_count),
                "total_chars": int(row.total_chars or 0),
            }
            for row in rows
        ]

    async def _usernames(self, user_ids: Sequence[int]) -> dict[int, str | None]:
        """Look up the usernames of the given user ids.

        The lookup is a separate query with an `IN` clause: joining `users`
        into the grouped statement would change what the grouping is over.
        """
        if not user_ids:
            return {}
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(users.c.user_id, users.c.username).where(
                        users.c.user_id.in_(list(user_ids))
                    )
                )
            ).all()
        return {row.user_id: row.username for row in rows}

    async def activity(
        self,
        since: str | None = None,
        until: str | None = None,
        bucket: str = "day",
        channel_id: int | None = None,
    ) -> list[dict[str, Any]]:
        clauses = self._filter(since, until, channel_id)
        bucket_column = bucket_expr(messages.c.created_at, bucket)
        stmt = (
            select(bucket_column.label("bucket"), func.count().label("message_count"))
            .select_from(messages)
            .where(*clauses)
            .group_by(bucket_column)
            .order_by(bucket_column)
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        return [
            {"bucket": row.bucket, "message_count": int(row.message_count)} for row in rows
        ]

    async def topics(
        self,
        since: str | None = None,
        until: str | None = None,
        channel_id: int | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Aggregate extracted issues first, fall back to frequent words.

        An extracted issue is only counted when its message row exists: an
        analysis row whose message is gone contributes nothing, and if it
        was the only analysis the result falls back to frequent words.

        The `issues_json` column holds free text that nothing constrains, so
        a payload that is not a list of issues is skipped: it must neither
        crash the query nor contribute a nonsense topic.
        """
        clauses = self._filter(since, until, channel_id)
        # The analysis rows are joined to their message: the filters address
        # the message columns, and a join keeps one analysis row counted once
        # instead of once per matching message.
        stmt = (
            select(message_analysis.c.issues_json)
            .select_from(
                message_analysis.join(
                    messages, message_analysis.c.message_id == messages.c.message_id
                )
            )
            .where(*clauses, message_analysis.c.issues_json.is_not(None))
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        counter: Counter[str] = Counter()
        for row in rows:
            try:
                issues = json.loads(row.issues_json or "[]")
            except json.JSONDecodeError:
                continue
            # Only a list holds issues. A JSON null, an object or a bare
            # string is a payload the schema does not forbid, so such a row
            # is skipped instead of being iterated.
            if not isinstance(issues, list):
                continue
            for issue in issues:
                if isinstance(issue, str) and issue.strip():
                    counter[issue.strip()] += 1
        if counter:
            return [
                {"topic": topic, "count": count}
                for topic, count in _ranked(counter, limit)
            ]
        return await self._frequent_words(clauses, limit)

    async def _frequent_words(
        self, clauses: Sequence[Any], limit: int
    ) -> list[dict[str, Any]]:
        """Count the frequent words of the raw content.

        The fallback only runs when no analysis exists. The tokenisation is
        plain whitespace splitting: there is no word segmentation in this
        project, so CJK text is counted in whitespace separated runs.
        """
        stmt = select(messages.c.content).select_from(messages).where(*clauses)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        counter: Counter[str] = Counter()
        for row in rows:
            for token in (row.content or "").lower().split():
                token = token.strip()
                if len(token) > 2:
                    counter[token] += 1
        return [
            {"topic": topic, "count": count} for topic, count in _ranked(counter, limit)
        ]
