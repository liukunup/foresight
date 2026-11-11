# ForeSight M1 — 采集内核 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 搭起 ForeSight 项目骨架，实现「Discord 消息 → 领域对象 → 队列 → 数据库」的完整采集链路，服务能独立跑起来并落库。

**Architecture:** 分层单进程服务。`adapters/discord_mapper` 把 Discord 对象翻译成纯 dataclass（`domain/models.py`），`writer` 用有界 `asyncio.Queue` 批量落库，`storage/repository` 是唯一写 SQL 的地方。Discord 事件处理逻辑放在独立的 `events.py`（纯函数式，可脱离 discord.py 单测），`bot.py` 只做事件注册。

**Tech Stack:** Python 3.13、discord.py 2.7+、SQLAlchemy 2.0 Core（async）、aiosqlite、typer、pytest + pytest-asyncio。

**设计依据：** `docs/superpowers/specs/2026-11-11-discord-message-dump-design.md`

**后续计划：** M2（`2026-11-11-foresight-m2-query-and-metrics.md`）、M3（`2026-11-11-foresight-m3-analysis-media-backfill.md`）、M4（`2026-11-11-foresight-m4-delivery-skill-docker-docs.md`）

## Global Constraints

- Python `>=3.13`，包布局 `src/foresight/`，构建后端 `hatchling`，console script 为 `foresight`
- 领域模型（`domain/models.py`）**零依赖**，不得 import discord / sqlalchemy / fastapi
- Discord SDK 字段名只允许出现在 `adapters/discord_mapper.py` 与 `events.py` 中
- 所有时间戳存 **UTC ISO-8601 TEXT**（例 `2026-11-11T13:45:00+00:00`）
- 所有 JSON 字段存 **TEXT**（JSON 字符串），三方言一致
- Discord 雪花 ID 存 `BIGINT`
- 所有密钥只从环境变量读取，TOML 里不出现任何密钥
- 数据库后端三选一：`sqlite` / `postgresql` / `mysql`
- 实时写入策略为 upsert（只更新 `content` / `edited_at` / `edit_count` / `deleted_at`）；补录写入策略为 insert-only
- `messages.analysis_status` 取值集合：`pending` / `done` / `failed` / `skipped`
- 编辑只保留最新内容，不留存历史版本
- 删除是软删除（写 `deleted_at`，保留 `content`）
- 表情移除是软移除（写 `removed_at`，不删行）
- `reactions` 与 `attachments` **不声明外键到 `messages`**
- 队列满时丢弃并告警，**绝不阻塞 event loop**
- 测试命令统一为 `uv run pytest`；单个用例为 `uv run pytest <path> -v`

---

## Task 1: 项目脚手架与配置加载

**Files:**
- Create: `pyproject.toml`
- Create: `.gitignore`（扩展已有的 `.gitignore`，保留原有条目；需要忽略 `.superpowers/`、`data/`、`config.toml`）
- Create: `src/foresight/__init__.py`
- Create: `src/foresight/config.py`
- Create: `src/foresight/logging_setup.py`
- Create: `tests/conftest.py`
- Create: `tests/unit/test_config.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `foresight.config.Config`（frozen dataclass），字段：`discord: DiscordConfig`、`database: DatabaseConfig`、`media: MediaConfig`、`api: ApiConfig`、`ai: AiConfig`、`writer: WriterConfig`、`logging: LoggingConfig`
  - `foresight.config.load_config(path: str | Path | None = None) -> Config`
  - `foresight.config.ConfigError(Exception)`
  - `DatabaseConfig.sqlalchemy_url -> str`、`DatabaseConfig.readonly_sqlalchemy_url -> str`
  - `foresight.logging_setup.setup_logging(level: str = "INFO") -> None`

- [ ] **Step 1: 写失败测试**

创建 `tests/conftest.py`：

```python
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
```

创建 `tests/unit/test_config.py`：

```python
from pathlib import Path

import pytest

from foresight.config import ConfigError, load_config


def _write_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_load_config_reads_sections(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        """
[discord]
channel_ids = [111, 222]

[api]
port = 9000
""",
    )
    config = load_config(path)
    assert config.discord.channel_ids == (111, 222)
    assert config.api.port == 9000
    assert config.api.host == "127.0.0.1"
    assert config.database.backend == "sqlite"
    assert config.database.sqlalchemy_url.startswith("sqlite+aiosqlite:///")


def test_env_overrides_api_host_and_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write_config(tmp_path, "[api]\nport = 9000\n")
    monkeypatch.setenv("FORESIGHT_API_HOST", "0.0.0.0")
    monkeypatch.setenv("FORESIGHT_API_PORT", "8123")
    config = load_config(path)
    assert config.api.host == "0.0.0.0"
    assert config.api.port == 8123


def test_unknown_database_backend_raises(tmp_path: Path) -> None:
    path = _write_config(tmp_path, '[database]\nbackend = "oracle"\n')
    with pytest.raises(ConfigError):
        load_config(path)


def test_unknown_media_backend_raises(tmp_path: Path) -> None:
    path = _write_config(tmp_path, '[media]\nbackend = "gdrive"\n')
    with pytest.raises(ConfigError):
        load_config(path)


def test_missing_config_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope.toml")


def test_secrets_come_from_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FORESIGHT_DB_PASSWORD", "s3cret")
    monkeypatch.setenv("FORESIGHT_LLM_API_KEY", "llm-key")
    monkeypatch.setenv("FORESIGHT_S3_SECRET_ACCESS_KEY", "s3-key")
    path = _write_config(tmp_path, "[discord]\nchannel_ids = [1]\n")
    config = load_config(path)
    assert config.database.password == "s3cret"
    assert config.ai.api_key == "llm-key"
    assert config.media.s3.secret_access_key == "s3-key"


