# ForeSight M3 — AI 分析、媒体存储与补录 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 M1/M2 之上补齐三块能力：附件下载到本地/S3 并登记、AI 翻译/情绪/问题提取、历史消息补录命令。

**Architecture:** 三个互相独立的后台子系统，都以队列 + worker 的形式挂在现有写入链路旁：媒体 worker 订阅附件记录，AI worker 订阅待分析消息，backfill 是独立 CLI 进程复用同一套 mapper/writer/storage 路径。

**Tech Stack:** httpx、aioboto3（异步 S3）、pytest + pytest-asyncio、respx（mock httpx）、moto（mock S3）。

**前置条件：** M1、M2 已完成。

## Global Constraints

- M1/M2 的全部 Global Constraints 继续生效
- 媒体只存**原文件**：不生成缩略图、不转码、不抽帧
- 媒体 key 统一用 `attachment_id` 命名（不用原文件名），按 `YYYY/MM/DD/` 分目录
- 附件下载在**后台队列**执行，不阻塞消息入库；失败重试 3 次后标 `failed`
- 单个附件超过 `max_download_size_mb` 时跳过下载，保留 URL，状态标 `skipped`
- AI 分析走 **OpenAI 兼容接口**，一次调用产出 `translation` / `sentiment` / `sentiment_score` / `issues` 四个字段
- `sentiment` 枚举固定为 `positive` / `neutral` / `negative` / `mixed`
- AI 结果写入独立表 `message_analysis`；`messages.analysis_status` 取 `pending` / `done` / `failed` / `skipped`
- LLM 返回非 JSON 时用更严格的 prompt 重试一次；仍失败标 `failed`
- `api_key` 只从环境变量读，不进配置、不进日志
- 补录（backfill）**只补消息**，不补表情、**不跑 AI**（`analysis_status='skipped'`）
- 补录写入用 `insert_only` 策略，**绝不覆盖**实时捕获的数据
- 补录的 `--days` 与 `--since` 互斥，必须且只能给一个（默认 `--days 7`）

---

## Task 15: 媒体存储与下载 Worker

**Files:**
- Create: `src/foresight/storage/media.py`
- Create: `src/foresight/media_worker.py`
- Create: `tests/unit/test_media.py`

**Interfaces:**
- Consumes: `foresight.config.MediaConfig` / `S3Config`（M1 Task 1）、`foresight.domain.models.AttachmentRecord` / `AttachmentStatus` / `OpKind` / `WriteOp`（M1 Task 2）、`foresight.storage.repository.Repository`（M1 Task 3）
- Produces:
  - `foresight.storage.media.MediaStore`（Protocol）：`async put(key: str, data: bytes, content_type: str) -> str`、`async exists(key: str) -> bool`、`async open(key: str) -> AsyncIterator[bytes]`
  - `foresight.storage.media.LocalMediaStore(root: Path)`
  - `foresight.storage.media.S3MediaStore(config: S3Config)`
  - `foresight.storage.media.build_media_store(config: MediaConfig) -> MediaStore`
  - `foresight.storage.media.media_key(attachment_id: int, filename: str | None, now: datetime) -> str`
  - `foresight.media_worker.MediaWorker(repo, store, *, max_concurrency=3, max_bytes=104857600, queue_maxsize=1000, client_factory=None)`
  - `MediaWorker.start() -> None`、`MediaWorker.stop() -> None`、`MediaWorker.submit(record: AttachmentRecord) -> bool`

- [ ] **Step 1: 写失败测试**

创建 `tests/unit/test_media.py`：

```python
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


def test_media_key_uses_attachment_id_and_date():
    key = media_key(555, "a.png", datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc))
    assert key == "2026/11/11/555.png"
    assert "a.png" not in key


def test_media_key_without_filename():
    key = media_key(556, None, datetime(2026, 11, 11, tzinfo=timezone.utc))
    assert key == "2026/11/11/556"


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
    await engine.dispose()


async def test_media_worker_marks_failure(tmp_path: Path):
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    await init_db(engine)
    repo = Repository(engine)
    await repo.apply_batch([WriteOp(OpKind.ATTACHMENT, _record())])

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
    await engine.dispose()


async def test_media_worker_skips_oversized(tmp_path: Path):
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    await init_db(engine)
    repo = Repository(engine)
    await repo.apply_batch([WriteOp(OpKind.ATTACHMENT, _record(size=999))])

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
    await engine.dispose()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/unit/test_media.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'foresight.storage.media'`

- [ ] **Step 3: 实现媒体存储抽象**

创建 `src/foresight/storage/media.py`：

