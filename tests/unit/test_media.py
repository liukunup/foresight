import asyncio
import hashlib
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
import respx

from foresight.config import MediaConfig, S3Config
from foresight.domain.models import AttachmentRecord, OpKind, WriteOp
from foresight.media_worker import MediaWorker
from foresight.metrics import REGISTRY
from foresight.storage.db import init_db
from foresight.storage.media import LocalMediaStore, build_media_store, media_key
from foresight.storage.repository import Repository
from foresight.storage.tables import attachments


def _record(size: int = 10) -> AttachmentRecord:
    return AttachmentRecord(
        message_id=1,
        attachment_id=555,
        filename="a.png",
        content_type="image/png",
        size=size,
        url="https://cdn.example/a.png",
        storage_backend="local",
        status="pending",
        captured_at="2026-11-11T00:00:00+00:00",
    )


def _sample(name: str, labels: dict[str, str] | None = None) -> float:
    """The current value of a metric of the process wide registry."""
    value = REGISTRY.get_sample_value(name, labels)
    return 0.0 if value is None else value


def test_media_key_uses_attachment_id_and_date():
    key = media_key(555, "a.png", datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc))
    assert key == "2026/11/11/555.png"
    assert "a.png" not in key


def test_media_key_without_filename():
    key = media_key(556, None, datetime(2026, 11, 11, tzinfo=timezone.utc))
    assert key == "2026/11/11/556"


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


async def test_media_worker_downloads_and_marks_stored(tmp_path: Path):
    engine_kwargs = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(engine_kwargs)
    await init_db(engine)
    repo = Repository(engine)
    await repo.apply_batch([WriteOp(OpKind.ATTACHMENT, _record())])

    downloads_before = _sample("foresight_media_downloads_total", {"status": "ok"})
    bytes_before = _sample("foresight_media_bytes_total")

    payload = b"binary-payload"
    with respx.mock:
        route = respx.get("https://cdn.example/a.png").mock(
            return_value=httpx.Response(200, content=payload)
        )
        worker = MediaWorker(repo, LocalMediaStore(tmp_path / "media"), max_concurrency=1)
        await worker.start()
        worker.submit(_record())
        await asyncio.sleep(0.6)
        await worker.stop()
        assert route.called

    from sqlalchemy import select

    async with engine.connect() as conn:
        row = (await conn.execute(select(attachments))).one()
    assert row.status == "stored"
    assert row.sha256 == hashlib.sha256(payload).hexdigest()
    assert row.storage_key == "2026/11/11/555.png"
    assert _sample("foresight_media_downloads_total", {"status": "ok"}) == downloads_before + 1
    assert _sample("foresight_media_bytes_total") == bytes_before + len(payload)
    await engine.dispose()


async def test_media_worker_marks_failure(tmp_path: Path):
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    await init_db(engine)
    repo = Repository(engine)
    await repo.apply_batch([WriteOp(OpKind.ATTACHMENT, _record())])

    failures_before = _sample("foresight_media_downloads_total", {"status": "failed"})

    with respx.mock:
        respx.get("https://cdn.example/a.png").mock(
            return_value=httpx.Response(500, text="boom")
        )
        worker = MediaWorker(repo, LocalMediaStore(tmp_path / "media"), max_concurrency=1)
        await worker.start()
        worker.submit(_record())
        await asyncio.sleep(0.6)
        await worker.stop()

    async with engine.connect() as conn:
        row = (await conn.execute(select(attachments))).one()
    assert row.status == "failed"
    assert row.error
    assert (
        _sample("foresight_media_downloads_total", {"status": "failed"})
        == failures_before + 1
    )
    await engine.dispose()


async def test_media_worker_skips_oversized(tmp_path: Path):
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    await init_db(engine)
    repo = Repository(engine)
    await repo.apply_batch([WriteOp(OpKind.ATTACHMENT, _record(size=999))])

    skipped_before = _sample("foresight_media_downloads_total", {"status": "skipped"})

    worker = MediaWorker(
        repo, LocalMediaStore(tmp_path / "media"), max_concurrency=1, max_bytes=10
    )
    await worker.start()
    worker.submit(_record(size=999))
    await asyncio.sleep(0.4)
    await worker.stop()

    async with engine.connect() as conn:
        row = (await conn.execute(select(attachments))).one()
    assert row.status == "skipped"
    assert (
        _sample("foresight_media_downloads_total", {"status": "skipped"})
        == skipped_before + 1
    )
    await engine.dispose()
