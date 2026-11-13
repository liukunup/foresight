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

The text filter looks at the code of the statement and not at its text:
a literal holds a value, not an operation, so a read that searches for
the word "drop" is not a write.

SQLite runs on the standard library driver, because SQLAlchemy's async
driver does not expose the authorizer hook. The connection is opened
read-only (`file:...?mode=ro`), which makes a write fail in the driver
as well. PostgreSQL and MySQL have no equivalent hook, so they get the
same guarantee from a read-only transaction instead — the form of which
is the one of the backend, because MySQL applies `SET TRANSACTION` to
the next transaction and not to the current one.
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
# The statement that opens a read-only transaction, per backend. The
# form of the statement is what differs: MySQL applies `SET TRANSACTION`
# to the next transaction, not to the current one.
READ_ONLY_TRANSACTIONS = {
    "postgresql": "SET TRANSACTION READ ONLY",
    "mysql": "SET SESSION TRANSACTION READ ONLY",
}


class QueryError(Exception):
    """Raised when a statement is not a safe read-only query."""


class QueryDatabaseError(Exception):
    """Raised when the database cannot serve the statement at all.

    The caller did nothing wrong: the file is missing, the disk cannot
    be read, the database is locked. The API answers 500 for this and
    not 400, because there is nothing the caller can fix.
    """


# The errors that describe the database and not the statement the
# caller sent. Everything else that SQLite reports is about the
# statement: it could not be parsed, it could not be resolved against
# the schema, or the authorizer refused it.
DATABASE_ERRORS = (
    "unable to open database file",
    "unable to open file",
    "database is locked",
    "disk i/o error",
    "disk image is malformed",
    "file is not a database",
    "database or disk is full",
    "out of memory",
)


def _as_query_error(exc: sqlite3.Error) -> Exception:
    """Tell a statement the caller wrote from a database that cannot answer.

    The authorizer refuses a statement with "not authorized", and the
    database reports a statement it cannot parse or cannot resolve
    against the schema with a message about that statement: both are
    the caller's fault, and both are answered with a 400. The messages
    in :data:`DATABASE_ERRORS` describe the database itself — a missing
    file, a locked database, a disk that cannot be read — and are a
    failure of the server.
    """
    message = str(exc)
    lowered = message.lower()
    if "not authorized" in lowered:
        return QueryError(message)
    for marker in DATABASE_ERRORS:
        if marker in lowered:
            return QueryDatabaseError(message)
    return QueryError(message)


def _skip_quoted(statement: str, start: int, quote: str) -> int:
    """Return the index just after the quoted text that starts at `start`.

    A quote inside the text is escaped by doubling it, so a doubled
    quote does not close the text it belongs to.
    """
    index = start + 1
    while index < len(statement):
        if statement[index] == quote:
            if statement[index + 1 : index + 2] == quote:
                index += 2
                continue
            return index + 1
        index += 1
    return index


def _strip_quoted_text(statement: str) -> str:
    """Return the statement without its literals and quoted identifiers.

    A single-quoted string, a double-quoted identifier and a
    backtick-quoted identifier all hold text, not code: what is inside
    them cannot be an operation, so it must not be read as one. Each of
    them is closed by its own quote, and a quote inside one of them is
    escaped by doubling it.
    """
    parts: list[str] = []
    index = 0
    while index < len(statement):
        char = statement[index]
        if char in "'\"`":
            index = _skip_quoted(statement, index, char)
            parts.append(" ")
            continue
        parts.append(char)
        index += 1
    return "".join(parts)


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

    async def dispose(self) -> None: ...


def validate_read_only(sql: str) -> str:
    """Reject anything that is not a single read-only statement.

    The statement is returned without its trailing semicolon, so that
    the caller can run what was checked here.

    The keyword filter looks at the *code* of the statement: a literal
    is a value, not an operation, so a read that searches for the word
    "drop" is not a write. The filter is the layer that explains itself
    to the caller; the authorizer in the engine is the layer that keeps
    the dump safe.
    """
    statement = sql.strip().rstrip(";").strip()
    if not statement:
        raise QueryError("empty statement")
    code = _strip_quoted_text(statement)
    # A semicolon anywhere but at the end is what would turn one
    # statement into two, and the second one could be a write. A
    # semicolon inside a literal is a value, not a separator.
    if ";" in code:
        raise QueryError("only a single statement is allowed")
    lowered = statement.lower()
    if not lowered.startswith(READ_ONLY_PREFIX):
        raise QueryError("only SELECT and WITH statements are allowed")
    code = code.lower()
    for keyword in BLOCKED_KEYWORDS:
        if re.search(rf"\b{keyword}\b", code):
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
        try:
            conn = self._connect()
        except sqlite3.Error as exc:
            # The database could not be opened at all: the file is
            # missing or the disk cannot be read. That is a failure of
            # the server and not a bad statement.
            raise QueryDatabaseError(str(exc)) from exc
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
            raise _as_query_error(exc) from exc
        finally:
            conn.close()

    async def dispose(self) -> None:
        """Nothing to close: every query opens and closes its connection."""


def _read_only_statement(backend: str) -> str:
    """Return the statement that opens a read-only transaction.

    On PostgreSQL `SET TRANSACTION READ ONLY` applies to the current
    transaction, which is what is wanted. On MySQL the same statement
    applies to the *next* transaction and leaves the session writable,
    so the session form is the one that keeps the guarantee there.
    """
    try:
        return READ_ONLY_TRANSACTIONS[backend]
    except KeyError:
        raise ValueError(
            f"no read-only transaction is defined for the backend {backend!r}"
        ) from None


class SqlAlchemyQueryRunner:
    """Runs queries inside a read-only transaction (PostgreSQL / MySQL).

    The runner owns the engine it builds and closes it when it is
    closed. An engine that was injected belongs to the caller, which
    may still be using it, so the runner leaves that one alone.
    """

    def __init__(self, config: DatabaseConfig, *, engine: Any | None = None) -> None:
        if engine is None:
            from foresight.storage.db import create_engine

            self.engine = create_engine(config, readonly=True)
            self._owns_engine = True
        else:
            self.engine = engine
            self._owns_engine = False
        self._read_only_statement = _read_only_statement(config.backend)

    async def run(
        self, sql: str, params: dict[str, Any], limit: int, offset: int
    ) -> QueryResult:
        from sqlalchemy import text

        statement = validate_read_only(sql)
        started = time.perf_counter()
        async with self.engine.connect() as conn:
            # The transaction is read-only, so the database itself
            # refuses a write even if the statement slipped past the
            # validation.
            await conn.execute(text(self._read_only_statement))
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

    async def dispose(self) -> None:
        """Close the engine, if this runner is the one that built it."""
        if self._owns_engine:
            await self.engine.dispose()


def build_query_runner(config: DatabaseConfig) -> QueryRunner:
    """Build the runner that fits the configured backend."""
    if config.backend == "sqlite":
        return SqliteQueryRunner(config.path)
    return SqlAlchemyQueryRunner(config)


def clamp_limit(limit: int | None) -> int:
    """Bound the number of rows a query may return.

    A caller that asks for more rows than the maximum gets the maximum
    instead of an error: it asked for a page, not for a refusal.
    """
    if limit is None:
        return DEFAULT_LIMIT
    return max(1, min(limit, MAX_LIMIT))
