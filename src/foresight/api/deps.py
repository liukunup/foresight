"""FastAPI dependencies: engine access, stats service, bearer auth."""

from __future__ import annotations

import os
import secrets

from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncEngine

from foresight.storage.stats import StatsService


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
    """
    expected = os.environ.get("FORESIGHT_API_TOKEN", "").strip()
    if not expected:
        return
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    provided = header[len("Bearer ") :]
    # The comparison runs on bytes: `compare_digest` refuses strings that
    # carry non-ASCII characters, and a client that sends such a token must
    # get a 401 instead of a 500.
    if not secrets.compare_digest(provided.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(status_code=401, detail="invalid bearer token")
