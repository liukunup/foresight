"""Background worker that analyses messages with an LLM."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

from foresight.analysis.llm import LlmError
from foresight.analysis.prompts import PROMPT_VERSION
from foresight.domain.models import MessageAnalysis, OpKind, WriteOp
from foresight.metrics import ai_queue_depth

logger = logging.getLogger(__name__)

# The number of attempts at most: the message is marked as failed when
# they are all used up.
MAX_ATTEMPTS = 3

# A message with less text than this has nothing to analyse.
SKIP_MIN_LENGTH = 2

# The end of the queue: it tells a worker that no more work is coming.
_STOP = object()


class AnalysisWorker:
    """Consumes message content and stores analysis results.

    The worker owns its own queue and its own tasks, so the call to the
    model never runs on the callback of Discord: the callback only hands
    the content over.
    """

    def __init__(
        self,
        repo: Any,
        llm: Any,
        *,
        max_concurrency: int = 2,
        queue_maxsize: int = 1000,
    ) -> None:
        self._repo = repo
        self._llm = llm
        self._max_concurrency = max_concurrency
        self._queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=queue_maxsize)
        self._tasks: list[asyncio.Task[None]] = []
        self._stopping = False

    async def start(self) -> None:
        if self._tasks:
            return
        # A worker that was stopped before is running again, so the
        # stopping flag of the old run must not leak into the new tasks.
        self._stopping = False
        self._tasks = [
            asyncio.create_task(self._worker(index), name=f"foresight-analysis-{index}")
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

    def submit(self, message_id: int, content: str) -> bool:
        """Queue a message for analysis.

        Returns False when the queue is full or the worker was stopped;
        the message is not analysed in either case.
        """
        if self._stopping:
            logger.warning(
                "analysis worker is stopped, dropping message %s", message_id
            )
            return False
        try:
            self._queue.put_nowait((message_id, content))
        except asyncio.QueueFull:
            logger.warning("analysis queue is full, dropping message %s", message_id)
            return False
        ai_queue_depth.set(self._queue.qsize())
        return True

    async def _worker(self, index: int) -> None:  # noqa: ARG002
        while True:
            item = await self._queue.get()
            if item is _STOP:
                self._queue.task_done()
                return
            message_id, content = item
            try:
                await self._process(message_id, content)
            except Exception:
                logger.exception("analysis failed for message %s", message_id)
            finally:
                self._queue.task_done()
                ai_queue_depth.set(self._queue.qsize())

    async def _process(self, message_id: int, content: str) -> None:
        """Analyse one message and write the result."""
        text = (content or "").strip()
        if len(text) < SKIP_MIN_LENGTH:
            # There is nothing to analyse: the message is skipped, not failed.
            await self._set_status(message_id, "skipped")
            return
        last_error = "analysis failed"
        for attempt in range(MAX_ATTEMPTS):
            try:
                result = await self._llm.analyze(content)
            except LlmError as exc:
                last_error = str(exc)
                logger.warning(
                    "llm analysis failed (attempt %d/%d): %s",
                    attempt + 1,
                    MAX_ATTEMPTS,
                    exc,
                )
                if attempt < MAX_ATTEMPTS - 1:
                    await asyncio.sleep(1.0 * (attempt + 1))
                continue
            await self._store(message_id, result)
            return
        await self._store_failure(message_id, last_error)

    async def _store(self, message_id: int, result: Any) -> None:
        """Write the analysis of a message and mark the message as done."""
        now = datetime.now(timezone.utc)
        await self._repo.apply_batch(
            [
                WriteOp(
                    OpKind.ANALYSIS,
                    MessageAnalysis(
                        message_id=message_id,
                        translated_text=result.translation,
                        sentiment=result.sentiment,
                        sentiment_score=result.sentiment_score,
                        issues_json=json.dumps(result.issues, ensure_ascii=False),
                        model=result.model,
                        prompt_version=result.prompt_version,
                        latency_ms=result.latency_ms,
                        error=None,
                        analyzed_at=now.isoformat(),
                    ),
                ),
                WriteOp(OpKind.ANALYSIS_STATUS, (message_id, "done")),
            ]
        )

    async def _store_failure(self, message_id: int, error: str) -> None:
        """Record the failure of a message and mark the message as failed.

        The model and the prompt version are written as well: the row says
        which model was tried, even when it did not answer.
        """
        now = datetime.now(timezone.utc)
        await self._repo.apply_batch(
            [
                WriteOp(
                    OpKind.ANALYSIS,
                    MessageAnalysis(
                        message_id=message_id,
                        translated_text=None,
                        sentiment=None,
                        sentiment_score=None,
                        issues_json=None,
                        model=getattr(self._llm, "model", "") or "",
                        prompt_version=PROMPT_VERSION,
                        latency_ms=0,
                        error=error,
                        analyzed_at=now.isoformat(),
                    ),
                ),
                WriteOp(OpKind.ANALYSIS_STATUS, (message_id, "failed")),
            ]
        )

    async def _set_status(self, message_id: int, status: str) -> None:
        """Set the analysis status of a message."""
        await self._repo.apply_batch(
            [WriteOp(OpKind.ANALYSIS_STATUS, (message_id, status))]
        )
