"""Tests for the query, stats and serve commands.

These commands are the ones an operator reaches for when the API is not
running: they read the dump directly, so a broken API does not hide the
data. The query command must stay read-only, and the serve command must
refuse to expose the API to the network without a token.
"""

from pathlib import Path

from typer.testing import CliRunner

from foresight.cli import app

runner = CliRunner()


def _seed_db(tmp_path: Path) -> Path:
    import asyncio

    from foresight.config import DatabaseConfig
    from foresight.domain.models import MessageRecord, OpKind, WriteOp
    from foresight.storage.db import create_engine, init_db
    from foresight.storage.repository import Repository

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
    # The statement is refused by the rules of the API, and the reason is
    # reported instead of a traceback.
    assert "query error" in result.output
    assert "SELECT" in result.output


def test_stats_command_prints_overview(tmp_path: Path):
    path = _seed_db(tmp_path)
    result = runner.invoke(app, ["stats", "--db", str(path)])
    assert result.exit_code == 0, result.output
    assert "total_messages" in result.output


def test_stats_command_reports_an_unusable_database(tmp_path: Path):
    result = runner.invoke(app, ["stats", "--db", str(tmp_path / "missing.db")])
    assert result.exit_code != 0
    # The operator gets a message, not a traceback.
    assert "Traceback" not in result.output
    assert "error:" in result.output


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
    # The operator is told what is missing.
    assert "FORESIGHT_API_TOKEN" in result.output


def test_both_gives_the_api_its_own_read_only_engine(tmp_path: Path, monkeypatch):
    """The API must not be able to write, not even beside the collector.

    `both` runs the collector and the API in one process, so the engine
    of the collector is right there. The API gets an engine of its own,
    one that the driver itself refuses to write with.
    """
    import asyncio
    from typing import Any

    import uvicorn

    from foresight.storage import db as storage_db

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"""
[discord]
channel_ids = [1]

[database]
backend = "sqlite"
path = "{tmp_path / 'data' / 'foresight.db'}"

[api]
host = "127.0.0.1"
port = 8000
""",
        encoding="utf-8",
    )
    monkeypatch.delenv("FORESIGHT_API_TOKEN", raising=False)

    engines: list[tuple[bool, Any]] = []
    applications: list[Any] = []
    events: list[str] = []
    real_create_engine = storage_db.create_engine

    def fake_create_engine(config: Any, **kwargs: Any) -> Any:
        engine = real_create_engine(config, **kwargs)
        engines.append((bool(kwargs.get("readonly")), engine))
        return engine

    class _FakeBot:
        async def start(self, token: str) -> None:
            events.append("bot.start")
            # The real bot awaits the login, so the event loop gets a turn
            # and the server task starts.
            await asyncio.sleep(0)

        async def close(self) -> None:
            events.append("bot.close")

    class _FakeServer:
        def __init__(self, config: Any) -> None:
            applications.append(config.app)

        async def serve(self) -> None:
            events.append("server.serve")
            await asyncio.sleep(0)

    monkeypatch.setattr("foresight.storage.db.create_engine", fake_create_engine)
    monkeypatch.setattr("foresight.bot.build_bot", lambda settings, handler: _FakeBot())
    monkeypatch.setattr("foresight.bot.require_token", lambda: "token")
    monkeypatch.setattr(uvicorn, "Server", _FakeServer)

    result = runner.invoke(app, ["both", "--config", str(config_path)])
    assert result.exit_code == 0, result.output
    # The collector writes, the API reads: two engines, one of them
    # read-only.
    assert [readonly for readonly, _ in engines] == [False, True]
    assert applications[0].state.engine is engines[1][1]
    assert "mode=ro" in str(applications[0].state.engine.url)
    # The API engine belongs to the command, so the command disposes it.
    assert applications[0].state.owns_engine is False
    assert events[0] == "bot.start"
    assert "server.serve" in events
    # The bot is closed when the command ends.
    assert events[-1] == "bot.close"
