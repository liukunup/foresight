"""Tests for the batched write queue.

The writer is the only component that talks to the database from the event
loop, so these tests pin the guarantees the rest of the service relies on: a
submitted operation is eventually written, a full queue never blocks the
caller, a failed batch is kept on disk, and a failed batch is replayed on the
next start.
"""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

import foresight.writer as writer_module
from foresight.domain.models import MessageRecord, OpKind, WriteOp
from foresight.storage.db import init_db
from foresight.storage.repository import Repository
from foresight.storage.tables import messages
from foresight.writer import Writer


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


async def _make_writer(tmp_path, repo=None, queue_maxsize: int | None = None):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    await init_db(engine)
    repository = repo or Repository(engine)
    kwargs = {} if queue_maxsize is None else {"queue_maxsize": queue_maxsize}
    writer = Writer(repository, batch_size=2, flush_interval_ms=10, **kwargs)
    await writer.start()
    return writer, engine


async def test_writer_flushes_submitted_ops(tmp_path):
    writer, engine = await _make_writer(tmp_path)
    assert writer.submit(WriteOp(OpKind.MESSAGE, _message())) is True
    await writer.stop()
    async with engine.connect() as conn:
        rows = (await conn.execute(select(messages))).all()
    assert len(rows) == 1


async def test_writer_drains_queue_on_stop(tmp_path):
    writer, engine = await _make_writer(tmp_path)
    for index in range(5):
        record = _message()
        record.message_id = index + 1
        writer.submit(WriteOp(OpKind.MESSAGE, record))
    await writer.stop()
    async with engine.connect() as conn:
        rows = (await conn.execute(select(messages))).all()
    assert len(rows) == 5


async def test_writer_returns_false_when_queue_is_full(tmp_path):
    writer, _ = await _make_writer(tmp_path, queue_maxsize=1)
    writer.submit(WriteOp(OpKind.MESSAGE, _message()))
    assert writer.submit(WriteOp(OpKind.MESSAGE, _message())) is False
    await writer.stop()


async def test_writer_reports_the_queue_depth(tmp_path):
    writer, _ = await _make_writer(tmp_path)
    assert writer.queue_depth == 0
    writer.submit(WriteOp(OpKind.MESSAGE, _message()))
    assert writer.queue_depth == 1
    await writer.stop()


async def test_recover_failed_batches_replays_records(tmp_path):
    from foresight.writer import recover_failed_batches

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    await init_db(engine)
    repo = Repository(engine)
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
    async with engine.connect() as conn:
        rows = (await conn.execute(select(messages))).all()
    assert len(rows) == 1
    assert rows[0].content == "recovered"
    assert failure_path.read_text(encoding="utf-8") == ""


async def test_recover_failed_batches_keeps_the_file_when_the_replay_fails(
    tmp_path, monkeypatch
):
    """A failed replay must not lose the operations it could not write."""
    from foresight.writer import recover_failed_batches

    monkeypatch.setattr(writer_module, "RETRY_DELAYS", (0.0, 0.0, 0.0))

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


async def test_writer_persists_failed_batch(tmp_path):
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
    # The failed batch was tried three times before it was given up on.
    assert repo.calls == 4


def test_op_classes_cover_every_op_kind_with_a_dataclass_payload():
    """A kind without a mapping would be recovered as a plain dict."""
    kinds = {
        value
        for name, value in vars(OpKind).items()
        if not name.startswith("_") and isinstance(value, str)
    }
    unmapped = kinds - set(writer_module._OP_CLASSES)  # noqa: SLF001
    # ANALYSIS_STATUS is the only kind whose payload is not a dataclass: it
    # carries a plain (message_id, status) tuple.
    assert unmapped == {OpKind.ANALYSIS_STATUS}
    assert all(
        is_dataclass(cls) for cls in writer_module._OP_CLASSES.values()  # noqa: SLF001
    )


def test_rebuild_restores_the_domain_object():
    record = _message("rebuilt")
    rebuilt = writer_module._rebuild(OpKind.MESSAGE, asdict(record))  # noqa: SLF001
    assert rebuilt == record


def test_rebuild_turns_an_analysis_status_payload_into_a_tuple():
    rebuilt = writer_module._rebuild(  # noqa: SLF001
        OpKind.ANALYSIS_STATUS, [42, "done"]
    )
    assert rebuilt == (42, "done")