def test_postgres_url_encodes_password(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORESIGHT_DB_PASSWORD", "p@ss/word")
    path = _write_config(
        tmp_path,
        """
[database]
backend = "postgresql"
host = "db"
port = 5432
database = "fs"
user = "fsuser"
""",
    )
    config = load_config(path)
    assert "p%40ss%2Fword" in config.database.sqlalchemy_url
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/unit/test_config.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'foresight'`

- [ ] **Step 3: 实现配置加载**

创建 `pyproject.toml`：

```toml
[project]
name = "foresight"
version = "0.1.0"
description = "Discord channel message dump service for analytics"
requires-python = ">=3.13"
dependencies = [
    "discord.py>=2.7",
    "SQLAlchemy[asyncio]>=2.0",
    "aiosqlite>=0.20",
    "asyncpg>=0.30",
    "asyncmy>=0.2",
    "fastapi>=0.115",
    "uvicorn[standard]>=0.32",
    "httpx>=0.27",
    "aioboto3>=13.4",
    "prometheus-client>=0.21",
    "typer>=0.12",
    "rich>=13.9",
]

[dependency-groups]
dev = [
    "pytest>=8.3",
    "pytest-asyncio>=0.24",
    "pytest-cov>=6.0",
    "respx>=0.21",
    "moto[s3]>=5.0",
]

[project.scripts]
foresight = "foresight.cli:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/foresight"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "function"
testpaths = ["tests"]
```

注意：dev 依赖用 PEP 735 的 `[dependency-groups]` 而不是 `[project.optional-dependencies]` —— uv 默认只安装 dependency group，把它写成 extra 会导致 `uv run pytest` 在全新检出上直接失败（`Failed to spawn: pytest`），而且 `uv sync` 会把 pytest 整套工具链卸载掉。

创建 `src/foresight/__init__.py`：

```python
"""ForeSight — Discord channel message dump service."""

__version__ = "0.1.0"
```

创建 `src/foresight/config.py`：

```python
"""Configuration loading.

Secrets never live in the TOML file; they are read from environment
variables. Environment variables listed in the design spec override the
matching TOML keys.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote_plus

DatabaseBackend = Literal["sqlite", "postgresql", "mysql"]
MediaBackend = Literal["local", "s3"]

DEFAULT_CONFIG_PATH = "config.toml"


class ConfigError(Exception):
    """Raised when configuration is missing or invalid."""


@dataclass(frozen=True)
class DiscordConfig:
    channel_ids: tuple[int, ...]


@dataclass(frozen=True)
class DatabaseConfig:
    backend: DatabaseBackend
    path: str = "data/foresight.db"
    host: str = "localhost"
    port: int = 5432
    database: str = "foresight"
    user: str = "foresight"
    password: str = ""

    @property
    def sqlalchemy_url(self) -> str:
        if self.backend == "sqlite":
            return f"sqlite+aiosqlite:///{self.path}"
        if self.backend == "postgresql":
            return (
                f"postgresql+asyncpg://{quote_plus(self.user)}:{quote_plus(self.password)}"
                f"@{self.host}:{self.port}/{self.database}"
            )
        return (
            f"mysql+asyncmy://{quote_plus(self.user)}:{quote_plus(self.password)}"
            f"@{self.host}:{self.port}/{self.database}"
        )

    @property
    def readonly_sqlalchemy_url(self) -> str:
        if self.backend == "sqlite":
            return f"sqlite+aiosqlite:///file:{self.path}?mode=ro&uri=true"
        return self.sqlalchemy_url


@dataclass(frozen=True)
class S3Config:
    bucket: str = ""
    endpoint_url: str = ""
    region: str = "auto"
    prefix: str = "foresight"
    use_path_style: bool = True
    access_key_id: str = ""
    secret_access_key: str = ""


@dataclass(frozen=True)
class MediaConfig:
    backend: MediaBackend
    local_root: str = "data/media"
    max_download_size_mb: int = 100
    download_concurrency: int = 3
    s3: S3Config = field(default_factory=S3Config)


@dataclass(frozen=True)
class ApiConfig:
    host: str = "127.0.0.1"
    port: int = 8000
    metrics_token: str = ""


@dataclass(frozen=True)
class AiConfig:
    enabled: bool = False
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4o-mini"
    translation_target: str = "zh-CN"
    max_concurrency: int = 2
    timeout_seconds: int = 30
    api_key: str = ""


@dataclass(frozen=True)
class WriterConfig:
    batch_size: int = 100
    flush_interval_ms: int = 50
    queue_maxsize: int = 10000


@dataclass(frozen=True)
class LoggingConfig:
    level: str = "INFO"


@dataclass(frozen=True)
class Config:
    discord: DiscordConfig
    database: DatabaseConfig
    media: MediaConfig
    api: ApiConfig
    ai: AiConfig
    writer: WriterConfig
    logging: LoggingConfig
    config_path: Path


def _section(raw: dict[str, Any], name: str) -> dict[str, Any]:
    value = raw.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] must be a table")
    return value


def load_config(path: str | Path | None = None) -> Config:
    """Load configuration from TOML plus environment overrides."""
    config_path = Path(path or os.environ.get("FORESIGHT_CONFIG", DEFAULT_CONFIG_PATH))
    if not config_path.exists():
        raise ConfigError(f"config file not found: {config_path}")
    with config_path.open("rb") as fh:
        raw = tomllib.load(fh)
    return _build(raw, config_path)


def _build(raw: dict[str, Any], config_path: Path) -> Config:
    discord_raw = _section(raw, "discord")
    channel_ids = tuple(int(cid) for cid in discord_raw.get("channel_ids", []))

    db_raw = _section(raw, "database")
    backend = db_raw.get("backend", "sqlite")
    if backend not in ("sqlite", "postgresql", "mysql"):
        raise ConfigError(f"unsupported database backend: {backend!r}")
    database = DatabaseConfig(
        backend=backend,
        path=db_raw.get("path", "data/foresight.db"),
        host=db_raw.get("host", "localhost"),
        port=int(db_raw.get("port", 5432)),
        database=db_raw.get("database", "foresight"),
        user=db_raw.get("user", "foresight"),
        password=os.environ.get("FORESIGHT_DB_PASSWORD", ""),
    )

    media_raw = _section(raw, "media")
    media_backend = media_raw.get("backend", "local")
    if media_backend not in ("local", "s3"):
        raise ConfigError(f"unsupported media backend: {media_backend!r}")
    s3_raw = _section(media_raw, "s3")
    media = MediaConfig(
        backend=media_backend,
        local_root=media_raw.get("local_root", "data/media"),
        max_download_size_mb=int(media_raw.get("max_download_size_mb", 100)),
        download_concurrency=int(media_raw.get("download_concurrency", 3)),
        s3=S3Config(
            bucket=s3_raw.get("bucket", ""),
            endpoint_url=s3_raw.get("endpoint_url", ""),
            region=s3_raw.get("region", "auto"),
            prefix=s3_raw.get("prefix", "foresight"),
            use_path_style=bool(s3_raw.get("use_path_style", True)),
            access_key_id=os.environ.get("FORESIGHT_S3_ACCESS_KEY_ID", ""),
            secret_access_key=os.environ.get("FORESIGHT_S3_SECRET_ACCESS_KEY", ""),
        ),
    )

    api_raw = _section(raw, "api")
    api = ApiConfig(
        host=os.environ.get("FORESIGHT_API_HOST", api_raw.get("host", "127.0.0.1")),
        port=int(os.environ.get("FORESIGHT_API_PORT", api_raw.get("port", 8000))),
        metrics_token=api_raw.get("metrics_token", ""),
    )

    ai_raw = _section(raw, "ai")
    ai = AiConfig(
        enabled=bool(ai_raw.get("enabled", False)),
        base_url=ai_raw.get("base_url", "https://api.openai.com/v1"),
        model=ai_raw.get("model", "gpt-4o-mini"),
        translation_target=ai_raw.get("translation_target", "zh-CN"),
        max_concurrency=int(ai_raw.get("max_concurrency", 2)),
        timeout_seconds=int(ai_raw.get("timeout_seconds", 30)),
        api_key=os.environ.get("FORESIGHT_LLM_API_KEY", ""),
    )

    writer_raw = _section(raw, "writer")
    writer = WriterConfig(
        batch_size=int(writer_raw.get("batch_size", 100)),
        flush_interval_ms=int(writer_raw.get("flush_interval_ms", 50)),
        queue_maxsize=int(writer_raw.get("queue_maxsize", 10000)),
    )

    logging_raw = _section(raw, "logging")
    logging_cfg = LoggingConfig(level=logging_raw.get("level", "INFO"))

    return Config(
        discord=DiscordConfig(channel_ids=channel_ids),
        database=database,
        media=media,
        api=api,
        ai=ai,
        writer=writer,
        logging=logging_cfg,
        config_path=config_path,
    )
```

创建 `src/foresight/logging_setup.py`：

```python
"""Logging configuration shared by all entry points."""

from __future__ import annotations

import logging


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/unit/test_config.py -v`
Expected: PASS —— 8 passed

若尚未安装依赖，先执行：`uv venv && uv sync`（`uv sync` 会安装 dev dependency group）

- [ ] **Step 5: 提交**

```bash
git add pyproject.toml .gitignore src/foresight/__init__.py src/foresight/config.py src/foresight/logging_setup.py tests/conftest.py tests/unit/test_config.py
git commit -m "feat: project scaffold and config loading"
```

---

## Task 2: 领域模型与数据库表定义

**Files:**
- Create: `src/foresight/domain/__init__.py`
- Create: `src/foresight/domain/models.py`
- Create: `src/foresight/storage/__init__.py`
- Create: `src/foresight/storage/tables.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `foresight.domain.models` 中的 dataclass：`MessageRecord`、`MessageEdit`、`MessageDelete`、`ReactionRecord`、`ReactionRemove`、`UserSnapshot`、`ChannelSnapshot`、`GuildSnapshot`、`AttachmentRecord`、`AttachmentStatus`、`MessageAnalysis`
  - `foresight.domain.models.WriteOp(kind: str, payload: Any)`
  - `foresight.domain.models.OpKind` 常量类
  - `foresight.storage.tables` 中的 Table 对象：`guilds`、`channels`、`users`、`messages`、`reactions`、`attachments`、`message_analysis`，以及 `metadata: MetaData`

- [ ] **Step 1: 写失败测试**

创建 `tests/unit/test_domain_models.py`：

```python
from foresight.domain.models import MessageRecord, OpKind, WriteOp


def test_message_record_defaults():
    record = MessageRecord(
        message_id=1,
        guild_id=2,
        channel_id=3,
        author_id=4,
        content="hi",
        message_type=0,
        created_at="2026-11-11T00:00:00+00:00",
        captured_at="2026-11-11T00:00:00+00:00",
    )
    assert record.edited_at is None
    assert record.edit_count == 0
    assert record.deleted_at is None
    assert record.reply_to_message_id is None
    assert record.analysis_status == "pending"


def test_write_op_holds_kind_and_payload():
    op = WriteOp(OpKind.MESSAGE, {"a": 1})
    assert op.kind == "message"
    assert op.payload == {"a": 1}


def test_op_kind_constants_are_unique():
    values = [
        value
        for name, value in vars(OpKind).items()
        if not name.startswith("_") and isinstance(value, str)
    ]
    assert len(values) == len(set(values))
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/unit/test_domain_models.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'foresight.domain'`

- [ ] **Step 3: 实现领域模型**

创建 `src/foresight/domain/__init__.py`（空文件）：

```python
```

创建 `src/foresight/domain/models.py`：

```python
"""Plain dataclasses shared by every layer.

This module must stay dependency free: no discord, no sqlalchemy, no fastapi.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class OpKind:
    """Kinds of write operations understood by the repository."""

    MESSAGE = "message"
    MESSAGE_INSERT_ONLY = "message_insert_only"
    MESSAGE_EDIT = "message_edit"
    MESSAGE_DELETE = "message_delete"
    REACTION = "reaction"
    REACTION_REMOVE = "reaction_remove"
    USER = "user"
    CHANNEL = "channel"
    GUILD = "guild"
    ATTACHMENT = "attachment"
    ATTACHMENT_STATUS = "attachment_status"
    ANALYSIS_STATUS = "analysis_status"
    ANALYSIS = "analysis"


@dataclass
class WriteOp:
    """A single unit of work handed to the repository."""

    kind: str
    payload: Any


@dataclass
class MessageRecord:
    message_id: int
    guild_id: int | None
    channel_id: int
    author_id: int
    content: str
    message_type: int
    created_at: str
    captured_at: str
    edited_at: str | None = None
    edit_count: int = 0
    deleted_at: str | None = None
    reply_to_message_id: int | None = None
    mentions_count: int = 0
    char_count: int = 0
    word_count: int = 0
    attachment_count: int = 0
    attachments_json: str | None = None
    embeds_json: str | None = None
    is_bot: bool = False
    analysis_status: str = "pending"


@dataclass
class MessageEdit:
    message_id: int
    content: str
    edited_at: str


@dataclass
class MessageDelete:
    message_id: int
    deleted_at: str


@dataclass
class ReactionRecord:
    message_id: int
    user_id: int
    emoji: str
    emoji_id: int | None
    is_custom: bool
    added_at: str
    removed_at: str | None = None


@dataclass
class ReactionRemove:
    message_id: int
    user_id: int
    emoji: str
    removed_at: str


@dataclass
class UserSnapshot:
    user_id: int
    username: str
    global_name: str | None
    display_name: str | None
    discriminator: str | None
    avatar_url: str | None
    is_bot: bool
    account_created_at: str | None
    guild_joined_at: str | None
    roles_json: str
    updated_at: str


@dataclass
class ChannelSnapshot:
    channel_id: int
    guild_id: int | None
    name: str | None
    topic: str | None
    channel_type: str | None
    position: int | None
    captured_at: str


@dataclass
class GuildSnapshot:
    guild_id: int
    name: str | None
    member_count: int | None
    captured_at: str


@dataclass
class AttachmentRecord:
    message_id: int
    attachment_id: int
    filename: str | None
    content_type: str | None
    size: int | None
    url: str
    storage_backend: str
    status: str
    captured_at: str


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


@dataclass
class MessageAnalysis:
    message_id: int
    translated_text: str | None
    sentiment: str | None
    sentiment_score: float | None
    issues_json: str | None
    model: str
    prompt_version: str
    latency_ms: int
    error: str | None
    analyzed_at: str
```

- [ ] **Step 4: 实现表定义**

创建 `src/foresight/storage/__init__.py`（空文件）：

```python
```

创建 `src/foresight/storage/tables.py`：

```python
"""SQLAlchemy Core table definitions.

JSON payloads are stored as TEXT so that SQLite, PostgreSQL and MySQL all
behave identically. `reactions` and `attachments` deliberately declare no
foreign key to `messages`: a reaction may arrive for a message that was sent
before the service started and therefore has no row in `messages`.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Float,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
)

metadata = MetaData()


def _pk_type() -> BigInteger:
    """Auto-increment primary key type (SQLite needs INTEGER for rowid alias)."""
    return BigInteger().with_variant(Integer, "sqlite")


guilds = Table(
    "guilds",
    metadata,
    Column("guild_id", BigInteger, primary_key=True),
    Column("name", Text),
    Column("member_count", Integer),
    Column("captured_at", Text, nullable=False),
)

channels = Table(
    "channels",
    metadata,
    Column("channel_id", BigInteger, primary_key=True),
    Column("guild_id", BigInteger),
    Column("name", Text),
    Column("topic", Text),
    Column("channel_type", Text),
    Column("position", Integer),
    Column("captured_at", Text, nullable=False),
)

users = Table(
    "users",
    metadata,
    Column("user_id", BigInteger, primary_key=True),
    Column("username", Text, nullable=False),
    Column("global_name", Text),
    Column("display_name", Text),
    Column("discriminator", Text),
    Column("avatar_url", Text),
    Column("is_bot", Boolean, nullable=False, default=False),
    Column("account_created_at", Text),
    Column("guild_joined_at", Text),
    Column("roles_json", Text, nullable=False, default="[]"),
    Column("updated_at", Text, nullable=False),
)

messages = Table(
    "messages",
    metadata,
    Column("message_id", BigInteger, primary_key=True),
    Column("guild_id", BigInteger),
    Column("channel_id", BigInteger, nullable=False),
    Column("author_id", BigInteger, nullable=False),
    Column("content", Text, nullable=False, default=""),
    Column("message_type", Integer, nullable=False, default=0),
    Column("created_at", Text, nullable=False),
    Column("captured_at", Text, nullable=False),
    Column("edited_at", Text),
    Column("edit_count", Integer, nullable=False, default=0),
    Column("deleted_at", Text),
    Column("reply_to_message_id", BigInteger),
    Column("mentions_count", Integer, nullable=False, default=0),
    Column("char_count", Integer, nullable=False, default=0),
    Column("word_count", Integer, nullable=False, default=0),
    Column("attachment_count", Integer, nullable=False, default=0),
    Column("attachments_json", Text),
    Column("embeds_json", Text),
    Column("is_bot", Boolean, nullable=False, default=False),
    Column("analysis_status", Text, nullable=False, default="pending"),
)

reactions = Table(
    "reactions",
    metadata,
    Column("id", _pk_type(), primary_key=True, autoincrement=True),
    Column("message_id", BigInteger, nullable=False),
    Column("user_id", BigInteger, nullable=False),
    Column("emoji", Text, nullable=False),
    Column("emoji_id", BigInteger),
    Column("is_custom", Boolean, nullable=False, default=False),
    Column("added_at", Text, nullable=False),
    Column("removed_at", Text),
    UniqueConstraint("message_id", "user_id", "emoji", name="uq_reactions_target"),
)

attachments = Table(
    "attachments",
    metadata,
    Column("id", _pk_type(), primary_key=True, autoincrement=True),
    Column("message_id", BigInteger, nullable=False),
    Column("attachment_id", BigInteger),
    Column("filename", Text),
    Column("content_type", Text),
    Column("size", BigInteger),
    Column("url", Text, nullable=False),
    Column("sha256", Text),
    Column("storage_backend", Text, nullable=False, default="local"),
    Column("storage_key", Text),
    Column("storage_uri", Text),
    Column("status", Text, nullable=False, default="pending"),
    Column("error", Text),
    Column("captured_at", Text, nullable=False),
    Column("stored_at", Text),
)

message_analysis = Table(
    "message_analysis",
    metadata,
    Column("message_id", BigInteger, primary_key=True),
    Column("translated_text", Text),
    Column("sentiment", Text),
    Column("sentiment_score", Float),
    Column("issues_json", Text),
    Column("model", Text),
    Column("prompt_version", Text),
    Column("latency_ms", Integer),
    Column("error", Text),
    Column("analyzed_at", Text, nullable=False),
)

Index("ix_messages_channel_created", messages.c.channel_id, messages.c.created_at)
Index("ix_messages_author_created", messages.c.author_id, messages.c.created_at)
Index("ix_messages_guild_created", messages.c.guild_id, messages.c.created_at)
Index("ix_messages_created", messages.c.created_at)
Index("ix_messages_analysis_status", messages.c.analysis_status)
Index("ix_reactions_message", reactions.c.message_id)
Index("ix_attachments_message", attachments.c.message_id)
Index("ix_attachments_status", attachments.c.status)
```

- [ ] **Step 5: 运行测试确认通过**

Run: `uv run pytest tests/unit/test_domain_models.py -v`
Expected: PASS —— 3 passed

- [ ] **Step 6: 提交**

```bash
git add src/foresight/domain src/foresight/storage/tables.py src/foresight/storage/__init__.py tests/unit/test_domain_models.py
git commit -m "feat: domain models and SQLAlchemy table definitions"
```

---

## Task 3: 存储引擎与 Repository

**Files:**
- Create: `src/foresight/storage/db.py`
- Create: `src/foresight/storage/repository.py`
- Create: `tests/unit/test_repository.py`

**Interfaces:**
- Consumes: `foresight.domain.models`（Task 2）、`foresight.storage.tables`（Task 2）、`foresight.config.DatabaseConfig`（Task 1）
- Produces:
  - `foresight.storage.db.create_engine(config: DatabaseConfig) -> AsyncEngine`
  - `foresight.storage.db.init_db(engine: AsyncEngine) -> None`
  - `foresight.storage.repository.Repository(engine: AsyncEngine)`
  - `Repository.apply_batch(ops: Sequence[WriteOp]) -> None`
  - `Repository.insert_attachment(a: AttachmentRecord) -> int`（返回新行 id，附件行以 `(message_id, attachment_id)` 定位更新）

- [ ] **Step 1: 写失败测试**

创建 `tests/unit/test_repository.py`：

```python
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from foresight.domain.models import (
    MessageDelete,
    MessageEdit,
    MessageRecord,
    OpKind,
    ReactionRecord,
    UserSnapshot,
    WriteOp,
)
from foresight.storage.db import init_db
from foresight.storage.repository import Repository
from foresight.storage.tables import messages, reactions, users


async def _make_repo(tmp_path) -> tuple[Repository, AsyncEngine]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    await init_db(engine)
    return Repository(engine), engine


def _message(content: str = "hello", message_id: int = 1) -> MessageRecord:
    return MessageRecord(
        message_id=message_id,
        guild_id=10,
        channel_id=20,
        author_id=30,
        content=content,
        message_type=0,
        created_at="2026-11-11T00:00:00+00:00",
        captured_at="2026-11-11T00:00:00+00:00",
    )


async def test_apply_batch_inserts_message(tmp_path):
    repo, engine = await _make_repo(tmp_path)
    await repo.apply_batch([WriteOp(OpKind.MESSAGE, _message())])
    async with engine.connect() as conn:
        rows = (await conn.execute(select(messages))).all()
    assert len(rows) == 1
    assert rows[0].content == "hello"


async def test_insert_only_does_not_overwrite_existing(tmp_path):
    repo, engine = await _make_repo(tmp_path)
    await repo.apply_batch([WriteOp(OpKind.MESSAGE, _message("live"))])
    await repo.apply_batch([WriteOp(OpKind.MESSAGE_INSERT_ONLY, _message("backfill"))])
    async with engine.connect() as conn:
        row = (
            await conn.execute(select(messages).where(messages.c.message_id == 1))
        ).one()
    assert row.content == "live"


async def test_realtime_upsert_updates_content_but_keeps_analysis_status(tmp_path):
    repo, engine = await _make_repo(tmp_path)
    await repo.apply_batch([WriteOp(OpKind.MESSAGE, _message("v1"))])
    updated = _message("v2")
    updated.analysis_status = "done"
    await repo.apply_batch([WriteOp(OpKind.MESSAGE, updated)])
    async with engine.connect() as conn:
        row = (
            await conn.execute(select(messages).where(messages.c.message_id == 1))
        ).one()
    assert row.content == "v2"
    assert row.analysis_status == "pending"


async def test_message_edit_increments_edit_count(tmp_path):
    repo, engine = await _make_repo(tmp_path)
    await repo.apply_batch([WriteOp(OpKind.MESSAGE, _message())])
    await repo.apply_batch(
        [
            WriteOp(OpKind.MESSAGE_EDIT, MessageEdit(1, "edited", "2026-11-11T01:00:00+00:00")),
            WriteOp(OpKind.MESSAGE_EDIT, MessageEdit(1, "edited2", "2026-11-11T02:00:00+00:00")),
        ]
    )
    async with engine.connect() as conn:
        row = (
            await conn.execute(select(messages).where(messages.c.message_id == 1))
        ).one()
    assert row.content == "edited2"
    assert row.edit_count == 2
    assert row.edited_at == "2026-11-11T02:00:00+00:00"


async def test_message_delete_is_soft(tmp_path):
    repo, engine = await _make_repo(tmp_path)
    await repo.apply_batch([WriteOp(OpKind.MESSAGE, _message("keep me"))])
    await repo.apply_batch(
        [WriteOp(OpKind.MESSAGE_DELETE, MessageDelete(1, "2026-11-11T03:00:00+00:00"))]
    )
    async with engine.connect() as conn:
        row = (
            await conn.execute(select(messages).where(messages.c.message_id == 1))
        ).one()
    assert row.deleted_at == "2026-11-11T03:00:00+00:00"
    assert row.content == "keep me"


async def test_reaction_upsert_clears_removed_at(tmp_path):
    repo, engine = await _make_repo(tmp_path)
    reaction = ReactionRecord(1, 30, "🔥", None, False, "2026-11-11T00:00:00+00:00")
    await repo.apply_batch([WriteOp(OpKind.REACTION, reaction)])
    removed = ReactionRecord(1, 30, "🔥", None, False, "2026-11-11T00:00:00+00:00")
    removed.removed_at = "2026-11-11T04:00:00+00:00"
    await repo.apply_batch([WriteOp(OpKind.REACTION, removed)])
    await repo.apply_batch(
        [WriteOp(OpKind.REACTION, reaction)]
    )
    async with engine.connect() as conn:
        rows = (await conn.execute(select(reactions))).all()
    assert len(rows) == 1
    assert rows[0].removed_at is None


async def test_user_snapshot_is_upserted(tmp_path):
    repo, engine = await _make_repo(tmp_path)
    snapshot = UserSnapshot(
        user_id=30,
        username="alice",
        global_name="Alice",
        display_name="alice#1",
        discriminator="0",
        avatar_url=None,
        is_bot=False,
        account_created_at=None,
        guild_joined_at=None,
        roles_json="[]",
        updated_at="2026-11-11T00:00:00+00:00",
    )
    await repo.apply_batch([WriteOp(OpKind.USER, snapshot)])
    renamed = UserSnapshot(**{**snapshot.__dict__, "username": "alice2"})
    await repo.apply_batch([WriteOp(OpKind.USER, renamed)])
    async with engine.connect() as conn:
        rows = (await conn.execute(select(users))).all()
    assert len(rows) == 1
    assert rows[0].username == "alice2"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/unit/test_repository.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'foresight.storage.db'`

- [ ] **Step 3: 实现 db.py**

创建 `src/foresight/storage/db.py`：

```python
"""Async engine construction and schema bootstrap."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from foresight.config import DatabaseConfig
from foresight.storage.tables import metadata


def create_engine(config: DatabaseConfig) -> AsyncEngine:
    """Create an async engine for the configured backend."""
    url = config.sqlalchemy_url
    if config.backend == "sqlite":
        path = Path(config.path)
        if path.parent != Path(""):
            path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_async_engine(url, future=True, pool_pre_ping=True)
    if config.backend == "sqlite":
        _apply_sqlite_pragmas(engine)
    return engine


def _apply_sqlite_pragmas(engine: AsyncEngine) -> None:
    @event.listens_for(engine.sync_engine, "connect")
    def _set_pragmas(dbapi_connection, connection_record):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()


async def init_db(engine: AsyncEngine) -> None:
    """Create every table that does not exist yet."""
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
```

- [ ] **Step 4: 实现 repository.py**

创建 `src/foresight/storage/repository.py`：

```python
"""The only module that knows SQL."""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any, Callable, Sequence

from sqlalchemy import func, select, update
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from foresight.domain.models import (
    AttachmentRecord,
    AttachmentStatus,
    ChannelSnapshot,
    GuildSnapshot,
    MessageAnalysis,
    MessageDelete,
    MessageEdit,
    MessageRecord,
    OpKind,
    ReactionRecord,
    ReactionRemove,
    UserSnapshot,
    WriteOp,
)
from foresight.storage.tables import (
    attachments,
    channels,
    guilds,
    message_analysis,
    messages,
    reactions,
    users,
)

MESSAGE_UPDATABLE = (
    "content",
    "edited_at",
    "edit_count",
    "deleted_at",
    "mentions_count",
    "char_count",
    "word_count",
    "attachment_count",
    "attachments_json",
    "embeds_json",
    "message_type",
    "reply_to_message_id",
)


class Repository:
    """Batched, dialect-aware write access."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._dialect = engine.dialect.name
        self._handlers: dict[str, Callable[[AsyncConnection, list[Any]], Any]] = {
            OpKind.MESSAGE: self._handle_message,
            OpKind.MESSAGE_INSERT_ONLY: self._handle_message_insert_only,
            OpKind.MESSAGE_EDIT: self._handle_message_edit,
            OpKind.MESSAGE_DELETE: self._handle_message_delete,
            OpKind.REACTION: self._handle_reaction,
            OpKind.REACTION_REMOVE: self._handle_reaction_remove,
            OpKind.USER: self._handle_user,
            OpKind.CHANNEL: self._handle_channel,
            OpKind.GUILD: self._handle_guild,
            OpKind.ATTACHMENT: self._handle_attachment,
            OpKind.ATTACHMENT_STATUS: self._handle_attachment_status,
            OpKind.ANALYSIS_STATUS: self._handle_analysis_status,
            OpKind.ANALYSIS: self._handle_analysis,
        }

    async def apply_batch(self, ops: Sequence[WriteOp]) -> None:
        """Apply a batch of operations inside a single transaction."""
        if not ops:
            return
        groups: dict[str, list[Any]] = defaultdict(list)
        for op in ops:
            groups[op.kind].append(op.payload)
        async with self._engine.begin() as conn:
            for kind, payloads in groups.items():
                await self._handlers[kind](conn, payloads)

    async def insert_attachment(self, record: AttachmentRecord) -> int:
        """Insert one attachment row and return its primary key."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                attachments.insert().values(
                    message_id=record.message_id,
                    attachment_id=record.attachment_id,
                    filename=record.filename,
                    content_type=record.content_type,
                    size=record.size,
                    url=record.url,
                    storage_backend=record.storage_backend,
                    status=record.status,
                    captured_at=record.captured_at,
                )
            )
            return int(result.inserted_primary_key[0])

    # -- insert helpers -------------------------------------------------

    def _insert(self, table):  # noqa: ANN001, ANN202
        if self._dialect == "postgresql":
            return pg_insert(table)
        if self._dialect == "mysql":
            return mysql_insert(table)
        return sqlite_insert(table)

    def _upsert(
        self,
        table,  # noqa: ANN001
        values: list[dict[str, Any]],
        conflict_columns: list[str],
        update_columns: tuple[str, ...],
    ):  # noqa: ANN202
        stmt = self._insert(table).values(values)
        if self._dialect == "mysql":
            return stmt.on_duplicate_key_update(
                **{col: stmt.inserted[col] for col in update_columns}
            )
        return stmt.on_conflict_do_update(
            index_elements=conflict_columns,
            set_={col: stmt.inserted[col] for col in update_columns},
        )

    def _insert_only(self, table, values: list[dict[str, Any]]):  # noqa: ANN001, ANN202
        stmt = self._insert(table).values(values)
        if self._dialect == "mysql":
            primary_key = list(table.primary_key.columns)[0]
            return stmt.on_duplicate_key_update(
                **{primary_key.name: stmt.inserted[primary_key.name]}
            )
        return stmt.on_conflict_do_nothing()

    # -- handlers -------------------------------------------------------

    async def _handle_message(self, conn: AsyncConnection, records: list[MessageRecord]) -> None:
        values = [self._message_values(r) for r in records]
        stmt = self._upsert(
            messages,
            values,
            ["message_id"],
            MESSAGE_UPDATABLE,
        )
        await conn.execute(stmt)

    async def _handle_message_insert_only(
        self, conn: AsyncConnection, records: list[MessageRecord]
    ) -> None:
        values = [self._message_values(r) for r in records]
        await conn.execute(self._insert_only(messages, values))

    async def _handle_message_edit(self, conn: AsyncConnection, edits: list[MessageEdit]) -> None:
        for edit in edits:
            await conn.execute(
                update(messages)
                .where(messages.c.message_id == edit.message_id)
                .values(
                    content=edit.content,
                    edited_at=edit.edited_at,
                    edit_count=messages.c.edit_count + 1,
                )
            )

    async def _handle_message_delete(
        self, conn: AsyncConnection, deletes: list[MessageDelete]
    ) -> None:
        for deletion in deletes:
            await conn.execute(
                update(messages)
                .where(messages.c.message_id == deletion.message_id)
                .values(deleted_at=deletion.deleted_at)
            )

    async def _handle_reaction(
        self, conn: AsyncConnection, records: list[ReactionRecord]
    ) -> None:
        values = [
            {
                "message_id": r.message_id,
                "user_id": r.user_id,
                "emoji": r.emoji,
                "emoji_id": r.emoji_id,
                "is_custom": r.is_custom,
                "added_at": r.added_at,
                "removed_at": r.removed_at,
            }
            for r in records
        ]
        stmt = self._upsert(
            reactions,
            values,
            ["message_id", "user_id", "emoji"],
            ("emoji_id", "is_custom", "added_at", "removed_at"),
        )
        await conn.execute(stmt)

    async def _handle_reaction_remove(
        self, conn: AsyncConnection, removes: list[ReactionRemove]
    ) -> None:
        for remove in removes:
            await conn.execute(
                update(reactions)
                .where(
                    (reactions.c.message_id == remove.message_id)
                    & (reactions.c.user_id == remove.user_id)
                    & (reactions.c.emoji == remove.emoji)
                )
                .values(removed_at=remove.removed_at)
            )

    async def _handle_user(self, conn: AsyncConnection, records: list[UserSnapshot]) -> None:
        values = [
            {
                "user_id": r.user_id,
                "username": r.username,
                "global_name": r.global_name,
                "display_name": r.display_name,
                "discriminator": r.discriminator,
                "avatar_url": r.avatar_url,
                "is_bot": r.is_bot,
                "account_created_at": r.account_created_at,
                "guild_joined_at": r.guild_joined_at,
                "roles_json": r.roles_json,
                "updated_at": r.updated_at,
            }
            for r in records
        ]
        stmt = self._upsert(
            users,
            values,
            ["user_id"],
            (
                "username",
                "global_name",
                "display_name",
                "discriminator",
                "avatar_url",
                "is_bot",
                "account_created_at",
                "guild_joined_at",
                "roles_json",
                "updated_at",
            ),
        )
        await conn.execute(stmt)

    async def _handle_channel(
        self, conn: AsyncConnection, records: list[ChannelSnapshot]
    ) -> None:
        values = [
            {
                "channel_id": r.channel_id,
                "guild_id": r.guild_id,
                "name": r.name,
                "topic": r.topic,
                "channel_type": r.channel_type,
                "position": r.position,
                "captured_at": r.captured_at,
            }
            for r in records
        ]
        stmt = self._upsert(
            channels,
            values,
            ["channel_id"],
            ("guild_id", "name", "topic", "channel_type", "position", "captured_at"),
        )
        await conn.execute(stmt)

    async def _handle_guild(
        self, conn: AsyncConnection, records: list[GuildSnapshot]
    ) -> None:
        values = [
            {
                "guild_id": r.guild_id,
                "name": r.name,
                "member_count": r.member_count,
                "captured_at": r.captured_at,
            }
            for r in records
        ]
        stmt = self._upsert(
            guilds,
            values,
            ["guild_id"],
            ("name", "member_count", "captured_at"),
        )
        await conn.execute(stmt)

    async def _handle_attachment(
        self, conn: AsyncConnection, records: list[AttachmentRecord]
    ) -> None:
        values = [
            {
                "message_id": r.message_id,
                "attachment_id": r.attachment_id,
                "filename": r.filename,
                "content_type": r.content_type,
                "size": r.size,
                "url": r.url,
                "storage_backend": r.storage_backend,
                "status": r.status,
                "captured_at": r.captured_at,
            }
            for r in records
        ]
        await conn.execute(attachments.insert().values(values))

    async def _handle_attachment_status(
        self, conn: AsyncConnection, records: list[AttachmentStatus]
    ) -> None:
        for record in records:
            await conn.execute(
                update(attachments)
                .where(
                    (attachments.c.message_id == record.message_id)
                    & (attachments.c.attachment_id == record.attachment_id)
                )
                .values(
                    status=record.status,
                    error=record.error,
                    sha256=record.sha256,
                    storage_key=record.storage_key,
                    storage_uri=record.storage_uri,
                    stored_at=record.stored_at,
                )
            )

    async def _handle_analysis_status(
        self, conn: AsyncConnection, records: list[tuple[int, str]]
    ) -> None:
        for message_id, status in records:
            await conn.execute(
                update(messages)
                .where(messages.c.message_id == message_id)
                .values(analysis_status=status)
            )

    async def _handle_analysis(
        self, conn: AsyncConnection, records: list[MessageAnalysis]
    ) -> None:
        values = [
            {
                "message_id": r.message_id,
                "translated_text": r.translated_text,
                "sentiment": r.sentiment,
                "sentiment_score": r.sentiment_score,
                "issues_json": r.issues_json,
                "model": r.model,
                "prompt_version": r.prompt_version,
                "latency_ms": r.latency_ms,
                "error": r.error,
                "analyzed_at": r.analyzed_at,
            }
            for r in records
        ]
        stmt = self._upsert(
            message_analysis,
            values,
            ["message_id"],
            (
                "translated_text",
                "sentiment",
                "sentiment_score",
                "issues_json",
                "model",
                "prompt_version",
                "latency_ms",
                "error",
                "analyzed_at",
            ),
        )
        await conn.execute(stmt)

    @staticmethod
    def _message_values(record: MessageRecord) -> dict[str, Any]:
        return {
            "message_id": record.message_id,
            "guild_id": record.guild_id,
            "channel_id": record.channel_id,
            "author_id": record.author_id,
            "content": record.content,
            "message_type": record.message_type,
            "created_at": record.created_at,
            "captured_at": record.captured_at,
            "edited_at": record.edited_at,
            "edit_count": record.edit_count,
            "deleted_at": record.deleted_at,
            "reply_to_message_id": record.reply_to_message_id,
            "mentions_count": record.mentions_count,
            "char_count": record.char_count,
            "word_count": record.word_count,
            "attachment_count": record.attachment_count,
            "attachments_json": record.attachments_json,
            "embeds_json": record.embeds_json,
            "is_bot": record.is_bot,
            "analysis_status": record.analysis_status,
        }
