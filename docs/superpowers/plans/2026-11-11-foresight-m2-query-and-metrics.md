# ForeSight M2 — 查询 API 与指标 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 M1 采集内核之上，提供 HTTP API（资源端点 + 统计端点 + 只读 SQL 端点）、Prometheus 指标，以及 `serve` / `both` / `query` / `stats` 命令。

**Architecture:** FastAPI 应用只读连接数据库（SQLite 用 `file:...?mode=ro`），鉴权走 Bearer token。统计逻辑集中在 `storage/stats.py`，用 SQLAlchemy Core 表达式构建，方言差异收敛到少数几个 helper。自由 SQL 端点由 `QueryRunner` 抽象承载：SQLite 走标准库 `sqlite3` + `set_authorizer` 引擎层只读授权，PG/MySQL 走只读事务。

**Tech Stack:** FastAPI、uvicorn、SQLAlchemy 2.0 Core、prometheus-client、typer、pytest + pytest-asyncio。

**前置条件：** M1（`2026-11-11-foresight-m1-capture-core.md`）已完成。

## Global Constraints

- M1 的全部 Global Constraints 继续生效
- API 默认监听 `127.0.0.1:8000`；**绑定到非本机地址且未设置 `FORESIGHT_API_TOKEN` 时拒绝启动**
- API 使用**只读**数据库连接，从机制上保证不写库
- 分页信封固定为 `{"total", "limit", "offset", "has_more", "items"}`；`limit` 默认 50、上限 500
- 时间过滤用**字符串比较**（时间戳为定宽 UTC ISO-8601，字典序 = 时间序）
- 日期分桶用 `substr(created_at, 1, 10)`（天）/ `substr(created_at, 1, 13)`（小时）—— 纯字符串截取，三方言一致，避免方言特有的日期函数
- JSON 字段一律 TEXT
- `q` 过滤为**大小写不敏感**包含匹配，实现为 `lower(content) LIKE lower(:q)`
- `order` 按 `created_at` 排序，同值以 `message_id` 为次级键，保证分页稳定
- 指标标签**绝不使用 `user_id`**（高基数会打爆 Prometheus）
- API 错误码：401 认证失败 / 400 SQL 非法或非只读 / 422 参数错误 / 500 数据库错误

---

## Task 8: 统计服务

**Files:**
- Create: `src/foresight/storage/stats.py`
- Create: `tests/unit/test_stats.py`

**Interfaces:**
- Consumes: `foresight.storage.tables`（M1 Task 2）、`foresight.storage.db.init_db`（M1 Task 3）
- Produces:
  - `foresight.storage.stats.StatsService(engine: AsyncEngine)`
  - `StatsService.overview(since=None, until=None, channel_id=None) -> dict`
  - `StatsService.top_talkers(since=None, until=None, channel_id=None, limit=20) -> list[dict]`
  - `StatsService.activity(since=None, until=None, bucket="day", channel_id=None) -> list[dict]`
  - `StatsService.topics(since=None, until=None, channel_id=None, limit=20) -> list[dict]`
  - `foresight.storage.stats.bucket_expr(column, bucket: str) -> ColumnElement`（供 API 层复用）

- [ ] **Step 1: 写失败测试**

创建 `tests/unit/test_stats.py`：

```python
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from foresight.domain.models import MessageRecord, OpKind, WriteOp
from foresight.storage.db import init_db
from foresight.storage.repository import Repository
from foresight.storage.stats import StatsService


async def _make_stats(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    await init_db(engine)
    repo = Repository(engine)
    records = []
    for index in range(5):
        records.append(
            MessageRecord(
                message_id=index + 1,
                guild_id=10,
                channel_id=20 if index < 3 else 21,
                author_id=30 if index % 2 == 0 else 31,
                content=f"hello {index}",
                message_type=0,
                created_at=f"2026-11-1{index + 1}T10:00:00+00:00",
                captured_at="2026-11-11T10:00:00+00:00",
                char_count=7,
                word_count=2,
            )
        )
    deleted = records[0]
    deleted.deleted_at = "2026-11-11T12:00:00+00:00"
    await repo.apply_batch([WriteOp(OpKind.MESSAGE, rec) for rec in records])
    return StatsService(engine), engine


async def test_overview_counts(tmp_path):
    stats, _ = await _make_stats(tmp_path)
    result = await stats.overview()
    assert result["total_messages"] == 5
    assert result["unique_authors"] == 2
    assert result["deleted_messages"] == 1
    assert result["edited_messages"] == 0
    assert result["avg_chars"] == 7


async def test_overview_respects_since(tmp_path):
    stats, _ = await _make_stats(tmp_path)
    result = await stats.overview(since="2026-11-13T00:00:00+00:00")
    assert result["total_messages"] == 3


async def test_top_talkers(tmp_path):
    stats, _ = await _make_stats(tmp_path)
    result = await stats.top_talkers(limit=10)
    assert [row["author_id"] for row in result] == [30, 31]
    assert result[0]["message_count"] == 3


async def test_activity_buckets_by_day(tmp_path):
    stats, _ = await _make_stats(tmp_path)
    result = await stats.activity(bucket="day")
    assert [row["bucket"] for row in result] == [
        "2026-11-11",
        "2026-11-12",
        "2026-11-13",
        "2026-11-14",
        "2026-11-15",
    ]
    assert result[0]["message_count"] == 1


async def test_activity_buckets_by_hour(tmp_path):
    stats, _ = await _make_stats(tmp_path)
    result = await stats.activity(bucket="hour")
    assert result[0]["bucket"] == "2026-11-11T10"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/unit/test_stats.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'foresight.storage.stats'`

- [ ] **Step 3: 实现 stats.py**

创建 `src/foresight/storage/stats.py`：

