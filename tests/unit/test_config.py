import logging
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url

from foresight.config import ConfigError, load_config
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
