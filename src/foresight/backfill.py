"""Import historical messages from Discord.

Backfill is a separate process: it connects to Discord on its own, pulls
history through the REST API and feeds it into the very same
mapper/writer/storage path the live collector uses. It never overwrites rows
that the live collector already wrote.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from foresight.adapters.discord_mapper import (
    map_channel,
    map_guild,
    map_message,
    map_user,
)
from foresight.domain.models import OpKind, WriteOp
from foresight.metrics import backfill_messages_total

logger = logging.getLogger(__name__)

# The number of messages of one page of the history.
PAGE_SIZE = 100

# The number of messages between two progress reports.
PROGRESS_EVERY = 1000


def parse_window(days: int | None, since: str | None) -> tuple[datetime, datetime]:
    """Turn --days / --since into an inclusive time window."""
    if days is not None and since is not None:
        raise ValueError("--days and --since are mutually exclusive")
    end = datetime.now(timezone.utc)
    if since is not None:
        start = datetime.fromisoformat(since)
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        return start, end
    span = days if days is not None else 7
    if span < 0:
        raise ValueError("--days must not be negative")
    return end - timedelta(days=span), end


def require_token() -> str:
    """The Discord token of the backfill.

    The token never lives in the configuration file: it is read from the
    environment, like everywhere else in the service.
    """
    token = os.environ.get("DISCORD_TOKEN", "").strip()
    if not token:
        raise RuntimeError("DISCORD_TOKEN environment variable is required")
    return token


def _build_client() -> Any:
    """Build the Discord client of the backfill.

    The history of a channel is read from the REST API, but the channel
    itself comes from the cache of the gateway, so the client connects
    like the collector does.
    """
    import discord

    intents = discord.Intents.default()
    intents.messages = True
    intents.message_content = True
    return discord.Client(intents=intents)


class BackfillRunner:
    """Pulls channel history and feeds it into the write queue."""

    def __init__(self, config: Any, writer: Any, client_factory: Any = None) -> None:
        self._config = config
        self._writer = writer
        # The factory builds the Discord client. It is a seam for the
        # tests, which hand in a client that never talks to Discord.
        self._client_factory = client_factory
        self._task: asyncio.Task[None] | None = None

    async def run(
        self,
        channel_ids: Sequence[int],
        window: tuple[datetime, datetime],
        limit: int = 20000,
    ) -> dict[str, int]:
        """Import the history of the given channels into the writer."""
        client = (self._client_factory or _build_client)()
        counts = {"messages": 0, "channels": 0}
        start, end = window
        try:
            await self._connect(client)
            for channel_id in channel_ids:
                channel = client.get_channel(channel_id)
                if channel is None:
                    logger.warning("channel %s is not visible to the bot", channel_id)
                    continue
                counts["channels"] += 1
                counts["messages"] += await self._pull_channel(
                    channel, start, end, limit
                )
        finally:
            await self._disconnect(client)
        return counts

    async def _connect(self, client: Any) -> None:
        """Log in and wait until the cache of the client is ready.

        The gateway connection runs in the background, so that the import
        can wait for the client to be ready. A client that dies before
        it is ready is the reason why the import failed: its own
        exception says so.
        """
        token = require_token()
        self._task = asyncio.create_task(
            client.start(token), name="foresight-backfill-client"
        )
        ready = asyncio.create_task(
            client.wait_until_ready(), name="foresight-backfill-ready"
        )
        done, _pending = await asyncio.wait(
            {self._task, ready}, return_when=asyncio.FIRST_COMPLETED
        )
        ready.cancel()
        with suppress(asyncio.CancelledError):
            await ready
        if self._task in done:
            await self._task

    async def _disconnect(self, client: Any) -> None:
        """Close the client and wait for the connection to end.

        The client is released even when the import failed, and a
        failure of the release does not hide the failure of the import.
        """
        try:
            await client.close()
        except Exception:
            logger.exception("could not close the Discord client")
        task = self._task
        self._task = None
        if task is not None:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("the Discord client stopped with an error")

    async def _pull_channel(
        self,
        channel: Any,
        start: datetime,
        end: datetime,
        limit: int,
    ) -> int:
        """Pull the history of one channel into the write queue.

        The history is read page by page, from the newest messages to the
        oldest ones. A message outside of the window is not imported,
        but the page is read to its end: the window is a bound of the
        import, not of the reading.
        """
        pulled = 0
        before = None
        while pulled < limit:
            kwargs: dict[str, Any] = {"limit": min(PAGE_SIZE, limit - pulled)}
            if before is not None:
                kwargs["before"] = before
            batch = [message async for message in channel.history(**kwargs)]
            if not batch:
                break
            for message in batch:
                created = message.created_at
                if created < start or created > end:
                    continue
                await self._ingest(message)
                pulled += 1
                if pulled % PROGRESS_EVERY == 0:
                    logger.info("backfilled %d messages", pulled)
            before = batch[-1].id
            if len(batch) < kwargs["limit"]:
                break
        return pulled

    async def _ingest(self, message: Any) -> None:
        """Write one message of the history into the write queue."""
        captured_at = datetime.now(timezone.utc)
        record = map_message(message, captured_at=captured_at)
        # The backfill does not run the AI: the message is marked as
        # skipped, so that the analysis worker does not pick it up.
        record.analysis_status = "skipped"
        # The message is written with the `insert_only` strategy: a row
        # the live collector already wrote must not be overwritten.
        self._writer.submit(WriteOp(OpKind.MESSAGE_INSERT_ONLY, record))
        self._writer.submit(
            WriteOp(OpKind.USER, map_user(message.author, updated_at=captured_at))
        )
        self._writer.submit(
            WriteOp(
                OpKind.CHANNEL, map_channel(message.channel, captured_at=captured_at)
            )
        )
        guild = getattr(message, "guild", None)
        if guild is not None:
            self._writer.submit(
                WriteOp(OpKind.GUILD, map_guild(guild, captured_at=captured_at))
            )
        backfill_messages_total.labels(channel_id=str(record.channel_id)).inc()
