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

Every statement is bounded by a deadline, because the endpoint is often
open: a statement that runs for ever would hold a thread of the executor
or a connection of the pool, and a caller that repeats it would take the
service down with it. The database stops the statement where it can — on
PostgreSQL and on MySQL it is the server that cancels it — and SQLite,
which has no such setting, is stopped from the inside by the progress
handler of the driver and from the outside by an interrupt of the
connection.

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
import logging
import math
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Protocol

from foresight.config import DatabaseConfig, sqlite_readonly_uri

logger = logging.getLogger(__name__)

READ_ONLY_PREFIX = ("select", "with")
MAX_LIMIT = 500
DEFAULT_LIMIT = 100

# The largest offset a page may ask for. The offset is pushed into the
# statement, so the database never hands over more rows than the page
# needs — but a database that has to skip a billion rows before it
# reaches the first one is still busy for a long time. The offset is
# therefore bounded as well: a caller that asks for more than the
# maximum gets the page at the maximum instead of a long wait.
MAX_OFFSET = 100_000

# The deadline of a statement, in seconds. A statement that runs longer
# is stopped and the caller is told about it.
QUERY_TIMEOUT_SECONDS = 5.0

# The message a statement that hit the deadline is answered with.
DEADLINE_MESSAGE = "the statement took too long and was stopped"

# How often the progress handler of SQLite is called, in instructions of
# the virtual machine. The handler is cheap, and it is called often
# enough for the deadline to be noticed while the statement runs.
PROGRESS_INTERVAL = 1000

# How long the thread of a stopped statement is waited for, in seconds.
# The interrupt of the connection releases the thread; the wait is only
# there so that the connection is not closed under the statement.
INTERRUPT_GRACE = 5.0

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

# A statement that carries its own window ends with one of these. The
# window of such a statement is the window of the answer: a second
# LIMIT would be a syntax error, and an OFFSET would move the rows the
# caller asked for.
OWN_LIMIT = re.compile(
    r"\blimit\b\s+(:\w+|\?|\d+)\s*(offset\s+(:\w+|\?|\d+)\s*)?(--[^\n]*)?$",
    re.IGNORECASE,
)
OWN_FETCH = re.compile(
    r"\bfetch\s+first\s+(:\w+|\?|\d+)\s+rows\s+only\s*(--[^\n]*)?$",
    re.IGNORECASE,
)

# The marker of a dollar quoted string of PostgreSQL.
DOLLAR_QUOTE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$")


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

# The codes the databases use for a statement they cancelled because it
# ran too long. PostgreSQL answers with the SQLSTATE of a cancelled
# query, MySQL with the number of its own error.
TIMEOUT_CODES = frozenset({"57014", "3024", "HYT00", "HYT01"})


def _as_query_error(exc: sqlite3.Error) -> Exception:
    """Tell a statement the caller wrote from a database that cannot answer.

    The authorizer refuses a statement with "not authorized", and the
    database reports a statement it cannot parse or cannot resolve
    against the schema with a message about that statement: both are
    the caller's fault, and both are answered with a 400. The messages
    in :data:`DATABASE_ERRORS` describe the database itself — a missing
    file, a locked database, a disk that cannot be read — and are a
    failure of the server.

    An interrupted statement is the deadline of the runner: the caller
    asked for more than the endpoint can give, so it is a 400 as well.
    """
    message = str(exc)
    lowered = message.lower()
    if "interrupt" in lowered:
        return QueryError(DEADLINE_MESSAGE)
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


def _skip_line_comment(statement: str, start: int) -> int:
    """Return the index just after the line comment that starts at `start`.

    A line comment ends with the line it is on: the newline is part of
    the statement that follows it.
    """
    end = statement.find("\n", start)
    return len(statement) if end == -1 else end + 1


def _skip_block_comment(statement: str, start: int) -> int:
    """Return the index just after the block comment that starts at `start`.

    A block comment that is never closed runs to the end of the
    statement: the statement is broken anyway.
    """
    end = statement.find("*/", start + 2)
    return len(statement) if end == -1 else end + 2