```python
"""Read-only analytics queries.

Timestamps are stored as fixed-width UTC ISO-8601 strings, so string
comparison equals chronological comparison and `substr` gives us portable
date bucketing without dialect specific date functions.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any, Sequence

from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from foresight.storage.tables import message_analysis, messages, users


def bucket_expr(column: Any, bucket: str) -> Any:
    """Return a portable expression that truncates an ISO timestamp."""
    if bucket == "hour":
        return func.substr(column, 1, 13)
    return func.substr(column, 1, 10)


def _ranked(counter: Counter[str], limit: int) -> list[dict[str, Any]]:
    """Rank topics by count, then by name, so every backend agrees."""
    if limit <= 0:
        return []
    ordered = sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    return [{"topic": topic, "count": count} for topic, count in ordered[:limit]]


class StatsService:
    """Aggregate queries used by both the API and the CLI."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    def _filter(
        self,
        since: str | None,
        until: str | None,
        channel_id: int | None,
    ) -> list[Any]:
        clauses: list[Any] = []
        if since:
            clauses.append(messages.c.created_at >= since)
        if until:
            clauses.append(messages.c.created_at <= until)
        if channel_id is not None:
            clauses.append(messages.c.channel_id == channel_id)
        return clauses

    async def overview(
        self,
        since: str | None = None,
        until: str | None = None,
        channel_id: int | None = None,
    ) -> dict[str, Any]:
        clauses = self._filter(since, until, channel_id)
        total = select(func.count()).select_from(messages).where(*clauses)
        authors = select(func.count(distinct(messages.c.author_id))).where(*clauses)
        deleted = select(func.count()).select_from(messages).where(
            *clauses, messages.c.deleted_at.is_not(None)
        )
        edited = select(func.count()).select_from(messages).where(
            *clauses, messages.c.edited_at.is_not(None)
        )
        avg_chars = select(func.avg(messages.c.char_count)).where(*clauses)
        async with self._engine.connect() as conn:
            total_count = int((await conn.execute(total)).scalar_one())
            unique_authors = int((await conn.execute(authors)).scalar_one())
            deleted_count = int((await conn.execute(deleted)).scalar_one())
            edited_count = int((await conn.execute(edited)).scalar_one())
            average = (await conn.execute(avg_chars)).scalar_one()
        return {
            "total_messages": total_count,
            "unique_authors": unique_authors,
            "deleted_messages": deleted_count,
            "edited_messages": edited_count,
            "avg_chars": round(float(average or 0), 2),
        }

    async def top_talkers(
        self,
        since: str | None = None,
        until: str | None = None,
        channel_id: int | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        clauses = self._filter(since, until, channel_id)
        stmt = (
            select(
                messages.c.author_id,
                func.count().label("message_count"),
                func.sum(messages.c.char_count).label("total_chars"),
            )
            .where(*clauses)
            .group_by(messages.c.author_id)
            .order_by(func.count().desc(), messages.c.author_id)
            .limit(limit)
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        names = await self._usernames([row.author_id for row in rows])
        return [
            {
                "author_id": row.author_id,
                "username": names.get(row.author_id),
                "message_count": int(row.message_count),
                "total_chars": int(row.total_chars or 0),
            }
            for row in rows
        ]

    async def _usernames(self, user_ids: Sequence[int]) -> dict[int, str | None]:
        if not user_ids:
            return {}
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(users.c.user_id, users.c.username).where(
                        users.c.user_id.in_(list(user_ids))
                    )
                )
            ).all()
        return {row.user_id: row.username for row in rows}

    async def activity(
        self,
        since: str | None = None,
        until: str | None = None,
        bucket: str = "day",
        channel_id: int | None = None,
    ) -> list[dict[str, Any]]:
        clauses = self._filter(since, until, channel_id)
        bucket_column = bucket_expr(messages.c.created_at, bucket)
        stmt = (
            select(bucket_column.label("bucket"), func.count().label("message_count"))
            .where(*clauses)
            .group_by(bucket_column)
            .order_by(bucket_column)
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        return [
            {"bucket": row.bucket, "message_count": int(row.message_count)} for row in rows
        ]

    async def topics(
        self,
        since: str | None = None,
        until: str | None = None,
        channel_id: int | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Aggregate extracted issues first, fall back to frequent words."""
        clauses = self._filter(since, until, channel_id)
        # 必须 join 回 messages：过滤条件引用的是 messages 的列，
        # 不 join 会渲染成 FROM message_analysis, messages 的笛卡尔积。
        stmt = (
            select(message_analysis.c.issues_json)
            .select_from(
                message_analysis.join(
                    messages, message_analysis.c.message_id == messages.c.message_id
                )
            )
            .where(*clauses, message_analysis.c.issues_json.is_not(None))
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        counter: Counter[str] = Counter()
        for row in rows:
            try:
                issues = json.loads(row.issues_json or "[]")
            except (json.JSONDecodeError, RecursionError):
                continue
            # issues_json 是 TEXT 列，数据库不约束其内容：
            # 解码结果不是列表就跳过这一行，不能让整个查询崩掉。
            if not isinstance(issues, list):
                continue
            for issue in issues:
                if isinstance(issue, str) and issue.strip():
                    counter[issue.strip()] += 1
        if counter:
            return _ranked(counter, limit)
        return await self._frequent_words(clauses, limit)

    async def _frequent_words(
        self, clauses: Sequence[Any], limit: int
    ) -> list[dict[str, Any]]:
        stmt = select(messages.c.content).where(*clauses)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        counter: Counter[str] = Counter()
        for row in rows:
            for token in (row.content or "").lower().split():
                token = token.strip()
                if len(token) > 2:
                    counter[token] += 1
        return _ranked(counter, limit)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/unit/test_stats.py -v`
Expected: PASS —— 5 passed

- [ ] **Step 5: 提交**

```bash
git add src/foresight/storage/stats.py tests/unit/test_stats.py
git commit -m "feat: analytics service with portable date bucketing"
```

---

## Task 9: API 骨架、鉴权与消息端点

**Files:**
- Create: `src/foresight/api/__init__.py`
- Create: `src/foresight/api/deps.py`
- Create: `src/foresight/api/schemas.py`
- Create: `src/foresight/api/app.py`
- Create: `src/foresight/api/routers/__init__.py`
- Create: `src/foresight/api/routers/health.py`
- Create: `src/foresight/api/routers/messages.py`
- Create: `tests/integration/test_api_messages.py`

**Interfaces:**
- Consumes: `foresight.config.Config`（M1 Task 1）、`foresight.storage.db.create_engine`（M1 Task 3）、`foresight.storage.stats.StatsService`（Task 8）
- Produces:
  - `foresight.api.app.create_app(config: Config, engine: AsyncEngine | None = None) -> FastAPI`
  - `foresight.api.deps.get_engine(request) -> AsyncEngine`
  - `foresight.api.deps.get_stats(request) -> StatsService`
  - `foresight.api.deps.require_token(request) -> None`
  - `foresight.api.schemas.Page[T]` 泛型分页信封

- [ ] **Step 1: 写失败测试**

创建 `tests/integration/__init__.py`（空文件）：

```python
```

创建 `tests/integration/conftest.py`：

```python
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine

from foresight.api.app import create_app
from foresight.domain.models import MessageRecord, OpKind, WriteOp
from foresight.storage.db import init_db
from foresight.storage.repository import Repository


@pytest.fixture
async def api_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """HTTP client for the API.

    The engine and the request must live in the SAME event loop, so the
    client uses httpx's ASGI transport instead of starlette's TestClient
    (TestClient runs the app in its own loop, which breaks the async
    engine with "attached to a different loop").
    """
    monkeypatch.setenv("FORESIGHT_API_TOKEN", "secret-token")

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    await init_db(engine)
    repo = Repository(engine)
    records = [
        MessageRecord(
            message_id=index + 1,
            guild_id=10,
            channel_id=20,
            author_id=30,
            content=f"message number {index}",
            message_type=0,
            created_at=f"2026-11-1{index + 1}T10:00:00+00:00",
            captured_at="2026-11-11T10:00:00+00:00",
            char_count=16,
            word_count=3,
        )
        for index in range(4)
    ]
    await repo.apply_batch([WriteOp(OpKind.MESSAGE, record) for record in records])

    config = SimpleNamespace(
        api=SimpleNamespace(host="127.0.0.1", port=8000, metrics_token=""),
        database=SimpleNamespace(
            backend="sqlite",
            path=str(tmp_path / "test.db"),
            sqlalchemy_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
            readonly_sqlalchemy_url=(
                f"sqlite+aiosqlite:///file:{tmp_path / 'test.db'}?mode=ro&uri=true"
            ),
        ),
        media=SimpleNamespace(backend="local"),
    )
    app = create_app(config, engine=engine)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client
    await engine.dispose()


@pytest.fixture
def auth():
    return {"Authorization": "Bearer secret-token"}
```


- [ ] **Step 2: 写失败测试（续）**

创建 `tests/integration/test_api_messages.py`：

