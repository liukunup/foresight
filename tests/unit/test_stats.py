"""Tests for the read-only analytics service.

The service is the read side of the storage layer: it aggregates the dumped
messages for the API and the CLI. These tests pin the aggregation
semantics, the fallback of the topic extraction and the portability of the
date bucketing.
"""

from __future__ import annotations

import json
from contextlib import contextmanager

import pytest
from sqlalchemy import event
from sqlalchemy.dialects import mysql, postgresql, sqlite

from foresight.domain.models import (
    MessageAnalysis,
    MessageRecord,
    OpKind,
    UserSnapshot,
    WriteOp,
)
from foresight.storage.repository import Repository
from foresight.storage.stats import StatsService, bucket_expr
from foresight.storage.tables import messages


async def _make_stats(db_engine):
    """Return a service over a database holding five dumped messages.

    The engine comes from the `db_engine` fixture instead of being created
    here, because a fixture disposes the engine after the test. An engine
    that is only closed by the garbage collector reports a `ResourceWarning`,
    which fails the suite under `-W error`.
    """
    repo = Repository(db_engine)
    records = []
    for index in range(5):
        records.append(
            MessageRecord(
                message_id=index + 1,
                guild_id=10,
                channel_id=20 if index < 3 else 21,
                author_id=30 if index % 2 == 0 else 31,
                content=f"hello {index}",
                message_type=0,
                created_at=f"2026-11-1{index + 1}T10:00:00+00:00",
                captured_at="2026-11-11T10:00:00+00:00",
                char_count=7,
                word_count=2,
            )
        )
    deleted = records[0]
    deleted.deleted_at = "2026-11-11T12:00:00+00:00"
    await repo.apply_batch([WriteOp(OpKind.MESSAGE, rec) for rec in records])
    return StatsService(db_engine), db_engine


def _analysis(message_id: int, issues: list[str]) -> MessageAnalysis:
    """Build an analysis row the way the AI worker stores it."""
    return MessageAnalysis(
        message_id=message_id,
        translated_text=None,
        sentiment="negative",
        sentiment_score=-0.5,
        issues_json=json.dumps(issues),
        model="test-model",
        prompt_version="v1",
        latency_ms=12,
        error=None,
        analyzed_at="2026-11-11T11:00:00+00:00",
    )


def _raw_analysis(message_id: int, issues_json: str) -> MessageAnalysis:
    """Build an analysis row with a verbatim `issues_json` payload.

    The AI worker stores a JSON array, but the column is free text: nothing
    in the schema constrains the format, so a row can hold any payload.
    """
    analysis = _analysis(message_id, [])
    analysis.issues_json = issues_json
    return analysis


@contextmanager
def _record_selects(engine):
    """Collect the SELECT statements the engine runs inside the block.

    The listener sits on the synchronous engine behind the async wrapper:
    the async engine fires the events of its synchronous counterpart.
    """
    recorded: list[str] = []

    def _before(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            recorded.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _before)
    try:
        yield recorded
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _before)


async def test_overview_counts(db_engine):
    stats, _ = await _make_stats(db_engine)
    result = await stats.overview()
    assert result["total_messages"] == 5
    assert result["unique_authors"] == 2
    assert result["deleted_messages"] == 1
    assert result["edited_messages"] == 0
    assert result["avg_chars"] == 7


async def test_overview_respects_since(db_engine):
    stats, _ = await _make_stats(db_engine)
    result = await stats.overview(since="2026-11-13T00:00:00+00:00")
    assert result["total_messages"] == 3


async def test_overview_until_includes_the_boundary(db_engine):
    """`until` is inclusive: a message created exactly at it is counted."""
    stats, _ = await _make_stats(db_engine)
    result = await stats.overview(until="2026-11-13T10:00:00+00:00")
    assert result["total_messages"] == 3


async def test_overview_until_excludes_later_messages(db_engine):
    """A message created after `until` is not counted."""
    stats, _ = await _make_stats(db_engine)
    result = await stats.overview(until="2026-11-13T09:59:59+00:00")
    assert result["total_messages"] == 2


async def test_top_talkers(db_engine):
    stats, _ = await _make_stats(db_engine)
    result = await stats.top_talkers(limit=10)
    assert [row["author_id"] for row in result] == [30, 31]
    assert result[0]["message_count"] == 3


async def test_top_talkers_respects_the_time_filters(db_engine):
    """`since` and `until` narrow the talkers down to the matching messages."""
    stats, _ = await _make_stats(db_engine)
    assert await stats.top_talkers(since="2026-11-14T00:00:00+00:00") == [
        {"author_id": 30, "username": None, "message_count": 1, "total_chars": 7},
        {"author_id": 31, "username": None, "message_count": 1, "total_chars": 7},
    ]
    assert await stats.top_talkers(until="2026-11-13T10:00:00+00:00") == [
        {"author_id": 30, "username": None, "message_count": 2, "total_chars": 14},
        {"author_id": 31, "username": None, "message_count": 1, "total_chars": 7},
    ]