def _skip_dollar_quoted(statement: str, start: int) -> int | None:
    """Return the index just after the dollar quoted string at `start`.

    The strings of PostgreSQL are written between two markers: `$$` or
    a marker with a tag, as in `$tag$`. None of them is closed by a
    quote, so the scanner has to know about them: a `drop` between two
    markers is a value and not an operation.

    Returns None when the text at `start` is not the beginning of such
    a string — the placeholders of PostgreSQL, as in `$1`, are not.
    """
    match = DOLLAR_QUOTE.match(statement, start)
    if match is None:
        return None
    marker = match.group(0)
    end = statement.find(marker, match.end())
    if end == -1:
        return None
    return end + len(marker)


def _strip_quoted_text(statement: str) -> str:
    """Return the statement without its literals, its identifiers and its comments.

    A single-quoted string, a double-quoted identifier and a
    backtick-quoted identifier all hold text, not code: what is inside
    them cannot be an operation, so it must not be read as one. Each of
    them is closed by its own quote, and a quote inside one of them is
    escaped by doubling it.

    A comment holds a remark, and a remark is not code either: a `;` or
    a `drop` inside a comment is not a second statement and not an
    operation. The comments are therefore taken out before the
    statement is read — and a quote inside a comment does not open a
    literal that would swallow the rest of the statement.
    """
    parts: list[str] = []
    index = 0
    while index < len(statement):
        char = statement[index]
        if char in "'\"`":
            index = _skip_quoted(statement, index, char)
            parts.append(" ")
            continue
        if statement.startswith("--", index):
            index = _skip_line_comment(statement, index)
            parts.append(" ")
            continue
        if statement.startswith("/*", index):
            index = _skip_block_comment(statement, index)
            parts.append(" ")
            continue
        if char == "$":
            end = _skip_dollar_quoted(statement, index)
            if end is not None:
                index = end
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


def apply_window(statement: str, limit: int, offset: int) -> str:
    """Give the statement the window of the caller.

    The limit and the offset are pushed into the statement, so that the
    database hands over the page of the caller and not the whole table:
    a statement without a window would make the database walk every row
    of the table before the first row of the page is even read.

    A statement that carries a window of its own keeps it: a second
    LIMIT would be a syntax error, and the window of the statement is
    then the window of the answer.

    The window is written on a line of its own, so that a comment at the
    end of the statement does not swallow it.
    """
    code = _strip_quoted_text(statement)
    if OWN_LIMIT.search(code) or OWN_FETCH.search(code):
        return statement
    window = f"LIMIT {limit}"
    if offset:
        window = f"{window} OFFSET {offset}"
    return f"{statement}\n{window}"


def clamp_limit(limit: int | None) -> int:
    """Bound the number of rows a query may return.

    A caller that asks for more rows than the maximum gets the maximum
    instead of an error: it asked for a page, not for a refusal.
    """
    if limit is None:
        return DEFAULT_LIMIT
    return max(1, min(limit, MAX_LIMIT))


def clamp_offset(offset: int) -> int:
    """Bound the number of rows a query skips before its page.

    The offset is pushed into the statement, so the database does not
    hand over the rows before the page — but it still has to walk them.
    A caller that asks for an offset beyond the maximum gets the page at
    the maximum instead of a long wait.
    """
    return max(0, min(offset, MAX_OFFSET))


def _authorizer(
    action: int, arg1: Any, arg2: Any, dbname: Any, trigger: Any
) -> int:
    """Allow the reads and nothing else.

    The authorizer sees every action the statement asks the engine for,
    so a write that the text filter did not catch — a `WITH ... DELETE`,
    a `PRAGMA`, an `ATTACH` — is denied here, in the engine, before it
    reaches the data.
    """
    allowed = {
        sqlite3.SQLITE_SELECT,
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_FUNCTION,
    }
    if action in allowed:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def _deadline_handler(deadline: float) -> Any:
    """Return the progress handler that stops a statement in time.

    The handler is called by the driver while the statement runs: it
    raises as soon as the deadline has passed, and the driver aborts the
    statement. SQLite reports the abort as an interrupted statement, and
    the runner turns that into the answer of the deadline.
    """

    def handler() -> int:
        if time.monotonic() >= deadline:
            raise sqlite3.OperationalError(DEADLINE_MESSAGE)
        return 0

    return handler


async def _drain(future: Any) -> None:
    """Wait for the thread of a stopped statement to come back.

    The thread of the executor cannot be cancelled, so the connection is
    interrupted and the thread is waited for: the connection must not be
    closed while the statement still runs on it.
    """
    try:
        await asyncio.wait_for(asyncio.shield(future), timeout=INTERRUPT_GRACE)
    except asyncio.TimeoutError:
        logger.warning("the thread of a stopped statement did not come back")
    except Exception:
        # The statement was interrupted, and the error it raised says
        # why: the caller is told about the deadline instead.
        pass