```

说明：`_handle_analysis_status` 的 payload 统一为 `(message_id, status)` 元组，M3 的分析 worker 依赖这个约定。

- [ ] **Step 5: 运行测试确认通过**

Run: `uv run pytest tests/unit/test_repository.py -v`
Expected: PASS —— 7 passed

- [ ] **Step 6: 提交**

```bash
git add src/foresight/storage/db.py src/foresight/storage/repository.py tests/unit/test_repository.py
git commit -m "feat: async engine and batched repository"
```

---

## Task 4: Discord 事件映射

**Files:**
- Create: `src/foresight/adapters/__init__.py`
- Create: `src/foresight/adapters/discord_mapper.py`
- Create: `tests/unit/test_discord_mapper.py`

**Interfaces:**
- Consumes: `foresight.domain.models`（Task 2）
- Produces:
  - `map_message(message: Any, captured_at: datetime | None = None) -> MessageRecord`
  - `map_user(author: Any, updated_at: datetime | None = None) -> UserSnapshot`
  - `map_channel(channel: Any, captured_at: datetime | None = None) -> ChannelSnapshot`
  - `map_guild(guild: Any, captured_at: datetime | None = None) -> GuildSnapshot`
  - `map_reaction(payload: Any, removed: bool = False) -> ReactionRecord`
  - `map_attachments(message: Any, captured_at: datetime | None = None) -> list[AttachmentRecord]`
  - `count_words(text: str) -> int`
  - `iso(dt: datetime | None) -> str | None`

- [ ] **Step 1: 写失败测试**

创建 `tests/unit/test_discord_mapper.py`：

```python
from datetime import datetime, timezone
from types import SimpleNamespace