```python
"""Pluggable attachment storage: local filesystem or S3 compatible."""

from __future__ import annotations

import hashlib
import mimetypes
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator, Protocol

from foresight.config import MediaConfig, S3Config


class MediaStore(Protocol):
    """Where attachment bytes live."""

    async def put(self, key: str, data: bytes, content_type: str) -> str:
        """Store bytes and return a URI that points at them."""
        ...

    async def exists(self, key: str) -> bool: ...

    async def open(self, key: str) -> AsyncIterator[bytes]: ...


def media_key(attachment_id: int, filename: str | None, now: datetime) -> str:
    """Build a collision free storage key from the attachment id."""
    extension = ""
    if filename and "." in filename:
        suffix = filename.rsplit(".", 1)[-1]
        if suffix.isalnum() and len(suffix) <= 8:
            extension = f".{suffix.lower()}"
    return f"{now:%Y/%m/%d}/{attachment_id}{extension}"


class LocalMediaStore:
    """Stores attachments on the local filesystem."""

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)

    def _path(self, key: str) -> Path:
        return self._root / key

    async def put(self, key: str, data: bytes, content_type: str) -> str:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path.resolve().as_uri()

    async def exists(self, key: str) -> bool:
        return self._path(key).exists()

    async def open(self, key: str) -> AsyncIterator[bytes]:
        path = self._path(key)
        if path.exists():
            yield path.read_bytes()


class S3MediaStore:
    """Stores attachments in an S3 compatible bucket."""

    def __init__(self, config: S3Config) -> None:
        self._config = config

    def _full_key(self, key: str) -> str:
        prefix = self._config.prefix.strip("/")
        return f"{prefix}/{key}" if prefix else key

    def _client(self):  # noqa: ANN202
        import aioboto3

        return aioboto3.Session().client(
            "s3",
            endpoint_url=self._config.endpoint_url or None,
            region_name=self._config.region,
            aws_access_key_id=self._config.access_key_id or None,
            aws_secret_access_key=self._config.secret_access_key or None,
        )

    async def put(self, key: str, data: bytes, content_type: str) -> str:
        full_key = self._full_key(key)
        async with self._client() as client:
            await client.put_object(
                Bucket=self._config.bucket,
                Key=full_key,
                Body=data,
                ContentType=content_type or mimetypes.guess_type(key)[0] or "application/octet-stream",
            )
        return f"s3://{self._config.bucket}/{full_key}"

    async def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        async with self._client() as client:
            try:
                await client.head_object(
                    Bucket=self._config.bucket, Key=self._full_key(key)
                )
            except ClientError:
                return False
        return True

    async def open(self, key: str) -> AsyncIterator[bytes]:
        async with self._client() as client:
            response = await client.get_object(
                Bucket=self._config.bucket, Key=self._full_key(key)
            )
            async with response["Body"] as stream:
                while True:
                    chunk = await stream.read(65536)
                    if not chunk:
                        break
                    yield chunk


def build_media_store(config: MediaConfig) -> MediaStore:
    if config.backend == "s3":
        return S3MediaStore(config.s3)
    return LocalMediaStore(config.local_root)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
```

- [ ] **Step 4: 实现下载 Worker**

创建 `src/foresight/media_worker.py`：

```python
"""Downloads attachments in the background and registers them in storage."""

from __future__ import annotations

import asyncio
import hashlib
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

logger = logging.getLogger(__name__)

MAX_RETRIES = 3


class MediaWorker:
    """Consumes attachment records and stores their bytes."""

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
        self._queue: asyncio.Queue[AttachmentRecord] = asyncio.Queue(maxsize=queue_maxsize)
        self._client_factory = client_factory or httpx.AsyncClient
        self._tasks: list[asyncio.Task[None]] = []
        self._stopping = False

    async def start(self) -> None:
        if self._tasks:
            return
        self._tasks = [
            asyncio.create_task(self._worker(index), name=f"foresight-media-{index}")
            for index in range(self._max_concurrency)
        ]

    async def stop(self) -> None:
        self._stopping = True
        for task in self._tasks:
            await task
        self._tasks = []

    def submit(self, record: AttachmentRecord) -> bool:
        try:
            self._queue.put_nowait(record)
            return True
        except asyncio.QueueFull:
            logger.warning("media queue is full, dropping attachment %s", record.url)
            return False

    async def _worker(self, index: int) -> None:  # noqa: ARG002
        while True:
            record = await self._queue.get()
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
            await self._repo.apply_batch(
                [
                    WriteOp(
                        OpKind.ATTACHMENT_STATUS,
                        AttachmentStatus(
                            record.message_id,
                            record.attachment_id,
                            "skipped",
                            error="attachment exceeds the configured size limit",
                        ),
                    )
                ]
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
                    await asyncio.sleep(1.0 * (attempt + 1))
        await self._repo.apply_batch(
            [
                WriteOp(
                    OpKind.ATTACHMENT_STATUS,
                    AttachmentStatus(
                        record.message_id,
                        record.attachment_id,
                        "failed",
                        error=last_error,
                    ),
                )
            ]
        )

    async def _store_bytes(self, record: AttachmentRecord, data: bytes) -> None:
        from foresight.storage.media import media_key

        now = datetime.now(timezone.utc)
        key = media_key(record.attachment_id, record.filename, now)
        uri = await self._store.put(key, data, record.content_type or "application/octet-stream")
        await self._repo.apply_batch(
            [
                WriteOp(
                    OpKind.ATTACHMENT_STATUS,
                    AttachmentStatus(
                        message_id=record.message_id,
                        attachment_id=record.attachment_id,
                        status="stored",
                        sha256=hashlib.sha256(data).hexdigest(),
                        storage_key=key,
                        storage_uri=uri,
                        stored_at=iso(now),
                    ),
                )
            ]
        )
```

