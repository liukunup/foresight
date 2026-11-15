"""Fakes that the tests of the query runners share.

The runners talk to a database through SQLAlchemy, and the tests of the
rules of the runners — the deadline, the errors, the window of the
caller — do not need a database to pin those rules down. The fakes below
record what the runner asked the database to do and can be told to fail
with the error the test wants to see.
"""

from __future__ import annotations

from typing import Any


class RecordingResult:
    """The result of a statement that returned no rows."""

    def keys(self) -> list[str]:
        return ["x"]

    def fetchall(self) -> list[list[object]]:
        return []

    def fetchmany(self, size: int) -> list[list[object]]:
        return []


class RecordingConnection:
    """A connection that records the statements it was asked to run."""

    def __init__(self, statements: list[str]) -> None:
        self._statements = statements

    async def __aenter__(self) -> "RecordingConnection":
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    async def execute(
        self, statement: object, parameters: object = None
    ) -> RecordingResult:
        self._statements.append(str(statement))
        return RecordingResult()


class FailingConnection(RecordingConnection):
    """A connection that refuses the statement it is given."""

    def __init__(self, statements: list[str], error: Exception) -> None:
        super().__init__(statements)
        self._error = error

    async def execute(
        self, statement: object, parameters: object = None
    ) -> RecordingResult:
        self._statements.append(str(statement))
        raise self._error


class RecordingEngine:
    """An engine that records the statements it was asked to run."""

    def __init__(self, error: Exception | None = None) -> None:
        self.statements: list[str] = []
        self.disposed = False
        self._error = error

    def connect(self) -> RecordingConnection:
        if self._error is None:
            return RecordingConnection(self.statements)
        return FailingConnection(self.statements, self._error)

    async def dispose(self) -> None:
        self.disposed = True


def any_error() -> Any:
    """An error of the database, for the tests that only need one."""
    return Exception("boom")