class SqliteQueryRunner:
    """Runs queries on a read-only SQLite connection."""

    def __init__(self, path: str, *, timeout: float | None = None) -> None:
        self._path = path
        self._timeout = QUERY_TIMEOUT_SECONDS if timeout is None else timeout

    def _connect(self) -> sqlite3.Connection:
        # The connection is opened in the thread of the event loop and
        # used in a worker thread, so the check of the driver is
        # switched off: only one thread uses the connection at a time,
        # and the interrupt of the deadline is the one operation the
        # driver allows from another thread.
        conn = sqlite3.connect(
            sqlite_readonly_uri(self._path), uri=True, check_same_thread=False
        )
        conn.row_factory = sqlite3.Row
        conn.set_authorizer(_authorizer)
        return conn

    async def run(
        self, sql: str, params: dict[str, Any], limit: int, offset: int
    ) -> QueryResult:
        statement = apply_window(
            validate_read_only(sql), clamp_limit(limit), clamp_offset(offset)
        )
        started = time.perf_counter()
        deadline = started + self._timeout
        try:
            conn = self._connect()
        except sqlite3.Error as exc:
            # The database could not be opened at all: the file is
            # missing or the disk cannot be read. That is a failure of
            # the server and not a bad statement.
            raise QueryDatabaseError(str(exc)) from exc
        try:
            result = await self._execute(conn, statement, params, deadline)
        finally:
            try:
                conn.close()
            except sqlite3.Error:
                # The connection is already gone: there is nothing left
                # to close, and the statement has been stopped anyway.
                pass
        result.elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
        return result

    async def _execute(
        self,
        conn: sqlite3.Connection,
        statement: str,
        params: dict[str, Any],
        deadline: float,
    ) -> QueryResult:
        """Run the statement with a deadline.

        The driver is synchronous, so the query runs in a worker thread:
        a slow statement must not stop the event loop from serving the
        other requests. The thread cannot be cancelled, so the deadline
        is enforced from the inside — by the progress handler of the
        driver — and from the outside, by the interrupt of the
        connection, which is what releases the thread.
        """
        conn.set_progress_handler(_deadline_handler(deadline), PROGRESS_INTERVAL)
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(None, self._run_sync, conn, statement, params)
        try:
            return await asyncio.wait_for(
                asyncio.shield(future), timeout=max(0.0, deadline - loop.time())
            )
        except asyncio.TimeoutError:
            conn.interrupt()
            await _drain(future)
            raise QueryError(DEADLINE_MESSAGE) from None

    def _run_sync(
        self, conn: sqlite3.Connection, statement: str, params: dict[str, Any]
    ) -> QueryResult:
        try:
            cursor = conn.execute(statement, params)
            columns = [description[0] for description in cursor.description or []]
            rows = cursor.fetchall()
            return QueryResult(
                columns=columns,
                rows=[list(row) for row in rows],
                row_count=len(rows),
                elapsed_ms=0.0,
            )
        except sqlite3.Error as exc:
            raise _as_query_error(exc) from exc

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


def _deadline_statement(backend: str, timeout: float) -> str:
    """Return the statement that bounds a statement on this backend.

    PostgreSQL knows the bound as a setting of the transaction: the
    server cancels the statement when it runs too long, and the
    connection is free again. MySQL knows it as a setting of the
    session, in milliseconds, and it applies to the read-only statements
    only — which is what the endpoint runs.
    """
    seconds = max(1, math.ceil(timeout))
    if backend == "postgresql":
        return f"SET LOCAL statement_timeout = '{seconds}s'"
    if backend == "mysql":
        return f"SET SESSION max_execution_time = {seconds * 1000}"
    raise ValueError(f"no deadline is defined for the backend {backend!r}")


def _safe_message(exc: Exception) -> str:
    """The message of a database error, without the statement it carries.

    SQLAlchemy writes the statement and the parameters of the caller
    into the message of its errors. That text must not travel with the
    error: it ends up in the log of the server, and a statement may
    carry a newline — which is a way of forging a log line.
    """
    text = str(exc)
    index = text.find("[SQL:")
    if index >= 0:
        text = text[:index]
    text = text.strip()
    return text or type(exc).__name__