async def test_top_talkers_respects_the_channel_filter(db_engine):
    stats, _ = await _make_stats(db_engine)
    assert await stats.top_talkers(channel_id=20) == [
        {"author_id": 30, "username": None, "message_count": 2, "total_chars": 14},
        {"author_id": 31, "username": None, "message_count": 1, "total_chars": 7},
    ]


@pytest.mark.parametrize("limit", [0, -1])
async def test_top_talkers_clamps_a_non_positive_limit(db_engine, limit):
    """A non-positive limit returns nothing and never reaches the database.

    A negative limit renders as `LIMIT -1`: SQLite reads that as "no limit"
    while PostgreSQL rejects it, so the same call would succeed on one
    backend and fail on another. The limit is therefore clamped before the
    statement is built and every backend agrees on an empty result.
    """
    stats, _ = await _make_stats(db_engine)
    with _record_selects(db_engine) as selects:
        assert await stats.top_talkers(limit=limit) == []
        assert selects == []


async def test_activity_buckets_by_day(db_engine):
    stats, _ = await _make_stats(db_engine)
    result = await stats.activity(bucket="day")
    assert [row["bucket"] for row in result] == [
        "2026-11-11",
        "2026-11-12",
        "2026-11-13",
        "2026-11-14",
        "2026-11-15",
    ]
    assert result[0]["message_count"] == 1


async def test_activity_buckets_by_hour(db_engine):
    stats, _ = await _make_stats(db_engine)
    result = await stats.activity(bucket="hour")
    assert result[0]["bucket"] == "2026-11-11T10"


async def test_activity_respects_the_time_filters(db_engine):
    """`since` and `until` narrow the buckets down to the matching messages."""
    stats, _ = await _make_stats(db_engine)
    assert await stats.activity(since="2026-11-14T00:00:00+00:00") == [
        {"bucket": "2026-11-14", "message_count": 1},
        {"bucket": "2026-11-15", "message_count": 1},
    ]
    assert await stats.activity(until="2026-11-13T10:00:00+00:00") == [
        {"bucket": "2026-11-11", "message_count": 1},
        {"bucket": "2026-11-12", "message_count": 1},
        {"bucket": "2026-11-13", "message_count": 1},
    ]


async def test_activity_respects_the_channel_filter(db_engine):
    stats, _ = await _make_stats(db_engine)
    assert await stats.activity(channel_id=21) == [
        {"bucket": "2026-11-14", "message_count": 1},
        {"bucket": "2026-11-15", "message_count": 1},
    ]


async def test_overview_respects_channel(db_engine):
    stats, _ = await _make_stats(db_engine)
    result = await stats.overview(channel_id=20)
    assert result["total_messages"] == 3


async def test_overview_on_empty_database(db_engine):
    stats = StatsService(db_engine)
    result = await stats.overview()
    assert result == {
        "total_messages": 0,
        "unique_authors": 0,
        "deleted_messages": 0,
        "edited_messages": 0,
        "avg_chars": 0.0,
    }


async def test_aggregations_on_empty_database(db_engine):
    stats = StatsService(db_engine)
    assert await stats.top_talkers() == []
    assert await stats.activity() == []
    assert await stats.topics() == []


async def test_top_talkers_carries_username(db_engine):
    stats, engine = await _make_stats(db_engine)
    await Repository(engine).apply_batch(
        [
            WriteOp(
                OpKind.USER,
                UserSnapshot(
                    user_id=30,
                    username="alice",
                    global_name=None,
                    display_name="Alice",
                    discriminator="0",
                    avatar_url=None,
                    is_bot=False,
                    account_created_at=None,
                    guild_joined_at=None,
                    roles_json="[]",
                    updated_at="2026-11-11T10:00:00+00:00",
                ),
            )
        ]
    )
    result = await stats.top_talkers()
    assert result[0]["username"] == "alice"
    assert result[1]["username"] is None


async def test_topics_prefers_extracted_issues(db_engine):
    stats, engine = await _make_stats(db_engine)
    await Repository(engine).apply_batch(
        [
            WriteOp(OpKind.ANALYSIS, _analysis(2, ["bug", "feature"])),
            WriteOp(OpKind.ANALYSIS, _analysis(4, ["bug"])),
        ]
    )
    result = await stats.topics()
    assert result == [{"topic": "bug", "count": 2}, {"topic": "feature", "count": 1}]


async def test_topics_respects_filters(db_engine):
    """A filtered topic count counts every analysis row exactly once."""
    stats, engine = await _make_stats(db_engine)
    await Repository(engine).apply_batch(
        [
            WriteOp(OpKind.ANALYSIS, _analysis(2, ["bug"])),
            WriteOp(OpKind.ANALYSIS, _analysis(4, ["bug"])),
        ]
    )
    assert await stats.topics(channel_id=20) == [{"topic": "bug", "count": 1}]
    assert await stats.topics(since="2026-11-14T00:00:00+00:00") == [
        {"topic": "bug", "count": 1}
    ]


