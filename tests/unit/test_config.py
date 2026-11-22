import asyncio
import logging
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from foresight.config import (
    ConfigError,
    DatabaseConfig,
    load_config,
    sqlite_readonly_uri,
)
from foresight.logging_setup import setup_logging


def _write_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return path


def _postgres_config(tmp_path: Path) -> Path:
    return _write_config(
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


def test_env_selects_the_deployment_architecture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write_config(tmp_path, "")
    monkeypatch.setenv("FORESIGHT_DATABASE_BACKEND", "mysql")
    monkeypatch.setenv("FORESIGHT_DATABASE_PATH", "other.db")
    monkeypatch.setenv("FORESIGHT_DATABASE_HOST", "db")
    monkeypatch.setenv("FORESIGHT_DATABASE_PORT", "3307")
    monkeypatch.setenv("FORESIGHT_DATABASE_NAME", "fs")
    monkeypatch.setenv("FORESIGHT_DATABASE_USER", "fsuser")
    monkeypatch.setenv("FORESIGHT_MEDIA_BACKEND", "s3")
    monkeypatch.setenv("FORESIGHT_MEDIA_LOCAL_ROOT", "other-media")
    monkeypatch.setenv("FORESIGHT_S3_BUCKET", "bucket")
    monkeypatch.setenv("FORESIGHT_S3_ENDPOINT_URL", "http://s3:9000")
    monkeypatch.setenv("FORESIGHT_S3_REGION", "us-east-1")
    monkeypatch.setenv("FORESIGHT_S3_PREFIX", "media")
    monkeypatch.setenv("FORESIGHT_S3_USE_PATH_STYLE", "false")
    config = load_config(path)
    assert config.database.backend == "mysql"
    assert config.database.path == "other.db"
    assert config.database.host == "db"
    assert config.database.port == 3307
    assert config.database.database == "fs"
    assert config.database.user == "fsuser"
    assert config.media.backend == "s3"
    assert config.media.local_root == "other-media"
    assert config.media.s3.bucket == "bucket"
    assert config.media.s3.endpoint_url == "http://s3:9000"
    assert config.media.s3.region == "us-east-1"
    assert config.media.s3.prefix == "media"
    assert config.media.s3.use_path_style is False


def test_empty_environment_values_fall_back_to_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _postgres_config(tmp_path)
    monkeypatch.setenv("FORESIGHT_DATABASE_BACKEND", "")
    monkeypatch.setenv("FORESIGHT_DATABASE_HOST", "")
    config = load_config(path)
    assert config.database.backend == "postgresql"
    assert config.database.host == "db"


def test_unknown_backend_from_environment_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write_config(tmp_path, "")
    monkeypatch.setenv("FORESIGHT_DATABASE_BACKEND", "oracle")
    with pytest.raises(ConfigError):
        load_config(path)
    monkeypatch.setenv("FORESIGHT_DATABASE_BACKEND", "")
    monkeypatch.setenv("FORESIGHT_MEDIA_BACKEND", "gdrive")
    with pytest.raises(ConfigError):
        load_config(path)


def test_invalid_s3_path_style_from_environment_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write_config(tmp_path, "")
    monkeypatch.setenv("FORESIGHT_S3_USE_PATH_STYLE", "maybe")
    with pytest.raises(ConfigError):
        load_config(path)


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
    monkeypatch.setenv("FORESIGHT_S3_ACCESS_KEY_ID", "s3-id")
    monkeypatch.setenv("FORESIGHT_S3_SECRET_ACCESS_KEY", "s3-key")
    path = _write_config(tmp_path, "[discord]\nchannel_ids = [1]\n")
    config = load_config(path)
    assert config.database.password == "s3cret"
    assert config.ai.api_key == "llm-key"
    assert config.media.s3.access_key_id == "s3-id"
    assert config.media.s3.secret_access_key == "s3-key"


def test_toml_secrets_are_ignored_in_favour_of_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FORESIGHT_DB_PASSWORD", "db-password-from-env")
    monkeypatch.setenv("FORESIGHT_LLM_API_KEY", "llm-key-from-env")
    monkeypatch.setenv("FORESIGHT_S3_ACCESS_KEY_ID", "s3-id-from-env")
    monkeypatch.setenv("FORESIGHT_S3_SECRET_ACCESS_KEY", "s3-secret-from-env")
    path = _write_config(
        tmp_path,
        """
[database]
password = "db-password-from-toml"

[media.s3]
access_key_id = "s3-id-from-toml"
secret_access_key = "s3-secret-from-toml"

[ai]
api_key = "llm-key-from-toml"
""",
    )
    config = load_config(path)
    assert config.database.password == "db-password-from-env"
    assert config.ai.api_key == "llm-key-from-env"
    assert config.media.s3.access_key_id == "s3-id-from-env"
    assert config.media.s3.secret_access_key == "s3-secret-from-env"


def test_postgres_url_encodes_password(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORESIGHT_DB_PASSWORD", "p@ss/word")
    path = _postgres_config(tmp_path)
    config = load_config(path)
    assert "p%40ss%2Fword" in config.database.sqlalchemy_url


def test_password_with_space_is_percent_encoded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FORESIGHT_DB_PASSWORD", "pa ss word")
    path = _postgres_config(tmp_path)
    config = load_config(path)
    url = config.database.sqlalchemy_url
    userinfo = url.split("://", 1)[1].split("@", 1)[0]
    assert "%20" in userinfo
    assert "+" not in userinfo
    assert make_url(url).password == "pa ss word"


def test_malformed_toml_raises_config_error(tmp_path: Path) -> None:
    path = _write_config(tmp_path, "[discord\nchannel_ids = [1]\n")
    with pytest.raises(ConfigError):
        load_config(path)


def test_non_numeric_api_port_raises_config_error(tmp_path: Path) -> None:
    path = _write_config(tmp_path, '[api]\nport = "abc"\n')
    with pytest.raises(ConfigError):
        load_config(path)


def test_non_numeric_channel_ids_raise_config_error(tmp_path: Path) -> None:
    path = _write_config(tmp_path, '[discord]\nchannel_ids = ["abc"]\n')
    with pytest.raises(ConfigError):
        load_config(path)


def test_non_numeric_env_api_port_raises_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write_config(tmp_path, "[api]\nport = 9000\n")
    monkeypatch.setenv("FORESIGHT_API_PORT", "not-a-number")
    with pytest.raises(ConfigError):
        load_config(path)


def test_non_string_database_user_raises_config_error(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path, '[database]\nbackend = "postgresql"\nuser = 123\n'
    )
    with pytest.raises(ConfigError, match="database.user"):
        load_config(path)


def test_string_use_path_style_raises_config_error(tmp_path: Path) -> None:
    path = _write_config(tmp_path, '[media.s3]\nuse_path_style = "false"\n')
    with pytest.raises(ConfigError, match="use_path_style"):
        load_config(path)


def test_string_ai_enabled_raises_config_error(tmp_path: Path) -> None:
    path = _write_config(tmp_path, '[ai]\nenabled = "false"\n')
    with pytest.raises(ConfigError, match="ai.enabled"):
        load_config(path)


def test_correctly_typed_values_still_load(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        """
[database]
backend = "postgresql"
user = "fsuser"

[media.s3]
use_path_style = false

[ai]
enabled = true
""",
    )
    config = load_config(path)
    assert config.database.user == "fsuser"
    assert config.media.s3.use_path_style is False
    assert config.ai.enabled is True


def test_unknown_log_level_raises_config_error(tmp_path: Path) -> None:
    path = _write_config(tmp_path, '[logging]\nlevel = "BOGUS"\n')
    with pytest.raises(ConfigError):
        load_config(path)


def test_log_level_is_normalised(tmp_path: Path) -> None:
    path = _write_config(tmp_path, '[logging]\nlevel = "debug"\n')
    config = load_config(path)
    assert config.logging.level == "DEBUG"


def test_readonly_sqlalchemy_url_for_sqlite_is_read_only_uri(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path, '[database]\nbackend = "sqlite"\npath = "data/foresight.db"\n'
    )
    config = load_config(path)
    assert (
        config.database.readonly_sqlalchemy_url
        == "sqlite+aiosqlite:///file:data/foresight.db?mode=ro&uri=true"
    )


def test_the_read_only_uri_escapes_the_path_of_the_database(tmp_path: Path) -> None:
    """A path with a `?` or a `#` in it still opens the right file.

    Those two characters start the query string and the fragment of a
    URI: without the escaping the driver would read the rest of the path
    as a parameter of the connection, and the database of the path would
    never be opened.
    """
    path = tmp_path / "a?b#c.db"
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE t (a INTEGER)")
        conn.commit()
    finally:
        conn.close()

    uri = sqlite_readonly_uri(str(path))
    conn = sqlite3.connect(uri, uri=True)
    try:
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0
    finally:
        conn.close()


def test_the_read_only_uri_of_the_config_escapes_the_path(tmp_path: Path) -> None:
    """The URL of the engine opens the file the path names."""
    path = tmp_path / "a?b#c.db"
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE t (a INTEGER)")
        conn.commit()
    finally:
        conn.close()

    config = DatabaseConfig(backend="sqlite", path=str(path))
    engine = create_async_engine(config.readonly_sqlalchemy_url)

    async def _count() -> int:
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT COUNT(*) FROM t"))
            return int(result.scalar_one())

    try:
        assert asyncio.run(_count()) == 0
    finally:
        asyncio.run(engine.dispose())


def test_readonly_sqlalchemy_url_for_postgres_matches_sqlalchemy_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FORESIGHT_DB_PASSWORD", "p@ss/word")
    path = _postgres_config(tmp_path)
    config = load_config(path)
    assert config.database.readonly_sqlalchemy_url == config.database.sqlalchemy_url


def test_readonly_sqlalchemy_url_for_mysql_matches_sqlalchemy_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FORESIGHT_DB_PASSWORD", "p@ss/word")
    path = _write_config(
        tmp_path,
        """
[database]
backend = "mysql"
host = "db"
port = 3306
database = "fs"
user = "fsuser"
""",
    )
    config = load_config(path)
    assert config.database.readonly_sqlalchemy_url == config.database.sqlalchemy_url


def test_env_foreseight_config_selects_config_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write_config(tmp_path, "[api]\nport = 9999\n")
    monkeypatch.setenv("FORESIGHT_CONFIG", str(path))
    config = load_config()
    assert config.config_path == path
    assert config.api.port == 9999


def test_setup_logging_sets_requested_level() -> None:
    root = logging.getLogger()
    original_handlers = root.handlers[:]
    original_level = root.level
    try:
        setup_logging("DEBUG")
        assert root.level == logging.DEBUG
    finally:
        root.handlers = original_handlers
        root.setLevel(original_level)


def test_setup_logging_keeps_existing_file_handler_usable(tmp_path: Path) -> None:
    root = logging.getLogger()
    original_handlers = root.handlers[:]
    original_level = root.level
    log_file = tmp_path / "app.log"
    handler = logging.FileHandler(log_file, encoding="utf-8")
    try:
        root.addHandler(handler)
        setup_logging("INFO")
        assert handler in root.handlers
        logging.getLogger("foresight.test").warning("file handler still works")
        assert "file handler still works" in log_file.read_text(encoding="utf-8")
    finally:
        root.removeHandler(handler)
        handler.close()
        root.handlers = original_handlers
        root.setLevel(original_level)


def test_setup_logging_sets_level_when_root_logger_has_handlers() -> None:
    root = logging.getLogger()
    original_handlers = root.handlers[:]
    original_level = root.level
    try:
        root.addHandler(logging.NullHandler())
        root.setLevel(logging.INFO)
        setup_logging("WARNING")
        assert root.level == logging.WARNING
    finally:
        root.handlers = original_handlers
        root.setLevel(original_level)