```python
async def test_health_requires_token(api_client):
    response = await api_client.get("/api/health")
    assert response.status_code == 401


async def test_health_returns_counts(api_client, auth):
    response = await api_client.get("/api/health", headers=auth)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["total_messages"] == 4


async def test_messages_are_paginated(api_client, auth):
    response = await api_client.get("/api/messages", headers=auth, params={"limit": 2})
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 4
    assert body["limit"] == 2
    assert body["has_more"] is True
    assert len(body["items"]) == 2


async def test_messages_limit_is_capped(api_client, auth):
    response = await api_client.get("/api/messages", headers=auth, params={"limit": 5000})
    assert response.status_code == 200
    assert response.json()["limit"] == 500


async def test_messages_search_is_case_insensitive(api_client, auth):
    response = await api_client.get(
        "/api/messages", headers=auth, params={"q": "MESSAGE NUMBER 1"}
    )
    assert response.status_code == 200
    assert response.json()["total"] == 1


async def test_messages_filter_by_channel(api_client, auth):
    response = await api_client.get(
        "/api/messages", headers=auth, params={"channel_id": 999}
    )
    assert response.json()["total"] == 0


async def test_message_detail(api_client, auth):
    response = await api_client.get("/api/messages/2", headers=auth)
    assert response.status_code == 200
    body = response.json()
    assert body["message_id"] == 2
    assert body["analysis"] is None


async def test_message_detail_not_found(api_client, auth):
    response = await api_client.get("/api/messages/9999", headers=auth)
    assert response.status_code == 404
```

- [ ] **Step 3: 运行测试确认失败**

Run: `uv run pytest tests/integration/test_api_messages.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'foresight.api'`

- [ ] **Step 4: 实现鉴权与依赖**

创建 `src/foresight/api/__init__.py`（空文件）：

```python
```

创建 `src/foresight/api/routers/__init__.py`（空文件）：

```python
```

创建 `src/foresight/api/deps.py`：

```python
"""FastAPI dependencies: engine access, stats service, bearer auth."""

from __future__ import annotations

import os
import secrets

from fastapi import HTTPException, Request

from foresight.storage.stats import StatsService


def get_engine(request: Request):  # noqa: ANN201
    engine = getattr(request.app.state, "engine", None)
    if engine is None:
        raise HTTPException(status_code=500, detail="database engine is not configured")
    return engine


def get_stats(request: Request) -> StatsService:
    return StatsService(get_engine(request))


def require_token(request: Request) -> None:
    """Validate the bearer token when one is configured."""
    expected = os.environ.get("FORESIGHT_API_TOKEN", "").strip()
    if not expected:
        return
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    if not secrets.compare_digest(header[len("Bearer ") :], expected):
        raise HTTPException(status_code=401, detail="invalid bearer token")
```

创建 `src/foresight/api/schemas.py`：

```python
"""Response models."""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    total: int
    limit: int
    offset: int
    has_more: bool
    items: list[T]


class MessageOut(BaseModel):
    message_id: int
    guild_id: int | None
    channel_id: int
    author_id: int
    content: str
    message_type: int
    created_at: str
    captured_at: str
    edited_at: str | None
    edit_count: int
    deleted_at: str | None
    reply_to_message_id: int | None
    mentions_count: int
    char_count: int
    word_count: int
    attachment_count: int
    attachments_json: str | None
    embeds_json: str | None
    is_bot: bool
    analysis_status: str


class AnalysisOut(BaseModel):
    message_id: int
    translated_text: str | None
    sentiment: str | None
    sentiment_score: float | None
    issues_json: str | None
    model: str | None
    prompt_version: str | None
    latency_ms: int | None
    error: str | None
    analyzed_at: str | None


class MessageDetail(MessageOut):
    """A message plus its AI analysis, if one exists."""

    analysis: AnalysisOut | None = None


class HealthOut(BaseModel):
    status: str
    total_messages: int
    total_attachments: int
    db_size_mb: float
    last_captured_at: str | None
```

- [ ] **Step 5: 实现消息与健康端点**

创建 `src/foresight/api/routers/health.py`：

```python
"""Health endpoint."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends
from sqlalchemy import func, select

from foresight.api.deps import get_engine, require_token
from foresight.api.schemas import HealthOut
from foresight.storage.tables import attachments, messages

router = APIRouter()


@router.get("/health", response_model=HealthOut, dependencies=[Depends(require_token)])
async def health(engine=Depends(get_engine)) -> HealthOut:
    async with engine.connect() as conn:
        total = int((await conn.execute(select(func.count()).select_from(messages))).scalar_one())
        media = int(
            (await conn.execute(select(func.count()).select_from(attachments))).scalar_one()
        )
        last = (
            await conn.execute(select(messages.c.created_at).order_by(messages.c.created_at.desc()).limit(1))
        ).first()
    size_mb = 0.0
    path = getattr(engine.url, "database", None)
    if path:
        db_path = Path(str(path))
        if db_path.exists():
            size_mb = round(db_path.stat().st_size / (1024 * 1024), 3)
    return HealthOut(
        status="ok",
        total_messages=total,
        total_attachments=media,
        db_size_mb=size_mb,
        last_captured_at=last[0] if last else None,
    )
```

创建 `src/foresight/api/routers/messages.py`：

```python
"""Message read endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select

from foresight.api.deps import get_engine, require_token
from foresight.api.schemas import AnalysisOut, MessageDetail, MessageOut, Page
from foresight.storage.tables import message_analysis, messages

router = APIRouter()

MAX_LIMIT = 500
DEFAULT_LIMIT = 50


@router.get("/messages", response_model=Page[MessageOut], dependencies=[Depends(require_token)])
async def list_messages(
    channel_id: int | None = None,
    author_id: int | None = None,
    guild_id: int | None = None,
    since: str | None = None,
    until: str | None = None,
    q: str | None = None,
    min_len: int | None = None,
    has_attachment: bool | None = None,
    deleted: bool | None = None,
    edited: bool | None = None,
    order: str = Query("desc", pattern="^(asc|desc)$"),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
    engine=Depends(get_engine),
) -> Page[MessageOut]:
    clauses = []
    if channel_id is not None:
        clauses.append(messages.c.channel_id == channel_id)
    if author_id is not None:
        clauses.append(messages.c.author_id == author_id)
    if guild_id is not None:
        clauses.append(messages.c.guild_id == guild_id)
    if since:
        clauses.append(messages.c.created_at >= since)
    if until:
        clauses.append(messages.c.created_at <= until)
    if min_len is not None:
        clauses.append(messages.c.char_count >= min_len)
    if has_attachment is True:
        clauses.append(messages.c.attachment_count > 0)
    if has_attachment is False:
        clauses.append(messages.c.attachment_count == 0)
    if deleted is True:
        clauses.append(messages.c.deleted_at.is_not(None))
    if deleted is False:
        clauses.append(messages.c.deleted_at.is_(None))
    if edited is True:
        clauses.append(messages.c.edited_at.is_not(None))
    if edited is False:
        clauses.append(messages.c.edited_at.is_(None))
    if q:
        clauses.append(func.lower(messages.c.content).like(f"%{q.lower()}%"))

    total_stmt = select(func.count()).select_from(messages).where(*clauses)
    order_column = (
        messages.c.created_at.asc() if order == "asc" else messages.c.created_at.desc()
    )
    data_stmt = (
        select(messages)
        .where(*clauses)
        .order_by(order_column, messages.c.message_id.desc())
        .limit(limit)
        .offset(offset)
    )
    async with engine.connect() as conn:
        total = int((await conn.execute(total_stmt)).scalar_one())
        rows = (await conn.execute(data_stmt)).all()
    items = [MessageOut(**dict(row._mapping)) for row in rows]
    return Page[MessageOut](
        total=total,
        limit=limit,
        offset=offset,
        has_more=offset + len(items) < total,
        items=items,
    )


@router.get(
    "/messages/{message_id}",
    response_model=MessageDetail,
    dependencies=[Depends(require_token)],
)
async def get_message(message_id: int, engine=Depends(get_engine)) -> MessageDetail:
    async with engine.connect() as conn:
        row = (
            await conn.execute(select(messages).where(messages.c.message_id == message_id))
        ).first()
        analysis = (
            await conn.execute(
                select(message_analysis).where(
                    message_analysis.c.message_id == message_id
                )
            )
        ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="message not found")
    payload = dict(row._mapping)
    payload["analysis"] = (
        AnalysisOut(**dict(analysis._mapping)) if analysis is not None else None
    )
    return MessageDetail(**payload)
```

