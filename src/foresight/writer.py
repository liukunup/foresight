"""Bounded queue that batches database writes off the event loop.

A batch that cannot be written is handed to a failure file, which is
replayed on the next start. The failure file comes in two parts, and
both of them matter to whoever reads the directory:

- The main failure file, `<name>.jsonl` — for example
  `failed_batch.jsonl`. It holds the records that are still to be
  written. Every line is one JSON object:
  `{"kind": ..., "payload": {...}}`. A record leaves the file only when
  it really reached the database.
- The rejected sibling file, `<name>.jsonl.rejected` — for example
  `failed_batch.jsonl.rejected`, next to the main failure file. It
  holds the records that can never be applied, such as a record with an
  unknown kind or a payload that does not fit its dataclass. They are
  kept for inspection only; they are never replayed.

So a reader of `failed_batch.jsonl` sees the operations that are still
pending, and a reader of `failed_batch.jsonl.rejected` sees the ones
that are pending nowhere else.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Protocol, Sequence

from foresight.domain import models as domain_models
from foresight.domain.models import OpKind, WriteOp
from foresight.metrics import writer_batch_size, writer_batches_total, writer_queue_depth

logger = logging.getLogger(__name__)

# The number of retries after the first attempt: one attempt plus three
# retries make four attempts in total, with the backoff in `RETRY_DELAYS`
# waited out between them. `RETRY_DELAYS` holds one delay per retry.
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

# The kinds a record of the failure file may carry: the ones whose payload
# is a domain dataclass, plus the one whose payload is a plain pair. A
# record with any other kind can never be applied — the repository raises
# on an unknown kind — so it is quarantined instead of failing on every
# start.
_REPLAYABLE_KINDS = frozenset(_OP_CLASSES) | {OpKind.ANALYSIS_STATUS}


def _rebuild(kind: str, payload: Any) -> Any:
    """Turn a JSON payload back into the domain object it came from."""
    if kind == OpKind.ANALYSIS_STATUS:
        return tuple(payload) if isinstance(payload, list) else payload
    cls = _OP_CLASSES.get(kind)
    if cls is None or not isinstance(payload, dict):
        return payload
    return cls(**payload)


class BatchWriter(Protocol):
    """The part of the repository the writer needs.

    Typing the repository this way keeps the writer independent of the
    concrete `Repository` class — which would risk a circular import — while
    still documenting the single call the writer makes.
    """

    async def apply_batch(self, ops: Sequence[WriteOp]) -> None: ...


class Writer:
    """Collect write operations and flush them in one transaction."""

    def __init__(
        self,
        repo: BatchWriter,
        *,
        batch_size: int = 100,
        flush_interval_ms: int = 50,
        queue_maxsize: int = 10000,
        failure_path: Path | None = None,
    ) -> None:
        if batch_size < 1:
            raise ValueError(f"batch_size must be at least 1, got {batch_size}")
        if flush_interval_ms < 1:
            raise ValueError(
                f"flush_interval_ms must be at least 1, got {flush_interval_ms}"
            )
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

    def _publish_depth(self) -> None:
        """Publish the depth of the queue.

        The gauge is published every time the queue changes, so it shows
        the real backlog and not a zero that hides it.
        """
        writer_queue_depth.set(self._queue.qsize())

    async def start(self) -> None:
        # A writer that was stopped before is running again, so the
        # stopping flag of the old run must not leak into the new task.
        self._stopping = False
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="foresight-writer")

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            await self._task
            self._task = None

    def submit(self, op: WriteOp) -> bool:
        """Enqueue an operation.

        Returns False when the queue is full or the writer has been
        stopped; the operation is never written in either case.
        """
        if self._stopping:
            logger.warning("writer is stopped, dropping operation kind=%s", op.kind)
            return False
        try:
            self._queue.put_nowait(op)
        except asyncio.QueueFull:
            logger.warning("write queue is full, dropping operation kind=%s", op.kind)
            return False
        self._publish_depth()
        return True

    async def apply_ops(self, ops: Sequence[WriteOp]) -> int:
        """Write operations in one batch, without retrying them.

        This is the entry point for a write that does not go through the
        queue — the startup replay of the failure file. Like the replay,
        it writes on the caller's coroutine, so it belongs to the startup
        and not to the running service.

        It performs a single attempt: the operations that are not written
        stay in the failure file and are replayed on the next start, so
        there is no backoff to wait for here.

        Returns the number of operations that reached the database. The
        repository writes a batch in a single transaction, so either
        every operation of the batch is written or none of them is.
        """
        if not ops:
            return 0
        if await self._apply_once(ops):
            return len(ops)
        return 0

    async def _run(self) -> None:
        while True:
            batch = await self._collect_batch()
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

    async def _collect_batch(self) -> list[WriteOp]:
        """Wait for operations and collect them into one batch.

        The wait always happens: the first wait lasts until an operation
        arrives or the flush interval elapses, and a deadline that has
        already passed starts a fresh interval instead of another round of
        the loop. The background task therefore never goes round without an
        await point, which would starve every other coroutine.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._flush_interval
        batch: list[WriteOp] = []
        while len(batch) < self._batch_size:
            remaining = deadline - loop.time()
            if remaining <= 0:
                remaining = self._flush_interval
                deadline = loop.time() + remaining
            try:
                batch.append(await asyncio.wait_for(self._queue.get(), remaining))
            except asyncio.TimeoutError:
                break
            self._publish_depth()
        return batch

    async def _flush(self, batch: Sequence[WriteOp]) -> bool:
        """Write a batch. Returns True when the batch reached the database.

        A batch that could not be written is retried `MAX_RETRIES` times
        with the backoff in `RETRY_DELAYS` — one attempt plus three
        retries — and is then handed to the failure file.
        """
        # The size of the batch is measured once per batch, not once per
        # attempt: a batch that is retried is still one batch.
        writer_batch_size.observe(len(batch))
        for attempt in range(MAX_RETRIES + 1):
            if await self._apply_once(batch):
                writer_batches_total.labels(status="ok").inc()
                return True
            if attempt < MAX_RETRIES:
                logger.warning(
                    "batch write failed, retrying (attempt %d/%d)",
                    attempt + 2,
                    MAX_RETRIES + 1,
                )
                await asyncio.sleep(RETRY_DELAYS[attempt])
        self._persist_failure(batch)
        writer_batches_total.labels(status="failed").inc()
        return False

    async def _apply_once(self, batch: Sequence[WriteOp]) -> bool:
        """Apply a batch once. Returns False when the write failed."""
        try:
            await self._repo.apply_batch(list(batch))
        except Exception:
            logger.exception("batch write failed")
            return False
        return True

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