async def test_topics_falls_back_to_frequent_words(db_engine):
    stats, _ = await _make_stats(db_engine)
    result = await stats.topics()
    assert result == [{"topic": "hello", "count": 5}]


@pytest.mark.parametrize(
    "issues_json",
    [
        pytest.param("null", id="json-null"),
        pytest.param('{"a": 1}', id="json-object"),
        pytest.param('"bug"', id="json-string"),
        pytest.param("{not json", id="malformed"),
    ],
)
async def test_topics_skips_payloads_that_are_not_issue_lists(db_engine, issues_json):
    """A row whose payload is not a list of issues is skipped, not counted.

    The column is free text, so a row can hold anything. A payload that is
    not a list must neither crash the query nor contribute a nonsense topic.
    """
    stats, engine = await _make_stats(db_engine)
    await Repository(engine).apply_batch(
        [
            WriteOp(OpKind.ANALYSIS, _raw_analysis(1, issues_json)),
            WriteOp(OpKind.ANALYSIS, _analysis(2, ["bug"])),
            WriteOp(OpKind.ANALYSIS, _raw_analysis(3, issues_json)),
            WriteOp(OpKind.ANALYSIS, _analysis(4, ["bug"])),
        ]
    )
    assert await stats.topics() == [{"topic": "bug", "count": 2}]


async def test_topics_skips_a_deeply_nested_payload(db_engine):
    """A pathologically nested payload is skipped, not raised.

    The JSON decoder recurses once per nesting level, so a payload nested
    thousands of levels deep raises `RecursionError` instead of
    `JSONDecodeError`. Such a row must be skipped like a malformed one: the
    query never crashes.
    """
    stats, engine = await _make_stats(db_engine)
    deep = "[" * 100_000 + "]" * 100_000
    await Repository(engine).apply_batch(
        [
            WriteOp(OpKind.ANALYSIS, _raw_analysis(1, deep)),
            WriteOp(OpKind.ANALYSIS, _analysis(2, ["bug"])),
        ]
    )
    assert await stats.topics() == [{"topic": "bug", "count": 1}]


async def test_topics_breaks_a_tie_by_the_topic(db_engine):
    """Topics with an equal count come back in a stable, alphabetical order."""
    stats, engine = await _make_stats(db_engine)
    await Repository(engine).apply_batch(
        [
            WriteOp(OpKind.ANALYSIS, _analysis(2, ["zebra"])),
            WriteOp(OpKind.ANALYSIS, _analysis(4, ["apple"])),
        ]
    )
    assert await stats.topics() == [
        {"topic": "apple", "count": 1},
        {"topic": "zebra", "count": 1},
    ]


async def test_topics_respects_the_limit(db_engine):
    """The limit caps the number of topics; a non-positive limit returns none."""
    stats, engine = await _make_stats(db_engine)
    await Repository(engine).apply_batch(
        [
            WriteOp(OpKind.ANALYSIS, _analysis(2, ["zebra"])),
            WriteOp(OpKind.ANALYSIS, _analysis(4, ["apple"])),
        ]
    )
    assert await stats.topics(limit=1) == [{"topic": "apple", "count": 1}]
    assert await stats.topics(limit=0) == []
    assert await stats.topics(limit=-1) == []


async def test_topics_fallback_breaks_a_tie_by_the_word(db_engine):
    """The word fallback sorts equal counts alphabetically too."""
    repo = Repository(db_engine)
    records = [
        MessageRecord(
            message_id=index + 1,
            guild_id=10,
            channel_id=20,
            author_id=30,
            content=content,
            message_type=0,
            created_at=f"2026-11-1{index + 1}T10:00:00+00:00",
            captured_at="2026-11-11T10:00:00+00:00",
            char_count=7,
            word_count=1,
        )
        for index, content in enumerate(["zebra", "apple", "mango"])
    ]
    await repo.apply_batch([WriteOp(OpKind.MESSAGE, rec) for rec in records])
    stats = StatsService(db_engine)
    assert await stats.topics() == [
        {"topic": "apple", "count": 1},
        {"topic": "mango", "count": 1},
        {"topic": "zebra", "count": 1},
    ]


def test_bucket_expr_is_portable():
    """The bucketing expression must not depend on the dialect."""
    for dialect in (sqlite.dialect(), postgresql.dialect(), mysql.dialect()):
        day = str(
            bucket_expr(messages.c.created_at, "day").compile(
                dialect=dialect, compile_kwargs={"literal_binds": True}
            )
        )
        hour = str(
            bucket_expr(messages.c.created_at, "hour").compile(
                dialect=dialect, compile_kwargs={"literal_binds": True}
            )
        )
        assert day == "substr(messages.created_at, 1, 10)"
        assert hour == "substr(messages.created_at, 1, 13)"
