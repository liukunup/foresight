"""Tests for the runners of the free-form SQL endpoint.

The runners are the security boundary of the endpoint: they decide what a
statement may do, how long it may run and what the caller is told when it
fails. The tests pin down the three of them:

- a statement is bounded by a deadline, so that it cannot hold a thread or
  a connection for ever;
- an illegal statement is a bad request and a broken database is a failure
  of the server, and the statement itself never reaches the log;
- the window of the caller is pushed into the statement, so that the
  database never hands over more rows than the page needs.
"""

from __future__ import annotations

import asyncio
import sqlite3
import pytest

from foresight.api.query import (
    QueryDatabaseError,
    QueryError,
    SqlAlchemyQueryRunner,
    SqliteQueryRunner,
    apply_window,
    clamp_limit,
    validate_read_only,
)
from foresight.config import DatabaseConfig

from fakes import RecordingEngine


def _seed_big_table(path: str, rows: int = 2000) -> None:
    """Write a database with a table whose self-join never ends."""
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE big (i INTEGER)")
        conn.executemany(
            "INSERT INTO big (i) VALUES (?)", [(index,) for index in range(rows)]
        )
        conn.commit()
    finally:
        conn.close()


# --- the deadline --------------------------------------------------------


async def test_the_sqlite_runner_stops_a_statement_that_runs_too_long(tmp_path):
    """A statement that never ends must not hold the endpoint for ever.

    The runner answers with an error instead of waiting for the statement,
    and the thread of the statement is released: the next statement runs
    as if nothing had happened.
    """
    path = tmp_path / "slow.db"
    _seed_big_table(str(path))
    runner = SqliteQueryRunner(str(path), timeout=0.5)

    with pytest.raises(QueryError):
        await asyncio.wait_for(
            runner.run("SELECT COUNT(*) FROM big a, big b, big c", {}, 10, 0),
            timeout=20,
        )

    # The thread of the stopped statement is free again.
    result = await asyncio.wait_for(
        runner.run("SELECT COUNT(*) FROM big", {}, 10, 0), timeout=20
    )
    assert result.rows == [[2000]]


async def test_the_sqlite_runner_answers_a_fast_statement(tmp_path):
    """A statement that is quick is not disturbed by the deadline."""
    path = tmp_path / "fast.db"
    _seed_big_table(str(path), rows=10)
    runner = SqliteQueryRunner(str(path), timeout=5)

    result = await runner.run("SELECT COUNT(*) FROM big", {}, 10, 0)

    assert result.rows == [[10]]


async def test_the_postgresql_runner_bounds_the_statement():
    """PostgreSQL stops the statement itself: the timeout is a setting."""
    engine = RecordingEngine()
    runner = SqlAlchemyQueryRunner(DatabaseConfig(backend="postgresql"), engine=engine)

    await runner.run("SELECT 1", {}, 10, 0)

    assert "SET LOCAL statement_timeout = '5s'" in engine.statements


async def test_the_mysql_runner_bounds_the_statement():
    """MySQL knows the timeout as a session setting, in milliseconds."""
    engine = RecordingEngine()
    runner = SqlAlchemyQueryRunner(DatabaseConfig(backend="mysql"), engine=engine)

    await runner.run("SELECT 1", {}, 10, 0)

    assert "SET SESSION max_execution_time = 5000" in engine.statements


async def test_the_timeout_of_the_runner_is_used_by_the_setting():
    """The setting and the deadline are the same number."""
    engine = RecordingEngine()
    runner = SqlAlchemyQueryRunner(
        DatabaseConfig(backend="postgresql"), engine=engine, timeout=2
    )

    await runner.run("SELECT 1", {}, 10, 0)

    assert "SET LOCAL statement_timeout = '2s'" in engine.statements


# --- the errors of the database -------------------------------------------


async def test_a_statement_the_database_cannot_parse_is_a_bad_request():
    """The caller wrote the statement, so the caller is the one to fix it."""
    from sqlalchemy.exc import ProgrammingError

    engine = RecordingEngine(ProgrammingError("SELECT FROM WHERE", None, Exception()))
    runner = SqlAlchemyQueryRunner(DatabaseConfig(backend="postgresql"), engine=engine)

    with pytest.raises(QueryError):
        await runner.run("SELECT FROM WHERE", {}, 10, 0)


async def test_a_parameter_the_caller_forgot_is_a_bad_request():
    """A bind parameter without a value is a mistake of the caller."""
    from sqlalchemy.exc import StatementError

    engine = RecordingEngine(
        StatementError("A value is required", None, None, Exception())
    )
    runner = SqlAlchemyQueryRunner(DatabaseConfig(backend="postgresql"), engine=engine)

    with pytest.raises(QueryError):
        await runner.run("SELECT :missing", {}, 10, 0)


async def test_a_database_that_cannot_answer_is_a_failure_of_the_server():
    """The statement is fine, so the answer is a 500 and not a 400."""
    from sqlalchemy.exc import OperationalError

    engine = RecordingEngine(OperationalError("the server is gone", None, Exception()))
    runner = SqlAlchemyQueryRunner(DatabaseConfig(backend="postgresql"), engine=engine)

    with pytest.raises(QueryDatabaseError):
        await runner.run("SELECT 1", {}, 10, 0)