from foresight.adapters.discord_mapper import (
    count_words,
    iso,
    map_attachments,
    map_channel,
    map_guild,
    map_message,
    map_reaction,
    map_user,
)


def _author(user_id: int = 30, bot: bool = False):
    return SimpleNamespace(
        id=user_id,
        name="alice",
        global_name="Alice",
        display_name="alice!",
        discriminator="0",
        bot=bot,
        avatar=SimpleNamespace(url="https://cdn/avatar.png"),
        created_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        joined_at=datetime(2021, 1, 1, tzinfo=timezone.utc),
        roles=[SimpleNamespace(id=5), SimpleNamespace(id=6)],
    )


def _channel(channel_id: int = 20):
    return SimpleNamespace(
        id=channel_id,
        name="general",
        topic="hello",
        type="text",
        position=3,
        guild=SimpleNamespace(id=10, name="Foresight", member_count=42),
    )


def _message(content: str = "hello", edited_at=None):
    return SimpleNamespace(
        id=100,
        content=content,
        type=0,
        created_at=datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc),
        edited_at=edited_at,
        author=_author(),
        channel=_channel(),
        guild=SimpleNamespace(id=10, name="Foresight", member_count=42),
        reference=SimpleNamespace(message_id=99),
        mentions=[SimpleNamespace(id=31)],
        attachments=[],
        embeds=[],
    )


