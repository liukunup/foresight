"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from foresight.api.deps import require_token
from foresight.api.routers import channels, health, messages, reactions, users
from foresight.config import Config
from foresight.storage.db import create_engine


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Close the engine the application built when it shuts down.

    Only an engine the application created itself is disposed: an
    injected engine belongs to the caller, which may still be using it.
    """
    yield
    if getattr(app.state, "owns_engine", False):
        engine = getattr(app.state, "engine", None)
        if engine is not None:
            await engine.dispose()


def include_api_router(app: FastAPI, router: APIRouter, *, tags: Sequence[str]) -> None:
    """Mount a router under `/api` with the bearer token applied to it.

    The dependency lives here and not on the routes: a router added
    later cannot forget the protection, because the mount site is the
    only place it is declared.
    """
    app.include_router(
        router,
        prefix="/api",
        tags=list(tags),
        dependencies=[Depends(require_token)],
    )


def create_app(config: Config, engine: AsyncEngine | None = None) -> FastAPI:
    """Build the read-only API application.

    The API never writes: when it opens the database itself it uses the
    read-only URL, so the driver itself rejects any write statement. An
    engine that is injected — the collector and the API in one process —
    is used as it is, and it is not disposed by the application either.

    The schema is not created here either: the API is a reader, the tables
    belong to `foresight init-db` and to the collector.
    """
    app = FastAPI(title="ForeSight API", version="0.1.0", lifespan=_lifespan)
    if engine is None:
        engine = create_engine(config.database, readonly=True)
        owns_engine = True
    else:
        owns_engine = False
    app.state.engine = engine
    app.state.owns_engine = owns_engine
    app.state.config = config
    include_api_router(app, health.router, tags=["health"])
    include_api_router(app, messages.router, tags=["messages"])
    include_api_router(app, users.router, tags=["users"])
    include_api_router(app, channels.router, tags=["channels"])
    include_api_router(app, reactions.router, tags=["reactions"])
    return app