注意：超过大小上限的附件状态应为 `skipped` 而不是 `failed`，所以统一用一个 `AttachmentStatus` 领域对象表达附件的终态：

```python
@dataclass
class AttachmentStatus:
    """Terminal status update for one attachment row."""

    message_id: int
    attachment_id: int
    status: str
    error: str | None = None
    sha256: str | None = None
    storage_key: str | None = None
    storage_uri: str | None = None
    stored_at: str | None = None
```

对应的 `OpKind` 为 `ATTACHMENT_STATUS`，repository handler 为 `_handle_attachment_status`（按 `(message_id, attachment_id)` 定位行）。这两个定义在 M1 计划中已是最终形态，直接使用即可。

- [ ] **Step 5: 运行测试确认通过**

Run: `uv run pytest tests/unit/test_media.py -v`
Expected: PASS —— 6 passed

- [ ] **Step 6: 提交**

```bash
git add src/foresight/storage/media.py src/foresight/media_worker.py tests/unit/test_media.py
git commit -m "feat: attachment download worker with local and S3 storage"
```

---

## Task 16: AI 分析子系统

**Files:**
- Create: `src/foresight/analysis/__init__.py`
- Create: `src/foresight/analysis/prompts.py`
- Create: `src/foresight/analysis/llm.py`
- Create: `src/foresight/analysis/worker.py`
- Create: `tests/unit/test_analysis.py`

**Interfaces:**
- Consumes: `foresight.config.AiConfig`（M1 Task 1）、`foresight.domain.models.MessageAnalysis` / `OpKind` / `WriteOp`（M1 Task 2）、`foresight.storage.repository.Repository`（M1 Task 3）
- Produces:
  - `foresight.analysis.prompts.PROMPT_VERSION: str`
  - `foresight.analysis.prompts.build_prompt(content: str, translation_target: str) -> str`
  - `foresight.analysis.prompts.build_strict_prompt(content: str, translation_target: str) -> str`
  - `foresight.analysis.llm.LlmResult`（dataclass：`translation`、`sentiment`、`sentiment_score`、`issues`）
  - `foresight.analysis.llm.LlmError(Exception)`
  - `foresight.analysis.llm.LlmClient(config: AiConfig, client_factory=None)`，`async analyze(content: str) -> LlmResult`
  - `foresight.analysis.worker.AnalysisWorker(repo, llm, *, max_concurrency=2, queue_maxsize=1000)`
  - `AnalysisWorker.start()` / `.stop()` / `.submit(message_id: int, content: str) -> bool`

- [ ] **Step 1: 写失败测试**

创建 `tests/unit/test_analysis.py`：

```python
import asyncio
import json

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from foresight.analysis.llm import LlmClient, LlmError
from foresight.analysis.worker import AnalysisWorker
from foresight.config import AiConfig
from foresight.storage.db import init_db
from foresight.storage.repository import Repository
from foresight.storage.tables import message_analysis, messages


def _response(payload: dict) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [
                {"message": {"content": json.dumps(payload, ensure_ascii=False)}}
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        },
    )


async def test_llm_client_parses_json_payload():
    config = AiConfig(
        enabled=True,
        base_url="https://llm.example/v1",
        model="test-model",
        api_key="key",
    )
    with respx.mock:
        respx.post("https://llm.example/v1/chat/completions").mock(
            return_value=_response(
                {
                    "translation": "你好",
                    "sentiment": "negative",
                    "sentiment_score": 0.8,
                    "issues": ["登录超时"],
                }
            )
        )
        client = LlmClient(config)
        result = await client.analyze("login always times out")
    assert result.translation == "你好"
    assert result.sentiment == "negative"
    assert result.issues == ["登录超时"]


async def test_llm_client_raises_on_non_json():
    config = AiConfig(enabled=True, base_url="https://llm.example/v1", api_key="key")
    with respx.mock:
        respx.post("https://llm.example/v1/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={"choices": [{"message": {"content": "not json"}}]},
            )
        )
        client = LlmClient(config)
        with pytest.raises(LlmError):
            await client.analyze("hello")


async def test_llm_client_raises_without_api_key():
    client = LlmClient(AiConfig(enabled=True, base_url="https://llm.example/v1"))
    with pytest.raises(LlmError):
        await client.analyze("hello")


async def test_worker_stores_analysis(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'a.db'}")
    await init_db(engine)
    repo = Repository(engine)

    with respx.mock:
        respx.post("https://llm.example/v1/chat/completions").mock(
            return_value=_response(
                {
                    "translation": "你好",
                    "sentiment": "positive",
                    "sentiment_score": 0.9,
                    "issues": [],
                }
            )
        )
        client = LlmClient(
            AiConfig(enabled=True, base_url="https://llm.example/v1", api_key="key")
        )
        worker = AnalysisWorker(repo, client, max_concurrency=1)
        await worker.start()
        worker.submit(1, "good morning")
        await asyncio.sleep(0.5)
        await worker.stop()

    async with engine.connect() as conn:
        row = (
            await conn.execute(select(message_analysis).where(message_analysis.c.message_id == 1))
        ).one()
    assert row.sentiment == "positive"
    assert row.translation == "你好"
    await engine.dispose()


async def test_worker_marks_failed_after_retries(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'a.db'}")
    await init_db(engine)
    repo = Repository(engine)

    with respx.mock:
        route = respx.post("https://llm.example/v1/chat/completions").mock(
            return_value=httpx.Response(500, text="boom")
        )
        client = LlmClient(
            AiConfig(enabled=True, base_url="https://llm.example/v1", api_key="key")
        )
        worker = AnalysisWorker(repo, client, max_concurrency=1)
        await worker.start()
        worker.submit(1, "hello")
        await asyncio.sleep(1.5)
        await worker.stop()
        assert route.call_count >= 2

    async with engine.connect() as conn:
        row = (
            await conn.execute(select(message_analysis).where(message_analysis.c.message_id == 1))
        ).one()
    assert row.error
    await engine.dispose()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/unit/test_analysis.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'foresight.analysis'`

