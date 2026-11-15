"""Tests for the attachment storage and the download worker.

The storage is tested against the backends it talks to: the local one on
a temporary directory, the S3 one against a server that speaks the S3
protocol. The worker is tested against a mocked HTTP API, so that the
bytes it stores, the status it writes and the metrics it counts are all
pinned down.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import aioboto3
import httpx
import pytest
import respx
from moto.server import ThreadedMotoServer
from sqlalchemy import select

from foresight.config import MediaConfig, S3Config
from foresight.domain.models import AttachmentRecord, OpKind, WriteOp
from foresight.media_worker import MediaWorker
from foresight.metrics import REGISTRY
from foresight.storage.media import (
    LocalMediaStore,
    S3MediaStore,
    build_media_store,
    media_key,
)
from foresight.storage.repository import Repository
from foresight.storage.tables import attachments
from foresight.writer import Writer


def _record(
    size: int | None = 10, url: str = "https://cdn.example/a.png"
) -> AttachmentRecord:
    return AttachmentRecord(
        message_id=1,
        attachment_id=555,
        filename="a.png",
        content_type="image/png",
        size=size,
        url=url,
        storage_backend="local",
        status="pending",
        captured_at="2026-11-11T00:00:00+00:00",
    )


def _sample(name: str, labels: dict[str, str] | None = None) -> float:
    """The current value of a metric of the process wide registry."""
    value = REGISTRY.get_sample_value(name, labels)
    return 0.0 if value is None else value


async def _no_wait(delay: float) -> None:
    """Stand in for the sleep between the attempts of the worker."""


async def _within(coro, timeout: float = 30.0):
    """Run a coroutine, and fail the test when it takes too long.

    The server of the tests is a real one: a call that hangs must fail
    the test instead of holding the suite.
    """
    return await asyncio.wait_for(coro, timeout=timeout)


async def _stop(worker: MediaWorker) -> None:
    """Stop the worker, and fail the test when it does not come back.

    The wait is bounded: a worker that hangs must fail the test instead
    of holding the suite until the timeout of the job.
    """
    await asyncio.wait_for(worker.stop(), timeout=10)


def test_media_key_uses_attachment_id_and_date():
    key = media_key(555, "a.png", datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc))
    assert key == "2026/11/11/555.png"
    assert "a.png" not in key


def test_media_key_without_filename():
    key = media_key(556, None, datetime(2026, 11, 11, tzinfo=timezone.utc))
    assert key == "2026/11/11/556"


def test_media_key_without_an_attachment_id_is_unique():
    """Two attachments without an id must not share a key.

    The key of an attachment without an id is built from the hash of its
    URL: the id alone would be the same for every one of them, and the
    second file would overwrite the first.
    """
    now = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)
    first = media_key(None, "a.png", now, seed="https://cdn.example/a.png")
    second = media_key(None, "a.png", now, seed="https://cdn.example/b.png")

    assert first != second
    assert "None" not in first
    assert first.endswith(".png")


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("photo.jpeg", "2026/11/11/555.jpeg"),
        ("archive.tar.gz", "2026/11/11/555.gz"),
        ("no-extension", "2026/11/11/555"),
        ("../../etc/passwd", "2026/11/11/555"),
        ("/absolute/path.bin", "2026/11/11/555.bin"),
    ],
)
def test_media_key_only_keeps_a_safe_extension(filename: str, expected: str):
    key = media_key(555, filename, datetime(2026, 11, 11, tzinfo=timezone.utc))
    assert key == expected


async def test_local_store_round_trip(tmp_path: Path):
    store = LocalMediaStore(tmp_path)
    uri = await store.put("2026/11/11/1.bin", b"hello", "application/octet-stream")
    assert uri.startswith("file://")
    assert await store.exists("2026/11/11/1.bin") is True
    chunks = [chunk async for chunk in store.open("2026/11/11/1.bin")]
    assert b"".join(chunks) == b"hello"


def test_build_media_store_selects_backend(tmp_path: Path):
    local = build_media_store(MediaConfig(backend="local", local_root=str(tmp_path)))
    assert isinstance(local, LocalMediaStore)
    s3 = build_media_store(
        MediaConfig(backend="s3", s3=S3Config(bucket="b", endpoint_url="http://localhost:9000"))
    )
    assert s3.__class__.__name__ == "S3MediaStore"


async def test_media_worker_downloads_and_marks_stored(db_engine, tmp_path: Path):
    writer = Writer(Repository(db_engine), batch_size=10, flush_interval_ms=10)
    await writer.start()
    writer.submit(WriteOp(OpKind.ATTACHMENT, _record()))

    downloads_before = _sample("foresight_media_downloads_total", {"status": "ok"})
    bytes_before = _sample("foresight_media_bytes_total")

    payload = b"binary-payload"
    with respx.mock:
        route = respx.get("https://cdn.example/a.png").mock(
            return_value=httpx.Response(200, content=payload)
        )
        worker = MediaWorker(
            writer, LocalMediaStore(tmp_path / "media"), max_concurrency=1
        )
        await worker.start()
        worker.submit(_record())
        await _stop(worker)
        await writer.stop()
        assert route.called

    async with db_engine.connect() as conn:
        row = (await conn.execute(select(attachments))).one()
    assert row.status == "stored"
    assert row.sha256 == hashlib.sha256(payload).hexdigest()
    assert row.storage_key == "2026/11/11/555.png"
    assert _sample("foresight_media_downloads_total", {"status": "ok"}) == downloads_before + 1
    assert _sample("foresight_media_bytes_total") == bytes_before + len(payload)


async def test_media_worker_follows_a_redirect(db_engine, tmp_path: Path):
    """The bytes of the redirect are not the bytes of the attachment."""
    writer = Writer(Repository(db_engine), batch_size=10, flush_interval_ms=10)
    await writer.start()
    writer.submit(WriteOp(OpKind.ATTACHMENT, _record()))

    payload = b"final-payload"
    with respx.mock:
        respx.get("https://cdn.example/a.png").mock(
            return_value=httpx.Response(
                302, headers={"Location": "https://cdn.example/final.png"}
            )
        )
        respx.get("https://cdn.example/final.png").mock(
            return_value=httpx.Response(200, content=payload)
        )
        worker = MediaWorker(
            writer, LocalMediaStore(tmp_path / "media"), max_concurrency=1
        )
        await worker.start()
        worker.submit(_record())
        await _stop(worker)
        await writer.stop()

    async with db_engine.connect() as conn:
        row = (await conn.execute(select(attachments))).one()
    assert row.status == "stored"
    assert row.sha256 == hashlib.sha256(payload).hexdigest()


async def test_media_worker_marks_failure(db_engine, tmp_path: Path, monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _no_wait)
    writer = Writer(Repository(db_engine), batch_size=10, flush_interval_ms=10)
    await writer.start()
    writer.submit(WriteOp(OpKind.ATTACHMENT, _record()))

    failures_before = _sample("foresight_media_downloads_total", {"status": "failed"})

    with respx.mock:
        respx.get("https://cdn.example/a.png").mock(
            return_value=httpx.Response(500, text="boom")
        )
        worker = MediaWorker(
            writer, LocalMediaStore(tmp_path / "media"), max_concurrency=1
        )
        await worker.start()
        worker.submit(_record())
        await _stop(worker)
        await writer.stop()

    async with db_engine.connect() as conn:
        row = (await conn.execute(select(attachments))).one()
    assert row.status == "failed"
    assert row.error
    assert (
        _sample("foresight_media_downloads_total", {"status": "failed"})
        == failures_before + 1
    )


async def test_media_worker_retries_the_download_three_times(
    db_engine, tmp_path: Path, monkeypatch
):
    """One attempt and three retries are four attempts."""
    monkeypatch.setattr(asyncio, "sleep", _no_wait)
    writer = Writer(Repository(db_engine), batch_size=10, flush_interval_ms=10)
    await writer.start()
    writer.submit(WriteOp(OpKind.ATTACHMENT, _record()))

    with respx.mock:
        route = respx.get("https://cdn.example/a.png").mock(
            return_value=httpx.Response(500, text="boom")
        )
        worker = MediaWorker(
            writer, LocalMediaStore(tmp_path / "media"), max_concurrency=1
        )
        await worker.start()
        worker.submit(_record())
        await _stop(worker)
        await writer.stop()

    assert route.call_count == 4


async def test_media_worker_waits_longer_after_every_retry(
    db_engine, tmp_path: Path, monkeypatch
):
    delays: list[float] = []

    async def record(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(asyncio, "sleep", record)
    writer = Writer(Repository(db_engine), batch_size=10, flush_interval_ms=10)
    await writer.start()
    writer.submit(WriteOp(OpKind.ATTACHMENT, _record()))

    with respx.mock:
        respx.get("https://cdn.example/a.png").mock(
            return_value=httpx.Response(500, text="boom")
        )
        worker = MediaWorker(
            writer, LocalMediaStore(tmp_path / "media"), max_concurrency=1
        )
        await worker.start()
        worker.submit(_record())
        await _stop(worker)
        await writer.stop()

    assert len(delays) == 3
    assert delays == sorted(delays)
    assert delays[0] < delays[-1]


async def test_media_worker_uses_one_client_for_every_attempt(
    db_engine, tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(asyncio, "sleep", _no_wait)
    clients: list[httpx.AsyncClient] = []

    def factory(**kwargs) -> httpx.AsyncClient:
        client = httpx.AsyncClient(**kwargs)
        clients.append(client)
        return client

    writer = Writer(Repository(db_engine), batch_size=10, flush_interval_ms=10)
    await writer.start()
    writer.submit(WriteOp(OpKind.ATTACHMENT, _record()))

    with respx.mock:
        respx.get("https://cdn.example/a.png").mock(
            return_value=httpx.Response(500, text="boom")
        )
        worker = MediaWorker(
            writer,
            LocalMediaStore(tmp_path / "media"),
            max_concurrency=1,
            client_factory=factory,
        )
        await worker.start()
        worker.submit(_record())
        await _stop(worker)
        await writer.stop()

    assert len(clients) == 1
    assert clients[0].is_closed


async def test_media_worker_skips_oversized(db_engine, tmp_path: Path):
    writer = Writer(Repository(db_engine), batch_size=10, flush_interval_ms=10)
    await writer.start()
    writer.submit(WriteOp(OpKind.ATTACHMENT, _record(size=999)))

    skipped_before = _sample("foresight_media_downloads_total", {"status": "skipped"})

    with respx.mock:
        route = respx.get("https://cdn.example/a.png").mock(
            return_value=httpx.Response(200, content=b"too-big")
        )
        worker = MediaWorker(
            writer, LocalMediaStore(tmp_path / "media"), max_concurrency=1, max_bytes=10
        )
        await worker.start()
        worker.submit(_record(size=999))
        await _stop(worker)
        await writer.stop()
        assert not route.called

    async with db_engine.connect() as conn:
        row = (await conn.execute(select(attachments))).one()
    assert row.status == "skipped"
    assert (
        _sample("foresight_media_downloads_total", {"status": "skipped"})
        == skipped_before + 1
    )


async def test_media_worker_skips_a_file_that_is_bigger_than_the_cap(
    db_engine, tmp_path: Path
):
    """The server may send more bytes than it announced.

    Discord does not always announce the size of an attachment, and the
    server may send more bytes than it announced in any case: the bytes
    are counted as they arrive, and the file is skipped as soon as it is
    bigger than the cap.
    """
    writer = Writer(Repository(db_engine), batch_size=10, flush_interval_ms=10)
    await writer.start()
    writer.submit(WriteOp(OpKind.ATTACHMENT, _record(size=None)))

    with respx.mock:
        respx.get("https://cdn.example/a.png").mock(
            return_value=httpx.Response(200, content=b"x" * 4096)
        )
        worker = MediaWorker(
            writer, LocalMediaStore(tmp_path / "media"), max_concurrency=1, max_bytes=10
        )
        await worker.start()
        worker.submit(_record(size=None))
        await _stop(worker)
        await writer.stop()

    async with db_engine.connect() as conn:
        row = (await conn.execute(select(attachments))).one()
    assert row.status == "skipped"
    assert row.storage_key is None
    assert not (tmp_path / "media").exists() or not list(
        (tmp_path / "media").rglob("*.png")
    )


async def test_the_log_does_not_carry_the_signature_of_the_cdn(
    db_engine, tmp_path: Path, caplog, monkeypatch
):
    """The query string of the URL of Discord grants read access to the file.

    It must not reach the log, and it must not reach the row of the
    attachment either: the error of a failed download carries the URL.
    """
    monkeypatch.setattr(asyncio, "sleep", _no_wait)
    url = "https://cdn.discordapp.com/attachments/1/2/a.png?ex=abc&sig=SECRET"
    writer = Writer(Repository(db_engine), batch_size=10, flush_interval_ms=10)
    await writer.start()
    writer.submit(WriteOp(OpKind.ATTACHMENT, _record(url=url)))

    with caplog.at_level(logging.WARNING):
        with respx.mock:
            respx.get(url).mock(return_value=httpx.Response(404, text="nope"))
            worker = MediaWorker(
                writer, LocalMediaStore(tmp_path / "media"), max_concurrency=1
            )
            await worker.start()
            worker.submit(_record(url=url))
            await _stop(worker)
            await writer.stop()

    assert "SECRET" not in caplog.text
    async with db_engine.connect() as conn:
        row = (await conn.execute(select(attachments))).one()
    assert row.error
    assert "SECRET" not in row.error


async def test_the_media_worker_stops_with_a_full_queue(db_engine, tmp_path: Path):
    """The shutdown must not hang on a backlog.

    The worker puts the end of the queue behind the work that is still
    to be done, so a long backlog would keep the process alive for a
    long time. The wait is bounded, and the workers are cancelled when
    the bound is reached.
    """
    writer = Writer(Repository(db_engine), batch_size=10, flush_interval_ms=10)
    await writer.start()

    class _SlowClient:
        def __init__(self, **kwargs: Any) -> None:
            self.closed = False

        async def get(self, url: str) -> Any:
            await asyncio.sleep(30)
            raise AssertionError("the download must not have been awaited")

        async def aclose(self) -> None:
            self.closed = True

    with respx.mock:
        respx.get("https://cdn.example/a.png").mock(
            return_value=httpx.Response(200, content=b"payload")
        )
        worker = MediaWorker(
            writer,
            LocalMediaStore(tmp_path / "media"),
            max_concurrency=1,
            client_factory=_SlowClient,
            drain_timeout=0.5,
        )
        await worker.start()
        worker.submit(_record())
        started = time.monotonic()
        await asyncio.wait_for(worker.stop(), timeout=20)
        elapsed = time.monotonic() - started

    assert elapsed < 10
    await writer.stop()


# --- the S3 store, against a real S3 API -------------------------------
#
# The store speaks the S3 protocol, so it is tested against a server that
# speaks it too: `moto`'s server mode is a real HTTP server, and it works
# with the async client of `aioboto3`.


@dataclass
class _S3Server:
    """A running S3 API and the bucket the tests store their files in."""

    endpoint_url: str
    bucket: str

    def client(self):  # noqa: ANN201
        """A client of the server, with the credentials it accepts."""
        return aioboto3.Session().client(
            "s3",
            endpoint_url=self.endpoint_url,
            region_name="us-east-1",
            aws_access_key_id="test",
            aws_secret_access_key="test",
        )

    def store(self) -> S3MediaStore:
        """The store under test, pointed at the server."""
        return S3MediaStore(
            S3Config(
                bucket=self.bucket,
                endpoint_url=self.endpoint_url,
                region="us-east-1",
                prefix="foresight",
                use_path_style=True,
                access_key_id="test",
                secret_access_key="test",
            )
        )


def _start_s3_server() -> ThreadedMotoServer:
    """Start the S3 server, and fail fast when it does not come up.

    The server binds a port of its own, so it cannot lose a race for it.
    The wait for it is bounded: a server that stays down must fail the
    test instead of blocking the whole suite.
    """
    server = ThreadedMotoServer(ip_address="127.0.0.1", port=0, verbose=False)
    failures: list[BaseException] = []

    def run() -> None:
        try:
            server.start()
        except BaseException as exc:  # noqa: BLE001
            failures.append(exc)

    thread = threading.Thread(target=run, daemon=True, name="moto-s3")
    thread.start()
    thread.join(timeout=10)
    if thread.is_alive():
        raise RuntimeError("the S3 server did not start within 10 seconds")
    if failures:
        raise failures[0]
    return server


@pytest.fixture
async def s3_server() -> AsyncIterator[_S3Server]:
    """An S3 API on the loopback, with a bucket of its own.

    The bucket carries a random name: the state of the server lives in the
    process, so a bucket of a fixed name would survive the test that
    created it.
    """
    server = _start_s3_server()
    try:
        host, port = server.get_host_and_port()
        endpoint_url = f"http://{host}:{port}"
        bucket = f"foresight-test-{uuid4().hex[:12]}"
        async with aioboto3.Session().client(
            "s3",
            endpoint_url=endpoint_url,
            region_name="us-east-1",
            aws_access_key_id="test",
            aws_secret_access_key="test",
        ) as client:
            await _within(client.create_bucket(Bucket=bucket))
        yield _S3Server(endpoint_url=endpoint_url, bucket=bucket)
    finally:
        server.stop()


async def _read_all(store: S3MediaStore, key: str) -> bytes:
    """Every byte the store gives for a key."""
    chunks = [chunk async for chunk in store.open(key)]
    return b"".join(chunks)


async def test_s3_store_put_returns_the_uri_and_stores_the_bytes(s3_server: _S3Server):
    store = s3_server.store()
    payload = b"binary-payload"

    uri = await _within(store.put("2026/11/11/555.png", payload, "image/png"))

    assert uri == f"s3://{s3_server.bucket}/foresight/2026/11/11/555.png"
    async with s3_server.client() as client:
        head = await _within(
            client.head_object(
                Bucket=s3_server.bucket, Key="foresight/2026/11/11/555.png"
            )
        )
        assert head["ContentLength"] == len(payload)
        assert head["ContentType"] == "image/png"
        stored = await _within(
            client.get_object(
                Bucket=s3_server.bucket, Key="foresight/2026/11/11/555.png"
            )
        )
        assert await stored["Body"].read() == payload


async def test_s3_store_exists_is_true_only_for_a_stored_key(s3_server: _S3Server):
    store = s3_server.store()
    await _within(store.put("2026/11/11/555.png", b"binary-payload", "image/png"))

    assert await _within(store.exists("2026/11/11/555.png")) is True
    assert await _within(store.exists("2026/11/11/999.png")) is False


async def test_s3_store_open_yields_the_stored_bytes(s3_server: _S3Server):
    store = s3_server.store()
    payload = b"binary-payload"
    await _within(store.put("2026/11/11/555.png", payload, "image/png"))

    data = await _within(_read_all(store, "2026/11/11/555.png"))

    assert data == payload


# --- the status of an attachment ----------------------------------------
#
# The status of an attachment is written through the queue of the writer,
# so it can never overtake the insert of the row it belongs to: the writer
# applies the attachments before the statuses of the attachments.


async def test_the_status_write_cannot_overtake_the_insert(db_engine, tmp_path: Path):
    """The row of an attachment must exist before its status is written.

    The worker writes the status while the row is still only queued: the
    status must not be lost, and the row must not stay `pending` for good.
    """
    from foresight.writer import Writer

    repo = Repository(db_engine)
    writer = Writer(repo, batch_size=50, flush_interval_ms=10)
    # The row is only queued: the writer has not written it yet.
    writer.submit(WriteOp(OpKind.ATTACHMENT, _record()))

    with respx.mock:
        respx.get("https://cdn.example/a.png").mock(
            return_value=httpx.Response(200, content=b"payload")
        )
        worker = MediaWorker(
            writer, LocalMediaStore(tmp_path / "media"), max_concurrency=1
        )
        await worker.start()
        worker.submit(_record())
        await _stop(worker)

    # The status was written while the row did not exist yet.
    async with db_engine.connect() as conn:
        assert (await conn.execute(select(attachments))).all() == []

    await writer.start()
    await writer.stop()

    async with db_engine.connect() as conn:
        row = (await conn.execute(select(attachments))).one()
    assert row.status == "stored"
    assert row.sha256 == hashlib.sha256(b"payload").hexdigest()
