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
