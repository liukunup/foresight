"""Background worker that analyses messages with an LLM."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from foresight.analysis.llm import LlmPermanentError
from foresight.analysis.prompts import PROMPT_VERSION
from foresight.domain.models import MessageAnalysis, OpKind, WriteOp
from foresight.metrics import ai_queue_depth

logger = logging.getLogger(__name__)

# The number of retries after the first attempt: one attempt plus three
# retries make four attempts in total, with the backoff in
# `RETRY_DELAYS` waited out between them. The name is spelled out, so
# that it cannot be confused with the retries of the writer, which count
# the same way.
MAX_RETRIES = 3

# The delays between the attempts grow every time: 1 s, 2 s and 4 s.
RETRY_DELAYS = (1.0, 2.0, 4.0)

# A message with less text than this has nothing to analyse.
SKIP_MIN_LENGTH = 2

# How long the worker waits for its queue to be empty when it is
# stopped, in seconds. The queue is put behind the work that is still
# to be done, so a backlog would keep the process alive for a long
# time — and the shutdown of the container must not wait for it.
DRAIN_TIMEOUT_SECONDS = 5.0

# The characters of an emoji, and the two that glue them together. A
# message that carries nothing else is a message of emojis.
EMOJI = re.compile(
    "["
    "\u2600-\u27bf"  # the symbols and the dingbats
    "\u2b00-\u2bff"  # the symbols and the arrows
    "\U0001f000-\U0001faff"  # the pictographs and the emoticons
    "\ufe0f"  # the variation selector
    "\u200d"  # the zero width joiner
    "]"
)

# A message that is nothing but a link.
URL = re.compile(r"^(?:https?://|ftp://|www\.)\S+$", re.IGNORECASE)


def should_skip(content: str) -> bool:
    """Whether the message carries nothing the model could analyse.

    The rules are the ones of the spec (§10.4): an empty message, a
    message that is only emojis, a message that is only a link and a
    message of less than two characters are skipped, and the model is
    not called for them.

    The decision is taken here and nowhere else: the caller hands over
    every message that reached the writer, and the worker marks the ones
    that carry nothing as `skipped`. A message that was handed over and
    not marked would stay `pending` for ever.
    """
    text = (content or "").strip()
    if len(text) < SKIP_MIN_LENGTH:
        return True
    if URL.match(text):
        return True
    # What is left of the text once the emojis are taken out of it tells
    # whether the message was only emojis.
    return not EMOJI.sub("", text).strip()

# The end of the queue: it tells a worker that no more work is coming.
_STOP = object()

# The seam between the worker and the wait between two attempts. The
# tests hand in their own, so that they do not have to patch the sleep of
# the standard library: a patch there would silence every library that
# sleeps inside the test as well.
Sleep = Callable[[float], Awaitable[None]]


async def _sleep(delay: float) -> None:
    """Wait for the given number of seconds."""
    await asyncio.sleep(delay)


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
        sleep: Sleep | None = None,
        drain_timeout: float = DRAIN_TIMEOUT_SECONDS,
    ) -> None:
        self._repo = repo
        self._llm = llm
        self._max_concurrency = max_concurrency
        self._queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=queue_maxsize)
        self._tasks: list[asyncio.Task[None]] = []
        self._stopping = False
        self._pending = 0
        # The counts of the run: how many messages the model analysed,
        # how many carried nothing to analyse and how many the model
        # could not analyse. The command that drives the worker reports
        # them, and a message that was not analysed must not be counted
        # as one that was.
        self._analysed = 0
        self._skipped = 0
        self._failed = 0
        # The seam of the wait between two attempts: None means the
        # default wait of the module.
        self._sleep = sleep
        # How long the worker waits for its queue to be empty when it is
        # stopped, in seconds.
        self._drain_timeout = drain_timeout

    async def start(self) -> None:
        if self._tasks:
            return
        # A worker that was stopped before is running again, so the
        # stopping flag of the old run must not leak into the new tasks.
        self._stopping = False
        # A worker that starts has nothing to wait for yet.
        self._pending = 0
        self._publish_depth()
        self._tasks = [
            asyncio.create_task(self._worker(index), name=f"foresight-analysis-{index}")
            for index in range(self._max_concurrency)
        ]

    async def stop(self) -> None:
        """Stop the workers, with a bounded wait.

        The workers wait for the end of the queue, so the stop has to
        reach them: one end marker per worker is put on the queue, behind
        the work that is still to be done. The wait is bounded — a
        backlog of a thousand messages would keep the process alive for
        hours — and the workers are cancelled when the bound is reached.
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
                logger.warning("the analysis queue was not drained in time")
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        # Nothing waits any more: the end markers of the queue are not
        # work, so they must not show up as a backlog.
        self._pending = 0
        self._publish_depth()

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
        self._pending += 1
        self._publish_depth()
        return True

    def pending(self) -> int:
        """The number of messages that still wait for their analysis.

        The count is the one of the gauge: the end markers of the queue
        are not work, so they must not show up as a backlog.
        """
        return self._pending

    @property
    def analysed(self) -> int:
        """The number of messages the model analysed."""
        return self._analysed

    @property
    def skipped(self) -> int:
        """The number of messages that carried nothing to analyse."""
        return self._skipped

    @property
    def failed(self) -> int:
        """The number of messages the model could not analyse."""
        return self._failed

    def _publish_depth(self) -> None:
        """Publish the number of messages that wait for their analysis.

        The gauge counts the messages and not the entries of the queue: the
        end markers of the queue are not work, and counting them would
        leave a phantom backlog behind after every stop.
        """
        ai_queue_depth.set(self._pending)

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
                self._pending -= 1
                self._publish_depth()

    async def _process(self, message_id: int, content: str) -> None:
        """Analyse one message and write the result."""
        if should_skip(content):
            # There is nothing to analyse: the message is skipped, not failed.
            await self._set_status(message_id, "skipped")
            self._skipped += 1
            return
        last_error = "analysis failed"
        for attempt in range(MAX_RETRIES + 1):
            try:
                result = await self._llm.analyze(content)
            except LlmPermanentError as exc:
                # A permanent failure — a missing key, a disabled AI — does
                # not get better by another attempt: the message is failed
                # right away.
                last_error = str(exc) or type(exc).__name__
                logger.warning("llm analysis failed for good: %s", exc)
                break
            except Exception as exc:
                # Every other failure is worth another attempt — and it
                # must never leave the message in `pending`.
                last_error = str(exc) or type(exc).__name__
                logger.warning(
                    "llm analysis failed (attempt %d/%d): %s",
                    attempt + 1,
                    MAX_RETRIES + 1,
                    exc,
                )
                if attempt < MAX_RETRIES:
                    await self._wait(RETRY_DELAYS[attempt])
                continue
            await self._store(message_id, result)
            self._analysed += 1
            return
        await self._store_failure(message_id, last_error)
        self._failed += 1

    async def _wait(self, delay: float) -> None:
        """Wait between two attempts."""
        sleep = self._sleep if self._sleep is not None else _sleep
        await sleep(delay)

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
