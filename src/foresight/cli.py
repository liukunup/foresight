"""Command line entry points."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn, Optional

import typer

from foresight.config import Config, ConfigError, DatabaseConfig, load_config
from foresight.logging_setup import setup_logging

logger = logging.getLogger(__name__)

app = typer.Typer(add_completion=False, help="ForeSight — Discord message dump service")

# The number of messages that are queued for the analysis again when the
# collector starts. A message that was never analysed must not wait for
# the next restart of the operator (spec §10.3).
PENDING_REQUEUE_LIMIT = 1000

# How long the collector waits before it tries to reach Discord again, in
# seconds. A Discord outage must not take the API down: the collector is
# tried again after this pause, and the API keeps serving in the
# meantime.
COLLECTOR_RETRY_SECONDS = 30.0


def _load(config_path: Optional[str]) -> Config:
    config = load_config(config_path)
    setup_logging(config.logging.level)
    return config


def _fail(message: str) -> NoReturn:
    """Report a failure as one line on stderr and stop the command."""
    # The message of the original exception can span several lines — the
    # ones of SQLAlchemy do — but the report stays a single line.
    typer.secho(f"error: {' '.join(message.split())}", fg=typer.colors.RED, err=True)
    raise typer.Exit(code=1)


def _time_bound(value: str | None, option: str, *, upper: bool = False) -> str | None:
    """The time bound of the command, read the way the API reads it.

    A bare date names a whole day, and a bound that cannot be read is
    refused instead of answering with an empty range. The message names
    the option the operator typed, and not the parameter of the API.
    """
    from foresight.api.deps import normalize_time_bounds

    if value is None:
        return None
    if upper:
        return normalize_time_bounds(None, value, until_name=option).until
    return normalize_time_bounds(value, None, since_name=option).since


def _fail_bound(exc: ValueError) -> NoReturn:
    """Report a bound that cannot be read and stop the command.

    A bound that cannot be read is a mistake of the operator, so the
    command leaves with the code of a bad argument and not with the
    code of a failure.
    """
    typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
    raise typer.Exit(code=2)


def _database_target(database: DatabaseConfig) -> str:
    """The place the schema was created at.

    A SQLite database is a file, and the file has a name. A PostgreSQL
    or a MySQL database is a database on a server: there is no file to
    name, and the path of the configuration is not one.
    """
    if database.backend == "sqlite":
        return database.path
    return (
        f"{database.backend}://{database.host}:{database.port}/{database.database}"
    )


def _require_token_for_host(host: str) -> None:
    """Refuse to expose the API remotely without a token.

    The API is protected by a bearer token, and a token that is not set
    leaves the API open. An API that is open is fine on the loopback
    interface, where only the operator reaches it — but on any other
    address it is open to the network, so the command refuses to start
    instead of publishing the dump to whoever finds the port.
    """
    token = os.environ.get("FORESIGHT_API_TOKEN", "").strip()
    if token:
        return
    if host in ("127.0.0.1", "localhost", "::1"):
        return
    typer.secho(
        "FORESIGHT_API_TOKEN must be set when the API is not bound to localhost",
        fg=typer.colors.RED,
        err=True,
    )
    raise typer.Exit(code=2)


@app.command()
def init_db(
    config: Optional[str] = typer.Option(None, "--config", help="Path to config.toml"),
) -> None:
    """Create the database schema."""
    from sqlalchemy.exc import SQLAlchemyError

    from foresight.storage.db import create_engine, init_db as create_tables

    settings = _load(config)

    async def _create_schema() -> None:
        try:
            engine = create_engine(settings.database)
        except (OSError, SQLAlchemyError) as exc:
            # A directory that cannot be created, an unwritable path, a
            # backend that is not there: the operator gets the cause, not a
            # traceback. Anything else is a bug and stays a bug.
            _fail(f"cannot open the database: {exc}")
        try:
            await create_tables(engine)
        except (OSError, SQLAlchemyError) as exc:
            _fail(f"cannot open the database: {exc}")
        finally:
            # The command is done with the database: the connection is
            # closed, so the write-ahead log is checkpointed and no
            # connection is left behind.
            await engine.dispose()

    asyncio.run(_create_schema())
    typer.echo(f"schema ready at {_database_target(settings.database)}")


@dataclass
class _CollectorResources:
    """Everything a running collector owns.

    The command that built them releases them again, in the order the
    service needs: the bot first, so that no event arrives any more, then
    the workers, then the writer and finally the engine.

    The bot is replaced when the collector is started again after a
    Discord outage, so that the command releases the bot that is really
    running.
    """

    engine: Any
    writer: Any
    bot: Any
    media_worker: Any
    analysis_worker: Any
    handler: Any = None


async def _requeue_pending_analysis(repo: Any, worker: Any) -> int:
    """Queue the messages that are still waiting for their analysis.

    A message that was captured while the AI was disabled — or while the
    worker was down — keeps the status `pending`. A restart must not
    leave it there for good, so the pending messages are queued again
    (spec §10.3).
    """
    pending = await repo.pending_messages(limit=PENDING_REQUEUE_LIMIT)
    for message_id, content in pending:
        worker.submit(message_id, content)
    if pending:
        logger.info("queued %d messages for the analysis again", len(pending))
    return len(pending)


async def _requeue_pending_attachments(repo: Any, worker: Any) -> int:
    """Queue the attachments that are still waiting for their download.

    An attachment whose download never happened — because the worker was
    down, or because its status was lost — keeps the status `pending`. A
    restart must not leave it there for good, so the pending attachments
    are queued again.
    """
    pending = await repo.pending_attachments(limit=PENDING_REQUEUE_LIMIT)
    for record in pending:
        worker.submit(record)
    if pending:
        logger.info("queued %d attachments for the download again", len(pending))
    return len(pending)


async def _build_collector(settings: Config) -> _CollectorResources:
    """Build the engine, the writer and the bot of the collector.

    Both commands that collect messages use this helper, so the two of
    them cannot drift apart: the database is opened, the schema is
    created, the failure file is replayed, the writer is started, the
    media and the analysis workers are started and the bot is built —
    always in that order.

    The resources are returned to the caller, which releases them again.
    A failure in the middle releases what was already built, so a command
    that does not start leaves nothing behind.
    """
    from sqlalchemy.exc import SQLAlchemyError

    from foresight.analysis.llm import LlmClient
    from foresight.analysis.worker import AnalysisWorker
    from foresight.bot import build_bot
    from foresight.events import EventHandler
    from foresight.media_worker import MediaWorker
    from foresight.storage.db import create_engine, init_db as create_tables
    from foresight.storage.media import build_media_store
    from foresight.storage.repository import Repository
    from foresight.writer import Writer, recover_failed_batches

    # The failure file lives next to the database, so that a database and
    # its pending writes are always found together.
    failure_path = Path(settings.database.path).parent / "failed_batch.jsonl"

    engine: Any = None
    writer: Any = None
    bot: Any = None
    media_worker: Any = None
    analysis_worker: Any = None
    try:
        try:
            engine = create_engine(settings.database)
        except (OSError, SQLAlchemyError) as exc:
            # A directory that cannot be created, an unwritable path, a
            # backend that is not there: the operator gets the cause, not a
            # traceback. Anything else is a bug and stays a bug.
            _fail(f"cannot open the database: {exc}")
        try:
            await create_tables(engine)
        except (OSError, SQLAlchemyError) as exc:
            _fail(f"cannot open the database: {exc}")
        repo = Repository(engine)
        writer = Writer(
            repo,
            batch_size=settings.writer.batch_size,
            flush_interval_ms=settings.writer.flush_interval_ms,
            queue_maxsize=settings.writer.queue_maxsize,
            failure_path=failure_path,
        )
        try:
            # The replay writes on this coroutine and bypasses the queue,
            # so it belongs to the startup: the background task of the
            # writer is not running yet, so it cannot race the replay for
            # the database lock.
            await recover_failed_batches(writer, failure_path)
        except Exception as exc:
            # A failure file that cannot be read or parsed is a problem of
            # the operator, not a crash of the collector.
            _fail(f"cannot replay the failure file {failure_path}: {exc}")
        await writer.start()
        media_worker = MediaWorker(
            writer,
            build_media_store(settings.media),
            max_concurrency=settings.media.download_concurrency,
            max_bytes=settings.media.max_download_size_mb * 1024 * 1024,
        )
        await media_worker.start()
        # The attachments that were never downloaded are queued again, so
        # that a restart does not leave them behind for good.
        await _requeue_pending_attachments(repo, media_worker)
        if settings.ai.enabled:
            analysis_worker = AnalysisWorker(
                repo,
                LlmClient(settings.ai),
                max_concurrency=settings.ai.max_concurrency,
            )
            await analysis_worker.start()
            # The messages that were never analysed are queued again, so
            # that a restart does not leave them behind for good.
            await _requeue_pending_analysis(repo, analysis_worker)
        handler = EventHandler(
            settings,
            writer,
            media_sink=media_worker.submit,
            analysis_sink=(
                analysis_worker.submit if analysis_worker is not None else None
            ),
        )
        bot = build_bot(settings, handler)
        return _CollectorResources(
            engine, writer, bot, media_worker, analysis_worker, handler
        )
    except BaseException:
        # The resources that were already built are released again, so
        # that a command that does not start leaves nothing behind.
        await _release(
            _CollectorResources(engine, writer, bot, media_worker, analysis_worker)
        )
        raise


async def _release(resources: _CollectorResources | None) -> None:
    """Release the resources of a collector.

    Every step runs, even when the one before it failed: a bot that
    cannot be closed must not keep the writer from being stopped. The
    order is the one the service needs — the bot first, so that no event
    arrives any more, then the workers, then the writer and finally the
    engine.
    """
    if resources is None:
        return
    try:
        if resources.bot is not None:
            await resources.bot.close()
    finally:
        try:
            if resources.analysis_worker is not None:
                await resources.analysis_worker.stop()
        finally:
            try:
                if resources.media_worker is not None:
                    await resources.media_worker.stop()
            finally:
                try:
                    if resources.writer is not None:
                        await resources.writer.stop()
                finally:
                    if resources.engine is not None:
                        await resources.engine.dispose()


@app.command()
def run(
    config: Optional[str] = typer.Option(None, "--config", help="Path to config.toml"),
) -> None:
    """Start the Discord collector."""
    import discord

    from foresight.bot import require_token

    settings = _load(config)
    if not settings.discord.channel_ids:
        # The same shape as every other configuration problem: the message
        # names the key that has to be fixed.
        raise ConfigError("[discord].channel_ids must not be empty")
    try:
        token = require_token()
    except RuntimeError as exc:
        _fail(str(exc))

    async def _run() -> None:
        # Every resource that was created is released again, even when the
        # step before it failed.
        resources: _CollectorResources | None = None
        try:
            resources = await _build_collector(settings)
            logger.info(
                "collector starting: channels=%s database=%s",
                ", ".join(str(cid) for cid in settings.discord.channel_ids),
                settings.database.path,
            )
            try:
                await resources.bot.start(token)
            except discord.LoginFailure as exc:
                # A wrong, an expired or a revoked token is one of the
                # most common mistakes of an operator: the reason is
                # reported, and not a traceback.
                _fail(f"the Discord login failed: {exc}")
        finally:
            logger.info("collector stopping")
            # Every step runs, even when the one before it failed: the bot
            # is closed first, then the workers are stopped, then the
            # writer is stopped, then the engine is disposed.
            await _release(resources)

    asyncio.run(_run())


@app.command()
def serve(
    config: Optional[str] = typer.Option(None, "--config", help="Path to config.toml"),
    host: Optional[str] = typer.Option(None, "--host", help="Override api.host"),
    port: Optional[int] = typer.Option(None, "--port", help="Override api.port"),
) -> None:
    """Serve the read-only HTTP API."""
    import uvicorn

    from foresight.api.app import create_app

    settings = _load(config)
    bind_host = host or settings.api.host
    bind_port = port or settings.api.port
    # The check happens before the server is built: an API that would be
    # open to the network must not even start.
    _require_token_for_host(bind_host)
    # The application builds its own engine, and that engine can only
    # read: the API is a reader, the dump belongs to the collector.
    application = create_app(settings)
    server = uvicorn.Server(
        uvicorn.Config(
            application,
            host=bind_host,
            port=bind_port,
            log_level=settings.logging.level.lower(),
        )
    )
    try:
        server.run()
    except SystemExit as exc:
        # The server leaves with its own exit code when it cannot bind
        # the port — 3, and a message of its own. The operator gets one
        # line and the exit code the other commands use.
        _fail(_describe_server_failure(exc))


async def _stop_server(server_task: Any) -> None:
    """Stop the API server that runs beside the collector."""
    server_task.cancel()
    try:
        await server_task
    except asyncio.CancelledError:
        pass
    except BaseException:
        # The server died on its own — a port that is already taken, for
        # example. The reason belongs in the log, not in a traceback that
        # buries the reason the collector is stopping.
        logger.exception("the API server stopped with an error")


async def _run_server(server: Any) -> BaseException | None:
    """Serve the API and return the reason the server stopped.

    The failure is caught here and not in the task: an exception that
    escapes a task stops the whole event loop, and the collector would
    then either go on without the API or die with a traceback. The
    `SystemExit` that `uvicorn` raises when it cannot bind the port is
    caught here as well.
    """
    try:
        await server.serve()
    except asyncio.CancelledError:
        # A cancellation is not a failure: the task was stopped on
        # purpose.
        raise
    except BaseException as exc:
        return exc
    return None


def _describe_server_failure(exc: BaseException | None) -> str:
    """The reason the API server stopped, in one line.

    The server leaves with a `SystemExit` when it cannot bind the port:
    the exit code says nothing, but the error it was raised for does —
    "[Errno 48] address already in use".
    """
    if exc is None:
        return "the API server stopped"
    if isinstance(exc, SystemExit):
        cause = exc.__cause__ or exc.__context__
        if cause is not None:
            return f"the API server stopped: {cause}"
        return f"the API server stopped with exit code {exc.code}"
    return f"the API server stopped: {exc}"


def _server_failure(server_task: Any) -> str:
    """The reason the API server stopped, in one line."""
    if server_task.cancelled():
        return "the API server was cancelled"
    return _describe_server_failure(server_task.result())


def _report_task_failure(task: Any) -> None:
    """Report the failure of a task that ended on its own."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        raise exc