- [ ] **Step 6: 实现应用工厂**

创建 `src/foresight/api/app.py`：

```python
"""FastAPI application factory."""

from __future__ import annotations

from fastapi import FastAPI

from foresight.api.routers import health, messages
from foresight.config import Config
from foresight.storage.db import create_engine


def create_app(config: Config, engine=None) -> FastAPI:  # noqa: ANN001
    app = FastAPI(title="ForeSight API", version="0.1.0")
    if engine is None:
        engine = create_engine(config.database)
    app.state.engine = engine
    app.state.config = config
    app.include_router(health.router, prefix="/api", tags=["health"])
    app.include_router(messages.router, prefix="/api", tags=["messages"])
    return app
```

- [ ] **Step 7: 运行测试确认通过**

Run: `uv run pytest tests/integration/test_api_messages.py -v`
Expected: PASS —— 7 passed

- [ ] **Step 8: 提交**

```bash
git add src/foresight/api tests/integration
git commit -m "feat: HTTP API with bearer auth and message endpoints"
```

---

## Task 10: 用户、频道与表情端点

**Files:**
- Create: `src/foresight/api/routers/users.py`
- Create: `src/foresight/api/routers/channels.py`
- Create: `src/foresight/api/routers/reactions.py`
- Modify: `src/foresight/api/app.py`
- Modify: `src/foresight/api/schemas.py`
- Create: `tests/integration/test_api_resources.py`

**Interfaces:**
- Consumes: `foresight.api.deps`（Task 9）、`foresight.storage.tables`（M1 Task 2）
- Produces: `GET /api/users`、`GET /api/users/{user_id}`、`GET /api/channels`、`GET /api/reactions`

- [ ] **Step 1: 写失败测试**

创建 `tests/integration/test_api_resources.py`：

```python
async def test_users_endpoint_lists_speakers(api_client, auth):
    response = await api_client.get("/api/users", headers=auth)
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["author_id"] == 30
    assert body["items"][0]["message_count"] == 4


async def test_user_detail(api_client, auth):
    response = await api_client.get("/api/users/30", headers=auth)
    assert response.status_code == 200
    assert response.json()["author_id"] == 30


async def test_user_detail_not_found(api_client, auth):
    assert (await api_client.get("/api/users/999", headers=auth).status_code) == 404


async def test_channels_endpoint(api_client, auth):
    response = await api_client.get("/api/channels", headers=auth)
    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["channel_id"] == 20


async def test_reactions_endpoint_is_empty(api_client, auth):
    response = await api_client.get("/api/reactions", headers=auth)
    assert response.status_code == 200
    assert response.json()["total"] == 0
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/integration/test_api_resources.py -v`
Expected: FAIL —— 404（路由不存在）

- [ ] **Step 3: 实现端点**

在 `src/foresight/api/schemas.py` 追加：

```python
class UserOut(BaseModel):
    author_id: int
    username: str | None
    message_count: int
    first_message_at: str | None
    last_message_at: str | None
    avg_chars: float


class ChannelOut(BaseModel):
    channel_id: int
    guild_id: int | None
    name: str | None
    message_count: int
    last_message_at: str | None


class ReactionOut(BaseModel):
    id: int
    message_id: int
    user_id: int
    emoji: str
    is_custom: bool
    added_at: str
    removed_at: str | None
```

创建 `src/foresight/api/routers/users.py`：

```python
"""Speaker endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select

from foresight.api.deps import get_engine, require_token
from foresight.api.schemas import Page, UserOut
from foresight.storage.tables import messages, users

router = APIRouter()


@router.get("/users", response_model=Page[UserOut], dependencies=[Depends(require_token)])
async def list_users(
    channel_id: int | None = None,
    limit: int = 50,
    offset: int = 0,
    engine=Depends(get_engine),
) -> Page[UserOut]:
    clauses = []
    if channel_id is not None:
        clauses.append(messages.c.channel_id == channel_id)
    total_stmt = select(func.count()).select_from(messages).where(*clauses)
    stmt = (
        select(
            messages.c.author_id,
            func.count().label("message_count"),
            func.min(messages.c.created_at).label("first_message_at"),
            func.max(messages.c.created_at).label("last_message_at"),
            func.avg(messages.c.char_count).label("avg_chars"),
        )
        .where(*clauses)
        .group_by(messages.c.author_id)
        .order_by(func.count().desc())
        .limit(limit)
        .offset(offset)
    )
    async with engine.connect() as conn:
        total = int((await conn.execute(total_stmt)).scalar_one())
        rows = (await conn.execute(stmt)).all()
        names = {}
        if rows:
            ids = [row.author_id for row in rows]
            name_rows = (
                await conn.execute(
                    select(users.c.user_id, users.c.username).where(
                        users.c.user_id.in_(ids)
                    )
                )
            ).all()
            names = {row.user_id: row.username for row in name_rows}
    items = [
        UserOut(
            author_id=row.author_id,
            username=names.get(row.author_id),
            message_count=int(row.message_count),
            first_message_at=row.first_message_at,
            last_message_at=row.last_message_at,
            avg_chars=round(float(row.avg_chars or 0), 2),
        )
        for row in rows
    ]
    return Page[UserOut](
        total=total,
        limit=limit,
        offset=offset,
        has_more=offset + len(items) < total,
        items=items,
    )


@router.get(
    "/users/{author_id}", response_model=UserOut, dependencies=[Depends(require_token)]
)
async def get_user(author_id: int, engine=Depends(get_engine)) -> UserOut:
    stmt = (
        select(
            messages.c.author_id,
            func.count().label("message_count"),
            func.min(messages.c.created_at).label("first_message_at"),
            func.max(messages.c.created_at).label("last_message_at"),
            func.avg(messages.c.char_count).label("avg_chars"),
        )
        .where(messages.c.author_id == author_id)
        .group_by(messages.c.author_id)
    )
    async with engine.connect() as conn:
        row = (await conn.execute(stmt)).first()
        username = (
            await conn.execute(
                select(users.c.username).where(users.c.user_id == author_id)
            )
        ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="user not found")
    return UserOut(
        author_id=row.author_id,
        username=username[0] if username else None,
        message_count=int(row.message_count),
        first_message_at=row.first_message_at,
        last_message_at=row.last_message_at,
        avg_chars=round(float(row.avg_chars or 0), 2),
    )
```

创建 `src/foresight/api/routers/channels.py`：

```python
"""Channel endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select

from foresight.api.deps import get_engine, require_token
from foresight.api.schemas import ChannelOut, Page
from foresight.storage.tables import channels, messages

router = APIRouter()


@router.get("/channels", response_model=Page[ChannelOut], dependencies=[Depends(require_token)])
async def list_channels(
    guild_id: int | None = None,
    limit: int = 50,
    offset: int = 0,
    engine=Depends(get_engine),
) -> Page[ChannelOut]:
    clauses = []
    if guild_id is not None:
        clauses.append(messages.c.guild_id == guild_id)
    total_stmt = select(func.count()).select_from(messages).where(*clauses)
    stmt = (
        select(
            messages.c.channel_id,
            func.count().label("message_count"),
            func.max(messages.c.created_at).label("last_message_at"),
        )
        .where(*clauses)
        .group_by(messages.c.channel_id)
        .order_by(func.count().desc())
        .limit(limit)
        .offset(offset)
    )
    async with engine.connect() as conn:
        total = int((await conn.execute(total_stmt)).scalar_one())
        rows = (await conn.execute(stmt)).all()
        names = {}
        if rows:
            ids = [row.channel_id for row in rows]
            name_rows = (
                await conn.execute(
                    select(channels.c.channel_id, channels.c.name, channels.c.guild_id).where(
                        channels.c.channel_id.in_(ids)
                    )
                )
            ).all()
            names = {row.channel_id: row for row in name_rows}
    items = []
    for row in rows:
        meta = names.get(row.channel_id)
        items.append(
            ChannelOut(
                channel_id=row.channel_id,
                guild_id=meta.guild_id if meta else None,
                name=meta.name if meta else None,
                message_count=int(row.message_count),
                last_message_at=row.last_message_at,
            )
        )
    return Page[ChannelOut](
        total=total,
        limit=limit,
        offset=offset,
        has_more=offset + len(items) < total,
        items=items,
    )
```

