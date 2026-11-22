"""FastAPI application factory."""

from __future__ import annotations

import logging
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI, Request, Response
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from foresight.api.deps import require_token
from foresight.api.metrics import (
    api_request_duration_seconds,
    api_requests_total,
    get_metrics,
    metrics_content_type,
)
from foresight.api.query import build_query_runner
from foresight.api.routers import (
    channels,
    health,
    messages,
    query,
    reactions,
    stats,
    users,
)
from foresight.config import Config
from foresight.storage.db import create_engine

logger = logging.getLogger(__name__)

# How long the counts of the dump are kept before they are read again.
# The gauges must not be older than this: a consumer that reads them
# has to see the dump as it is now, not as it was when the last scrape
# happened.
DEFAULT_METRICS_REFRESH_SECONDS = 30.0


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Close the resources the application built when it shuts down.

    Only a resource the application created itself is closed: an
    injected engine belongs to the caller, which may still be using it.
    The query runner owns the engine it built, so closing the runner
    closes that engine too.
    """
    yield
    runner = getattr(app.state, "query_runner", None)
    if runner is not None:
        await runner.dispose()
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


def _route_path(request: Request) -> str:
    """Return the path of the route that answered the request.

    The label of a request is the path of its route, with the values of
    the parameters replaced by their names: a path that carries an id
    would give one time series per id, and Prometheus would drown in
    them. A request that matched no route carries no bound either, so
    every such request is reported under one name.
    """
    scope = request.scope
    if scope.get("route") is None:
        # No route matched, so the path is whatever the caller sent.
        return "unmatched"
    path = scope.get("path")
    if not isinstance(path, str):
        return "unmatched"
    segments = path.split("/")
    for name, value in (scope.get("path_params") or {}).items():
        text = str(value)
        segments = [
            f"{{{name}}}" if segment == text else segment for segment in segments
        ]
    return "/".join(segments)


def _observe_request(request: Request, status: str, elapsed: float) -> None:
    """Count one request and record how long it took."""
    path = _route_path(request)
    api_requests_total.labels(method=request.method, path=path, status=status).inc()
    api_request_duration_seconds.labels(method=request.method, path=path).observe(elapsed)


def _refresh_interval(config: Config) -> float:
    """The interval between two reads of the counts of the dump.

    The interval comes from the configuration of the API, so that a
    deployment can trade the freshness of the gauges against the load
    the counts put on the database. A configuration that names no
    interval gets the default.
    """
    named = getattr(
        config.api, "metrics_refresh_seconds", DEFAULT_METRICS_REFRESH_SECONDS
    )
    try:
        interval = float(named)
    except (TypeError, ValueError):
        return DEFAULT_METRICS_REFRESH_SECONDS
    return max(interval, 0.0)


class _DatabaseGauges:
    """The gauges that hold the counts of the dump.

    The counts are read at most once per interval, and the gauges keep
    the value of the last read in between: a count is a full table scan
    on PostgreSQL and on MySQL, and Prometheus scrapes the endpoint
    every few seconds.

    The clock is injected, so that a test can drive the interval
    without waiting for it.
    """

    def __init__(
        self,
        engine: AsyncEngine,
        interval_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._engine = engine
        self._interval = interval_seconds
        self._clock = clock
        self._read_at: float | None = None

    async def refresh(self) -> bool:
        """Read the counts of the dump and publish them as the gauges.

        The counts are read again once the interval has passed. A
        database that cannot be read must not take the metrics down with
        it: the gauges keep the value of the last read, the reason goes
        into the log, and the next scrape tries again.
        """
        now = self._clock()
        if self._read_at is not None and now - self._read_at < self._interval:
            return False
        # The slot is taken before the counts are read, so that two
        # scrapes that arrive together do not read them twice.
        self._read_at = now
        try:
            await health.publish_counts(self._engine)
        except SQLAlchemyError as exc:
            logger.warning("could not refresh the database gauges: %s", exc)
            # The slot is given back: a database that answers again must
            # not wait for the whole interval.
            self._read_at = None
            return False
        except BaseException:
            # The same for an error nobody expected: the gauges must not
            # stay stale because of it.
            self._read_at = None
            raise
        return True


def create_app(
    config: Config,
    engine: AsyncEngine | None = None,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> FastAPI:
    """Build the read-only API application.

    The API never writes: when it opens the database itself it uses the
    read-only URL, so the driver itself rejects any write statement. An
    engine that is injected — the collector and the API in one process —
    is used as it is, and it is not disposed by the application either.

    The schema is not created here either: the API is a reader, the tables
    belong to `foresight init-db` and to the collector.

    The clock is injected, so that a test can drive the interval of the
    gauges of the database without waiting for it.
    """
    app = FastAPI(
        title="ForeSight API",
        version="0.1.0",
        lifespan=_lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )
    if engine is None:
        engine = create_engine(config.database, readonly=True)
        owns_engine = True
    else:
        owns_engine = False
    app.state.engine = engine
    app.state.owns_engine = owns_engine
    app.state.config = config
    # The gauges of the database are refreshed by the metrics endpoint,
    # and not on every scrape: a count is a full table scan on
    # PostgreSQL and on MySQL.
    gauges = _DatabaseGauges(engine, _refresh_interval(config), clock=clock)
    app.state.database_gauges = gauges
    # The free-form SQL endpoint runs on a runner of its own: the
    # SQLite runner opens a read-only connection with an authorizer
    # that denies every write, which the async engine cannot do.
    app.state.query_runner = build_query_runner(config.database)
    include_api_router(app, health.router, tags=["health"])
    include_api_router(app, messages.router, tags=["messages"])
    include_api_router(app, users.router, tags=["users"])
    include_api_router(app, channels.router, tags=["channels"])
    include_api_router(app, reactions.router, tags=["reactions"])
    include_api_router(app, stats.router, tags=["stats"])
    include_api_router(app, query.router, tags=["query"])

    @app.middleware("http")
    async def _count_requests(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Count every request and measure how long it took.

        The middleware sits outside the routers, so it also sees the
        requests that are refused: an API that answers 401 is still an API
        that was asked a question.
        """
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            # The request failed: the server answers 500, and a request
            # that failed is a request that was made.
            _observe_request(request, "500", time.perf_counter() - started)
            raise
        _observe_request(request, str(response.status_code), time.perf_counter() - started)
        return response

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        """Say that the service is alive.

        The route sits outside `/api`, so the bearer token of the API
        does not apply to it: a liveness probe carries no token, and it
        must not need one.

        It says nothing about the dump — no counts, no size, no
        timestamp — because a liveness probe must not tell a stranger
        what the archive holds.
        """
        return {"status": "ok"}

    @app.get("/metrics", include_in_schema=False)
    async def metrics(request: Request) -> Response:
        """Serve the metrics in the Prometheus exposition format.

        The endpoint sits outside `/api`, so the bearer token of the API
        does not apply to it: Prometheus scrapes it without a token. A
        metrics token guards it separately, for the deployments that do
        not want the endpoint open.

        The gauges of the database are refreshed here: the health
        endpoint sits behind the bearer token, so a probe that has no
        token would never reach it and the gauges would stay at zero
        while the dump holds rows. The counts are read at most once per
        interval, and the gauges keep the value of the last read in
        between — a scrape must not turn into a full table scan on the
        database.
        """
        expected = getattr(app.state.config.api, "metrics_token", "") or ""
        if expected:
            supplied = request.headers.get("x-metrics-token", "")
            # The comparison runs on bytes: `compare_digest` refuses
            # strings that carry non-ASCII characters, and a client that
            # sends such a token must get a 401 instead of a 500.
            if not secrets.compare_digest(
                supplied.encode("utf-8"), expected.encode("utf-8")
            ):
                return Response(
                    content=b'{"detail":"invalid metrics token"}',
                    status_code=401,
                    media_type="application/json",
                )
        await gauges.refresh()
        return Response(content=get_metrics(), media_type=metrics_content_type())

    return app