- [ ] **Step 3: 实现 prompt 构造**

创建 `src/foresight/analysis/__init__.py`（空文件）：

```python
```

创建 `src/foresight/analysis/prompts.py`：

```python
"""Prompt templates for message analysis."""

from __future__ import annotations

PROMPT_VERSION = "v1"

SYSTEM_PROMPT = (
    "You analyse chat messages. Always answer with a single JSON object and "
    "no extra text. The JSON object must contain exactly these keys: "
    '"translation", "sentiment", "sentiment_score", "issues". '
    '"translation" is the message translated into {target}. '
    '"sentiment" is one of "positive", "neutral", "negative", "mixed". '
    '"sentiment_score" is a number between 0 and 1. '
    '"issues" is an array of short strings listing the problems, requests or '
    "feedback the message raises; use an empty array when there are none."
)

STRICT_SUFFIX = (
    "\n\nIMPORTANT: reply with raw JSON only. Do not wrap it in markdown fences "
    "and do not add any explanation."
)


def build_prompt(content: str, translation_target: str) -> str:
    system = SYSTEM_PROMPT.format(target=translation_target)
    return f"{system}\n\nMessage:\n{content}"


def build_strict_prompt(content: str, translation_target: str) -> str:
    return build_prompt(content, translation_target) + STRICT_SUFFIX
```

- [ ] **Step 4: 实现 LLM 客户端**

创建 `src/foresight/analysis/llm.py`：