def test_map_message_extracts_fields():
    record = map_message(_message())
    assert record.message_id == 100
    assert record.guild_id == 10
    assert record.channel_id == 20
    assert record.author_id == 30
    assert record.content == "hello"
    assert record.reply_to_message_id == 99
    assert record.mentions_count == 1
    assert record.char_count == 5
    assert record.is_bot is False
    assert record.analysis_status == "pending"


def test_map_message_without_reference_has_no_reply():
    message = _message()
    message.reference = None
    assert map_message(message).reply_to_message_id is None


def test_map_message_counts_cjk_words():
    record = map_message(_message("你好世界"))
    assert record.char_count == 4
    assert record.word_count == 4


def test_map_user_snapshot():
    snapshot = map_user(_author())
    assert snapshot.user_id == 30
    assert snapshot.username == "alice"
    assert snapshot.global_name == "Alice"
    assert snapshot.avatar_url == "https://cdn/avatar.png"
    assert snapshot.guild_joined_at == "2021-01-01T00:00:00+00:00"
    assert '"5"' in snapshot.roles_json


def test_map_channel_and_guild():
    channel = map_channel(_channel())
    assert channel.channel_id == 20
    assert channel.guild_id == 10
    assert channel.name == "general"
    guild = map_guild(SimpleNamespace(id=10, name="Foresight", member_count=42))
    assert guild.guild_id == 10
    assert guild.member_count == 42


