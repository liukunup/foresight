"""FastAPI dependencies: engine access, stats service, bearer auth, time bounds.

The time bounds are normalized in one place, and the command line uses the
same normalization, so a bound means the same thing in the API and in the
terminal.
"""

from __future__ import annotations

import os
import re
import secrets
from dataclasses import dataclass
from datetime import date, datetime

from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncEngine

from foresight.storage.stats import StatsService

# RFC 7235 §3.1: a 401 names the scheme the client is expected to use.
_CHALLENGE = {"WWW-Authenticate": "Bearer"}

# A date without a time: `2026-11-01`.
_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# A date with a time: `2026-11-01T10:00:00`, with an optional fraction
# and an optional offset. The separator may be a `T` or a space.
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")

# The separator between the date and the time of a bound. The stored
# timestamps carry a `T`, and a space sorts before a `T` — so a bound that
# keeps its space would sort before every timestamp of the day it names
# and would exclude that day without saying so.
_SEPARATOR = re.compile(r"[T ]")

# The first and the last moment of a day. The stored timestamps carry
# microseconds, so the bound of a day has to carry them too: a bound
# without them would sort before a message of the same second.
_START_OF_DAY = "T00:00:00+00:00"
_END_OF_DAY = "T23:59:59.999999+00:00"


@dataclass(frozen=True)
class TimeBounds:
    """The time bounds of a request, normalized."""

    since: str | None = None
    until: str | None = None


def _normalize_bound(value: str, name: str, *, end_of_day: bool) -> str:
    """Normalize one time bound into a string the database can compare.

    The timestamps are stored as fixed-width UTC ISO-8601 strings and the
    bounds are compared as strings. A full timestamp is therefore used as
    it is, and a bare date is expanded to the first second of the day
    (`since`) or to its last one (`until`): a date names the whole day,
    and the bare date on its own sorts before every message of that day.

    A value that is neither a timestamp nor a date is refused: a bound
    that cannot be read must not answer with a silently empty range.

    The separator of a timestamp is normalized to the `T` the stored
    timestamps use: the bounds are compared as strings, and a space sorts
    before a `T`.
    """
    if _DATE_ONLY.match(value):
        try:
            date.fromisoformat(value)
        except ValueError:
            pass
        else:
            return value + (_END_OF_DAY if end_of_day else _START_OF_DAY)
    elif _TIMESTAMP.match(value):
        try:
            datetime.fromisoformat(value)
        except ValueError:
            pass
        else:
            return _SEPARATOR.sub("T", value, count=1)
    raise ValueError(
        f"invalid '{name}': {value!r} is neither an ISO-8601 timestamp "
        "nor a date of the form YYYY-MM-DD"
    )


def normalize_time_bounds(
    since: str | None = None,
    until: str | None = None,
) -> TimeBounds:
    """Read and normalize the time bounds.

    The API and the CLI normalize the bounds the same way, so a bound
    means the same thing in both: a bare date names a whole day, and a
    bound that cannot be read is refused instead of answering with a
    silently empty range.

    Raises a `ValueError` when a bound cannot be read — the caller
    decides how to report it.
    """
    return TimeBounds(
        since=_normalize_bound(since, "since", end_of_day=False) if since else None,
        until=_normalize_bound(until, "until", end_of_day=True) if until else None,
    )


def time_bounds(
    since: str | None = None,
    until: str | None = None,
) -> TimeBounds:
    """Read and normalize the time bounds of a request.

    Every endpoint that filters by time takes this dependency, so the
    bounds are read and normalized in one place: an endpoint cannot
    forget the normalization, and the endpoints cannot drift apart.

    A bound that cannot be read is refused with a 422 that names the
    parameter that is wrong.
    """
    try:
        return normalize_time_bounds(since, until)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def get_engine(request: Request) -> AsyncEngine:
    """Return the engine the application was created with."""
    engine = getattr(request.app.state, "engine", None)
    if engine is None:
        raise HTTPException(status_code=500, detail="database engine is not configured")
    return engine


def get_stats(request: Request) -> StatsService:
    """Return the analytics service over the application engine."""
    return StatsService(get_engine(request))


def require_token(request: Request) -> None:
    """Validate the bearer token when one is configured.

    Without a token the API is open: that is the localhost mode, where the
    API only listens on the loopback interface. A token turns every route
    into a protected one, so a wrong or missing token is answered with 401.

    The token is compared as it stands: a token of blanks is a token, not
    an unset variable, because stripping it would silently open an API
    that was meant to be protected. A client cannot send such a token
    anyway — a header value carries no leading or trailing blanks.
    """
    expected = os.environ.get("FORESIGHT_API_TOKEN", "")
    if not expected:
        return
    header = request.headers.get("authorization", "")
    scheme, _, provided = header.partition(" ")
    # RFC 7235 §2.1: the authentication scheme is case insensitive.
    if scheme.lower() != "bearer":
        raise HTTPException(
            status_code=401, detail="missing bearer token", headers=_CHALLENGE
        )
    # The comparison runs on bytes: `compare_digest` refuses strings that
    # carry non-ASCII characters, and a client that sends such a token must
    # get a 401 instead of a 500.
    if not secrets.compare_digest(provided.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(
            status_code=401, detail="invalid bearer token", headers=_CHALLENGE
        )
