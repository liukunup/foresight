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