def test_map_reaction_unicode_and_custom():
    payload = SimpleNamespace(
        message_id=100,
        user_id=31,
        emoji=SimpleNamespace(name="🔥", id=None, is_custom_emoji=lambda: False),
    )
    record = map_reaction(payload)
    assert record.emoji == "🔥"
    assert record.emoji_id is None
    assert record.is_custom is False

    custom = SimpleNamespace(
        message_id=100,
        user_id=31,
        emoji=SimpleNamespace(name="peepo", id=555, is_custom_emoji=lambda: True),
    )
    record = map_reaction(custom)
    assert record.emoji == "peepo"
    assert record.emoji_id == 555
    assert record.is_custom is True


def test_map_attachments():
    message = _message()
    message.attachments = [
        SimpleNamespace(
            id=1,
            filename="a.png",
            content_type="image/png",
            size=100,
            url="https://cdn/a.png",
        )
    ]
    records = map_attachments(message)
    assert len(records) == 1
    assert records[0].attachment_id == 1
    assert records[0].filename == "a.png"
    assert records[0].status == "pending"


def test_count_words_mixed_language():
    assert count_words("hello world") == 2
    assert count_words("你好世界") == 4
    assert count_words("hello 世界") == 3
    assert count_words("") == 0


def test_iso_handles_naive_datetime():
    assert iso(datetime(2026, 1, 1, 0, 0)) == "2026-01-01T00:00:00+00:00"
    assert iso(None) is None
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/unit/test_discord_mapper.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'foresight.adapters'`

- [ ] **Step 3: 实现映射层**

创建 `src/foresight/adapters/__init__.py`（空文件）：

```python
```

创建 `src/foresight/adapters/discord_mapper.py`：

```python
"""Translate Discord SDK objects into domain records.

This is the only module that may mention Discord field names.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from foresight.domain.models import (
    AttachmentRecord,
    ChannelSnapshot,
    GuildSnapshot,
    MessageRecord,
    ReactionRecord,
    UserSnapshot,
)

_CJK_PATTERN = re.compile(
    r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]"
)


def iso(dt: datetime | None) -> str | None:
    """Render a datetime as a UTC ISO-8601 string."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def count_words(text: str) -> int:
    """Count words: CJK characters count individually, other runs count per word."""
    if not text:
        return 0
    cjk = len(_CJK_PATTERN.findall(text))
    rest = _CJK_PATTERN.sub(" ", text)
    latin = len([chunk for chunk in re.split(r"\s+", rest) if chunk])
    return cjk + latin


def _avatar_url(author: Any) -> str | None:
    avatar = getattr(author, "avatar", None)
    if avatar is None:
        return None
    url = getattr(avatar, "url", None)
    return str(url) if url else None


def _display_name(author: Any) -> str | None:
    name = getattr(author, "display_name", None)
    if name:
        return str(name)
    raw = getattr(author, "name", None)
    return str(raw) if raw else None


def map_user(author: Any, updated_at: datetime | None = None) -> UserSnapshot:
    now = updated_at or _now()
    roles = [str(getattr(role, "id")) for role in (getattr(author, "roles", None) or [])]
    return UserSnapshot(
        user_id=author.id,
        username=str(getattr(author, "name", "") or ""),
        global_name=getattr(author, "global_name", None),
        display_name=_display_name(author),
        discriminator=getattr(author, "discriminator", None),
        avatar_url=_avatar_url(author),
        is_bot=bool(getattr(author, "bot", False)),
        account_created_at=iso(getattr(author, "created_at", None)),
        guild_joined_at=iso(getattr(author, "joined_at", None)),
        roles_json=json.dumps(roles),
        updated_at=iso(now),
    )


def map_channel(channel: Any, captured_at: datetime | None = None) -> ChannelSnapshot:
    now = captured_at or _now()
    guild = getattr(channel, "guild", None)
    channel_type = getattr(channel, "type", None)
    return ChannelSnapshot(
        channel_id=channel.id,
        guild_id=getattr(guild, "id", None),
        name=getattr(channel, "name", None),
        topic=getattr(channel, "topic", None),
        channel_type=str(channel_type) if channel_type is not None else None,
        position=getattr(channel, "position", None),
        captured_at=iso(now),
    )


def map_guild(guild: Any, captured_at: datetime | None = None) -> GuildSnapshot:
    now = captured_at or _now()
    return GuildSnapshot(
        guild_id=guild.id,
        name=getattr(guild, "name", None),
        member_count=getattr(guild, "member_count", None),
        captured_at=iso(now),
    )


def _attachment_summary(attachment: Any) -> dict[str, Any]:
    return {
        "id": getattr(attachment, "id", None),
        "filename": getattr(attachment, "filename", None),
        "content_type": getattr(attachment, "content_type", None),
        "size": getattr(attachment, "size", None),
        "url": getattr(attachment, "url", None),
    }


def map_message(message: Any, captured_at: datetime | None = None) -> MessageRecord:
    now = captured_at or _now()
    content = message.content or ""
    author = message.author
    channel = message.channel
    guild = getattr(message, "guild", None)
    reference = getattr(message, "reference", None)
    attachments = list(getattr(message, "attachments", None) or [])
    embeds = list(getattr(message, "embeds", None) or [])
    message_type = getattr(message, "type", 0)
    return MessageRecord(
        message_id=message.id,
        guild_id=getattr(guild, "id", None),
        channel_id=channel.id,
        author_id=author.id,
        content=content,
        message_type=int(message_type or 0),
        created_at=iso(message.created_at),
        captured_at=iso(now),
        edited_at=iso(getattr(message, "edited_at", None)),
        edit_count=0,
        deleted_at=None,
        reply_to_message_id=getattr(reference, "message_id", None),
        mentions_count=len(list(getattr(message, "mentions", None) or [])),
        char_count=len(content),
        word_count=count_words(content),
        attachment_count=len(attachments),
        attachments_json=json.dumps(
            [_attachment_summary(a) for a in attachments], ensure_ascii=False
        ),
        embeds_json=json.dumps(
            [embed.to_dict() for embed in embeds], ensure_ascii=False
        ),
        is_bot=bool(getattr(author, "bot", False)),
        analysis_status="pending",
    )


def map_attachments(
    message: Any, captured_at: datetime | None = None, storage_backend: str = "local"
) -> list[AttachmentRecord]:
    now = captured_at or _now()
    records: list[AttachmentRecord] = []
    for attachment in list(getattr(message, "attachments", None) or []):
        records.append(
            AttachmentRecord(
                message_id=message.id,
                attachment_id=getattr(attachment, "id", None),
                filename=getattr(attachment, "filename", None),
                content_type=getattr(attachment, "content_type", None),
                size=getattr(attachment, "size", None),
                url=str(getattr(attachment, "url", "") or ""),
                storage_backend=storage_backend,
                status="pending",
                captured_at=iso(now),
            )
        )
    return records


def _emoji_info(emoji: Any) -> tuple[str, int | None, bool]:
    name = getattr(emoji, "name", "") or ""
    emoji_id = getattr(emoji, "id", None)
    is_custom = False
    checker = getattr(emoji, "is_custom_emoji", None)
    if callable(checker):
        is_custom = bool(checker())
    if emoji_id is not None:
        is_custom = True
    return str(name), emoji_id, is_custom


