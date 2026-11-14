"""Downloads attachments in the background and registers them in storage."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit

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

# The number of retries after the first attempt: one attempt plus three
# retries make four attempts in total, with the backoff in
# `DOWNLOAD_RETRY_DELAYS` waited out between them. The name is spelled
# out, so that it cannot be confused with the retries of the writer,
# which count the same way.
DOWNLOAD_MAX_RETRIES = 3

# The delay between the attempts grows every time: 1 s, 2 s and 4 s.
DOWNLOAD_RETRY_DELAYS = (1.0, 2.0, 4.0)

# How long the worker waits for its queue to be empty when it is
# stopped, in seconds. The queue is put behind the work that is still
# to be done, so a backlog would keep the process alive for a long
# time — and the shutdown of the container must not wait for it.
DRAIN_TIMEOUT_SECONDS = 5.0

# The end of the queue: it tells a worker that no more work is coming.
_STOP = object()

# The query string of a URL inside a message.
_URL_QUERY = re.compile(r"(https?://[^\s'\"]*)\?[^\s'\"]*")


class _TooLarge(Exception):
    """Raised when the attachment is bigger than the configured limit."""


def safe_url(url: str) -> str:
    """The URL without its query string.

    The query string of the URL of Discord is the capability that grants
    read access to the file: it must not reach the log.
    """
    parsed = urlsplit(url)
    if not parsed.scheme or not parsed.netloc:
        return url
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def redact(text: str) -> str:
    """Remove the query string of every URL in the text.

    The error of a failed download carries the URL of the request: the
    query string of that URL grants read access to the file, and it must
    not reach the log nor the row of the attachment.
    """
    return _URL_QUERY.sub(r"\1?[redacted]", text)


class MediaWorker:
    """Consumes attachment records and stores their bytes.

    The worker owns its own queue and its own tasks, so the download never
    runs on the callback of Discord: the callback only hands the record
    over.

    The status of an attachment is written through the queue of the writer,
    not straight into the database. The row of the attachment and the
    status of that row are then written in the order they were queued: a
    status that reached the database before its row existed would match no
    row, and the attachment would stay `pending` for good.
    """

    def __init__(
        self,
        writer: Any,
        store: Any,
        *,
        max_concurrency: int = 3,
        max_bytes: int = 100 * 1024 * 1024,
        queue_maxsize: int = 1000,
        client_factory: Any = None,
        drain_timeout: float = DRAIN_TIMEOUT_SECONDS,
    ) -> None:
        self._writer = writer
        self._store = store
        self._max_concurrency = max_concurrency
        self._max_bytes = max_bytes
        self._queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=queue_maxsize)
        self._client_factory = client_factory or httpx.AsyncClient
        self._client: httpx.AsyncClient | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._stopping = False
        self._drain_timeout = drain_timeout

    async def start(self) -> None:
        if self._tasks:
            return
        # A worker that was stopped before is running again, so the
        # stopping flag of the old run must not leak into the new tasks.
        self._stopping = False
        # One client for the whole run of the worker: the connections are
        # pooled and every attachment reuses them, instead of paying for
        # a handshake of its own.
        self._client = self._client_factory(timeout=30.0, follow_redirects=True)
        self._tasks = [
            asyncio.create_task(self._worker(index), name=f"foresight-media-{index}")
            for index in range(self._max_concurrency)
        ]

    async def stop(self) -> None:
        """Stop the workers, with a bounded wait.

        The workers wait for the end of the queue, so the stop has to
        reach them: one end marker per worker is put on the queue, behind
        the work that is still to be done. The wait is bounded — a
        backlog that is long enough would keep the process alive for a
        long time — and the workers are cancelled when the bound is
        reached.
        """
        self._stopping = True
        for _ in self._tasks:
            try:
                self._queue.put_nowait(_STOP)
            except asyncio.QueueFull:
                # The queue is full of work: the workers are cancelled
                # instead of being waited for.
                break
        tasks = list(self._tasks)
        self._tasks = []
        if tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=self._drain_timeout,
                )
            except asyncio.TimeoutError:
                logger.warning("the media queue was not drained in time")
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        await self._close_client()

    async def _close_client(self) -> None:
        """Close the client of the run, if there is one."""
        client = self._client
        self._client = None
        if client is not None:
            await client.aclose()

    def submit(self, record: AttachmentRecord) -> bool:
        """Queue an attachment for download.

        Returns False when the queue is full or the worker was stopped;
        the attachment is not downloaded in either case.
        """
        if self._stopping:
            logger.warning(
                "media worker is stopped, dropping attachment %s", safe_url(record.url)
            )
            return False
        try:
            self._queue.put_nowait(record)
            return True
        except asyncio.QueueFull:
            logger.warning(
                "media queue is full, dropping attachment %s", safe_url(record.url)
            )
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
                logger.exception(
                    "media processing failed for %s", safe_url(record.url)
                )
            finally:
                self._queue.task_done()

    async def _process(self, record: AttachmentRecord) -> None:
        size = record.size or 0
        if size and size > self._max_bytes:
            # The attachment is too big to be downloaded. That is a
            # decision and not an error, so it is skipped and not failed.
            media_downloads_total.labels(status="skipped").inc()
            self._write_status(
                record,
                "skipped",
                error="attachment exceeds the configured size limit",
            )
            return
        last_error = "download failed"
        for attempt in range(DOWNLOAD_MAX_RETRIES + 1):
            try:
                data = await self._download(record)
                await self._store_bytes(record, data)
                media_downloads_total.labels(status="ok").inc()
                media_bytes_total.inc(len(data))
                return
            except _TooLarge:
                # The server sent more bytes than the limit allows. That
                # is a decision and not an error: the file is skipped and
                # not failed.
                media_downloads_total.labels(status="skipped").inc()
                self._write_status(
                    record,
                    "skipped",
                    error="the attachment is bigger than the configured size limit",
                )
                return
            except Exception as exc:
                last_error = redact(str(exc))
                logger.warning(
                    "attachment download failed (attempt %d/%d): %s",
                    attempt + 1,
                    DOWNLOAD_MAX_RETRIES + 1,
                    last_error,
                )
                if attempt < DOWNLOAD_MAX_RETRIES:
                    await asyncio.sleep(DOWNLOAD_RETRY_DELAYS[attempt])
        media_downloads_total.labels(status="failed").inc()
        self._write_status(record, "failed", error=last_error)

    async def _download(self, record: AttachmentRecord) -> bytes:
        """Download the bytes of an attachment, up to the limit.

        The body is read as a stream and the bytes are counted as they
        arrive: the size Discord announces is not trusted, and the whole
        body is never held in memory when it is bigger than the limit.
        """
        client = self._client
        if client is None:
            raise RuntimeError("the media worker is not started")
        chunks: list[bytes] = []
        total = 0
        async with client.stream("GET", record.url) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > self._max_bytes:
                    raise _TooLarge
                chunks.append(chunk)
        return b"".join(chunks)

    async def _store_bytes(self, record: AttachmentRecord, data: bytes) -> None:
        stored_at = datetime.now(timezone.utc)
        key = media_key(
            record.attachment_id,
            record.filename,
            _captured_at(record, stored_at),
            seed=record.url,
        )
        uri = await self._store.put(
            key, data, record.content_type or "application/octet-stream"
        )
        self._write_status(
            record,
            "stored",
            sha256=sha256_hex(data),
            storage_key=key,
            storage_uri=uri,
            stored_at=iso(stored_at),
        )

    def _write_status(
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
        """Write the terminal status of one attachment row.

        The status goes through the queue of the writer, so it is written
        after the row of the attachment and not before it.
        """
        written = self._writer.submit(
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
        )
        if not written:
            logger.warning(
                "the write queue refused the status of attachment %s",
                record.attachment_id,
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
