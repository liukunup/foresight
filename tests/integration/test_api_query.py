"""Tests for the free-form read-only SQL endpoint.

The endpoint is the security boundary of the API: it runs a statement
the caller wrote, so the tests check both that a read works and that the
ways of writing to the database are refused.
"""

from __future__ import annotations

import time

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from foresight.api.app import create_app
from foresight.api.query import (
    QueryError,
    SqlAlchemyQueryRunner,
    build_query_runner,
    validate_read_only,
)
from foresight.config import DatabaseConfig

from fakes import RecordingEngine

from .conftest import api_config, api_message


async def test_query_returns_rows(api_client, auth):
    response = await api_client.post(
        "/api/query",
        headers=auth,
        json={
            "sql": "SELECT message_id, content FROM messages ORDER BY message_id",
            "params": {},
            "limit": 10,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["row_count"] == 4
    assert body["columns"] == ["message_id", "content"]


async def test_query_supports_named_params(api_client, auth):
    response = await api_client.post(
        "/api/query",
        headers=auth,
        json={
            "sql": "SELECT COUNT(*) AS c FROM messages WHERE channel_id = :channel",
            "params": {"channel": 20},
        },
    )
    assert response.status_code == 200
    assert response.json()["rows"] == [[4]]


async def test_query_rejects_insert(api_client, auth):
    response = await api_client.post(
        "/api/query",
        headers=auth,
        json={"sql": "INSERT INTO messages (message_id) VALUES (1)", "params": {}},
    )
    assert response.status_code == 400


async def test_query_rejects_drop(api_client, auth):
    response = await api_client.post(
        "/api/query",
        headers=auth,
        json={"sql": "DROP TABLE messages", "params": {}},
    )
    assert response.status_code == 400


async def test_query_rejects_multi_statement(api_client, auth):
    response = await api_client.post(
        "/api/query",
        headers=auth,
        json={"sql": "SELECT 1; DROP TABLE messages", "params": {}},
    )
    assert response.status_code == 400


async def test_query_rejects_write_cte(api_client, auth):
    response = await api_client.post(
        "/api/query",
        headers=auth,
        json={
            "sql": "WITH x AS (DELETE FROM messages RETURNING message_id) SELECT * FROM x",
            "params": {},
        },
    )
    assert response.status_code == 400


async def test_query_accepts_a_forbidden_word_inside_a_literal(api_harness):
    """The keyword filter looks at the code of the statement, not at its text.

    A caller that searches the dump for the word "create" must not be
    refused for it: the filter exists to give a clear error message, and
    the authorizer in the engine is the layer that keeps the dump safe.
    """
    harness = await api_harness((api_message(1, "please create a report"),))
    response = await harness.client.post(
        "/api/query",
        headers={"Authorization": "Bearer secret-token"},
        json={
            "sql": "SELECT content FROM messages WHERE content LIKE '%create%'",
            "params": {},
        },
    )
    assert response.status_code == 200
    assert response.json()["rows"] == [["please create a report"]]


async def test_query_accepts_a_write_word_inside_a_literal(api_client, auth):
    """The caller may search the dump for the word "insert" or "drop"."""
    response = await api_client.post(
        "/api/query",
        headers=auth,
        json={
            "sql": "SELECT COUNT(*) AS c FROM messages WHERE content LIKE '%insert%'",
            "params": {},
        },
    )
    assert response.status_code == 200
    assert response.json()["rows"] == [[0]]


async def test_query_accepts_a_semicolon_inside_a_literal(api_client, auth):
    """A semicolon inside a literal does not make a second statement."""
    response = await api_client.post(
        "/api/query",
        headers=auth,
        json={"sql": "SELECT 'a;b' AS x", "params": {}},
    )
    assert response.status_code == 200
    assert response.json()["rows"] == [["a;b"]]


def test_the_keyword_filter_ignores_quoted_text():
    """A literal and a quoted identifier are not code."""
    assert validate_read_only("SELECT * FROM t WHERE c LIKE '%drop%'")
    assert validate_read_only("SELECT 'insert' AS x")
    assert validate_read_only('SELECT "create" FROM t')
    assert validate_read_only("SELECT `alter` FROM t")


def test_a_doubled_quote_is_an_escaped_quote():
    """A doubled quote stays inside the literal it belongs to."""
    assert validate_read_only("SELECT 'it''s a drop' AS x")


def test_the_filter_still_refuses_the_code_of_a_statement():
    """Only the quoted text is ignored: the code is still checked."""
    for statement in (
        "SELECT 1; DROP TABLE messages",
        "INSERT INTO messages (message_id) VALUES (1)",
        "WITH x AS (DELETE FROM messages RETURNING message_id) SELECT * FROM x",
        "DROP TABLE messages",
    ):
        with pytest.raises(QueryError):
            validate_read_only(statement)


async def test_the_query_endpoint_requires_a_token(api_client):
    """The protection lives at the mount site, not on the routes."""
    response = await api_client.post("/api/query", json={"sql": "SELECT 1"})
    assert response.status_code == 401


async def test_query_leaves_the_data_unchanged(api_client, auth):
    """A refused statement changes nothing.

    The endpoint is the only way into the dump, so the number of the
    messages after a refused write is the proof that the write did not
    happen.
    """
    rejected = await api_client.post(
        "/api/query",
        headers=auth,
        json={"sql": "INSERT INTO messages (message_id) VALUES (99)", "params": {}},
    )
    assert rejected.status_code == 400
    counted = await api_client.post(
        "/api/query",
        headers=auth,
        json={"sql": "SELECT COUNT(*) FROM messages", "params": {}},
    )
    assert counted.status_code == 200
    assert counted.json()["rows"] == [[4]]


async def test_query_clamps_the_limit(api_client, auth):
    """The limit is clamped, not refused.

    A caller that asks for more rows than the maximum gets the maximum,
    and a caller that asks for none gets one row: it asked for a page,
    not for a refusal.
    """
    statement = {
        "sql": "SELECT message_id FROM messages ORDER BY message_id",
        "params": {},
    }
    too_small = await api_client.post(
        "/api/query", headers=auth, json={**statement, "limit": 0}
    )
    assert too_small.status_code == 200
    assert too_small.json()["row_count"] == 1
    too_large = await api_client.post(
        "/api/query", headers=auth, json={**statement, "limit": 10_000}
    )
    assert too_large.status_code == 200
    assert too_large.json()["row_count"] == 4


async def test_query_pages_with_the_offset(api_client, auth):
    """The offset skips the rows of the previous page."""
    response = await api_client.post(
        "/api/query",
        headers=auth,
        json={
            "sql": "SELECT message_id FROM messages ORDER BY message_id",
            "params": {},
            "limit": 2,
            "offset": 2,
        },
    )
    assert response.status_code == 200
    assert response.json()["rows"] == [[3], [4]]


async def test_query_is_denied_by_the_engine(api_client, auth):
    """The engine refuses a statement the validation let through.

    The validation is the first layer and it tells the caller what it
    did not like. The authorizer in the engine is the layer that cannot
    be talked out of: it allows a fixed set of actions, and a statement
    that asks for another one is denied there, in the database.
    """
    response = await api_client.post(
        "/api/query",
        headers=auth,
        json={
            "sql": (
                "WITH RECURSIVE x(n) AS ("
                "SELECT 1 UNION ALL SELECT n + 1 FROM x WHERE n < 5"
                ") SELECT * FROM x"
            ),
            "params": {},
        },
    )
    assert response.status_code == 400


async def test_query_reports_a_broken_statement(api_client, auth):
    """A statement the database cannot parse is a bad request too.

    The caller wrote the statement, so the caller is the one to fix it:
    the answer is a 400 and not a failure of the server.
    """
    response = await api_client.post(
        "/api/query",
        headers=auth,
        json={"sql": "SELECT FROM WHERE", "params": {}},
    )
    assert response.status_code == 400


async def test_the_endpoint_stops_a_statement_that_runs_too_long(
    api_harness, monkeypatch
):
    """A statement that never ends is answered, not waited for.

    The endpoint is often open, so a caller that repeats a statement
    that never ends would take the service down with it: every
    statement is bounded by a deadline.
    """
    monkeypatch.setattr("foresight.api.query.QUERY_TIMEOUT_SECONDS", 0.5)
    harness = await api_harness()
    async with harness.engine.begin() as conn:
        await conn.execute(text("CREATE TABLE big (i INTEGER)"))
        await conn.execute(
            text("INSERT INTO big (i) VALUES (:i)"),
            [{"i": index} for index in range(2000)],
        )

    started = time.perf_counter()
    response = await harness.client.post(
        "/api/query",
        headers={"Authorization": "Bearer secret-token"},
        json={"sql": "SELECT COUNT(*) FROM big a, big b, big c", "params": {}},
    )
    elapsed = time.perf_counter() - started

    assert response.status_code == 400
    assert elapsed < 20
    # The endpoint is still alive: the next statement is answered.
    counted = await harness.client.post(
        "/api/query",
        headers={"Authorization": "Bearer secret-token"},
        json={"sql": "SELECT COUNT(*) FROM big", "params": {}},
    )
    assert counted.status_code == 200
    assert counted.json()["rows"] == [[2000]]


async def test_query_reports_a_broken_database_as_a_server_error(
    tmp_path, monkeypatch
):
    """A database that cannot be read is a failure of the server.

    The statement is a plain read, so the caller did nothing wrong: the
    file is missing, the disk cannot be read or the database is locked,
    and none of that is the caller's fault. The answer is a 500.
    """
    monkeypatch.setenv("FORESIGHT_API_TOKEN", "secret-token")
    app = create_app(api_config(tmp_path / "missing.db"))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        async with app.router.lifespan_context(app):
            response = await client.post(
                "/api/query",
                headers={"Authorization": "Bearer secret-token"},
                json={"sql": "SELECT message_id FROM messages", "params": {}},
            )
    assert response.status_code == 500


async def test_the_read_only_transaction_matches_the_dialect():
    """The statement that opens the transaction is the one of the dialect.

    `SET TRANSACTION READ ONLY` applies to the current transaction on
    PostgreSQL, but on MySQL it applies to the *next* one: the session
    stays writable. MySQL needs the session form, and the backend is
    known when the runner is built, so the statement is chosen then.
    """
    engine = RecordingEngine()
    runner = SqlAlchemyQueryRunner(
        DatabaseConfig(backend="postgresql"), engine=engine
    )
    await runner.run("SELECT 1", {}, 10, 0)
    assert engine.statements[0] == "SET TRANSACTION READ ONLY"

    engine = RecordingEngine()
    runner = SqlAlchemyQueryRunner(DatabaseConfig(backend="mysql"), engine=engine)
    await runner.run("SELECT 1", {}, 10, 0)
    assert engine.statements[0] == "SET SESSION TRANSACTION READ ONLY"


async def test_the_runner_closes_the_engine_it_builds():
    """The runner owns the engine it builds, and closes it.

    The engine is a resource of the runner, exactly like the engine the
    application builds for itself, and it is closed when the runner is.
    """
    runner = build_query_runner(DatabaseConfig(backend="postgresql"))
    pool = runner.engine.pool
    await runner.dispose()
    assert runner.engine.pool is not pool


async def test_the_runner_does_not_close_an_injected_engine():
    """An injected engine belongs to the caller, which may still use it."""
    engine = RecordingEngine()
    runner = SqlAlchemyQueryRunner(
        DatabaseConfig(backend="postgresql"), engine=engine
    )
    await runner.dispose()
    assert engine.disposed is False


async def test_the_app_closes_the_query_runner_on_shutdown(api_harness):
    """The runner is a resource of the application: it is closed with it."""
    harness = await api_harness()
    closed: list[str] = []

    class _SpyRunner:
        async def run(self, sql, params, limit, offset):
            raise AssertionError("the runner must not run here")

        async def dispose(self) -> None:
            closed.append("disposed")

    harness.app.state.query_runner = _SpyRunner()
    async with harness.lifespan():
        pass
    assert closed == ["disposed"]