def map_reaction(payload: Any, removed: bool = False) -> ReactionRecord:
    name, emoji_id, is_custom = _emoji_info(payload.emoji)
    now = _now()
    return ReactionRecord(
        message_id=payload.message_id,
        user_id=payload.user_id,
        emoji=name,
        emoji_id=emoji_id,
        is_custom=is_custom,
        added_at=iso(now),
        removed_at=iso(now) if removed else None,
    )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/unit/test_discord_mapper.py -v`
Expected: PASS —— 9 passed

- [ ] **Step 5: 提交**

```bash
git add src/foresight/adapters tests/unit/test_discord_mapper.py
git commit -m "feat: map Discord objects to domain records"
```

---

## Task 5: 写入队列（Writer）

**Files:**
- Create: `src/foresight/writer.py`
- Create: `tests/unit/test_writer.py`

**Interfaces:**
- Consumes: `foresight.domain.models.WriteOp`（Task 2）、`foresight.storage.repository.Repository`（Task 3）
- Produces:
  - `foresight.writer.Writer(repo: Repository, *, batch_size: int = 100, flush_interval_ms: int = 50, queue_maxsize: int = 10000, failure_path: Path | None = None)`
  - `Writer.start() -> None`、`Writer.stop() -> None`、`Writer.submit(op: WriteOp) -> bool`、`Writer.queue_depth -> int`
  - `foresight.writer.recover_failed_batches(writer: Writer, path: Path | None) -> int`（重启时回放 `failed_batch.jsonl`）

- [ ] **Step 1: 写失败测试**

创建 `tests/unit/test_writer.py`：

```python
import asyncio
import json
from dataclasses import asdict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

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


async def _make_writer(tmp_path, repo=None):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    await init_db(engine)
    repository = repo or Repository(engine)
    writer = Writer(repository, batch_size=2, flush_interval_ms=10)
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
    writer, _ = await _make_writer(tmp_path)
    writer._queue.maxsize = 1  # noqa: SLF001
    writer.submit(WriteOp(OpKind.MESSAGE, _message()))
    assert writer.submit(WriteOp(OpKind.MESSAGE, _message())) is False
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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/unit/test_writer.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'foresight.writer'`

- [ ] **Step 3: 实现 Writer**

创建 `src/foresight/writer.py`：

```python
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
                await self._flush(batch)
            if self._stopping and self._queue.empty():
                return

    async def _flush(self, batch: Sequence[WriteOp]) -> None:
        for attempt in range(MAX_RETRIES):
            try:
                await self._repo.apply_batch(list(batch))
                return
            except Exception:
                logger.exception(
                    "batch write failed (attempt %d/%d)", attempt + 1, MAX_RETRIES
                )
                if attempt == MAX_RETRIES - 1:
                    break
                await asyncio.sleep(RETRY_DELAYS[attempt])
        self._persist_failure(batch)

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
    the replay succeeds, so a batch is never applied twice.
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
    await writer._flush(ops)  # noqa: SLF001
    path.write_text("", encoding="utf-8")
    logger.info("recovered %d failed write operations", len(ops))
    return len(ops)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/unit/test_writer.py -v`
Expected: PASS —— 4 passed

- [ ] **Step 5: 提交**

```bash
git add src/foresight/writer.py tests/unit/test_writer.py
git commit -m "feat: batched write queue with failure persistence"
```

---

## Task 6: 事件处理与 Bot 接线

**Files:**
- Create: `src/foresight/events.py`
- Create: `src/foresight/bot.py`
- Create: `tests/unit/test_events.py`

**Interfaces:**
- Consumes: `foresight.adapters.discord_mapper`（Task 4）、`foresight.writer.Writer`（Task 5）、`foresight.domain.models`（Task 2）
- Produces:
  - `foresight.events.EventHandler(config: Config, writer: Writer, media_sink: Callable[[AttachmentRecord], None] | None = None, bot_user_id: int | None = None)`
  - `EventHandler.handle_message(message) -> None`
  - `EventHandler.handle_raw_edit(payload) -> None`
  - `EventHandler.handle_raw_delete(payload) -> None`
  - `EventHandler.handle_raw_reaction_add(payload) -> None`
  - `EventHandler.handle_raw_reaction_remove(payload) -> None`
  - `foresight.bot.build_bot(config: Config, handler: EventHandler) -> commands.Bot`
  - `foresight.bot.build_intents() -> discord.Intents`

- [ ] **Step 1: 写失败测试**

创建 `tests/unit/test_events.py`：

```python
from datetime import datetime, timezone
from types import SimpleNamespace

from foresight.domain.models import OpKind
from foresight.events import EventHandler
from foresight.writer import Writer


class RecordingWriter:
    def __init__(self):
        self.ops = []

    def submit(self, op):
        self.ops.append(op)
        return True

    def kinds(self):
        return [op.kind for op in self.ops]


def _config(channel_ids=(20,)):
    return SimpleNamespace(discord=SimpleNamespace(channel_ids=tuple(channel_ids)),
                           media=SimpleNamespace(backend="local"))


def _author(user_id=30):
    return SimpleNamespace(
        id=user_id,
        name="alice",
        global_name="Alice",
        display_name="alice",
        discriminator="0",
        bot=False,
        avatar=None,
        created_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        joined_at=None,
        roles=[],
    )


