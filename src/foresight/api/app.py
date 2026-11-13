"""FastAPI application factory."""

from __future__ import annotations

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from foresight.api.routers import health, messages
from foresight.config import Config


def create_app(config: Config, engine: AsyncEngine | None = None) -> FastAPI:
    """Build the read-only API application.

    The API never writes: when it opens the database itself it uses the
    read-only URL, so the driver itself rejects any write statement. An
    engine that is injected — the collector and the API in one process —
    is used as it is.

    The schema is not created here either: the API is a reader, the tables
    belong to `foresight init-db` and to the collector.
    """
    app = FastAPI(title="ForeSight API", version="0.1.0")
    if engine is None:
        engine = create_async_engine(
            config.database.readonly_sqlalchemy_url, future=True, pool_pre_ping=True
        )
    app.state.engine = engine
    app.state.config = config
    app.include_router(health.router, prefix="/api", tags=["health"])
    app.include_router(messages.router, prefix="/api", tags=["messages"])
    return app
