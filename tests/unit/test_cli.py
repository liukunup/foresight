"""Tests for the command line entry points.

The CLI is the only entry point of the service, so these tests pin what an
operator gets from it: a schema that is created without destroying the data
that is already there, a database that is closed again when the command is
done, a missing token that is reported as a message instead of a traceback,
and a broken configuration that ends in the documented exit code.
"""

from __future__ import annotations

import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pytest
from typer.testing import CliRunner

from foresight.cli import app, main

runner = CliRunner()


def _write_config(tmp_path: Path) -> Path:
    """Write a config file for a SQLite database below ``tmp_path``."""
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
    return config_path


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


def test_init_db_closes_the_database(tmp_path: Path):
    config_path = _write_config(tmp_path)
    db_path = tmp_path / "data" / "foresight.db"
    result = runner.invoke(app, ["init-db", "--config", str(config_path)])
    assert result.exit_code == 0, result.output
    # A closed database has no write-ahead log left next to it.
    assert not db_path.with_name(db_path.name + "-wal").exists()
    assert not db_path.with_name(db_path.name + "-shm").exists()


def test_init_db_keeps_existing_rows(tmp_path: Path):
    config_path = _write_config(tmp_path)
    db_path = tmp_path / "data" / "foresight.db"
    assert runner.invoke(app, ["init-db", "--config", str(config_path)]).exit_code == 0

    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            "INSERT INTO users (user_id, username, is_bot, roles_json, updated_at) "
            "VALUES (1, 'kept', 0, '[]', '2026-01-01T00:00:00+00:00')"
        )
        conn.commit()

    # A second run must add the missing tables, not drop the ones that exist.
    assert runner.invoke(app, ["init-db", "--config", str(config_path)]).exit_code == 0

    with closing(sqlite3.connect(db_path)) as conn:
        rows = conn.execute("SELECT username FROM users WHERE user_id = 1").fetchall()
    assert rows == [("kept",)]


def test_run_reports_missing_discord_token(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)
    config_path = _write_config(tmp_path)
    result = runner.invoke(app, ["run", "--config", str(config_path)])
    assert result.exit_code != 0
    assert "DISCORD_TOKEN" in result.output
    assert "Traceback" not in result.output


def test_main_reports_configuration_error(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr(
        sys, "argv", ["foresight", "init-db", "--config", str(tmp_path / "nope.toml")]
    )
    with pytest.raises(SystemExit) as excinfo:
        main()
    assert excinfo.value.code == 2
    assert "configuration error" in capsys.readouterr().err
