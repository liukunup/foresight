"""Command line entry points."""

from __future__ import annotations

import asyncio
import logging
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


def main() -> None:
    try:
        app()
    except ConfigError as exc:
        typer.secho(f"configuration error: {exc}", fg=typer.colors.RED, err=True)
        raise SystemExit(2)