def _message(channel_id=20, message_id=100, content="hello"):
    return SimpleNamespace(
        id=message_id,
        content=content,
        type=0,
        created_at=datetime(2026, 11, 11, tzinfo=timezone.utc),
        edited_at=None,
        author=_author(),
        channel=SimpleNamespace(
            id=channel_id,
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


def test_message_outside_whitelist_is_ignored():
    writer = RecordingWriter()
    handler = EventHandler(_config(), writer)
    handler.handle_message(_message(channel_id=999))
    assert writer.ops == []


def test_message_inside_whitelist_is_captured():
    writer = RecordingWriter()
    handler = EventHandler(_config(), writer)
    handler.handle_message(_message())
    kinds = writer.kinds()
    assert OpKind.MESSAGE in kinds
    assert OpKind.USER in kinds
    assert OpKind.CHANNEL in kinds
    assert OpKind.GUILD in kinds


def test_own_messages_are_skipped():
    writer = RecordingWriter()
    handler = EventHandler(_config(), writer, bot_user_id=30)
    handler.handle_message(_message())
    assert writer.ops == []


def test_raw_edit_queues_edit_op():
    writer = RecordingWriter()
    handler = EventHandler(_config(), writer)
    payload = SimpleNamespace(
        channel_id=20,
        message_id=100,
        data={"content": "edited", "edited_timestamp": "2026-11-11T01:00:00+00:00"},
    )
    handler.handle_raw_edit(payload)
    op = writer.ops[0]
    assert op.kind == OpKind.MESSAGE_EDIT
    assert op.payload.content == "edited"


def test_raw_edit_outside_whitelist_is_ignored():
    writer = RecordingWriter()
    handler = EventHandler(_config(), writer)
    payload = SimpleNamespace(
        channel_id=999,
        message_id=100,
        data={"content": "edited", "edited_timestamp": "2026-11-11T01:00:00+00:00"},
    )
    handler.handle_raw_edit(payload)
    assert writer.ops == []


def test_raw_delete_queues_delete_op():
    writer = RecordingWriter()
    handler = EventHandler(_config(), writer)
    payload = SimpleNamespace(channel_id=20, message_id=100, cached_message=None)
    handler.handle_raw_delete(payload)
    assert writer.ops[0].kind == OpKind.MESSAGE_DELETE


def test_reaction_add_and_remove():
    writer = RecordingWriter()
    handler = EventHandler(_config(), writer)
    payload = SimpleNamespace(
        message_id=100,
        user_id=31,
        channel_id=20,
        emoji=SimpleNamespace(name="🔥", id=None, is_custom_emoji=lambda: False),
    )
    handler.handle_raw_reaction_add(payload)
    handler.handle_raw_reaction_remove(payload)
    assert writer.kinds() == [OpKind.REACTION, OpKind.REACTION_REMOVE]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/unit/test_events.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'foresight.events'`

- [ ] **Step 3: 实现 events.py**

创建 `src/foresight/events.py`：

```python
"""Discord event handling.

All Discord-specific branching lives here so that the bot object stays a
thin registration layer and the logic stays unit testable.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable

from foresight.adapters.discord_mapper import (
    iso,
    map_attachments,
    map_channel,
    map_guild,
    map_message,
    map_reaction,
    map_user,
)
from foresight.domain.models import (
    AttachmentRecord,
    MessageDelete,
    MessageEdit,
    OpKind,
    ReactionRemove,
    WriteOp,
)

logger = logging.getLogger(__name__)


class EventHandler:
    """Turn Discord events into write operations."""

    def __init__(
        self,
        config: Any,
        writer: Any,
        media_sink: Callable[[AttachmentRecord], None] | None = None,
        bot_user_id: int | None = None,
    ) -> None:
        self._whitelist = set(config.discord.channel_ids)
        self._writer = writer
        self._media_sink = media_sink
        self._bot_user_id = bot_user_id
        self._media_backend = getattr(getattr(config, "media", None), "backend", "local")

    def _allowed(self, channel_id: int | None) -> bool:
        return channel_id is not None and channel_id in self._whitelist

    def handle_message(self, message: Any) -> None:
        try:
            channel_id = getattr(message.channel, "id", None)
            if not self._allowed(channel_id):
                return
            author_id = getattr(message.author, "id", None)
            if self._bot_user_id is not None and author_id == self._bot_user_id:
                return
            captured_at = datetime.now(timezone.utc)
            record = map_message(message, captured_at=captured_at)
            self._writer.submit(WriteOp(OpKind.MESSAGE, record))
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
            for attachment in map_attachments(
                message,
                captured_at=captured_at,
                storage_backend=self._media_backend,
            ):
                self._writer.submit(WriteOp(OpKind.ATTACHMENT, attachment))
                if self._media_sink is not None:
                    self._media_sink(attachment)
        except Exception:
            logger.exception("failed to handle message")

    def handle_raw_edit(self, payload: Any) -> None:
        try:
            if not self._allowed(getattr(payload, "channel_id", None)):
                return
            data = getattr(payload, "data", None) or {}
            content = data.get("content")
            if content is None:
                return
            edited_at = data.get("edited_timestamp") or iso(datetime.now(timezone.utc))
            self._writer.submit(
                WriteOp(
                    OpKind.MESSAGE_EDIT,
                    MessageEdit(payload.message_id, content, edited_at),
                )
            )
        except Exception:
            logger.exception("failed to handle message edit")

    def handle_raw_delete(self, payload: Any) -> None:
        try:
            if not self._allowed(getattr(payload, "channel_id", None)):
                return
            deleted_at = iso(datetime.now(timezone.utc))
            self._writer.submit(
                WriteOp(OpKind.MESSAGE_DELETE, MessageDelete(payload.message_id, deleted_at))
            )
        except Exception:
            logger.exception("failed to handle message delete")

    def handle_raw_reaction_add(self, payload: Any) -> None:
        try:
            if not self._allowed(getattr(payload, "channel_id", None)):
                return
            self._writer.submit(
                WriteOp(OpKind.REACTION, map_reaction(payload, removed=False))
            )
        except Exception:
            logger.exception("failed to handle reaction add")

    def handle_raw_reaction_remove(self, payload: Any) -> None:
        try:
            if not self._allowed(getattr(payload, "channel_id", None)):
                return
            record = map_reaction(payload, removed=False)
            self._writer.submit(
                WriteOp(
                    OpKind.REACTION_REMOVE,
                    ReactionRemove(
                        record.message_id,
                        record.user_id,
                        record.emoji,
                        record.removed_at or iso(datetime.now(timezone.utc)),
                    ),
                )
            )
        except Exception:
            logger.exception("failed to handle reaction remove")
```

- [ ] **Step 4: 实现 bot.py**

创建 `src/foresight/bot.py`：

```python
"""discord.py wiring: intents, event registration, startup."""

from __future__ import annotations

import logging
import os
from typing import Any

import discord
from discord.ext import commands

from foresight.events import EventHandler

logger = logging.getLogger(__name__)


def build_intents() -> discord.Intents:
    intents = discord.Intents.default()
    intents.messages = True
    intents.message_content = True
    intents.reactions = True
    intents.guilds = True
    return intents


def build_bot(config: Any, handler: EventHandler) -> commands.Bot:
    bot = commands.Bot(command_prefix="!foresight ", intents=build_intents())

    @bot.event
    async def on_message(message: discord.Message) -> None:
        if bot.user is not None and message.author.id == bot.user.id:
            return
        await handler.handle_message(message)

    @bot.event
    async def on_raw_message_edit(payload: discord.RawMessageUpdateEvent) -> None:
        await handler.handle_raw_edit(payload)

    @bot.event
    async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent) -> None:
        await handler.handle_raw_delete(payload)

    @bot.event
    async def on_raw_reaction_add(payload: discord.RawReactionActionEvent) -> None:
        await handler.handle_raw_reaction_add(payload)

    @bot.event
    async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent) -> None:
        await handler.handle_raw_reaction_remove(payload)

    return bot


def require_token() -> str:
    token = os.environ.get("DISCORD_TOKEN", "").strip()
    if not token:
        raise RuntimeError("DISCORD_TOKEN environment variable is required")
    return token
```

- [ ] **Step 5: 运行测试确认通过**

Run: `uv run pytest tests/unit/test_events.py -v`
Expected: PASS —— 7 passed

Run: `uv run pytest -v`
Expected: 全部通过（40+ 用例）

- [ ] **Step 6: 提交**

```bash
git add src/foresight/events.py src/foresight/bot.py tests/unit/test_events.py
git commit -m "feat: Discord event handling and bot wiring"
```

---

## Task 7: CLI 骨架与 run / init-db 命令

**Files:**
- Create: `src/foresight/cli.py`
- Create: `src/foresight/__main__.py`
- Create: `tests/unit/test_cli.py`
- Create: `config.example.toml`

**Interfaces:**
- Consumes: `foresight.config.load_config`（Task 1）、`foresight.bot`（Task 6）、`foresight.storage.db`（Task 3）、`foresight.writer.Writer`（Task 5）、`foresight.events.EventHandler`（Task 6）
- Produces:
  - `foresight.cli.app`（typer.Typer 实例）
  - `foresight.cli.main() -> None`
  - 命令：`foresight run`、`foresight init-db`（M2 会加 `serve` / `both`，M3 加 `backfill`，M4 加 `query` / `stats`）

- [ ] **Step 1: 写失败测试**

创建 `tests/unit/test_cli.py`：

```python
from pathlib import Path

from typer.testing import CliRunner

from foresight.cli import app

runner = CliRunner()


def test_init_db_creates_sqlite_file(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"""
[discord]
channel_ids = [1]

[database]
backend = "sqlite"
path = "{tmp_path / 'data' / 'foresight.db'}"
""",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["init-db", "--config", str(config_path)])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "data" / "foresight.db").exists()


def test_init_db_fails_when_config_missing(tmp_path: Path):
    result = runner.invoke(app, ["init-db", "--config", str(tmp_path / "nope.toml")])
    assert result.exit_code != 0
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/unit/test_cli.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'foresight.cli'`

- [ ] **Step 3: 实现 cli.py**

创建 `src/foresight/cli.py`：

```python
"""Command line entry points."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional

import typer

from foresight.config import Config, ConfigError, load_config
from foresight.logging_setup import setup_logging

logger = logging.getLogger(__name__)

app = typer.Typer(add_completion=False, help="ForeSight — Discord message dump service")


def _load(config_path: Optional[str]) -> Config:
    config = load_config(config_path)
    setup_logging(config.logging.level)
    return config


@app.command()
def init_db(
    config: Optional[str] = typer.Option(None, "--config", help="Path to config.toml"),
) -> None:
    """Create the database schema."""
    from foresight.storage.db import create_engine, init_db as create_tables

    settings = _load(config)
    engine = create_engine(settings.database)
    asyncio.run(create_tables(engine))
    typer.echo(f"schema ready at {settings.database.path}")


@app.command()
def run(
    config: Optional[str] = typer.Option(None, "--config", help="Path to config.toml"),
) -> None:
    """Start the Discord collector."""
    from foresight.bot import build_bot, require_token
    from foresight.events import EventHandler
    from foresight.storage.db import create_engine, init_db as create_tables
    from foresight.storage.repository import Repository
    from foresight.writer import Writer, recover_failed_batches

    settings = _load(config)
    if not settings.discord.channel_ids:
        raise typer.BadParameter("[discord].channel_ids must not be empty")

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
        try:
            await bot.start(require_token())
        finally:
            await writer.stop()
            await engine.dispose()

    asyncio.run(_run())


def main() -> None:
    try:
        app()
    except ConfigError as exc:
        typer.secho(f"configuration error: {exc}", fg=typer.colors.RED, err=True)
        raise SystemExit(2)
```

创建 `src/foresight/__main__.py`：

```python
from foresight.cli import main

if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 创建 config.example.toml**

创建 `config.example.toml`：

```toml
# Copy to config.toml and adjust. Secrets belong in environment variables,
# never in this file.

[discord]
# Whitelist of channel IDs to capture.
channel_ids = [123456789012345678]

[database]
backend = "sqlite"          # sqlite | postgresql | mysql
path = "data/foresight.db"
# host = "localhost"
# port = 5432
# database = "foresight"
# user = "foresight"

[media]
backend = "local"            # local | s3
local_root = "data/media"
max_download_size_mb = 100
download_concurrency = 3

[media.s3]
bucket = ""
endpoint_url = ""
region = "auto"
prefix = "foresight"
use_path_style = true

[api]
host = "127.0.0.1"
port = 8000
metrics_token = ""

[ai]
enabled = false
base_url = "https://api.openai.com/v1"
model = "gpt-4o-mini"
translation_target = "zh-CN"
max_concurrency = 2
timeout_seconds = 30

[writer]
batch_size = 100
flush_interval_ms = 50
queue_maxsize = 10000

[logging]
level = "INFO"
```

- [ ] **Step 5: 运行测试确认通过**

Run: `uv run pytest tests/unit/test_cli.py -v`
Expected: PASS —— 2 passed

Run: `uv run pytest -v`
Expected: 全部通过

- [ ] **Step 6: 提交**

```bash
git add src/foresight/cli.py src/foresight/__main__.py config.example.toml tests/unit/test_cli.py
git commit -m "feat: CLI entry points with run and init-db"
```

---

## M1 验收

M1 完成的判定标准：

```bash
uv run pytest -v          # 全部通过
uv run foresight init-db --config config.example.toml
```

`init-db` 成功且生成 `data/foresight.db`，即表示采集内核可用。真实 Discord 联调需要用户在 Discord 开发者后台开启 MESSAGE CONTENT INTENT 并提供 `DISCORD_TOKEN`（M4 的 README 会给出完整步骤）。

接下来执行 `docs/superpowers/plans/2026-11-11-foresight-m2-query-and-metrics.md`。