创建 `src/foresight/api/routers/reactions.py`：

```python
"""Reaction endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select

from foresight.api.deps import get_engine, require_token
from foresight.api.schemas import Page, ReactionOut
from foresight.storage.tables import reactions

router = APIRouter()


@router.get("/reactions", response_model=Page[ReactionOut], dependencies=[Depends(require_token)])
async def list_reactions(
    message_id: int | None = None,
    only_active: bool = False,
    limit: int = 50,
    offset: int = 0,
    engine=Depends(get_engine),
) -> Page[ReactionOut]:
    clauses = []
    if message_id is not None:
        clauses.append(reactions.c.message_id == message_id)
    if only_active:
        clauses.append(reactions.c.removed_at.is_(None))
    total_stmt = select(func.count()).select_from(reactions).where(*clauses)
    stmt = (
        select(reactions)
        .where(*clauses)
        .order_by(reactions.c.added_at.desc())
        .limit(limit)
        .offset(offset)
    )
    async with engine.connect() as conn:
        total = int((await conn.execute(total_stmt)).scalar_one())
        rows = (await conn.execute(stmt)).all()
    items = [ReactionOut(**dict(row._mapping)) for row in rows]
    return Page[ReactionOut](
        total=total,
        limit=limit,
        offset=offset,
        has_more=offset + len(items) < total,
        items=items,
    )
```

修改 `src/foresight/api/app.py`，在 `include_router` 处补上：

```python
    app.include_router(users.router, prefix="/api", tags=["users"])
    app.include_router(channels.router, prefix="/api", tags=["channels"])
    app.include_router(reactions.router, prefix="/api", tags=["reactions"])
```

并在文件顶部导入：

```python
from foresight.api.routers import channels, health, messages, reactions, users
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/integration/test_api_resources.py -v`
Expected: PASS —— 5 passed

- [ ] **Step 5: 提交**

```bash
git add src/foresight/api tests/integration/test_api_resources.py
git commit -m "feat: user, channel and reaction endpoints"
```

---

## Task 11: 统计端点

**Files:**
- Create: `src/foresight/api/routers/stats.py`
- Modify: `src/foresight/api/app.py`
- Create: `tests/integration/test_api_stats.py`

**Interfaces:**
- Consumes: `foresight.storage.stats.StatsService`（Task 8）
- Produces: `GET /api/stats/overview`、`GET /api/stats/top-talkers`、`GET /api/stats/activity`、`GET /api/stats/topics`

- [ ] **Step 1: 写失败测试**

创建 `tests/integration/test_api_stats.py`：

```python
async def test_stats_overview(api_client, auth):
    response = await api_client.get("/api/stats/overview", headers=auth)
    assert response.status_code == 200
    body = response.json()
    assert body["total_messages"] == 4
    assert body["unique_authors"] == 1


async def test_stats_top_talkers(api_client, auth):
    response = await api_client.get("/api/stats/top-talkers", headers=auth)
    assert response.status_code == 200
    assert response.json()["items"][0]["author_id"] == 30


async def test_stats_activity(api_client, auth):
    response = await api_client.get("/api/stats/activity", headers=auth)
    assert response.status_code == 200
    assert len(response.json()["items"]) == 4


async def test_stats_topics(api_client, auth):
    response = await api_client.get("/api/stats/topics", headers=auth)
    assert response.status_code == 200
    assert "items" in response.json()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/integration/test_api_stats.py -v`
Expected: FAIL —— 404

- [ ] **Step 3: 实现统计端点**

创建 `src/foresight/api/routers/stats.py`：

```python
"""Analytics endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from foresight.api.deps import get_stats, require_token
from foresight.storage.stats import StatsService

router = APIRouter()


@router.get("/stats/overview", dependencies=[Depends(require_token)])
async def stats_overview(
    since: str | None = None,
    until: str | None = None,
    channel_id: int | None = None,
    stats: StatsService = Depends(get_stats),
) -> dict:
    return await stats.overview(since=since, until=until, channel_id=channel_id)


@router.get("/stats/top-talkers", dependencies=[Depends(require_token)])
async def stats_top_talkers(
    since: str | None = None,
    until: str | None = None,
    channel_id: int | None = None,
    limit: int = Query(20, ge=1, le=200),
    stats: StatsService = Depends(get_stats),
) -> dict:
    rows = await stats.top_talkers(
        since=since, until=until, channel_id=channel_id, limit=limit
    )
    truncated = len(rows) >= limit
    return {
        "total": len(rows),
        "limit": limit,
        "offset": 0,
        "has_more": truncated,
        "truncated": truncated,
        "items": rows,
    }


@router.get("/stats/activity", dependencies=[Depends(require_token)])
async def stats_activity(
    since: str | None = None,
    until: str | None = None,
    channel_id: int | None = None,
    bucket: str = Query("day", pattern="^(hour|day)$"),
    stats: StatsService = Depends(get_stats),
) -> dict:
    buckets = await stats.activity(
        since=since, until=until, bucket=bucket, channel_id=channel_id
    )
    rows = buckets[:limit]
    truncated = len(rows) >= limit
    return {
        "total": len(rows),
        "limit": limit,
        "offset": 0,
        "has_more": truncated,
        "truncated": truncated,
        "items": rows,
    }


@router.get("/stats/topics", dependencies=[Depends(require_token)])
async def stats_topics(
    since: str | None = None,
    until: str | None = None,
    channel_id: int | None = None,
    limit: int = Query(20, ge=1, le=200),
    stats: StatsService = Depends(get_stats),
) -> dict:
    rows = await stats.topics(
        since=since, until=until, channel_id=channel_id, limit=limit
    )
    truncated = len(rows) >= limit
    return {
        "total": len(rows),
        "limit": limit,
        "offset": 0,
        "has_more": truncated,
        "truncated": truncated,
        "items": rows,
    }
```

