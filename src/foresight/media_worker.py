"""Downloads attachments in the background and registers them in storage."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from foresight.adapters.discord_mapper import iso
from foresight.domain.models import (
    AttachmentRecord,
    AttachmentStatus,
    OpKind,
    WriteOp,
)
from foresight.metrics import media_bytes_total, media_downloads_total
from foresight.storage.media import media_key, sha256_hex

logger = logging.getLogger(__name__)

# A download is attempted three times, with a growing delay between the
# attempts, before the attachment is marked as failed.
MAX_RETRIES = 3

# The delay between the attempts grows by a second every time.
RETRY_DELAYS = (1.0, 2.0)

# The end of the queue: it tells a worker that no more work is coming.
_STOP = object()


class MediaWorker:
    """Consumes attachment records and stores their bytes.

    The worker owns its own queue and its own tasks, so the download never
    runs on the callback of Discord: the callback only hands the record
    over.
    """

    def __init__(
        self,
        repo: Any,
        store: Any,
        *,
        max_concurrency: int = 3,
        max_bytes: int = 100 * 1024 * 1024,
        queue_maxsize: int = 1000,
        client_factory: Any = None,
    ) -> None:
        self._repo = repo
        self._store = store
        self._max_concurrency = max_concurrency
        self._max_bytes = max_bytes
        self._queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=queue_maxsize)
        self._client_factory = client_factory or httpx.AsyncClient
        self._tasks: list[asyncio.Task[None]] = []
        self._stopping = False

    async def start(self) -> None:
        if self._tasks:
            return
        # A worker that was stopped before is running again, so the
        # stopping flag of the old run must not leak into the new tasks.
        self._stopping = False
        self._tasks = [
            asyncio.create_task(self._worker(index), name=f"foresight-media-{index}")
            for index in range(self._max_concurrency)
        ]

    async def stop(self) -> None:
        """Stop the workers once the queue is empty.

        The workers wait for the end of the queue, so the stop has to
        reach them: one end marker per worker is put on the queue, behind
        the work that is still to be done.
        """
        self._stopping = True
        for _ in self._tasks:
            await self._queue.put(_STOP)
        for task in self._tasks:
            await task
        self._tasks = []

    def submit(self, record: AttachmentRecord) -> bool:
        """Queue an attachment for download.

        Returns False when the queue is full or the worker was stopped;
        the attachment is not downloaded in either case.
        """
        if self._stopping:
            logger.warning("media worker is stopped, dropping attachment %s", record.url)
            return False
        try:
            self._queue.put_nowait(record)
            return True
        except asyncio.QueueFull:
            logger.warning("media queue is full, dropping attachment %s", record.url)
            return False

    async def _worker(self, index: int) -> None:  # noqa: ARG002
        while True:
            record = await self._queue.get()
            if record is _STOP:
                self._queue.task_done()
                return
            try:
                await self._process(record)
            except Exception:
                logger.exception("media processing failed for %s", record.url)
            finally:
                self._queue.task_done()
            if self._stopping and self._queue.empty():
                return

    async def _process(self, record: AttachmentRecord) -> None:
        size = record.size or 0
        if size and size > self._max_bytes:
            # The attachment is too big to be downloaded. That is a
            # decision and not an error, so it is skipped and not failed.
            media_downloads_total.labels(status="skipped").inc()
            await self._write_status(
                record,
                "skipped",
                error="attachment exceeds the configured size limit",
            )
            return
        last_error = "download failed"
        for attempt in range(MAX_RETRIES):
            try:
                async with self._client_factory(timeout=30.0) as client:
                    response = await client.get(record.url)
                    response.raise_for_status()
                    data = response.content
                await self._store_bytes(record, data)
                media_downloads_total.labels(status="ok").inc()
                media_bytes_total.inc(len(data))
                return
            except Exception as exc:
                last_error = str(exc)
                logger.warning(
                    "attachment download failed (attempt %d/%d): %s",
                    attempt + 1,
                    MAX_RETRIES,
                    exc,
                )
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_DELAYS[attempt])
        media_downloads_total.labels(status="failed").inc()
        await self._write_status(record, "failed", error=last_error)

    async def _store_bytes(self, record: AttachmentRecord, data: bytes) -> None:
        stored_at = datetime.now(timezone.utc)
        key = media_key(
            record.attachment_id,
            record.filename,
            _captured_at(record, stored_at),
        )
        uri = await self._store.put(
            key, data, record.content_type or "application/octet-stream"
        )
        await self._write_status(
            record,
            "stored",
            sha256=sha256_hex(data),
            storage_key=key,
            storage_uri=uri,
            stored_at=iso(stored_at),
        )

    async def _write_status(
        self,
        record: AttachmentRecord,
        status: str,
        *,
        error: str | None = None,
        sha256: str | None = None,
        storage_key: str | None = None,
        storage_uri: str | None = None,
        stored_at: str | None = None,
    ) -> None:
        """Write the terminal status of one attachment row."""
        await self._repo.apply_batch(
            [
                WriteOp(
                    OpKind.ATTACHMENT_STATUS,
                    AttachmentStatus(
                        message_id=record.message_id,
                        attachment_id=record.attachment_id,
                        status=status,
                        error=error,
                        sha256=sha256,
                        storage_key=storage_key,
                        storage_uri=storage_uri,
                        stored_at=stored_at,
                    ),
                )
            ]
        )


def _captured_at(record: AttachmentRecord, fallback: datetime) -> datetime:
    """The moment the attachment was captured, as a UTC datetime.

    The storage key is built from it, so that the key of an attachment
    does not depend on the moment it was downloaded: a retry or a restart
    stores the file under the same key instead of leaving a second copy
    of it behind.
    """
    try:
        captured = datetime.fromisoformat(record.captured_at)
    except (TypeError, ValueError):
        return fallback
    if captured.tzinfo is None:
        captured = captured.replace(tzinfo=timezone.utc)
    return captured.astimezone(timezone.utc)