async def test_the_statement_never_reaches_the_message_of_the_error():
    """The statement of the caller is not part of the answer.

    SQLAlchemy writes the statement and the parameters into the message of
    its errors: that text must not travel with the error, because it ends
    up in the log of the server.
    """
    from sqlalchemy.exc import ProgrammingError

    error = ProgrammingError(
        "syntax error\n[SQL: SELECT secret FROM messages]\n[parameters: {'a': 1}]",
        None,
        Exception(),
    )
    engine = RecordingEngine(error)
    runner = SqlAlchemyQueryRunner(DatabaseConfig(backend="postgresql"), engine=engine)

    with pytest.raises(QueryError) as caught:
        await runner.run("SELECT secret FROM messages", {}, 10, 0)

    assert "secret" not in str(caught.value)
    assert "[SQL:" not in str(caught.value)


# --- the window of the caller ---------------------------------------------


async def test_the_window_is_pushed_into_the_statement(tmp_path):
    """The database hands over the page and not the whole table."""
    path = tmp_path / "window.db"
    _seed_big_table(str(path), rows=100)
    runner = SqliteQueryRunner(str(path))

    result = await runner.run("SELECT i FROM big ORDER BY i", {}, 10, 20)

    assert result.rows == [[20], [21], [22], [23], [24], [25], [26], [27], [28], [29]]


async def test_a_statement_with_its_own_limit_keeps_it(tmp_path):
    """A second LIMIT would be a syntax error, so none is added."""
    path = tmp_path / "own.db"
    _seed_big_table(str(path), rows=100)
    runner = SqliteQueryRunner(str(path))

    result = await runner.run("SELECT i FROM big ORDER BY i LIMIT 3", {}, 10, 0)

    assert result.rows == [[0], [1], [2]]


def test_the_window_is_written_into_the_statement():
    """The limit and the offset are the window of the statement."""
    assert apply_window("SELECT * FROM t", 10, 0) == "SELECT * FROM t\nLIMIT 10"
    assert apply_window("SELECT * FROM t", 10, 20) == "SELECT * FROM t\nLIMIT 10 OFFSET 20"


def test_a_statement_with_its_own_window_keeps_it():
    """A second window would be a syntax error."""
    assert apply_window("SELECT * FROM t LIMIT 5", 10, 20) == "SELECT * FROM t LIMIT 5"
    assert (
        apply_window("SELECT * FROM t LIMIT 5 OFFSET 2", 10, 20)
        == "SELECT * FROM t LIMIT 5 OFFSET 2"
    )
    assert (
        apply_window("SELECT * FROM t FETCH FIRST 5 ROWS ONLY", 10, 20)
        == "SELECT * FROM t FETCH FIRST 5 ROWS ONLY"
    )


def test_a_limit_inside_a_subquery_is_not_the_window_of_the_statement():
    """The window of the answer is the one of the statement itself."""
    assert (
        apply_window("SELECT * FROM (SELECT i FROM t LIMIT 5) AS s", 10, 0)
        == "SELECT * FROM (SELECT i FROM t LIMIT 5) AS s\nLIMIT 10"
    )


def test_a_limit_inside_a_literal_is_not_the_window_of_the_statement():
    """A literal holds a value, not an operation."""
    assert (
        apply_window("SELECT 'LIMIT 5' AS x", 10, 0) == "SELECT 'LIMIT 5' AS x\nLIMIT 10"
    )


def test_a_comment_at_the_end_does_not_swallow_the_window():
    """The window is written on a line of its own."""
    assert apply_window("SELECT 1 -- a comment", 10, 0) == (
        "SELECT 1 -- a comment\nLIMIT 10"
    )


def test_the_limit_is_clamped():
    assert clamp_limit(None) == 100
    assert clamp_limit(0) == 1
    assert clamp_limit(10_000) == 500


def test_a_statement_that_is_not_a_read_is_refused():
    with pytest.raises(QueryError):
        validate_read_only("DELETE FROM messages")


def test_a_comment_cannot_hide_a_second_statement():
    """A comment is not code, and the code after it is still code.

    The quote of the comment used to open a literal that swallowed the
    rest of the statement: the semicolon of the second statement was
    never seen, and the filter let the statement through.
    """
    with pytest.raises(QueryError):
        validate_read_only(
            "SELECT 1 -- '\n; INSERT INTO messages (message_id) VALUES (1)"
        )


def test_a_block_comment_cannot_hide_a_write():
    """The same goes for a comment that spans more than one line."""
    with pytest.raises(QueryError):
        validate_read_only("SELECT 1 /* ' */ ; DROP TABLE messages")


def test_a_comment_may_hold_a_forbidden_word():
    """A remark is not an operation."""
    assert validate_read_only("SELECT 1 -- drop the table")
    assert validate_read_only("SELECT 1 /* insert a row */")


def test_a_dollar_quoted_string_is_not_code():
    """The strings of PostgreSQL hold a value, like every other literal."""
    assert validate_read_only("SELECT $$drop$$ AS x")
    assert validate_read_only("SELECT $tag$insert$tag$ AS x")


def test_a_parameter_of_postgresql_is_not_a_dollar_quote():
    """The placeholders of PostgreSQL are not strings."""
    assert validate_read_only("SELECT $1 FROM messages")