修改 `src/foresight/api/app.py`：导入 `stats` 并加入 `app.include_router(stats.router, prefix="/api", tags=["stats"])`。

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/integration/test_api_stats.py -v`
Expected: PASS —— 4 passed

- [ ] **Step 5: 提交**

```bash
git add src/foresight/api/routers/stats.py src/foresight/api/app.py tests/integration/test_api_stats.py
git commit -m "feat: analytics endpoints"
```

---

## Task 12: 只读 SQL 端点

**Files:**
- Create: `src/foresight/api/query.py`
- Create: `src/foresight/api/routers/query.py`
- Modify: `src/foresight/api/app.py`
- Create: `tests/integration/test_api_query.py`

**Interfaces:**
- Consumes: `foresight.config.DatabaseConfig`（M1 Task 1）
- Produces:
  - `foresight.api.query.QueryResult`（dataclass：`columns: list[str]`、`rows: list[list[Any]]`、`row_count: int`、`elapsed_ms: float`）
  - `foresight.api.query.QueryRunner`（Protocol）：`async run(sql, params, limit, offset) -> QueryResult`
  - `foresight.api.query.build_query_runner(config: DatabaseConfig) -> QueryRunner`
  - `POST /api/query`

- [ ] **Step 1: 写失败测试**

创建 `tests/integration/test_api_query.py`：

```python
async def test_query_returns_rows(api_client, auth):
    response = await api_client.post(
        "/api/query",
        headers=auth,
        json={
            "sql": "SELECT message_id, content FROM messages ORDER BY message_id",
            "params": {},
            "limit": 10,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["row_count"] == 4
    assert body["columns"] == ["message_id", "content"]


async def test_query_supports_named_params(api_client, auth):
    response = await api_client.post(
        "/api/query",
        headers=auth,
        json={
            "sql": "SELECT COUNT(*) AS c FROM messages WHERE channel_id = :channel",
            "params": {"channel": 20},
        },
    )
    assert response.status_code == 200
    assert response.json()["rows"] == [[4]]


async def test_query_rejects_insert(api_client, auth):
    response = await api_client.post(
        "/api/query",
        headers=auth,
        json={"sql": "INSERT INTO messages (message_id) VALUES (1)", "params": {}},
    )
    assert response.status_code == 400


async def test_query_rejects_drop(api_client, auth):
    response = await api_client.post(
        "/api/query",
        headers=auth,
        json={"sql": "DROP TABLE messages", "params": {}},
    )
    assert response.status_code == 400


async def test_query_rejects_multi_statement(api_client, auth):
    response = await api_client.post(
        "/api/query",
        headers=auth,
        json={"sql": "SELECT 1; DROP TABLE messages", "params": {}},
    )
    assert response.status_code == 400


async def test_query_rejects_write_cte(api_client, auth):
    response = await api_client.post(
        "/api/query",
        headers=auth,
        json={
            "sql": "WITH x AS (DELETE FROM messages RETURNING message_id) SELECT * FROM x",
            "params": {},
        },
    )
    assert response.status_code == 400
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/integration/test_api_query.py -v`
Expected: FAIL —— 404

- [ ] **Step 3: 实现查询执行器**

创建 `src/foresight/api/query.py`：

```python
"""Free-form read-only SQL execution.

SQLite runs on the standard library driver so that `set_authorizer` can
reject write operations at the engine level; PostgreSQL and MySQL use a
read-only transaction instead.
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Protocol

from foresight.config import DatabaseConfig

READ_ONLY_PREFIX = ("select", "with")
MAX_LIMIT = 500
DEFAULT_LIMIT = 100
BLOCKED_KEYWORDS = (
    "insert",
    "update",
    "delete",
    "drop",
    "alter",
    "create",
    "replace",
    "truncate",
    "attach",
    "detach",
    "pragma",
    "vacuum",
    "grant",
    "revoke",
)


class QueryError(Exception):
    """Raised when a statement is not a safe read-only query."""


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    elapsed_ms: float


class QueryRunner(Protocol):
    async def run(
        self, sql: str, params: dict[str, Any], limit: int, offset: int
    ) -> QueryResult: ...


def validate_read_only(sql: str) -> str:
    """Reject anything that is not a single read-only statement."""
    statement = sql.strip().rstrip(";").strip()
    if not statement:
        raise QueryError("empty statement")
    if ";" in statement:
        raise QueryError("only a single statement is allowed")
    lowered = statement.lower()
    if not lowered.startswith(READ_ONLY_PREFIX):
        raise QueryError("only SELECT and WITH statements are allowed")
    for keyword in BLOCKED_KEYWORDS:
        if re.search(rf"\b{keyword}\b", lowered):
            raise QueryError(f"statement contains forbidden keyword: {keyword}")
    return statement


class SqliteQueryRunner:
    """Runs queries on a read-only SQLite connection."""

    def __init__(self, path: str) -> None:
        self._path = path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{self._path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row

        def authorizer(action: int, arg1: Any, arg2: Any, dbname: Any, trigger: Any) -> int:
            allowed = {
                sqlite3.SQLITE_SELECT,
                sqlite3.SQLITE_READ,
                sqlite3.SQLITE_FUNCTION,
            }
            if action in allowed:
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY

        conn.set_authorizer(authorizer)
        return conn

    async def run(
        self, sql: str, params: dict[str, Any], limit: int, offset: int
    ) -> QueryResult:
        statement = validate_read_only(sql)
        started = time.perf_counter()
        result = await asyncio.to_thread(self._run_sync, statement, params, limit, offset)
        result.elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
        return result

    def _run_sync(
        self, statement: str, params: dict[str, Any], limit: int, offset: int
    ) -> QueryResult:
        conn = self._connect()
        try:
            cursor = conn.execute(statement, params)
            columns = [description[0] for description in cursor.description or []]
            rows = cursor.fetchmany(limit + offset)
            window = rows[offset : offset + limit]
            return QueryResult(
                columns=columns,
                rows=[list(row) for row in window],
                row_count=len(window),
                elapsed_ms=0.0,
            )
        except sqlite3.Error as exc:
            raise QueryError(str(exc)) from exc
        finally:
            conn.close()


class SqlAlchemyQueryRunner:
    """Runs queries inside a read-only transaction (PostgreSQL / MySQL)."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    async def run(
        self, sql: str, params: dict[str, Any], limit: int, offset: int
    ) -> QueryResult:
        from sqlalchemy import text

        statement = validate_read_only(sql)
        started = time.perf_counter()
        async with self._engine.connect() as conn:
            await conn.execute(text("SET TRANSACTION READ ONLY"))
            result = await conn.execute(
                text(statement), {k: v for k, v in params.items()}
            )
            columns = list(result.keys())
            rows = result.fetchmany(limit + offset)
        window = rows[offset : offset + limit]
        return QueryResult(
            columns=columns,
            rows=[list(row) for row in window],
            row_count=len(window),
            elapsed_ms=round((time.perf_counter() - started) * 1000, 3),
        )


def build_query_runner(config: DatabaseConfig) -> QueryRunner:
    if config.backend == "sqlite":
        return SqliteQueryRunner(config.path)
    from foresight.storage.db import create_engine

    return SqlAlchemyQueryRunner(create_engine(config))


def clamp_limit(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_LIMIT
    return max(1, min(limit, MAX_LIMIT))
```

- [ ] **Step 4: 实现查询端点**

创建 `src/foresight/api/routers/query.py`：

```python
"""Free-form read-only SQL endpoint."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from foresight.api.deps import require_token
from foresight.api.query import QueryError, QueryResult, clamp_limit

router = APIRouter()


class QueryRequest(BaseModel):
    sql: str = Field(..., min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)
    limit: int | None = None
    offset: int = Field(default=0, ge=0)


class QueryResponse(BaseModel):
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    elapsed_ms: float


@router.post(
    "/query", response_model=QueryResponse, dependencies=[Depends(require_token)]
)
async def run_query(payload: QueryRequest, request: Request) -> QueryResponse:
    runner = request.app.state.query_runner
    try:
        result: QueryResult = await runner.run(
            payload.sql, payload.params, clamp_limit(payload.limit), payload.offset
        )
    except QueryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return QueryResponse(
        columns=result.columns,
        rows=result.rows,
        row_count=result.row_count,
        elapsed_ms=result.elapsed_ms,
    )
```

修改 `src/foresight/api/app.py`：新增 `from foresight.api.query import build_query_runner`，并在 `create_app` 中设置 `app.state.query_runner = build_query_runner(config.database)`；再 `app.include_router(query.router, prefix="/api", tags=["query"])`。

- [ ] **Step 5: 运行测试确认通过**

Run: `uv run pytest tests/integration/test_api_query.py -v`
Expected: PASS —— 6 passed

- [ ] **Step 6: 提交**

```bash
git add src/foresight/api tests/integration/test_api_query.py
git commit -m "feat: read-only SQL endpoint with engine level protection"
```

---

## Task 13: Prometheus 指标

**Files:**
- Create: `src/foresight/api/metrics.py`
- Modify: `src/foresight/api/app.py`
- Create: `tests/integration/test_api_metrics.py`

**Interfaces:**
- Consumes: `foresight.api.app.create_app`（Task 9）
- Produces:
  - `foresight.api.metrics.REGISTRY`（prometheus_client CollectorRegistry）
  - `foresight.api.metrics.messages_captured`（Counter，标签 `channel_id` / `guild_id`）
  - `foresight.api.metrics.messages_deleted`、`messages_edited`、`reactions_total`、`backfill_messages_total`（Counter）
  - `foresight.api.metrics.writer_queue_depth`、`ai_queue_depth`、`db_messages_total`、`db_attachments_total`（Gauge）
  - `foresight.api.metrics.writer_batches_total`、`ai_requests_total`、`media_downloads_total`（Counter）
  - `foresight.api.metrics.api_requests_total`（Counter，标签 `method` / `path` / `status`）
  - `foresight.api.metrics.api_request_duration_seconds`（Histogram）
  - `foresight.api.metrics.get_metrics() -> bytes`

- [ ] **Step 1: 写失败测试**

创建 `tests/integration/test_api_metrics.py`：

```python
from types import SimpleNamespace

from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine

from foresight.api.app import create_app
from foresight.storage.db import init_db


async def test_metrics_endpoint_exposes_foresight_metrics(api_client):
    response = await api_client.get("/metrics")
    assert response.status_code == 200
    assert "foresight_api_requests_total" in response.text


async def test_metrics_records_api_calls(api_client, auth):
    await api_client.get("/api/health", headers=auth)
    response = await api_client.get("/metrics")
    assert "foresight_api_requests_total" in response.text


async def test_metrics_token_is_enforced_when_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("FORESIGHT_API_TOKEN", "secret-token")
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'm.db'}")
    await init_db(engine)
    config = SimpleNamespace(
        api=SimpleNamespace(host="127.0.0.1", port=8000, metrics_token="metrics-secret"),
        database=SimpleNamespace(
            backend="sqlite",
            path=str(tmp_path / "m.db"),
            sqlalchemy_url=f"sqlite+aiosqlite:///{tmp_path / 'm.db'}",
            readonly_sqlalchemy_url=(
                f"sqlite+aiosqlite:///file:{tmp_path / 'm.db'}?mode=ro&uri=true"
            ),
        ),
        media=SimpleNamespace(backend="local"),
    )
    app = create_app(config, engine=engine)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        denied = await client.get("/metrics")
        allowed = await client.get(
            "/metrics", headers={"X-Metrics-Token": "metrics-secret"}
        )
    assert denied.status_code == 401
    assert allowed.status_code == 200
    await engine.dispose()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/integration/test_api_metrics.py -v`
Expected: FAIL —— 404

- [ ] **Step 3: 实现指标模块**

创建 `src/foresight/api/metrics.py`：

```python
"""Prometheus metrics.

Label discipline: `channel_id` and `guild_id` are bounded and safe.
`user_id` must never be used as a label — the cardinality would melt
Prometheus.
"""

from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

REGISTRY = CollectorRegistry()

messages_captured = Counter(
    "foresight_messages_captured_total",
    "Messages captured from Discord",
    ["channel_id", "guild_id"],
    registry=REGISTRY,
)
messages_deleted = Counter(
    "foresight_messages_deleted_total",
    "Messages soft deleted",
    ["channel_id"],
    registry=REGISTRY,
)
messages_edited = Counter(
    "foresight_messages_edited_total",
    "Messages edited",
    ["channel_id"],
    registry=REGISTRY,
)
reactions_total = Counter(
    "foresight_reactions_total",
    "Reactions received",
    ["channel_id"],
    registry=REGISTRY,
)
backfill_messages_total = Counter(
    "foresight_backfill_messages_total",
    "Messages imported by backfill",
    ["channel_id"],
    registry=REGISTRY,
)
writer_queue_depth = Gauge(
    "foresight_writer_queue_depth",
    "Pending write operations",
    registry=REGISTRY,
)
writer_batches_total = Counter(
    "foresight_writer_batches_total",
    "Write batches flushed",
    ["status"],
    registry=REGISTRY,
)
writer_batch_size = Histogram(
    "foresight_writer_batch_size",
    "Rows per write batch",
    registry=REGISTRY,
)
ai_queue_depth = Gauge(
    "foresight_ai_queue_depth",
    "Messages waiting for AI analysis",
    registry=REGISTRY,
)
ai_requests_total = Counter(
    "foresight_ai_requests_total",
    "LLM analysis calls",
    ["status"],
    registry=REGISTRY,
)
ai_request_duration_seconds = Histogram(
    "foresight_ai_request_duration_seconds",
    "LLM analysis latency",
    registry=REGISTRY,
)
ai_tokens_total = Counter(
    "foresight_ai_tokens_total",
    "LLM tokens consumed",
    ["type"],
    registry=REGISTRY,
)
media_downloads_total = Counter(
    "foresight_media_downloads_total",
    "Attachment downloads",
    ["status"],
    registry=REGISTRY,
)
media_bytes_total = Counter(
    "foresight_media_bytes_total",
    "Bytes downloaded",
    registry=REGISTRY,
)
api_requests_total = Counter(
    "foresight_api_requests_total",
    "API requests",
    ["method", "path", "status"],
    registry=REGISTRY,
)
api_request_duration_seconds = Histogram(
    "foresight_api_request_duration_seconds",
    "API request latency",
    ["method", "path"],
    registry=REGISTRY,
)
db_messages_total = Gauge(
    "foresight_db_messages_total",
    "Messages stored in the database",
    registry=REGISTRY,
)
db_attachments_total = Gauge(
    "foresight_db_attachments_total",
    "Attachments stored in the database",
    registry=REGISTRY,
)


def get_metrics() -> bytes:
    """Render the registry in Prometheus exposition format."""
    return generate_latest(REGISTRY)


def metrics_content_type() -> str:
    return CONTENT_TYPE_LATEST
```

- [ ] **Step 4: 挂载 /metrics 与中间件**

修改 `src/foresight/api/app.py`：

```python
from fastapi import FastAPI, Request, Response

from foresight.api.metrics import (
    api_request_duration_seconds,
    api_requests_total,
    get_metrics,
    metrics_content_type,
)


def create_app(config: Config, engine=None) -> FastAPI:  # noqa: ANN001
    app = FastAPI(title="ForeSight API", version="0.1.0")
    ...
    @app.middleware("http")
    async def _count_requests(request: Request, call_next):  # noqa: ANN001
        started = time.perf_counter()
        response = await call_next(request)
        elapsed = time.perf_counter() - started
        path = request.url.path
        api_requests_total.labels(
            method=request.method, path=path, status=str(response.status_code)
        ).inc()
        api_request_duration_seconds.labels(method=request.method, path=path).observe(elapsed)
        return response

    @app.get("/metrics", include_in_schema=False)
    async def metrics(request: Request) -> Response:
        expected = getattr(app.state.config.api, "metrics_token", "") or ""
        if expected:
            supplied = request.headers.get("x-metrics-token", "")
            if not secrets.compare_digest(supplied, expected):
                return Response(
                    content=b'{"detail":"invalid metrics token"}',
                    status_code=401,
                    media_type="application/json",
                )
        return Response(content=get_metrics(), media_type=metrics_content_type())

    return app
```

（`import secrets` 与 `import time` 加到文件顶部。）

- [ ] **Step 5: 运行测试确认通过**

Run: `uv run pytest tests/integration/test_api_metrics.py -v`
Expected: PASS —— 3 passed

Run: `uv run pytest -v`
Expected: 全部通过

- [ ] **Step 6: 提交**

```bash
git add src/foresight/api/metrics.py src/foresight/api/app.py tests/integration/test_api_metrics.py
git commit -m "feat: Prometheus metrics endpoint"
```

---

## Task 14: serve / both / query / stats 命令

**Files:**
- Modify: `src/foresight/cli.py`
- Create: `tests/unit/test_cli_query.py`

**Interfaces:**
- Consumes: `foresight.api.app.create_app`（Task 9）、`foresight.api.query.build_query_runner`（Task 12）、`foresight.storage.stats.StatsService`（Task 8）
- Produces: 命令 `foresight serve`、`foresight both`、`foresight query`、`foresight stats`

- [ ] **Step 1: 写失败测试**

创建 `tests/unit/test_cli_query.py`：

```python
from pathlib import Path

from typer.testing import CliRunner

from foresight.cli import app

runner = CliRunner()


def _seed_db(tmp_path: Path) -> Path:
    import asyncio

    from foresight.domain.models import MessageRecord, OpKind, WriteOp
    from foresight.storage.db import create_engine, init_db
    from foresight.storage.repository import Repository
    from foresight.config import DatabaseConfig

    path = tmp_path / "foresight.db"
    config = DatabaseConfig(backend="sqlite", path=str(path))

    async def _run():
        engine = create_engine(config)
        await init_db(engine)
        repo = Repository(engine)
        await repo.apply_batch(
            [
                WriteOp(
                    OpKind.MESSAGE,
                    MessageRecord(
                        message_id=1,
                        guild_id=10,
                        channel_id=20,
                        author_id=30,
                        content="hello",
                        message_type=0,
                        created_at="2026-11-11T00:00:00+00:00",
                        captured_at="2026-11-11T00:00:00+00:00",
                    ),
                )
            ]
        )
        await engine.dispose()

    asyncio.run(_run())
    return path


def test_query_command_outputs_json(tmp_path: Path):
    path = _seed_db(tmp_path)
    result = runner.invoke(
        app,
        [
            "query",
            "--db",
            str(path),
            "--sql",
            "SELECT COUNT(*) FROM messages",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "1" in result.output


def test_query_command_rejects_writes(tmp_path: Path):
    path = _seed_db(tmp_path)
    result = runner.invoke(
        app, ["query", "--db", str(path), "--sql", "DELETE FROM messages"]
    )
    assert result.exit_code != 0


def test_stats_command_prints_overview(tmp_path: Path):
    path = _seed_db(tmp_path)
    result = runner.invoke(app, ["stats", "--db", str(path)])
    assert result.exit_code == 0, result.output
    assert "total_messages" in result.output


def test_serve_refuses_remote_bind_without_token(tmp_path: Path, monkeypatch):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[discord]
channel_ids = [1]

[database]
backend = "sqlite"
path = "data/foresight.db"

[api]
host = "0.0.0.0"
port = 8000
""",
        encoding="utf-8",
    )
    monkeypatch.delenv("FORESIGHT_API_TOKEN", raising=False)
    result = runner.invoke(app, ["serve", "--config", str(config_path)])
    assert result.exit_code != 0
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/unit/test_cli_query.py -v`
Expected: FAIL —— `No such command 'query'`

- [ ] **Step 3: 实现命令**

在 `src/foresight/cli.py` 追加：

```python
@app.command()
def serve(
    config: Optional[str] = typer.Option(None, "--config", help="Path to config.toml"),
    host: Optional[str] = typer.Option(None, "--host", help="Override api.host"),
    port: Optional[int] = typer.Option(None, "--port", help="Override api.port"),
) -> None:
    """Serve the read-only HTTP API."""
    import uvicorn

    from foresight.api.app import create_app

    settings = _load(config)
    bind_host = host or settings.api.host
    bind_port = port or settings.api.port
    _require_token_for_host(bind_host)
    app = create_app(settings)
    uvicorn.run(app, host=bind_host, port=bind_port, log_level=settings.logging.level.lower())


@app.command()
def both(
    config: Optional[str] = typer.Option(None, "--config", help="Path to config.toml"),
) -> None:
    """Run the collector and the HTTP API in a single process."""
    import uvicorn

    from foresight.api.app import create_app
    from foresight.bot import build_bot, require_token
    from foresight.events import EventHandler
    from foresight.storage.db import create_engine, init_db as create_tables
    from foresight.storage.repository import Repository
    from foresight.writer import Writer, recover_failed_batches

    settings = _load(config)
    if not settings.discord.channel_ids:
        raise typer.BadParameter("[discord].channel_ids must not be empty")
    _require_token_for_host(settings.api.host)

    async def _run() -> None:
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
        handler = EventHandler(settings, writer)
        bot = build_bot(settings, handler)
        app = create_app(settings, engine=engine)
        server = uvicorn.Server(
            uvicorn.Config(app, host=settings.api.host, port=settings.api.port)
        )
        server_task = asyncio.create_task(server.serve())
        try:
            await bot.start(require_token())
        finally:
            server_task.cancel()
            await writer.stop()
            await engine.dispose()

    asyncio.run(_run())


@app.command()
def query(
    sql: str = typer.Argument(..., help="Read-only SQL statement"),
    db: str = typer.Option(..., "--db", help="Path to the SQLite database file"),
    limit: int = typer.Option(50, help="Maximum number of rows"),
) -> None:
    """Run a read-only SQL statement against the database."""
    import json

    from foresight.api.query import QueryError, SqliteQueryRunner

    runner = SqliteQueryRunner(db)
    try:
        result = asyncio.run(runner.run(sql, {}, limit, 0))
    except QueryError as exc:
        typer.secho(f"query error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    typer.echo(json.dumps({"columns": result.columns, "rows": result.rows}, ensure_ascii=False))


@app.command()
def stats(
    db: str = typer.Option(..., "--db", help="Path to the SQLite database file"),
    since: Optional[str] = typer.Option(None, help="ISO timestamp lower bound"),
    until: Optional[str] = typer.Option(None, help="ISO timestamp upper bound"),
) -> None:
    """Print an overview of the captured messages."""
    import json

    from sqlalchemy.ext.asyncio import create_async_engine

    from foresight.storage.stats import StatsService

    async def _run() -> dict:
        engine = create_async_engine(f"sqlite+aiosqlite:///{db}")
        try:
            return await StatsService(engine).overview(since=since, until=until)
        finally:
            await engine.dispose()

    typer.echo(json.dumps(asyncio.run(_run()), ensure_ascii=False, indent=2))
```

并在文件顶部补充：

```python
import os
```

以及新增辅助函数：

```python
def _require_token_for_host(host: str) -> None:
    """Refuse to expose the API remotely without a token."""
    token = os.environ.get("FORESIGHT_API_TOKEN", "").strip()
    if token:
        return
    if host in ("127.0.0.1", "localhost", "::1"):
        return
    typer.secho(
        "FORESIGHT_API_TOKEN must be set when the API is not bound to localhost",
        fg=typer.colors.RED,
        err=True,
    )
    raise typer.Exit(code=2)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/unit/test_cli_query.py -v`
Expected: PASS —— 4 passed

Run: `uv run pytest -v`
Expected: 全部通过

- [ ] **Step 5: 提交**

```bash
git add src/foresight/cli.py tests/unit/test_cli_query.py
git commit -m "feat: serve, both, query and stats commands"
```

---

## M2 验收

```bash
uv run pytest -v          # 全部通过
uv run foresight serve --config config.example.toml   # API 启动，/api/health 可访问
```

`/api/health`、`/api/messages`、`/api/stats/*`、`/api/query`、`/metrics` 全部可访问即表示 M2 完成。

接下来执行 `docs/superpowers/plans/2026-11-11-foresight-m3-analysis-media-backfill.md`。
