"""Configuration loading.

Secrets never live in the TOML file; they are read from environment
variables. Environment variables listed in the design spec override the
matching TOML keys.
"""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

DatabaseBackend = Literal["sqlite", "postgresql", "mysql"]
MediaBackend = Literal["local", "s3"]

DEFAULT_CONFIG_PATH = "config.toml"


def sqlite_readonly_uri(path: str) -> str:
    """The URI that opens a SQLite database read-only.

    The path is escaped, so that a path that carries a `?` or a `#` does
    not break the URI: those two characters start the query string and
    the fragment of a URI, and the driver would read the rest of the
    path as a parameter of the connection.
    """
    return f"file:{quote(path, safe='/')}?mode=ro"


def sqlite_readonly_url(path: str) -> str:
    """The URL of the engine that opens a SQLite database read-only.

    The path is escaped twice: the first escaping is for the URI of
    SQLite, the second one for the URL of SQLAlchemy — which unquotes
    the part of the URL it hands to the driver. Without the second
    escaping the driver would read the `?` of the path as the start of
    the query string of the URI, and would open another database.
    """
    escaped = quote(quote(path, safe="/"), safe="/")
    return f"sqlite+aiosqlite:///file:{escaped}?mode=ro&uri=true"


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
                f"postgresql+asyncpg://{quote(self.user, safe='')}:"
                f"{quote(self.password, safe='')}"
                f"@{self.host}:{self.port}/{self.database}"
            )
        return (
            f"mysql+asyncmy://{quote(self.user, safe='')}:"
            f"{quote(self.password, safe='')}"
            f"@{self.host}:{self.port}/{self.database}"
        )

    @property
    def readonly_sqlalchemy_url(self) -> str:
        """URL to use when the database must only be read from.

        For SQLite this is a ``file:...?mode=ro`` URI, so the driver itself
        rejects any write statement. PostgreSQL and MySQL have no equivalent
        URI-level switch, so for those backends the normal
        :attr:`sqlalchemy_url` is returned and read-only access is enforced
        at query time by a read-only transaction in the query runner.
        """
        if self.backend == "sqlite":
            return sqlite_readonly_url(self.path)
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
    metrics_refresh_seconds: int = 30


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


def _as_int(value: Any, key: str) -> int:
    """Coerce ``value`` to ``int``, reporting failures as :class:`ConfigError`."""
    if isinstance(value, bool):
        raise ConfigError(f"invalid value for {key}: {value!r} (expected an integer)")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(
            f"invalid value for {key}: {value!r} (expected an integer)"
        ) from exc


def _as_str(value: Any, key: str) -> str:
    """Validate that ``value`` is a string, reporting failures as a
    :class:`ConfigError`."""
    if not isinstance(value, str):
        raise ConfigError(f"invalid value for {key}: {value!r} (expected a string)")
    return value


def _as_bool(value: Any, key: str) -> bool:
    """Validate that ``value`` is a boolean, reporting failures as :class:`ConfigError`.

    Strings are rejected on purpose: ``"false"`` is truthy in Python, so
    accepting it would silently enable a feature the operator meant to disable.
    """
    if not isinstance(value, bool):
        raise ConfigError(f"invalid value for {key}: {value!r} (expected a boolean)")
    return value


def _as_int_list(value: Any, key: str) -> tuple[int, ...]:
    """Coerce a sequence of values to a tuple of ints."""
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise ConfigError(
            f"invalid value for {key}: {value!r} (expected a list of integers)"
        )
    return tuple(_as_int(item, f"{key}[{index}]") for index, item in enumerate(value))


def _as_log_level(value: Any, key: str = "logging.level") -> str:
    """Normalise and validate a logging level name."""
    level = _as_str(value, key).strip().upper()
    if level not in logging.getLevelNamesMapping():
        raise ConfigError(f"invalid value for {key}: {value!r} (unknown log level)")
    return level


def load_config(path: str | Path | None = None) -> Config:
    """Load configuration from TOML plus environment overrides.

    Every problem with the configuration (missing file, malformed TOML,
    unusable value) is reported as :class:`ConfigError`.
    """
    config_path = Path(path or os.environ.get("FORESIGHT_CONFIG", DEFAULT_CONFIG_PATH))
    if not config_path.exists():
        raise ConfigError(f"config file not found: {config_path}")
    try:
        with config_path.open("rb") as fh:
            raw = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {config_path}: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read config file {config_path}: {exc}") from exc
    return _build(raw, config_path)


