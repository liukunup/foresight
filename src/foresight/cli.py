"""Command line entry points."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, NoReturn, Optional

import typer

from foresight.config import Config, ConfigError, load_config
from foresight.logging_setup import setup_logging

logger = logging.getLogger(__name__)

app = typer.Typer(add_completion=False, help="ForeSight — Discord message dump service")


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
    typer.echo(f"schema ready at {settings.database.path}")


@app.command()
def run(
    config: Optional[str] = typer.Option(None, "--config", help="Path to config.toml"),
) -> None:
    """Start the Discord collector."""
    from sqlalchemy.exc import SQLAlchemyError

    from foresight.bot import build_bot, require_token
    from foresight.events import EventHandler
    from foresight.storage.db import create_engine, init_db as create_tables
    from foresight.storage.repository import Repository
    from foresight.writer import Writer, recover_failed_batches

    settings = _load(config)
    if not settings.discord.channel_ids:
        # The same shape as every other configuration problem: the message
        # names the key that has to be fixed.
        raise ConfigError("[discord].channel_ids must not be empty")
    try:
        token = require_token()
    except RuntimeError as exc:
        _fail(str(exc))

    # The failure file lives next to the database, so that a database and
    # its pending writes are always found together.
    failure_path = Path(settings.database.path).parent / "failed_batch.jsonl"

    async def _run() -> None:
        # Every resource that was created is released again, even when the
        # step before it failed.
        engine: Any = None
        writer: Any = None
        bot: Any = None
        try:
            try:
                engine = create_engine(settings.database)
            except (OSError, SQLAlchemyError) as exc:
                # A directory that cannot be created, an unwritable path, a
                # backend that is not there: the operator gets the cause,
                # not a traceback. Anything else is a bug and stays a bug.
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
                # The replay writes on this coroutine and bypasses the
                # queue, so it belongs to the startup: the background task
                # of the writer is not running yet, so it cannot race the
                # replay for the database lock.
                await recover_failed_batches(writer, failure_path)
            except Exception as exc:
                # A failure file that cannot be read or parsed is a problem
                # of the operator, not a crash of the collector.
                _fail(f"cannot replay the failure file {failure_path}: {exc}")
            await writer.start()
            handler = EventHandler(settings, writer)
            bot = build_bot(settings, handler)
            logger.info(
                "collector starting: channels=%s database=%s",
                ", ".join(str(cid) for cid in settings.discord.channel_ids),
                settings.database.path,
            )
            await bot.start(token)
        finally:
            logger.info("collector stopping")
            # Every step runs, even when the one before it failed: the bot
            # is closed first, then the writer is stopped, then the engine
            # is disposed.
            try:
                if bot is not None:
                    await bot.close()
            finally:
                try:
                    if writer is not None:
                        await writer.stop()
                finally:
                    if engine is not None:
                        await engine.dispose()

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
    uvicorn.run(
        application,
        host=bind_host,
        port=bind_port,
        log_level=settings.logging.level.lower(),
    )


async def _stop_server(server_task: Any) -> None:
    """Stop the API server that runs beside the collector."""
    server_task.cancel()
    try:
        await server_task
    except asyncio.CancelledError:
        pass
    except Exception:
        # The server died on its own — a port that is already taken, for
        # example. The reason belongs in the log, not in a traceback that
        # buries the reason the collector is stopping.
        logger.exception("the API server stopped with an error")


@app.command()
def both(
    config: Optional[str] = typer.Option(None, "--config", help="Path to config.toml"),
) -> None:
    """Run the collector and the HTTP API in a single process."""
    import uvicorn
    from sqlalchemy.exc import SQLAlchemyError

    from foresight.api.app import create_app
    from foresight.bot import build_bot, require_token
    from foresight.events import EventHandler
    from foresight.storage.db import create_engine, init_db as create_tables
    from foresight.storage.repository import Repository
    from foresight.writer import Writer, recover_failed_batches

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

    failure_path = Path(settings.database.path).parent / "failed_batch.jsonl"

    async def _run() -> None:
        # Every resource that was created is released again, even when the
        # step before it failed.
        engine: Any = None
        api_engine: Any = None
        writer: Any = None
        bot: Any = None
        server_task: Any = None
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
                # The replay writes on this coroutine and bypasses the
                # queue, so it belongs to the startup: the background task
                # of the writer is not running yet, so it cannot race the
                # replay for the database lock.
                await recover_failed_batches(writer, failure_path)
            except Exception as exc:
                # A failure file that cannot be read or parsed is a problem
                # of the operator, not a crash of the collector.
                _fail(f"cannot replay the failure file {failure_path}: {exc}")
            await writer.start()
            handler = EventHandler(settings, writer)
            bot = build_bot(settings, handler)
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
            server_task = asyncio.create_task(server.serve())
            logger.info(
                "collector and API starting: channels=%s database=%s api=%s:%s",
                ", ".join(str(cid) for cid in settings.discord.channel_ids),
                settings.database.path,
                settings.api.host,
                settings.api.port,
            )
            await bot.start(token)
        finally:
            logger.info("collector and API stopping")
            # Every step runs, even when the one before it failed: the API
            # server is stopped first, then the bot is closed, then the
            # writer is stopped, then the engines are disposed.
            try:
                if server_task is not None:
                    await _stop_server(server_task)
            finally:
                try:
                    if bot is not None:
                        await bot.close()
                finally:
                    try:
                        if writer is not None:
                            await writer.stop()
                    finally:
                        try:
                            if api_engine is not None:
                                await api_engine.dispose()
                        finally:
                            if engine is not None:
                                await engine.dispose()

    asyncio.run(_run())


@app.command()
def query(
    sql: str = typer.Option(..., "--sql", help="Read-only SQL statement"),
    db: str = typer.Option(..., "--db", help="Path to the SQLite database file"),
    limit: int = typer.Option(50, help="Maximum number of rows"),
) -> None:
    """Run a read-only SQL statement against the database."""
    import json

    from foresight.api.query import QueryDatabaseError, QueryError, SqliteQueryRunner

    # The same runner the API uses: the statement is checked by the same
    # rules, and the connection is opened read-only, so a write cannot
    # reach the dump even if the check were fooled.
    runner = SqliteQueryRunner(db)
    try:
        result = asyncio.run(runner.run(sql, {}, limit, 0))
    except (QueryError, QueryDatabaseError) as exc:
        typer.secho(f"query error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    typer.echo(
        json.dumps({"columns": result.columns, "rows": result.rows}, ensure_ascii=False)
    )


@app.command()
def stats(
    db: str = typer.Option(..., "--db", help="Path to the SQLite database file"),
    since: Optional[str] = typer.Option(None, help="ISO timestamp lower bound"),
    until: Optional[str] = typer.Option(None, help="ISO timestamp upper bound"),
) -> None:
    """Print an overview of the captured messages."""
    import json

    from sqlalchemy.exc import SQLAlchemyError
    from sqlalchemy.ext.asyncio import create_async_engine

    from foresight.storage.stats import StatsService

    async def _run() -> dict:
        # The engine is built here and not through the storage layer: this
        # command knows a path and not a configuration, and it only reads.
        engine = create_async_engine(f"sqlite+aiosqlite:///{db}")
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