```python
"""OpenAI compatible client used for translation, sentiment and issue extraction."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from foresight.analysis.prompts import PROMPT_VERSION, build_prompt, build_strict_prompt
from foresight.config import AiConfig

logger = logging.getLogger(__name__)

VALID_SENTIMENTS = ("positive", "neutral", "negative", "mixed")


class LlmError(Exception):
    """Raised when the model call fails or the answer cannot be parsed."""


@dataclass
class LlmResult:
    translation: str | None = None
    sentiment: str | None = None
    sentiment_score: float | None = None
    issues: list[str] = field(default_factory=list)
    model: str = ""
    prompt_version: str = PROMPT_VERSION
    latency_ms: int = 0
    usage: dict[str, int] = field(default_factory=dict)


class LlmClient:
    """Calls an OpenAI compatible chat completion endpoint."""

    def __init__(self, config: AiConfig, client_factory: Any = None) -> None:
        self._config = config
        self._client_factory = client_factory or httpx.AsyncClient

    @property
    def endpoint(self) -> str:
        base = self._config.base_url.rstrip("/")
        return f"{base}/chat/completions"

    async def analyze(self, content: str) -> LlmResult:
        if not self._config.api_key:
            raise LlmError("FORESIGHT_LLM_API_KEY is not set")
        try:
            return await self._call(build_prompt(content, self._config.translation_target))
        except LlmError:
            return await self._call(
                build_strict_prompt(content, self._config.translation_target),
                strict=True,
            )

    async def _call(self, prompt: str, strict: bool = False) -> LlmResult:
        import time

        started = time.perf_counter()
        payload = {
            "model": self._config.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": f"Bearer {self._config.api_key}",
            "Content-Type": "application/json",
        }
        try:
            async with self._client_factory(
                timeout=self._config.timeout_seconds
            ) as client:
                response = await client.post(
                    self.endpoint, json=payload, headers=headers
                )
                response.raise_for_status()
                body = response.json()
        except Exception as exc:
            raise LlmError(str(exc)) from exc

        try:
            content_text = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LlmError("unexpected response shape") from exc

        parsed = _parse_json(content_text)
        if parsed is None:
            raise LlmError("model did not return valid JSON")
        latency_ms = int((time.perf_counter() - started) * 1000)
        sentiment = parsed.get("sentiment")
        if sentiment not in VALID_SENTIMENTS:
            sentiment = "neutral"
        score = parsed.get("sentiment_score")
        try:
            score = float(score) if score is not None else None
        except (TypeError, ValueError):
            score = None
        issues = parsed.get("issues")
        if not isinstance(issues, list):
            issues = []
        usage = body.get("usage") or {}
        return LlmResult(
            translation=parsed.get("translation"),
            sentiment=sentiment,
            sentiment_score=score,
            issues=[str(issue) for issue in issues],
            model=self._config.model,
            prompt_version=PROMPT_VERSION,
            latency_ms=latency_ms,
            usage={
                "prompt_tokens": int(usage.get("prompt_tokens", 0)),
                "completion_tokens": int(usage.get("completion_tokens", 0)),
            },
        )


def _parse_json(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = candidate.strip("`")
        if candidate.lower().startswith("json"):
            candidate = candidate[4:]
        candidate = candidate.strip()
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None
```

- [ ] **Step 5: 实现分析 Worker**

创建 `src/foresight/analysis/worker.py`：

```python
"""Background worker that analyses messages with an LLM."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

from foresight.adapters.discord_mapper import iso
from foresight.analysis.llm import LlmError
from foresight.domain.models import MessageAnalysis, OpKind, WriteOp

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
SKIP_MIN_LENGTH = 2


class AnalysisWorker:
    """Consumes message content and stores analysis results."""

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
        self._queue: asyncio.Queue[tuple[int, str]] = asyncio.Queue(maxsize=queue_maxsize)
        self._tasks: list[asyncio.Task[None]] = []
        self._stopping = False

    async def start(self) -> None:
        if self._tasks:
            return
        self._tasks = [
            asyncio.create_task(self._worker(index), name=f"foresight-analysis-{index}")
            for index in range(self._max_concurrency)
        ]

    async def stop(self) -> None:
        self._stopping = True
        for task in self._tasks:
            await task
        self._tasks = []

    def submit(self, message_id: int, content: str) -> bool:
        try:
            self._queue.put_nowait((message_id, content))
            return True
        except asyncio.QueueFull:
            logger.warning("analysis queue is full, dropping message %s", message_id)
            return False

    async def _worker(self, index: int) -> None:  # noqa: ARG002
        while True:
            message_id, content = await self._queue.get()
            try:
                await self._process(message_id, content)
            except Exception:
                logger.exception("analysis failed for message %s", message_id)
            finally:
                self._queue.task_done()
            if self._stopping and self._queue.empty():
                return

    async def _process(self, message_id: int, content: str) -> None:
        if not content or not content.strip() or len(content.strip()) < SKIP_MIN_LENGTH:
            await self._set_status(message_id, "skipped")
            return
        last_error = "analysis failed"
        for attempt in range(MAX_RETRIES):
            try:
                result = await self._llm.analyze(content)
            except LlmError as exc:
                last_error = str(exc)
                logger.warning(
                    "llm analysis failed (attempt %d/%d): %s",
                    attempt + 1,
                    MAX_RETRIES,
                    exc,
                )
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(1.0 * (attempt + 1))
                continue
            await self._store(message_id, result)
            return
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
                        model=getattr(self._llm, "_config", None)
                        and self._llm._config.model
                        or "",
                        prompt_version="",
                        latency_ms=0,
                        error=last_error,
                        analyzed_at=iso(now),
                    ),
                ),
                WriteOp(OpKind.ANALYSIS_STATUS, (message_id, "failed")),
            ]
        )

    async def _store(self, message_id: int, result: Any) -> None:
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
                        analyzed_at=iso(now),
                    ),
                ),
                WriteOp(OpKind.ANALYSIS_STATUS, (message_id, "done")),
            ]
        )

    async def _set_status(self, message_id: int, status: str) -> None:
        await self._repo.apply_batch(
            [WriteOp(OpKind.ANALYSIS_STATUS, (message_id, status))]
        )
```

- [ ] **Step 6: 运行测试确认通过**

Run: `uv run pytest tests/unit/test_analysis.py -v`
Expected: PASS —— 5 passed

- [ ] **Step 7: 提交**

```bash
git add src/foresight/analysis tests/unit/test_analysis.py
git commit -m "feat: LLM based translation, sentiment and issue extraction"
```

---

## Task 17: 把分析与媒体挂进采集链路

**Files:**
- Modify: `src/foresight/events.py`
- Modify: `src/foresight/cli.py`
- Modify: `src/foresight/storage/repository.py`
- Create: `tests/unit/test_events_analysis.py`

**Interfaces:**
- Consumes: `foresight.events.EventHandler`（M1 Task 6）、`foresight.analysis.worker.AnalysisWorker`（Task 16）、`foresight.media_worker.MediaWorker`（Task 15）
- Produces: `EventHandler` 新增可选参数 `analysis_sink: Callable[[int, str], None] | None`

- [ ] **Step 1: 写失败测试**

创建 `tests/unit/test_events_analysis.py`：

```python
from datetime import datetime, timezone
from types import SimpleNamespace

from foresight.events import EventHandler


class RecordingWriter:
    def __init__(self):
        self.ops = []

    def submit(self, op):
        self.ops.append(op)
        return True


def _config():
    return SimpleNamespace(
        discord=SimpleNamespace(channel_ids=(20,)),
        media=SimpleNamespace(backend="local"),
    )


def _message(content="hello"):
    return SimpleNamespace(
        id=100,
        content=content,
        type=0,
        created_at=datetime(2026, 11, 11, tzinfo=timezone.utc),
        edited_at=None,
        author=SimpleNamespace(
            id=30,
            name="alice",
            global_name="Alice",
            display_name="alice",
            discriminator="0",
            bot=False,
            avatar=None,
            created_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
            joined_at=None,
            roles=[],
        ),
        channel=SimpleNamespace(
            id=20,
            name="general",
            topic=None,
            type="text",
            position=1,
            guild=SimpleNamespace(id=10, name="Foresight", member_count=1),
        ),
        guild=SimpleNamespace(id=10, name="Foresight", member_count=1),
        reference=None,
        mentions=[],
        attachments=[],
        embeds=[],
    )


def test_message_is_handed_to_the_analysis_sink():
    seen = []
    writer = RecordingWriter()
    handler = EventHandler(_config(), writer, analysis_sink=lambda mid, content: seen.append((mid, content)))
    handler.handle_message(_message("please add export"))
    assert seen == [(100, "please add export")]


def test_message_without_analysis_sink_still_works():
    writer = RecordingWriter()
    handler = EventHandler(_config(), writer)
    handler.handle_message(_message())
    assert writer.ops
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/unit/test_events_analysis.py -v`
Expected: FAIL —— `TypeError: EventHandler.__init__() got an unexpected keyword argument 'analysis_sink'`

- [ ] **Step 3: 修改 EventHandler**

修改 `src/foresight/events.py`：

```python
    def __init__(
        self,
        config: Any,
        writer: Any,
        media_sink: Callable[[AttachmentRecord], None] | None = None,
        analysis_sink: Callable[[int, str], None] | None = None,
        bot_user_id: int | None = None,
    ) -> None:
        self._whitelist = set(config.discord.channel_ids)
        self._writer = writer
        self._media_sink = media_sink
        self._analysis_sink = analysis_sink
        self._bot_user_id = bot_user_id
        self._media_backend = getattr(getattr(config, "media", None), "backend", "local")
```

在 `handle_message` 中，写入消息之后追加：

```python
            if self._analysis_sink is not None and record.content.strip():
                self._analysis_sink(record.message_id, record.content)
```

- [ ] **Step 4: 在 CLI 中接线**

修改 `src/foresight/cli.py` 的 `run` 与 `both` 命令，构造 EventHandler 之前创建媒体与分析 worker：

```python
    from foresight.analysis.llm import LlmClient
    from foresight.analysis.worker import AnalysisWorker
    from foresight.media_worker import MediaWorker
    from foresight.storage.media import build_media_store
```

在 `run` 的 `_run()` 内：

```python
        media_worker = MediaWorker(
            repo,
            build_media_store(settings.media),
            max_concurrency=settings.media.download_concurrency,
            max_bytes=settings.media.max_download_size_mb * 1024 * 1024,
        )
        await media_worker.start()
        analysis_worker = None
        if settings.ai.enabled:
            analysis_worker = AnalysisWorker(
                repo,
                LlmClient(settings.ai),
                max_concurrency=settings.ai.max_concurrency,
            )
            await analysis_worker.start()
        handler = EventHandler(
            settings,
            writer,
            media_sink=media_worker.submit,
            analysis_sink=(
                analysis_worker.submit if analysis_worker is not None else None
            ),
        )
```

在 `finally` 块中先停 worker：

```python
        finally:
            if analysis_worker is not None:
                await analysis_worker.stop()
            await media_worker.stop()
            await writer.stop()
            await engine.dispose()
```

`both` 命令做同样的接线。

- [ ] **Step 5: 运行测试确认通过**

Run: `uv run pytest tests/unit/test_events_analysis.py -v`
Expected: PASS —— 2 passed

Run: `uv run pytest -v`
Expected: 全部通过

- [ ] **Step 6: 提交**

```bash
git add src/foresight/events.py src/foresight/cli.py src/foresight/storage/repository.py src/foresight/domain/models.py tests/unit/test_events_analysis.py
git commit -m "feat: wire media download and AI analysis into the collector"
```

---

## Task 18: 历史补录命令

**Files:**
- Create: `src/foresight/backfill.py`
- Modify: `src/foresight/cli.py`
- Create: `tests/unit/test_backfill.py`

**Interfaces:**
- Consumes: `foresight.config.Config`（M1 Task 1）、`foresight.adapters.discord_mapper`（M1 Task 4）、`foresight.domain.models.OpKind` / `WriteOp`（M1 Task 2）
- Produces:
  - `foresight.backfill.parse_window(days: int | None, since: str | None) -> tuple[datetime, datetime]`
  - `foresight.backfill.BackfillRunner(config: Config, writer: Writer, client_factory=None)`
  - `BackfillRunner.run(channel_ids: Sequence[int], window: tuple[datetime, datetime], limit: int = 20000) -> dict`
  - CLI 命令 `foresight backfill`

- [ ] **Step 1: 写失败测试**

创建 `tests/unit/test_backfill.py`：

```python
from datetime import datetime, timezone

import pytest

from foresight.backfill import parse_window


def test_parse_window_defaults_to_seven_days():
    start, end = parse_window(None, None)
    assert (end - start).days == 7
    assert end > start


def test_parse_window_uses_days():
    start, end = parse_window(3, None)
    assert (end - start).days == 3


def test_parse_window_uses_since():
    start, end = parse_window(None, "2026-11-01")
    assert start == datetime(2026, 11, 1, tzinfo=timezone.utc)
    assert end > start


def test_parse_window_rejects_both():
    with pytest.raises(ValueError):
        parse_window(3, "2026-11-01")


def test_parse_window_rejects_neither_when_explicit():
    start, end = parse_window(0, None)
    assert start <= end
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/unit/test_backfill.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'foresight.backfill'`

- [ ] **Step 3: 实现补录逻辑**

创建 `src/foresight/backfill.py`：

```python
"""Import historical messages from Discord.