def _parse_record(line: str) -> WriteOp | None:
    """Turn one failure file line into an operation.

    Returns None when the line cannot be parsed or rebuilt. The caller
    keeps such a line aside instead of replaying it.

    A record is only replayed when it can really be applied: the kind
    must be one the repository knows, and the payload must have the shape
    the kind expects. A record that passes both checks but does not fit
    its dataclass is rejected as well, when the dataclass is built.
    """
    try:
        entry = json.loads(line)
        if not isinstance(entry, dict):
            raise ValueError(f"expected a JSON object, got {type(entry).__name__}")
        kind = entry.get("kind")
        if not isinstance(kind, str) or not kind:
            raise ValueError("the record carries no kind")
        if kind not in _REPLAYABLE_KINDS:
            raise ValueError(f"the record carries the unknown kind {kind!r}")
        payload = entry.get("payload")
        if kind == OpKind.ANALYSIS_STATUS:
            # The only kind whose payload is not a dataclass: the
            # repository expects a (message_id, status) pair.
            if not isinstance(payload, (list, tuple)) or len(payload) != 2:
                raise ValueError("an analysis status record carries no pair")
        elif not isinstance(payload, dict):
            raise ValueError(f"a {kind!r} record carries no object payload")
        payload = _rebuild(kind, payload)
    except Exception as exc:
        logger.warning("could not rebuild a failure record, keeping it aside: %s", exc)
        return None
    return WriteOp(kind, payload)


def _quarantine(path: Path, lines: Sequence[str]) -> bool:
    """Keep the records that cannot be replayed next to the failure file.

    Returns False when they could not be saved, in which case the caller
    keeps them in the failure file so that they are not lost.
    """
    if not lines:
        return True
    target = path.with_name(path.name + ".rejected")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            for line in lines:
                fh.write(line + "\n")
    except Exception:
        logger.exception("could not preserve %d unusable failure records", len(lines))
        return False
    logger.warning("kept %d unusable failure records in %s", len(lines), target)
    return True


def _write_lines(path: Path, lines: Sequence[str]) -> None:
    """Rewrite the failure file with the records that are still to replay."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    os.replace(tmp, path)


async def recover_failed_batches(writer: Writer, path: Path | None) -> int:
    """Replay the operations a previous run could not write.

    This is a startup-only operation. It writes to the database on the
    caller's coroutine and therefore bypasses the write queue on
    purpose: the queue is drained by the background task, which is not
    running yet when the replay happens. It must not be called while the
    writer is live — the background task would race this replay for the
    database lock.

    Every record is parsed and rebuilt on its own, so a record that no
    longer matches its dataclass — or a line that is not a JSON object —
    cannot stop the other records from being replayed. Such a line is
    logged and moved to a sibling `.rejected` file next to the failure
    file, so it is kept for inspection instead of being lost.

    The records are first applied as one batch. If that fails they are
    applied one by one, so a single record the database rejects cannot
    block the good ones. Every attempt is a single one: the replay does
    not wait out the retry backoff of the writer, because the failure
    file already holds the records and replays them on the next start.

    The write is idempotent, so a record that is replayed twice is
    harmless: a snapshot overwrites its row with the very same values, the
    counter of the edits only counts an edit that is newer than the one
    the row already carries, and a reaction that was taken back stays
    taken back. The two status kinds write an absolute value — the status
    of an attachment, the analysis status of a message — so a replay of
    an older record restores the older status.

    Only the records that really reached the database are removed from the
    failure file; the others stay there and are replayed on the next
    start.

    Returns the number of recovered operations.
    """
    if path is None or not path.exists():
        return 0
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line]
    if not lines:
        return 0

    records: list[tuple[int, WriteOp]] = []
    unusable: list[int] = []
    for index, line in enumerate(lines):
        op = _parse_record(line)
        if op is None:
            unusable.append(index)
        else:
            records.append((index, op))

    # The lines that could not be replayed leave the failure file only when
    # they have really been preserved next to it.
    kept: set[int] = set()
    if unusable and not _quarantine(path, [lines[index] for index in unusable]):
        kept.update(unusable)

    recovered = 0
    if records:
        ops = [op for _, op in records]
        if await writer.apply_ops(ops):
            recovered = len(records)
        else:
            # The batch failed: apply the records one by one so that a
            # single bad record cannot block the good ones.
            for index, op in records:
                if await writer.apply_ops([op]):
                    recovered += 1
                else:
                    kept.add(index)

    _write_lines(path, [lines[index] for index in sorted(kept)])
    if recovered:
        logger.info("recovered %d failed write operations", recovered)
    return recovered
