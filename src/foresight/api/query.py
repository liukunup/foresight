"""Free-form read-only SQL execution.

This module owns the SQL the API runs: the routers turn a request into a
call and a result into a response, they never build a statement of their
own.

The safety of the endpoint rests on two layers, and both of them are
needed. The statement validation rejects the writes a caller would
notice — a `DROP`, a second statement — and it gives the caller an
answer it can act upon. The SQLite authorizer is the layer that cannot
be talked out of: it sits in the engine and denies every action that is
not a read, so a statement that slips past the text filter still cannot
change the dump.

SQLite runs on the standard library driver, because SQLAlchemy's async
driver does not expose the authorizer hook. The connection is opened
read-only (`file:...?mode=ro`), which makes a write fail in the driver
as well. PostgreSQL and MySQL have no equivalent hook, so they get the
same guarantee from a read-only transaction instead.
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Protocol

from foresight.config import DatabaseConfig

READ_ONLY_PREFIX = ("select", "with")
MAX_LIMIT = 500
DEFAULT_LIMIT = 100
BLOCKED_KEYWORDS = (
    "insert",
    "update",
    "delete",
    "drop",
    "alter",
    "create",
    "replace",
    "truncate",
    "attach",
    "detach",
    "pragma",
    "vacuum",
    "grant",
    "revoke",
)


class QueryError(Exception):
    """Raised when a statement is not a safe read-only query."""


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    elapsed_ms: float


class QueryRunner(Protocol):
    async def run(
        self, sql: str, params: dict[str, Any], limit: int, offset: int
    ) -> QueryResult: ...


def validate_read_only(sql: str) -> str:
    """Reject anything that is not a single read-only statement.

    The statement is returned without its trailing semicolon, so that
    the caller can run what was checked here.
    """
    statement = sql.strip().rstrip(";").strip()
    if not statement:
        raise QueryError("empty statement")
    # A semicolon anywhere but at the end is what would turn one
    # statement into two, and the second one could be a write.
    if ";" in statement:
        raise QueryError("only a single statement is allowed")
    lowered = statement.lower()
    if not lowered.startswith(READ_ONLY_PREFIX):
        raise QueryError("only SELECT and WITH statements are allowed")
    for keyword in BLOCKED_KEYWORDS:
        if re.search(rf"\b{keyword}\b", lowered):
            raise QueryError(f"statement contains forbidden keyword: {keyword}")
    return statement


class SqliteQueryRunner:
    """Runs queries on a read-only SQLite connection."""

    def __init__(self, path: str) -> None:
        self._path = path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{self._path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row

        def authorizer(
            action: int, arg1: Any, arg2: Any, dbname: Any, trigger: Any
        ) -> int:
            """Allow the reads and nothing else.

            The authorizer sees every action the statement asks the
            engine for, so a write that the text filter did not catch —
            a `WITH ... DELETE`, a `PRAGMA`, an `ATTACH` — is denied
            here, in the engine, before it reaches the data.
            """
            allowed = {
                sqlite3.SQLITE_SELECT,
                sqlite3.SQLITE_READ,
                sqlite3.SQLITE_FUNCTION,
            }
            if action in allowed:
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY

        conn.set_authorizer(authorizer)
        return conn

    async def run(
        self, sql: str, params: dict[str, Any], limit: int, offset: int
    ) -> QueryResult:
        statement = validate_read_only(sql)
        started = time.perf_counter()
        # The driver is synchronous, so the query runs in a worker
        # thread: a slow statement must not stop the event loop from
        # serving the other requests.
        result = await asyncio.to_thread(
            self._run_sync, statement, params, limit, offset
        )
        result.elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
        return result

    def _run_sync(
        self, statement: str, params: dict[str, Any], limit: int, offset: int
    ) -> QueryResult:
        conn = self._connect()
        try:
            cursor = conn.execute(statement, params)
            columns = [description[0] for description in cursor.description or []]
            rows = cursor.fetchmany(limit + offset)
            window = rows[offset : offset + limit]
            return QueryResult(
                columns=columns,
                rows=[list(row) for row in window],
                row_count=len(window),
                elapsed_ms=0.0,
            )
        except sqlite3.Error as exc:
            raise QueryError(str(exc)) from exc
        finally:
            conn.close()


class SqlAlchemyQueryRunner:
    """Runs queries inside a read-only transaction (PostgreSQL / MySQL)."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    async def run(
        self, sql: str, params: dict[str, Any], limit: int, offset: int
    ) -> QueryResult:
        from sqlalchemy import text

        statement = validate_read_only(sql)
        started = time.perf_counter()
        async with self._engine.connect() as conn:
            # The transaction is read-only, so the database itself
            # refuses a write even if the statement slipped past the
            # validation.
            await conn.execute(text("SET TRANSACTION READ ONLY"))
            result = await conn.execute(
                text(statement), {k: v for k, v in params.items()}
            )
            columns = list(result.keys())
            rows = result.fetchmany(limit + offset)
        window = rows[offset : offset + limit]
        return QueryResult(
            columns=columns,
            rows=[list(row) for row in window],
            row_count=len(window),
            elapsed_ms=round((time.perf_counter() - started) * 1000, 3),
        )


def build_query_runner(config: DatabaseConfig) -> QueryRunner:
    """Build the runner that fits the configured backend."""
    if config.backend == "sqlite":
        return SqliteQueryRunner(config.path)
    from foresight.storage.db import create_engine

    return SqlAlchemyQueryRunner(create_engine(config))


def clamp_limit(limit: int | None) -> int:
    """Bound the number of rows a query may return.

    A caller that asks for more rows than the maximum gets the maximum
    instead of an error: it asked for a page, not for a refusal.
    """
    if limit is None:
        return DEFAULT_LIMIT
    return max(1, min(limit, MAX_LIMIT))