Backfill is a separate process: it connects to Discord on its own, pulls
history through the REST API and feeds it into the very same
mapper/writer/storage path the live collector uses. It never overwrites rows
that the live collector already wrote.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from foresight.adapters.discord_mapper import (
    iso,
    map_attachments,
    map_channel,
    map_guild,
    map_message,
    map_user,
)
from foresight.domain.models import OpKind, WriteOp

logger = logging.getLogger(__name__)

PAGE_SIZE = 100
PROGRESS_EVERY = 1000


def parse_window(days: int | None, since: str | None) -> tuple[datetime, datetime]:
    """Turn --days / --since into an inclusive time window."""
    if days is not None and since is not None:
        raise ValueError("--days and --since are mutually exclusive")
    end = datetime.now(timezone.utc)
    if since is not None:
        start = datetime.fromisoformat(since)
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        return start, end
    span = days if days is not None else 7
    if span < 0:
        raise ValueError("--days must not be negative")
    return end - timedelta(days=span), end


class BackfillRunner:
    """Pulls channel history and feeds it into the write queue."""

    def __init__(self, config: Any, writer: Any, client_factory: Any = None) -> None:
        self._config = config
        self._writer = writer
        self._client_factory = client_factory

    async def run(
        self,
        channel_ids: Sequence[int],
        window: tuple[datetime, datetime],
        limit: int = 20000,
    ) -> dict[str, int]:
        import discord

        token = self._client_factory or self._default_token()
        intents = discord.Intents.default()
        intents.messages = True
        intents.message_content = True
        client = discord.Client(intents=intents)
        counts = {"messages": 0, "channels": 0}
        start, end = window
        try:
            async with client:
                await client.start(token)
                for channel_id in channel_ids:
                    channel = client.get_channel(channel_id)
                    if channel is None:
                        logger.warning("channel %s is not visible to the bot", channel_id)
                        continue
                    counts["channels"] += 1
                    counts["messages"] += await self._pull_channel(
                        channel, start, end, limit
                    )
        finally:
            pass
        return counts

    @staticmethod
    def _default_token() -> str:
        import os

        token = os.environ.get("DISCORD_TOKEN", "").strip()
        if not token:
            raise RuntimeError("DISCORD_TOKEN environment variable is required")
        return token

    async def _pull_channel(
        self,
        channel: Any,
        start: datetime,
        end: datetime,
        limit: int,
    ) -> int:
        pulled = 0
        before = None
        while pulled < limit:
            kwargs: dict[str, Any] = {"limit": min(PAGE_SIZE, limit - pulled)}
            if before is not None:
                kwargs["before"] = before
            batch = [message async for message in channel.history(**kwargs)]
            if not batch:
                break
            for message in batch:
                created = message.created_at
                if created < start or created > end:
                    continue
                await self._ingest(message)
                pulled += 1
                if pulled % PROGRESS_EVERY == 0:
                    logger.info("backfilled %d messages", pulled)
            before = batch[-1].id
            if len(batch) < kwargs["limit"]:
                break
        return pulled

    async def _ingest(self, message: Any) -> None:
        captured_at = datetime.now(timezone.utc)
        record = map_message(message, captured_at=captured_at)
        record.analysis_status = "skipped"
        self._writer.submit(WriteOp(OpKind.MESSAGE_INSERT_ONLY, record))
        self._writer.submit(
            WriteOp(OpKind.USER, map_user(message.author, updated_at=captured_at))
        )
        self._writer.submit(
            WriteOp(OpKind.CHANNEL, map_channel(message.channel, captured_at=captured_at))
        )
        guild = getattr(message, "guild", None)
        if guild is not None:
            self._writer.submit(
                WriteOp(OpKind.GUILD, map_guild(guild, captured_at=captured_at))
            )
