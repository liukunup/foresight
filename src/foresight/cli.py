"""Command line entry points."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional

import typer

from foresight.config import Config, ConfigError, load_config
from foresight.logging_setup import setup_logging

logger = logging.getLogger(__name__)

app = typer.Typer(add_completion=False, help="ForeSight — Discord message dump service")


def _load(config_path: Optional[str]) -> Config:
    config = load_config(config_path)
    setup_logging(config.logging.level)
    return config


@app.command()
def init_db(
    config: Optional[str] = typer.Option(None, "--config", help="Path to config.toml"),
) -> None:
    """Create the database schema."""
    from foresight.storage.db import create_engine, init_db as create_tables

    settings = _load(config)

    async def _create_schema() -> None:
        engine = create_engine(settings.database)
        try:
            await create_tables(engine)
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
    from foresight.bot import build_bot, require_token
    from foresight.events import EventHandler
    from foresight.storage.db import create_engine, init_db as create_tables
    from foresight.storage.repository import Repository
    from foresight.writer import Writer, recover_failed_batches

    settings = _load(config)
    if not settings.discord.channel_ids:
        raise typer.BadParameter("[discord].channel_ids must not be empty")
    try:
        token = require_token()
    except RuntimeError as exc:
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    # The failure file lives next to the database, so that a database and
    # its pending writes are always found together.
    failure_path = Path(settings.database.path).parent / "failed_batch.jsonl"

    async def _run() -> None:
        engine = create_engine(settings.database)
        await create_tables(engine)
        repo = Repository(engine)
        writer = Writer(
            repo,
            batch_size=settings.writer.batch_size,
            flush_interval_ms=settings.writer.flush_interval_ms,
            queue_maxsize=settings.writer.queue_maxsize,
            failure_path=failure_path,
        )
        await writer.start()
        # The replay writes on this coroutine and bypasses the queue, so it
        # belongs to the startup: the queue is still empty here, so the
        # writer cannot race the replay for the database lock.
        await recover_failed_batches(writer, failure_path)
        handler = EventHandler(settings, writer)
        bot = build_bot(settings, handler)
        try:
            await bot.start(token)
        finally:
            await writer.stop()
            await engine.dispose()

    asyncio.run(_run())


def main() -> None:
    try:
        app()
    except ConfigError as exc:
        typer.secho(f"configuration error: {exc}", fg=typer.colors.RED, err=True)
        raise SystemExit(2)
