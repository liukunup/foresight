"""Bounded queue that batches database writes off the event loop."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Sequence

from foresight.domain import models as domain_models
from foresight.domain.models import OpKind, WriteOp

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_DELAYS = (1.0, 2.0, 4.0)

# Every kind whose payload is a domain dataclass, so a recovered operation can
# be turned back into the object the repository expects. A kind that is
# missing here would be replayed as a plain dict and the repository would
# fail on it, so the mapping is covered by a test.
_OP_CLASSES = {
    OpKind.MESSAGE: domain_models.MessageRecord,
    OpKind.MESSAGE_INSERT_ONLY: domain_models.MessageRecord,
    OpKind.MESSAGE_EDIT: domain_models.MessageEdit,
    OpKind.MESSAGE_DELETE: domain_models.MessageDelete,
    OpKind.REACTION: domain_models.ReactionRecord,
    OpKind.REACTION_REMOVE: domain_models.ReactionRemove,
    OpKind.USER: domain_models.UserSnapshot,
    OpKind.CHANNEL: domain_models.ChannelSnapshot,
    OpKind.GUILD: domain_models.GuildSnapshot,
    OpKind.ATTACHMENT: domain_models.AttachmentRecord,
    OpKind.ATTACHMENT_STATUS: domain_models.AttachmentStatus,
    OpKind.ANALYSIS: domain_models.MessageAnalysis,
}


def _rebuild(kind: str, payload: Any) -> Any:
    """Turn a JSON payload back into the domain object it came from."""
    if kind == OpKind.ANALYSIS_STATUS:
        return tuple(payload) if isinstance(payload, list) else payload
    cls = _OP_CLASSES.get(kind)
    if cls is None or not isinstance(payload, dict):
        return payload
    return cls(**payload)


class Writer:
    """Collect write operations and flush them in one transaction."""

    def __init__(
        self,
        repo: Any,
        *,
        batch_size: int = 100,
        flush_interval_ms: int = 50,
        queue_maxsize: int = 10000,
        failure_path: Path | None = None,
    ) -> None:
        self._repo = repo
        self._batch_size = batch_size
        self._flush_interval = flush_interval_ms / 1000
        self._queue: asyncio.Queue[WriteOp] = asyncio.Queue(maxsize=queue_maxsize)
        self._failure_path = failure_path
        self._task: asyncio.Task[None] | None = None
        self._stopping = False

    @property
    def queue_depth(self) -> int:
        return self._queue.qsize()

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="foresight-writer")

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            await self._task
            self._task = None

    def submit(self, op: WriteOp) -> bool:
        """Enqueue an operation. Returns False when the queue is full."""
        try:
            self._queue.put_nowait(op)
            return True
        except asyncio.QueueFull:
            logger.warning("write queue is full, dropping operation kind=%s", op.kind)
            return False

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            batch: list[WriteOp] = []
            deadline = loop.time() + self._flush_interval
            while len(batch) < self._batch_size:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                try:
                    batch.append(await asyncio.wait_for(self._queue.get(), remaining))
                except asyncio.TimeoutError:
                    break
            if batch:
                # The loop must outlive a failing batch: a dead background
                # task would leave every later operation in the queue and
                # make `stop()` await a failed task.
                try:
                    await self._flush(batch)
                except Exception:
                    logger.exception("unexpected error while writing a batch")
                    self._persist_failure(batch)
            if self._stopping and self._queue.empty():
                return

    async def _flush(self, batch: Sequence[WriteOp], *, persist: bool = True) -> bool:
        """Write a batch. Returns True when the batch reached the database.

        A batch that could not be written is retried and then handed to the
        failure file, unless `persist` is False. The recovery path turns it
        off: its operations are already on disk and must stay there until
        they are really written.
        """
        for attempt in range(MAX_RETRIES):
            try:
                await self._repo.apply_batch(list(batch))
                return True
            except Exception:
                logger.exception(
                    "batch write failed (attempt %d/%d)", attempt + 1, MAX_RETRIES
                )
                if attempt == MAX_RETRIES - 1:
                    break
                await asyncio.sleep(RETRY_DELAYS[attempt])
        if persist:
            self._persist_failure(batch)
        return False

    def _persist_failure(self, batch: Sequence[WriteOp]) -> None:
        if self._failure_path is None:
            logger.error("dropping %d operations, no failure path configured", len(batch))
            return
        try:
            self._failure_path.parent.mkdir(parents=True, exist_ok=True)
            with self._failure_path.open("a", encoding="utf-8") as fh:
                for op in batch:
                    payload = op.payload
                    if is_dataclass(payload) and not isinstance(payload, type):
                        payload = asdict(payload)
                    fh.write(
                        json.dumps(
                            {"kind": op.kind, "payload": payload},
                            ensure_ascii=False,
                            default=str,
                        )
                        + "\n"
                    )
        except Exception:
            logger.exception("could not persist failed batch")


async def recover_failed_batches(writer: Writer, path: Path | None) -> int:
    """Replay operations that a previous run could not persist.

    Returns the number of recovered operations. The file is truncated once
    the replay succeeds, so a batch is never applied twice. A replay that
    fails leaves the file untouched: the operations stay on disk and are
    replayed on the next start.
    """
    if path is None or not path.exists():
        return 0
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line]
    if not lines:
        return 0
    ops: list[WriteOp] = []
    for line in lines:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("skipping malformed failure record")
            continue
        kind = entry.get("kind", "")
        ops.append(WriteOp(kind, _rebuild(kind, entry.get("payload"))))
    if not ops:
        return 0
    if not await writer._flush(ops, persist=False):  # noqa: SLF001
        logger.error(
            "could not replay %d failed write operations, keeping them on disk",
            len(ops),
        )
        return 0
    path.write_text("", encoding="utf-8")
    logger.info("recovered %d failed write operations", len(ops))
    return len(ops)