def _build(raw: dict[str, Any], config_path: Path) -> Config:
    discord_raw = _section(raw, "discord")
    channel_ids = _as_int_list(
        discord_raw.get("channel_ids", []), "discord.channel_ids"
    )

    db_raw = _section(raw, "database")
    backend = db_raw.get("backend", "sqlite")
    if backend not in ("sqlite", "postgresql", "mysql"):
        raise ConfigError(f"unsupported database backend: {backend!r}")
    database = DatabaseConfig(
        backend=backend,
        path=_as_str(db_raw.get("path", "data/foresight.db"), "database.path"),
        host=_as_str(db_raw.get("host", "localhost"), "database.host"),
        port=_as_int(db_raw.get("port", 5432), "database.port"),
        database=_as_str(db_raw.get("database", "foresight"), "database.database"),
        user=_as_str(db_raw.get("user", "foresight"), "database.user"),
        password=os.environ.get("FORESIGHT_DB_PASSWORD", ""),
    )

    media_raw = _section(raw, "media")
    media_backend = media_raw.get("backend", "local")
    if media_backend not in ("local", "s3"):
        raise ConfigError(f"unsupported media backend: {media_backend!r}")
    s3_raw = _section(media_raw, "s3")
    media = MediaConfig(
        backend=media_backend,
        local_root=_as_str(
            media_raw.get("local_root", "data/media"), "media.local_root"
        ),
        max_download_size_mb=_as_int(
            media_raw.get("max_download_size_mb", 100), "media.max_download_size_mb"
        ),
        download_concurrency=_as_int(
            media_raw.get("download_concurrency", 3), "media.download_concurrency"
        ),
        s3=S3Config(
            bucket=_as_str(s3_raw.get("bucket", ""), "media.s3.bucket"),
            endpoint_url=_as_str(
                s3_raw.get("endpoint_url", ""), "media.s3.endpoint_url"
            ),
            region=_as_str(s3_raw.get("region", "auto"), "media.s3.region"),
            prefix=_as_str(s3_raw.get("prefix", "foresight"), "media.s3.prefix"),
            use_path_style=_as_bool(
                s3_raw.get("use_path_style", True), "media.s3.use_path_style"
            ),
            access_key_id=os.environ.get("FORESIGHT_S3_ACCESS_KEY_ID", ""),
            secret_access_key=os.environ.get("FORESIGHT_S3_SECRET_ACCESS_KEY", ""),
        ),
    )

    api_raw = _section(raw, "api")
    api_host = os.environ.get("FORESIGHT_API_HOST")
    if api_host is None:
        api_host = _as_str(api_raw.get("host", "127.0.0.1"), "api.host")
    api_port_value = os.environ.get("FORESIGHT_API_PORT")
    if api_port_value is None:
        api_port = _as_int(api_raw.get("port", 8000), "api.port")
    else:
        api_port = _as_int(api_port_value, "FORESIGHT_API_PORT (api.port)")
    api = ApiConfig(
        host=api_host,
        port=api_port,
        metrics_token=_as_str(api_raw.get("metrics_token", ""), "api.metrics_token"),
        metrics_refresh_seconds=_as_int(
            api_raw.get("metrics_refresh_seconds", 30), "api.metrics_refresh_seconds"
        ),
    )

    ai_raw = _section(raw, "ai")
    ai = AiConfig(
        enabled=_as_bool(ai_raw.get("enabled", False), "ai.enabled"),
        base_url=_as_str(
            ai_raw.get("base_url", "https://api.openai.com/v1"), "ai.base_url"
        ),
        model=_as_str(ai_raw.get("model", "gpt-4o-mini"), "ai.model"),
        translation_target=_as_str(
            ai_raw.get("translation_target", "zh-CN"), "ai.translation_target"
        ),
        max_concurrency=_as_int(
            ai_raw.get("max_concurrency", 2), "ai.max_concurrency"
        ),
        timeout_seconds=_as_int(
            ai_raw.get("timeout_seconds", 30), "ai.timeout_seconds"
        ),
        api_key=os.environ.get("FORESIGHT_LLM_API_KEY", ""),
    )

    writer_raw = _section(raw, "writer")
    writer = WriterConfig(
        batch_size=_as_int(writer_raw.get("batch_size", 100), "writer.batch_size"),
        flush_interval_ms=_as_int(
            writer_raw.get("flush_interval_ms", 50), "writer.flush_interval_ms"
        ),
        queue_maxsize=_as_int(
            writer_raw.get("queue_maxsize", 10000), "writer.queue_maxsize"
        ),
    )

    logging_raw = _section(raw, "logging")
    logging_cfg = LoggingConfig(level=_as_log_level(logging_raw.get("level", "INFO")))

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
