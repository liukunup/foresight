"""Tests for the batched write queue.

The writer is the only component that talks to the database from the event
loop, so these tests pin the guarantees the rest of the service relies on: a
submitted operation is eventually written, a full queue never blocks the
caller, a failed batch is kept on disk, and a failed batch is replayed on the
next start.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict

import pytest
from sqlalchemy import select

import foresight.writer as writer_module
from foresight.domain import models as domain_models
from foresight.domain.models import MessageRecord, OpKind, WriteOp
from foresight.metrics import REGISTRY
from foresight.storage.repository import Repository
from foresight.storage.tables import messages
from foresight.writer import Writer, recover_failed_batches


def _sample_value(name: str, labels: dict[str, str] | None = None) -> float:
    """The current value of a sample of the metric registry.

    The registry is process wide, so a test reads the value before and
    after the write and compares the two: the counters of one test must
    not disturb the counters of another one.
    """
    value = REGISTRY.get_sample_value(name, labels or {})
    return 0.0 if value is None else value


class _FakeRepository:
    """A repository that accepts everything and writes nothing."""

    async def apply_batch(self, ops):
        return None


class _SleepRecorder:
    """Stands in for the `asyncio` module and records the slept delays.

    The writer module refers to `asyncio` through its module globals, so
    replacing them with this recorder captures every `asyncio.sleep` the
    writer performs without waiting for the real delay.
    """

    def __init__(self, real) -> None:
        self._real = real
        self.delays: list[float] = []

    def __getattr__(self, name):
        return getattr(self._real, name)

    async def sleep(self, delay, *args, **kwargs):
        self.delays.append(delay)
        await self._real.sleep(0)


def _message(content: str = "hello") -> MessageRecord:
    return MessageRecord(
        message_id=1,
        guild_id=10,
        channel_id=20,
        author_id=30,
        content=content,
        message_type=0,
        created_at="2026-11-11T00:00:00+00:00",
        captured_at="2026-11-11T00:00:00+00:00",
    )


async def _make_writer(engine, repo=None, queue_maxsize: int | None = None):
    """A started writer on `engine`, which the caller owns."""
    repository = repo or Repository(engine)
    kwargs = {} if queue_maxsize is None else {"queue_maxsize": queue_maxsize}
    writer = Writer(repository, batch_size=2, flush_interval_ms=10, **kwargs)
    await writer.start()
    return writer


async def test_writer_flushes_submitted_ops(db_engine):
    writer = await _make_writer(db_engine)
    assert writer.submit(WriteOp(OpKind.MESSAGE, _message())) is True
    await writer.stop()
    async with db_engine.connect() as conn:
        rows = (await conn.execute(select(messages))).all()
    assert len(rows) == 1


async def test_writer_drains_queue_on_stop(db_engine):
    writer = await _make_writer(db_engine)
    for index in range(5):
        record = _message()
        record.message_id = index + 1
        writer.submit(WriteOp(OpKind.MESSAGE, record))
    await writer.stop()
    async with db_engine.connect() as conn:
        rows = (await conn.execute(select(messages))).all()
    assert len(rows) == 5


async def test_writer_returns_false_when_queue_is_full(db_engine):
    writer = await _make_writer(db_engine, queue_maxsize=1)
    writer.submit(WriteOp(OpKind.MESSAGE, _message()))
    assert writer.submit(WriteOp(OpKind.MESSAGE, _message())) is False
    await writer.stop()


async def test_writer_reports_the_queue_depth(db_engine):
    writer = await _make_writer(db_engine)
    assert writer.queue_depth == 0
    writer.submit(WriteOp(OpKind.MESSAGE, _message()))
    assert writer.queue_depth == 1
    await writer.stop()


async def test_writer_publishes_the_depth_of_the_queue(db_engine):
    """The gauge of the queue must show the real depth, not a stale zero."""
    writer = await _make_writer(db_engine)
    writer.submit(WriteOp(OpKind.MESSAGE, _message()))
    # The background task has not run yet: the operation is still queued.
    assert _sample_value("foresight_writer_queue_depth") == 1
    await writer.stop()
    # The queue is drained when the writer stops.
    assert _sample_value("foresight_writer_queue_depth") == 0


async def test_writer_counts_the_batches_it_writes(db_engine):
    before = _sample_value("foresight_writer_batches_total", {"status": "ok"})
    writer = await _make_writer(db_engine)
    writer.submit(WriteOp(OpKind.MESSAGE, _message()))
    await writer.stop()
    assert _sample_value("foresight_writer_batches_total", {"status": "ok"}) == before + 1


async def test_writer_counts_a_failed_batch_as_failed(tmp_path, monkeypatch):
    monkeypatch.setattr(writer_module, "RETRY_DELAYS", (0.0, 0.0, 0.0))

    class BrokenRepository:
        async def apply_batch(self, ops):
            raise RuntimeError("db is gone")

    before = _sample_value("foresight_writer_batches_total", {"status": "failed"})
    writer = Writer(
        BrokenRepository(),
        batch_size=1,
        flush_interval_ms=10,
        failure_path=tmp_path / "failed_batch.jsonl",
    )
    await writer.start()
    writer.submit(WriteOp(OpKind.MESSAGE, _message()))
    await writer.stop()
    assert (
        _sample_value("foresight_writer_batches_total", {"status": "failed"})
        == before + 1
    )


async def test_writer_measures_the_rows_of_a_batch(db_engine):
    before = _sample_value("foresight_writer_batch_size_sum")
    writer = await _make_writer(db_engine)
    writer.submit(WriteOp(OpKind.MESSAGE, _message()))
    writer.submit(WriteOp(OpKind.MESSAGE, _message()))
    await writer.stop()
    # The two operations went into one batch of two rows.
    assert _sample_value("foresight_writer_batch_size_sum") == before + 2


async def test_recover_failed_batches_replays_records(tmp_path, db_engine):
    repo = Repository(db_engine)
    writer = Writer(repo, batch_size=1, flush_interval_ms=10)
    failure_path = tmp_path / "failed_batch.jsonl"
    failure_path.write_text(
        json.dumps(
            {"kind": OpKind.MESSAGE, "payload": asdict(_message("recovered"))}
        )
        + "\n",
        encoding="utf-8",
    )
    await writer.start()
    recovered = await recover_failed_batches(writer, failure_path)
    await writer.stop()
    assert recovered == 1
    async with db_engine.connect() as conn:
        rows = (await conn.execute(select(messages))).all()
    assert len(rows) == 1
    assert rows[0].content == "recovered"
    assert failure_path.read_text(encoding="utf-8") == ""


async def test_recover_failed_batches_keeps_the_file_when_the_replay_fails(tmp_path):
    """A failed replay must not lose the operations it could not write."""

    class BrokenRepository:
        async def apply_batch(self, ops):
            raise RuntimeError("db is gone")

    failure_path = tmp_path / "failed_batch.jsonl"
    failure_path.write_text(
        json.dumps(
            {"kind": OpKind.MESSAGE, "payload": asdict(_message("recovered"))}
        )
        + "\n",
        encoding="utf-8",
    )
    writer = Writer(
        BrokenRepository(),
        batch_size=1,
        flush_interval_ms=10,
        failure_path=failure_path,
    )
    await writer.start()
    recovered = await recover_failed_batches(writer, failure_path)
    await writer.stop()
    assert recovered == 0
    lines = [line for line in failure_path.read_text(encoding="utf-8").splitlines() if line]
    assert len(lines) == 1


async def test_writer_persists_failed_batch(tmp_path, monkeypatch):
    monkeypatch.setattr(writer_module, "RETRY_DELAYS", (0.0, 0.0, 0.0))

    class BrokenRepository:
        async def apply_batch(self, ops):
            raise RuntimeError("db is gone")

    failure_path = tmp_path / "failed_batch.jsonl"
    writer = Writer(
        BrokenRepository(),
        batch_size=1,
        flush_interval_ms=10,
        failure_path=failure_path,
    )
    await writer.start()
    writer.submit(WriteOp(OpKind.MESSAGE, _message()))
    await writer.stop()
    lines = [line for line in failure_path.read_text(encoding="utf-8").splitlines() if line]
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["kind"] == "message"


async def test_writer_keeps_running_after_a_failed_batch(tmp_path, monkeypatch):
    monkeypatch.setattr(writer_module, "RETRY_DELAYS", (0.0, 0.0, 0.0))

    class FlakyRepository:
        """Fails every attempt of the first batch, writes every other one."""

        def __init__(self):
            self.calls = 0
            self.written = []

        async def apply_batch(self, ops):
            self.calls += 1
            if any(op.payload.content == "first" for op in ops):
                raise RuntimeError("db is gone")
            self.written.extend(ops)

    repo = FlakyRepository()
    writer = Writer(
        repo,
        batch_size=1,
        flush_interval_ms=10,
        failure_path=tmp_path / "failed_batch.jsonl",
    )
    await writer.start()
    writer.submit(WriteOp(OpKind.MESSAGE, _message("first")))
    writer.submit(WriteOp(OpKind.MESSAGE, _message("second")))
    await writer.stop()
    assert [op.payload.content for op in repo.written] == ["second"]
    # The failed batch was tried four times (one attempt plus three
    # retries) before it was given up on; the second batch took one more.
    assert repo.calls == 5


async def test_failed_batch_is_retried_with_one_two_four_second_backoff(
    tmp_path, monkeypatch
):
    """The spec asks for three retries with a 1s / 2s / 4s backoff."""

    class BrokenRepository:
        def __init__(self):
            self.calls = 0

        async def apply_batch(self, ops):
            self.calls += 1
            raise RuntimeError("db is gone")

    recorder = _SleepRecorder(asyncio)
    monkeypatch.setattr(writer_module, "asyncio", recorder)

    repo = BrokenRepository()
    writer = Writer(
        repo,
        batch_size=1,
        flush_interval_ms=10,
        failure_path=tmp_path / "failed_batch.jsonl",
    )
    await writer.start()
    writer.submit(WriteOp(OpKind.MESSAGE, _message()))
    await writer.stop()
    # One attempt plus three retries.
    assert repo.calls == 4
    assert recorder.delays == [1.0, 2.0, 4.0]


async def test_the_retry_log_names_the_attempt_that_is_coming(
    tmp_path, monkeypatch, caplog
):
    """The log must number the attempt that comes next, not the failed one."""

    class BrokenRepository:
        async def apply_batch(self, ops):
            raise RuntimeError("db is gone")

    recorder = _SleepRecorder(asyncio)
    monkeypatch.setattr(writer_module, "asyncio", recorder)

    writer = Writer(
        BrokenRepository(),
        batch_size=1,
        flush_interval_ms=10,
        failure_path=tmp_path / "failed_batch.jsonl",
    )
    await writer.start()
    with caplog.at_level(logging.WARNING, logger="foresight.writer"):
        writer.submit(WriteOp(OpKind.MESSAGE, _message()))
        await writer.stop()
    retries = [
        record.getMessage()
        for record in caplog.records
        if "retrying" in record.getMessage()
    ]
    assert retries == [
        "batch write failed, retrying (attempt 2/4)",
        "batch write failed, retrying (attempt 3/4)",
        "batch write failed, retrying (attempt 4/4)",
    ]


def test_op_classes_map_every_kind_to_its_dataclass():
    """Each kind must be mapped to the dataclass its payload carries."""
    assert writer_module._OP_CLASSES == {  # noqa: SLF001
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
    # ANALYSIS_STATUS is the only kind whose payload is not a dataclass: it
    # carries a plain (message_id, status) tuple.
    assert OpKind.ANALYSIS_STATUS not in writer_module._OP_CLASSES  # noqa: SLF001


def test_rebuild_restores_the_domain_object():
    record = _message("rebuilt")
    rebuilt = writer_module._rebuild(OpKind.MESSAGE, asdict(record))  # noqa: SLF001
    assert rebuilt == record


def test_rebuild_turns_an_analysis_status_payload_into_a_tuple():
    rebuilt = writer_module._rebuild(  # noqa: SLF001
        OpKind.ANALYSIS_STATUS, [42, "done"]
    )
    assert rebuilt == (42, "done")


@pytest.mark.parametrize("batch_size", [0, -1])
def test_writer_rejects_a_batch_size_below_one(batch_size):
    """A batch size of zero would make the flush loop spin forever."""
    with pytest.raises(ValueError, match="batch_size"):
        Writer(_FakeRepository(), batch_size=batch_size)


@pytest.mark.parametrize("flush_interval_ms", [0, -1])
def test_writer_rejects_a_flush_interval_below_one_millisecond(flush_interval_ms):
    """A flush interval of zero would make the flush loop spin forever."""
    with pytest.raises(ValueError, match="flush_interval_ms"):
        Writer(_FakeRepository(), flush_interval_ms=flush_interval_ms)


async def test_the_flush_loop_always_awaits(monkeypatch):
    """The background loop must never go round without an await point.

    A loop that spins without awaiting starves every other coroutine, so
    the writer has to wait for operations even when its deadline has
    already passed. The clock is faked to make the deadline pass at once
    and the recorder counts the waits.
    """
    real_asyncio = writer_module.asyncio

    class OutOfTime(Exception):
        """Raised by the fake clock to end the runaway loop."""

    class FakeLoop:
        """A clock that jumps past the deadline on every call."""

        def __init__(self) -> None:
            self.calls = 0

        def time(self) -> float:
            self.calls += 1
            if self.calls > 8:
                raise OutOfTime
            return float(self.calls)

    class Recorder:
        def __init__(self, fake_loop: FakeLoop) -> None:
            self._fake_loop = fake_loop
            self.waits = 0

        def __getattr__(self, name):
            return getattr(real_asyncio, name)

        def get_running_loop(self):
            return self._fake_loop

        async def wait_for(self, awaitable, timeout):
            self.waits += 1
            return await real_asyncio.wait_for(awaitable, timeout)

    recorder = Recorder(FakeLoop())
    monkeypatch.setattr(writer_module, "asyncio", recorder)

    writer = Writer(_FakeRepository(), batch_size=1, flush_interval_ms=50)
    await writer.start()
    with pytest.raises(OutOfTime):
        await writer._task  # noqa: SLF001
    assert recorder.waits > 0


async def test_apply_ops_writes_the_operations_and_reports_the_count(db_engine):
    """The public entry point for a write outside the queue."""
    writer = Writer(Repository(db_engine), batch_size=1, flush_interval_ms=10)
    written = await writer.apply_ops([WriteOp(OpKind.MESSAGE, _message())])
    assert written == 1
    async with db_engine.connect() as conn:
        rows = (await conn.execute(select(messages))).all()
    assert len(rows) == 1


async def test_apply_ops_reports_nothing_when_the_write_fails(tmp_path):
    """A failed write must not be reported as a written one."""

    class BrokenRepository:
        async def apply_batch(self, ops):
            raise RuntimeError("db is gone")

    writer = Writer(BrokenRepository(), batch_size=1, flush_interval_ms=10)
    assert await writer.apply_ops([WriteOp(OpKind.MESSAGE, _message())]) == 0


async def test_recover_failed_batches_does_not_wait_out_the_retry_backoff(
    tmp_path, monkeypatch
):
    """The startup replay must not delay the boot by the production backoff."""
    recorder = _SleepRecorder(asyncio)
    monkeypatch.setattr(writer_module, "asyncio", recorder)

    class BrokenRepository:
        def __init__(self):
            self.calls = 0

        async def apply_batch(self, ops):
            self.calls += 1
            raise RuntimeError("db is gone")

    failure_path = tmp_path / "failed_batch.jsonl"
    failure_path.write_text(
        json.dumps({"kind": OpKind.MESSAGE, "payload": asdict(_message("stuck"))})
        + "\n",
        encoding="utf-8",
    )
    repo = BrokenRepository()
    writer = Writer(
        repo,
        batch_size=1,
        flush_interval_ms=10,
        failure_path=failure_path,
    )
    await writer.start()
    recovered = await recover_failed_batches(writer, failure_path)
    await writer.stop()
    # The record stays in the failure file: the file is the retry that the
    # next start performs.
    assert recovered == 0
    assert len([line for line in failure_path.read_text().splitlines() if line]) == 1
    # One attempt for the batch, one for the single record of the
    # per-record fallback — and no backoff sleep at all.
    assert repo.calls == 2
    assert recorder.delays == []


async def test_recover_failed_batches_replays_the_good_record_of_a_malformed_file(
    tmp_path,
    db_engine,
):
    """One malformed line must not stop the other records from being replayed."""

    writer = Writer(Repository(db_engine), batch_size=1, flush_interval_ms=10)
    failure_path = tmp_path / "failed_batch.jsonl"
    failure_path.write_text(
        json.dumps({"kind": OpKind.MESSAGE, "payload": asdict(_message("good"))})
        + "\n"
        + "this line is not json\n",
        encoding="utf-8",
    )
    await writer.start()
    recovered = await recover_failed_batches(writer, failure_path)
    await writer.stop()
    assert recovered == 1
    async with db_engine.connect() as conn:
        rows = (await conn.execute(select(messages))).all()
    assert [row.content for row in rows] == ["good"]
    # The unusable line is kept next to the failure file instead of being
    # lost, and the recovered record is gone from the failure file.
    rejected = (tmp_path / "failed_batch.jsonl.rejected").read_text(encoding="utf-8")
    assert "this line is not json" in rejected
    assert "good" not in failure_path.read_text(encoding="utf-8")


async def test_recover_failed_batches_keeps_a_record_the_database_rejects(
    tmp_path, db_engine
):
    """One record the database rejects must not block the good ones."""

    class SelectiveRepository:
        """Accepts every record but the one that is marked as poison."""

        def __init__(self, inner) -> None:
            self._inner = inner

        async def apply_batch(self, ops):
            for op in ops:
                if op.payload.content == "poison":
                    raise RuntimeError("the database rejects this record")
            await self._inner.apply_batch(ops)

    writer = Writer(
        SelectiveRepository(Repository(db_engine)), batch_size=1, flush_interval_ms=10
    )
    failure_path = tmp_path / "failed_batch.jsonl"
    failure_path.write_text(
        json.dumps({"kind": OpKind.MESSAGE, "payload": asdict(_message("good"))})
        + "\n"
        + json.dumps({"kind": OpKind.MESSAGE, "payload": asdict(_message("poison"))})
        + "\n",
        encoding="utf-8",
    )
    await writer.start()
    recovered = await recover_failed_batches(writer, failure_path)
    await writer.stop()
    assert recovered == 1
    async with db_engine.connect() as conn:
        rows = (await conn.execute(select(messages))).all()
    assert [row.content for row in rows] == ["good"]
    # The rejected record stays in the file, the recovered one is gone.
    remaining = failure_path.read_text(encoding="utf-8")
    assert "poison" in remaining
    assert "good" not in remaining


async def test_recover_failed_batches_survives_a_record_it_cannot_rebuild(
    tmp_path, db_engine
):
    """A record that no longer matches its dataclass must not crash the start."""

    writer = Writer(Repository(db_engine), batch_size=1, flush_interval_ms=10)
    failure_path = tmp_path / "failed_batch.jsonl"
    failure_path.write_text(
        json.dumps({"kind": OpKind.MESSAGE, "payload": {"unknown_field": 1}})
        + "\n"
        + "[1, 2, 3]\n",
        encoding="utf-8",
    )
    await writer.start()
    recovered = await recover_failed_batches(writer, failure_path)
    await writer.stop()
    assert recovered == 0
    rejected = (tmp_path / "failed_batch.jsonl.rejected").read_text(encoding="utf-8")
    assert "unknown_field" in rejected
    assert "[1, 2, 3]" in rejected


async def test_recover_failed_batches_quarantines_a_record_with_an_unknown_kind(
    tmp_path,
    db_engine,
):
    """A record the repository can never handle must not block every start."""

    writer = Writer(Repository(db_engine), batch_size=1, flush_interval_ms=10)
    failure_path = tmp_path / "failed_batch.jsonl"
    failure_path.write_text(
        json.dumps({"kind": OpKind.MESSAGE, "payload": asdict(_message("good"))})
        + "\n"
        + json.dumps({"kind": "not_a_kind", "payload": {"message_id": 1}})
        + "\n",
        encoding="utf-8",
    )
    await writer.start()
    recovered = await recover_failed_batches(writer, failure_path)
    await writer.stop()
    assert recovered == 1
    async with db_engine.connect() as conn:
        rows = (await conn.execute(select(messages))).all()
    assert [row.content for row in rows] == ["good"]
    # The unknown kind is kept next to the failure file and does not stay
    # in it, where it would fail again on every start.
    remaining = failure_path.read_text(encoding="utf-8")
    assert "not_a_kind" not in remaining
    rejected = (tmp_path / "failed_batch.jsonl.rejected").read_text(encoding="utf-8")
    assert "not_a_kind" in rejected


async def test_recover_failed_batches_quarantines_a_record_without_an_object_payload(
    tmp_path,
    db_engine,
):
    """A record whose payload is not a dataclass payload can never be used."""

    writer = Writer(Repository(db_engine), batch_size=1, flush_interval_ms=10)
    failure_path = tmp_path / "failed_batch.jsonl"
    failure_path.write_text(
        json.dumps({"kind": OpKind.MESSAGE, "payload": "not an object"})
        + "\n",
        encoding="utf-8",
    )
    await writer.start()
    recovered = await recover_failed_batches(writer, failure_path)
    await writer.stop()
    assert recovered == 0
    assert failure_path.read_text(encoding="utf-8") == ""
    rejected = (tmp_path / "failed_batch.jsonl.rejected").read_text(encoding="utf-8")
    assert "not an object" in rejected


async def test_writer_keeps_writing_after_it_was_restarted(db_engine):
    """A restarted writer must not inherit the stopping flag of the old one."""
    writer = await _make_writer(db_engine)
    await writer.stop()
    await writer.start()
    # The restarted task needs a moment to notice that the writer is
    # stopping again: without the reset it leaves the loop at once and the
    # operation below is never written.
    await asyncio.sleep(0.05)
    record = _message("after the restart")
    record.message_id = 2
    assert writer.submit(WriteOp(OpKind.MESSAGE, record)) is True
    await writer.stop()
    async with db_engine.connect() as conn:
        rows = (await conn.execute(select(messages))).all()
    assert [row.content for row in rows] == ["after the restart"]


async def test_submit_after_stop_returns_false(db_engine, caplog):
    """An operation handed to a stopped writer would never be written."""
    writer = await _make_writer(db_engine)
    await writer.stop()
    with caplog.at_level(logging.WARNING, logger="foresight.writer"):
        assert writer.submit(WriteOp(OpKind.MESSAGE, _message())) is False
    assert any("stopped" in record.message for record in caplog.records)