def _is_timeout(exc: Exception) -> bool:
    """Whether the database cancelled the statement because it was too slow."""
    original = getattr(exc, "orig", None)
    if original is None:
        return False
    code = getattr(original, "sqlstate", None)
    if isinstance(code, str) and code in TIMEOUT_CODES:
        return True
    args = getattr(original, "args", ())
    if args and isinstance(args[0], int) and str(args[0]) in TIMEOUT_CODES:
        return True
    return False


def _as_sqlalchemy_error(exc: Exception) -> Exception:
    """Tell a statement the caller wrote from a database that cannot answer.

    A statement the database cannot parse, a table it does not know and
    a parameter the caller forgot are the mistakes of the caller: they
    are answered with a 400. A statement the database cancelled because
    it took too long is a 400 as well — the caller asked for more than
    the endpoint can give. Everything else describes the database
    itself and is a 500.
    """
    from sqlalchemy.exc import (
        CompileError,
        DBAPIError,
        InvalidRequestError,
        ProgrammingError,
        StatementError,
    )

    if _is_timeout(exc):
        return QueryError(DEADLINE_MESSAGE)
    if isinstance(exc, (ProgrammingError, InvalidRequestError, CompileError)):
        return QueryError(_safe_message(exc))
    if isinstance(exc, StatementError) and not isinstance(exc, DBAPIError):
        # A bind parameter without a value is a mistake of the caller,
        # and it is reported as a `StatementError` that is not an error
        # of the database.
        return QueryError(_safe_message(exc))
    return QueryDatabaseError(_safe_message(exc))


class SqlAlchemyQueryRunner:
    """Runs queries inside a read-only transaction (PostgreSQL / MySQL).

    The runner owns the engine it builds and closes it when it is
    closed. An engine that was injected belongs to the caller, which
    may still be using it, so the runner leaves that one alone.
    """

    def __init__(
        self,
        config: DatabaseConfig,
        *,
        engine: Any | None = None,
        timeout: float | None = None,
    ) -> None:
        if engine is None:
            from foresight.storage.db import create_engine

            self.engine = create_engine(config, readonly=True)
            self._owns_engine = True
        else:
            self.engine = engine
            self._owns_engine = False
        self._read_only_statement = _read_only_statement(config.backend)
        self._deadline_statement = _deadline_statement(
            config.backend, QUERY_TIMEOUT_SECONDS if timeout is None else timeout
        )

    async def run(
        self, sql: str, params: dict[str, Any], limit: int, offset: int
    ) -> QueryResult:
        from sqlalchemy import text
        from sqlalchemy.exc import SQLAlchemyError

        statement = apply_window(
            validate_read_only(sql), clamp_limit(limit), clamp_offset(offset)
        )
        started = time.perf_counter()
        try:
            async with self.engine.connect() as conn:
                # The transaction is read-only, so the database itself
                # refuses a write even if the statement slipped past the
                # validation.
                await conn.execute(text(self._read_only_statement))
                # The statement is bounded by the database itself: a
                # statement that runs too long is cancelled there, and
                # the connection comes back to the pool.
                await conn.execute(text(self._deadline_statement))
                result = await conn.execute(text(statement), dict(params))
                columns = list(result.keys())
                rows = result.fetchall()
        except SQLAlchemyError as exc:
            error = _as_sqlalchemy_error(exc)
            if isinstance(error, QueryDatabaseError):
                # Only the kind of the failure is logged: the message of
                # the error carries the statement and the parameters of
                # the caller, and those must not reach the log.
                logger.warning(
                    "the database could not run the statement: %s", type(exc).__name__
                )
            raise error from None
        return QueryResult(
            columns=columns,
            rows=[list(row) for row in rows],
            row_count=len(rows),
            elapsed_ms=round((time.perf_counter() - started) * 1000, 3),
        )

    async def dispose(self) -> None:
        """Close the engine, if this runner is the one that built it."""
        if self._owns_engine:
            await self.engine.dispose()


def build_query_runner(
    config: DatabaseConfig, *, timeout: float | None = None
) -> QueryRunner:
    """Build the runner that fits the configured backend."""
    if config.backend == "sqlite":
        return SqliteQueryRunner(config.path, timeout=timeout)
    return SqlAlchemyQueryRunner(config, timeout=timeout)