async def _run_collector(
    settings: Config, resources: _CollectorResources, token: str
) -> None:
    """Keep the collector running while the API serves.

    A Discord outage must not take the API down: the collector is built
    again and started after a pause, so that the archive keeps filling up
    once Discord answers again. The API keeps serving in the meantime.

    A login failure is not an outage: the token is wrong or revoked, and
    no retry can fix that. The operator is told and the process leaves.
    """
    import discord

    from foresight.bot import build_bot

    while True:
        bot = resources.bot
        try:
            await bot.start(token)
        except discord.LoginFailure as exc:
            _fail(f"the Discord login failed: {exc}")
        except Exception:
            logger.exception("the collector stopped with an error")
        else:
            logger.error("the collector stopped on its own")
        finally:
            with suppress(Exception):
                await bot.close()
        # Discord is asked again after a pause, so that an outage does
        # not turn into a flood of logins.
        await asyncio.sleep(COLLECTOR_RETRY_SECONDS)
        # A bot that was connected cannot be connected a second time, so
        # every attempt gets a bot of its own. The resources hold the
        # bot that is really running, so that the command releases it.
        resources.bot = build_bot(settings, resources.handler)


@app.command()
def both(
    config: Optional[str] = typer.Option(None, "--config", help="Path to config.toml"),
) -> None:
    """Run the collector and the HTTP API in a single process."""
    import uvicorn

    from foresight.api.app import create_app
    from foresight.bot import require_token
    from foresight.storage.db import create_engine

    settings = _load(config)
    if not settings.discord.channel_ids:
        # The same shape as every other configuration problem: the message
        # names the key that has to be fixed.
        raise ConfigError("[discord].channel_ids must not be empty")
    _require_token_for_host(settings.api.host)
    try:
        token = require_token()
    except RuntimeError as exc:
        _fail(str(exc))

    async def _run() -> None:
        # Every resource that was created is released again, even when the
        # step before it failed.
        resources: _CollectorResources | None = None
        api_engine: Any = None
        server_task: Any = None
        try:
            resources = await _build_collector(settings)
            # The API gets an engine of its own, one that can only read.
            # The engine of the collector writes, and the API must not be
            # able to write even when both run in the same process.
            api_engine = create_engine(settings.database, readonly=True)
            application = create_app(settings, engine=api_engine)
            server = uvicorn.Server(
                uvicorn.Config(
                    application,
                    host=settings.api.host,
                    port=settings.api.port,
                    log_level=settings.logging.level.lower(),
                )
            )
            logger.info(
                "collector and API starting: channels=%s database=%s api=%s:%s",
                ", ".join(str(cid) for cid in settings.discord.channel_ids),
                settings.database.path,
                settings.api.host,
                settings.api.port,
            )
            # The collector and the API are watched together: when the API
            # dies, the collector stops as well instead of running on
            # without it. The other way round it is not the case: the
            # collector comes back after a pause, and the API keeps
            # serving while Discord is away.
            bot_task = asyncio.create_task(
                _run_collector(settings, resources, token)
            )
            server_task = asyncio.create_task(_run_server(server))
            done, _pending = await asyncio.wait(
                {bot_task, server_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if server_task in done and bot_task not in done:
                # The API is gone while the collector still runs: the
                # reason is reported now and not at the end of the run.
                reason = _server_failure(server_task)
                logger.error("%s", reason)
                bot_task.cancel()
                with suppress(asyncio.CancelledError):
                    await bot_task
                _fail(reason)
            if bot_task in done:
                # The collector stopped for good: its reason belongs to
                # the caller.
                _report_task_failure(bot_task)
        finally:
            logger.info("collector and API stopping")
            # Every step runs, even when the one before it failed: the API
            # server is stopped first, then the bot is closed, then the
            # workers are stopped, then the writer is stopped and the
            # engines are disposed.
            try:
                if server_task is not None:
                    await _stop_server(server_task)
            finally:
                try:
                    await _release(resources)
                finally:
                    if api_engine is not None:
                        await api_engine.dispose()


    asyncio.run(_run())


@app.command()
def backfill(
    days: Optional[int] = typer.Option(None, "--days", help="Import the last N days"),
    since: Optional[str] = typer.Option(None, "--since", help="ISO date lower bound"),
    channel: Optional[int] = typer.Option(
        None, "--channel", help="Only this channel"
    ),
    limit: int = typer.Option(20000, help="Maximum messages per channel"),
    config: Optional[str] = typer.Option(None, "--config", help="Path to config.toml"),
) -> None:
    """Import historical messages from Discord."""
    import discord

    from foresight.backfill import BackfillRunner, parse_window, require_token
    from foresight.storage.db import create_engine, init_db as create_tables
    from foresight.storage.repository import Repository
    from foresight.writer import Writer, recover_failed_batches

    settings = _load(config)
    if days is None and since is None:
        days = 7
    try:
        # The bound is read the way the API reads it: a bare date names
        # a whole day, and a bound that cannot be read is reported
        # instead of importing nothing.
        window = parse_window(days, _time_bound(since, "--since"))
    except ValueError as exc:
        _fail_bound(exc)
    try:
        # The token is checked before the database is opened: an import
        # that cannot log in must not write anything.
        require_token()
    except RuntimeError as exc:
        _fail(str(exc))

    channel_ids = (
        [channel] if channel is not None else list(settings.discord.channel_ids)
    )

    async def _run() -> dict[str, int]:
        from sqlalchemy.exc import SQLAlchemyError

        engine: Any = None
        try:
            try:
                engine = create_engine(settings.database)
            except (OSError, SQLAlchemyError) as exc:
                # A directory that cannot be created, an unwritable path, a
                # backend that is not there: the operator gets the cause,
                # not a traceback.
                _fail(f"cannot open the database: {exc}")
            try:
                await create_tables(engine)
            except (OSError, SQLAlchemyError) as exc:
                _fail(f"cannot open the database: {exc}")
            repo = Repository(engine)
            # The failure file lives next to the database, so that a
            # database and its pending writes are always found together.
            failure_path = Path(settings.database.path).parent / "failed_batch.jsonl"
            writer = Writer(
                repo,
                batch_size=settings.writer.batch_size,
                flush_interval_ms=settings.writer.flush_interval_ms,
                queue_maxsize=settings.writer.queue_maxsize,
                failure_path=failure_path,
            )
            # The replay writes on this coroutine and bypasses the queue,
            # so it belongs to the startup: the background task of the
            # writer is not running yet, so it cannot race the replay for
            # the database lock.
            try:
                await recover_failed_batches(writer, failure_path)
            except Exception as exc:
                # A failure file that cannot be read or parsed is a
                # problem of the operator, not a crash of the import.
                _fail(f"cannot replay the failure file {failure_path}: {exc}")
            await writer.start()
            try:
                runner = BackfillRunner(settings, writer)
                try:
                    return await runner.run(channel_ids, window, limit=limit)
                except discord.LoginFailure as exc:
                    # A wrong, an expired or a revoked token is one of
                    # the most common mistakes of an operator: the
                    # reason is reported, and not a traceback.
                    _fail(f"the Discord login failed: {exc}")
            finally:
                await writer.stop()
        finally:
            # The command is done with the database: the connection is
            # closed, so nothing is left behind.
            if engine is not None:
                await engine.dispose()

    counts = asyncio.run(_run())
    typer.echo(
        f"backfilled {counts['messages']} messages from {counts['channels']} channels"
    )


@app.command()
def analyze(
    since: Optional[str] = typer.Option(
        None, "--since", help="ISO timestamp lower bound"
    ),
    limit: int = typer.Option(1000, help="Maximum messages to analyse"),
    config: Optional[str] = typer.Option(None, "--config", help="Path to config.toml"),
) -> None:
    """Analyse the messages that are still waiting for their analysis."""
    from foresight.analysis.llm import LlmClient
    from foresight.analysis.worker import AnalysisWorker
    from foresight.storage.db import create_engine, init_db as create_tables
    from foresight.storage.repository import Repository

    settings = _load(config)
    try:
        # The bound is read the way the API reads it: a bare date names
        # a whole day, and a bound that cannot be read is reported
        # instead of analysing nothing.
        since = _time_bound(since, "--since")
    except ValueError as exc:
        _fail_bound(exc)
    if not settings.ai.enabled:
        typer.secho(
            "[ai].enabled is false, nothing to do", fg=typer.colors.YELLOW, err=True
        )
        raise typer.Exit(code=2)

    async def _run() -> tuple[int, int, int]:
        from sqlalchemy.exc import SQLAlchemyError

        engine: Any = None
        try:
            try:
                engine = create_engine(settings.database)
            except (OSError, SQLAlchemyError) as exc:
                _fail(f"cannot open the database: {exc}")
            try:
                await create_tables(engine)
            except (OSError, SQLAlchemyError) as exc:
                _fail(f"cannot open the database: {exc}")
            repo = Repository(engine)
            pending = await repo.pending_messages(since=since, limit=limit)
            llm = LlmClient(settings.ai)
            worker = AnalysisWorker(
                repo,
                llm,
                max_concurrency=settings.ai.max_concurrency,
                # The queue holds every message the command hands over, so
                # that none of them is dropped on the way.
                queue_maxsize=max(limit, 1),
            )
            await worker.start()
            try:
                for message_id, content in pending:
                    worker.submit(message_id, content)
                # The command waits for the analysis: the messages that are
                # still queued are analysed before the worker is stopped.
                while worker.pending():
                    await asyncio.sleep(0.5)
            finally:
                # The worker is stopped before the client of the model is
                # closed: a message that is still analysed must not lose
                # its connection.
                await worker.stop()
                await llm.close()
        finally:
            if engine is not None:
                await engine.dispose()
        # The worker knows what it did: a message that carried nothing to
        # analyse was skipped, and a message the model refused was not
        # analysed at all.
        return worker.analysed, worker.skipped, worker.failed

    analysed, skipped, failed = asyncio.run(_run())
    summary = f"analysed {analysed} messages"
    if skipped or failed:
        summary += f" ({skipped} skipped, {failed} failed)"
    typer.echo(summary)


@app.command()
def query(
    sql: str = typer.Option(..., "--sql", help="Read-only SQL statement"),
    db: str = typer.Option(..., "--db", help="Path to the SQLite database file"),
    limit: int = typer.Option(50, help="Maximum number of rows"),
) -> None:
    """Run a read-only SQL statement against the database."""
    import json

    from foresight.api.query import (
        QueryDatabaseError,
        QueryError,
        SqliteQueryRunner,
        clamp_limit,
    )

    # The same runner the API uses: the statement is checked by the same
    # rules, and the connection is opened read-only, so a write cannot
    # reach the dump even if the check were fooled. The limit is bounded
    # by the same rule as well, so the command cannot ask for more rows
    # than the API hands out.
    runner = SqliteQueryRunner(db)
    try:
        result = asyncio.run(runner.run(sql, {}, clamp_limit(limit), 0))
    except (QueryError, QueryDatabaseError) as exc:
        typer.secho(f"query error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    typer.echo(
        json.dumps({"columns": result.columns, "rows": result.rows}, ensure_ascii=False)
    )


@app.command()
def stats(
    db: str = typer.Option(..., "--db", help="Path to the SQLite database file"),
    since: Optional[str] = typer.Option(None, help="ISO date or timestamp lower bound"),
    until: Optional[str] = typer.Option(None, help="ISO date or timestamp upper bound"),
) -> None:
    """Print an overview of the captured messages."""
    import json

    from sqlalchemy.exc import SQLAlchemyError
    from sqlalchemy.ext.asyncio import create_async_engine

    from foresight.storage.stats import StatsService

    # The bounds are normalized the way the API normalizes them: a bare
    # date names a whole day, so `--until 2026-11-01` does not drop the
    # day it names. A bound that cannot be read is reported instead of
    # answering with an empty range.
    try:
        since = _time_bound(since, "--since")
        until = _time_bound(until, "--until", upper=True)
    except ValueError as exc:
        _fail_bound(exc)

    async def _run() -> dict:
        # The engine is built here and not through the storage layer: this
        # command knows a path and not a configuration. It is opened
        # read-only, so the command cannot write to the dump even by
        # accident: the driver itself refuses every write.
        url = DatabaseConfig(backend="sqlite", path=db).readonly_sqlalchemy_url
        engine = create_async_engine(url)
        try:
            return await StatsService(engine).overview(since=since, until=until)
        except SQLAlchemyError as exc:
            # A file that is not there, a database without the schema: the
            # operator gets the cause, not a traceback.
            _fail(f"cannot read the database: {exc}")
        finally:
            # The command is done with the database: the connection is
            # closed, so nothing is left behind.
            await engine.dispose()

    typer.echo(json.dumps(asyncio.run(_run()), ensure_ascii=False, indent=2))


def main() -> None:
    try:
        app()
    except ConfigError as exc:
        typer.secho(f"configuration error: {exc}", fg=typer.colors.RED, err=True)
        raise SystemExit(2)
