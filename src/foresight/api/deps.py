"""FastAPI dependencies: engine access, stats service, bearer auth, time bounds."""

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
# and an optional offset.
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")

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
            return value
    raise HTTPException(
        status_code=422,
        detail=(
            f"invalid '{name}': {value!r} is neither an ISO-8601 timestamp "
            "nor a date of the form YYYY-MM-DD"
        ),
    )


def time_bounds(
    since: str | None = None,
    until: str | None = None,
) -> TimeBounds:
    """Read and normalize the time bounds of a request.

    Every endpoint that filters by time takes this dependency, so the
    bounds are read and normalized in one place: an endpoint cannot
    forget the normalization, and the endpoints cannot drift apart.
    """
    return TimeBounds(
        since=_normalize_bound(since, "since", end_of_day=False) if since else None,
        until=_normalize_bound(until, "until", end_of_day=True) if until else None,
    )


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