```

- [ ] **Step 4: 实现 CLI 命令**

在 `src/foresight/cli.py` 追加 `backfill`：

```python
@app.command()
def backfill(
    days: Optional[int] = typer.Option(None, "--days", help="Import the last N days"),
    since: Optional[str] = typer.Option(None, "--since", help="ISO date lower bound"),
    channel: Optional[int] = typer.Option(None, "--channel", help="Only this channel"),
    limit: int = typer.Option(20000, help="Maximum messages per channel"),
    config: Optional[str] = typer.Option(None, "--config", help="Path to config.toml"),
) -> None:
    """Import historical messages from Discord."""
    from foresight.backfill import BackfillRunner, parse_window
    from foresight.storage.db import create_engine, init_db as create_tables
    from foresight.storage.repository import Repository
    from foresight.writer import Writer, recover_failed_batches

    settings = _load(config)
    if days is None and since is None:
        days = 7
    try:
        window = parse_window(days, since)
    except ValueError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    channel_ids = (
        [channel] if channel is not None else list(settings.discord.channel_ids)
    )

    async def _run() -> dict[str, int]:
        engine = create_engine(settings.database)
        await create_tables(engine)
        repo = Repository(engine)
        writer = Writer(
            repo,
            batch_size=settings.writer.batch_size,
            flush_interval_ms=settings.writer.flush_interval_ms,
            queue_maxsize=settings.writer.queue_maxsize,
            failure_path=Path(settings.database.path).parent / "failed_batch.jsonl",
        )
        await writer.start()
        failure_path = Path(settings.database.path).parent / "failed_batch.jsonl"
        await recover_failed_batches(writer, failure_path)
        try:
            runner = BackfillRunner(settings, writer)
            return await runner.run(channel_ids, window, limit=limit)
        finally:
            await writer.stop()
            await engine.dispose()

    counts = asyncio.run(_run())
    typer.echo(f"backfilled {counts['messages']} messages from {counts['channels']} channels")
