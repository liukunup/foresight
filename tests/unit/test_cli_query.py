"""Tests for the query, stats, serve and both commands.

These commands are the ones an operator reaches for when the API is not
running: they read the dump directly, so a broken API does not hide the
data. The query command must stay read-only, the serve command must
refuse to expose the API to the network without a token, and the both
command must give the API an engine that can only read.
"""

import json
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from foresight.cli import app

runner = CliRunner()


def _seed_db(tmp_path: Path, count: int = 1) -> Path:
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
                        message_id=index + 1,
                        guild_id=10,
                        channel_id=20,
                        author_id=30,
                        content="hello",
                        message_type=0,
                        created_at="2026-11-11T00:00:00+00:00",
                        captured_at="2026-11-11T00:00:00+00:00",
                    ),
                )
                for index in range(count)
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


def test_query_command_clamps_the_limit(tmp_path: Path):
    """The command must ask for as many rows as the API would hand out."""
    path = _seed_db(tmp_path, count=600)
    result = runner.invoke(
        app,
        [
            "query",
            "--db",
            str(path),
            "--sql",
            "SELECT message_id FROM messages",
            "--limit",
            "10000",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    # The API clamps the limit to 500 rows, and the command does the same.
    assert len(payload["rows"]) == 500


def test_stats_command_prints_overview(tmp_path: Path):
    path = _seed_db(tmp_path)
    result = runner.invoke(app, ["stats", "--db", str(path)])
    assert result.exit_code == 0, result.output
    assert "total_messages" in result.output


def test_stats_command_does_not_write_to_the_database(tmp_path: Path):
    """The command only reads, so it must not even create the file."""
    path = tmp_path / "missing.db"
    result = runner.invoke(app, ["stats", "--db", str(path)])
    assert result.exit_code != 0
    # The database is opened read-only, so a file that is not there stays
    # not there.
    assert not path.exists()


def test_stats_command_reports_an_unusable_database(tmp_path: Path):
    result = runner.invoke(app, ["stats", "--db", str(tmp_path / "missing.db")])
    assert result.exit_code != 0
    # The operator gets a message, not a traceback.
    assert "Traceback" not in result.output
    assert "error:" in result.output


def test_serve_refuses_remote_bind_without_token(tmp_path: Path, monkeypatch):
    import uvicorn

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
    started: list[Any] = []
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: started.append(args))
    result = runner.invoke(app, ["serve", "--config", str(config_path)])
    assert result.exit_code != 0
    # The operator is told what is missing.
    assert "FORESIGHT_API_TOKEN" in result.output
    # The server was never started: the check happens before the server is
    # built, so the command cannot hang on a real server either.
    assert not started


def test_both_reports_the_api_server_that_cannot_bind(tmp_path: Path, monkeypatch):
    """A port that is taken is what kills the server in practice.

    `uvicorn` leaves the server with a `SystemExit` when it cannot bind
    the port, and the exit code alone says nothing about the reason.
    """
    import asyncio

    import uvicorn

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

    events: list[str] = []

    class _UnbindableServer:
        """A server that cannot bind the port, the way uvicorn does it."""

        def __init__(self, config: Any) -> None:
            pass

        async def serve(self) -> None:
            try:
                raise OSError(48, "address already in use")
            except OSError:
                raise SystemExit(3)

    class _SleepingBot:
        async def start(self, token: str) -> None:
            events.append("bot.start")
            await asyncio.sleep(0.2)

        async def close(self) -> None:
            events.append("bot.close")

    monkeypatch.setattr("foresight.bot.build_bot", lambda settings, handler: _SleepingBot())
    monkeypatch.setattr("foresight.bot.require_token", lambda: "token")
    monkeypatch.setattr(uvicorn, "Server", _UnbindableServer)

    result = runner.invoke(app, ["both", "--config", str(config_path)])
    assert result.exit_code != 0
    # The operator learns why the API is gone — the exit code alone would
    # not tell them anything.
    assert "address already in use" in result.output
    assert "bot.close" in events


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
    disposed: list[Any] = []
    real_create_engine = storage_db.create_engine

    class _EngineProxy:
        """The real engine, with its disposal recorded."""

        def __init__(self, engine: Any) -> None:
            self._engine = engine

        def __getattr__(self, name: str) -> Any:
            return getattr(self._engine, name)

        async def dispose(self) -> None:
            disposed.append(self._engine)
            await self._engine.dispose()

    def fake_create_engine(config: Any, **kwargs: Any) -> Any:
        engine = real_create_engine(config, **kwargs)
        proxy = _EngineProxy(engine)
        engines.append((bool(kwargs.get("readonly")), proxy))
        return proxy

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
    # Both engines are released when the command ends.
    assert len(disposed) == 2
    assert events[0] == "bot.start"
    assert "server.serve" in events
    # The bot is closed when the command ends.
    assert events[-1] == "bot.close"


def test_both_reports_the_api_server_that_dies(tmp_path: Path, monkeypatch):
    """A dead API must be reported at once, not at the end of the run.

    A port that is already taken kills the server while the collector
    keeps running: the collector must not go on without the API, and the
    reason must not wait for the shutdown to appear.
    """
    import asyncio

    import uvicorn

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

    events: list[str] = []

    class _FailingServer:
        def __init__(self, config: Any) -> None:
            pass

        async def serve(self) -> None:
            events.append("server.serve")
            raise RuntimeError("the port is already in use")

    class _SleepingBot:
        """A bot that keeps the collector running."""

        async def start(self, token: str) -> None:
            events.append("bot.start")
            await asyncio.sleep(0.2)

        async def close(self) -> None:
            events.append("bot.close")

    monkeypatch.setattr("foresight.bot.build_bot", lambda settings, handler: _SleepingBot())
    monkeypatch.setattr("foresight.bot.require_token", lambda: "token")
    monkeypatch.setattr(uvicorn, "Server", _FailingServer)

    result = runner.invoke(app, ["both", "--config", str(config_path)])
    assert result.exit_code != 0
    # The operator learns why the API is gone.
    assert "the port is already in use" in result.output
    # The collector does not go on running without the API.
    assert "bot.close" in events