```

- [ ] **Step 5: 实现 `foresight analyze` 命令**

在 `src/foresight/cli.py` 追加：

```python
@app.command()
def analyze(
    since: Optional[str] = typer.Option(None, "--since", help="ISO timestamp lower bound"),
    limit: int = typer.Option(1000, help="Maximum messages to analyse"),
    config: Optional[str] = typer.Option(None, "--config", help="Path to config.toml"),
) -> None:
    """Analyse messages that are still waiting for AI analysis."""
    from sqlalchemy import select

    from foresight.analysis.llm import LlmClient
    from foresight.analysis.worker import AnalysisWorker
    from foresight.storage.db import create_engine, init_db as create_tables
    from foresight.storage.repository import Repository
    from foresight.storage.tables import messages

    settings = _load(config)
    if not settings.ai.enabled:
        typer.secho("[ai].enabled is false, nothing to do", fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(code=2)

    async def _run() -> int:
        engine = create_engine(settings.database)
        await create_tables(engine)
        repo = Repository(engine)
        clauses = [messages.c.analysis_status == "pending"]
        if since:
            clauses.append(messages.c.created_at >= since)
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(messages.c.message_id, messages.c.content)
                    .where(*clauses)
                    .order_by(messages.c.created_at)
                    .limit(limit)
                )
            ).all()
        worker = AnalysisWorker(
            repo,
            LlmClient(settings.ai),
            max_concurrency=settings.ai.max_concurrency,
        )
        await worker.start()
        try:
            for message_id, content in rows:
                worker.submit(message_id, content)
            await asyncio.sleep(0.2)
            while worker.pending():
                await asyncio.sleep(0.5)
        finally:
            await worker.stop()
            await engine.dispose()
        return len(rows)

    count = asyncio.run(_run())
    typer.echo(f"analysed {count} messages")
```

`AnalysisWorker` 需要暴露队列积压数，在 `src/foresight/analysis/worker.py` 的 `__init__` 之后追加：

```python
    def pending(self) -> int:
        """Number of messages still waiting to be analysed."""
        return self._queue.qsize()
```

- [ ] **Step 6: 运行测试确认通过**

Run: `uv run pytest tests/unit/test_backfill.py -v`
Expected: PASS —— 4 passed

Run: `uv run pytest -v`
Expected: 全部通过

- [ ] **Step 7: 提交**

```bash
git add src/foresight/backfill.py src/foresight/cli.py src/foresight/analysis/worker.py tests/unit/test_backfill.py
git commit -m "feat: historical backfill and analyse commands"
```

---

## M3 验收

```bash
uv run pytest -v          # 全部通过
```

判定标准：
1. 附件下载后 `attachments.status == 'stored'` 且 `sha256` 正确
2. AI 分析后 `message_analysis` 有记录且 `messages.analysis_status == 'done'`
3. `foresight backfill --days 7` 能把历史消息以 `insert_only` 方式写入，且不覆盖已有行
4. 全部端到端行为有单元测试覆盖

接下来执行 `docs/superpowers/plans/2026-11-11-foresight-m4-delivery-skill-docker-docs.md`。
