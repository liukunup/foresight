"""Tests for the command line entry points.

The CLI is the only entry point of the service, so these tests pin what an
operator gets from it: a schema that is created without destroying the data
that is already there, a database that is closed again when the command is
done, a missing token that is reported as a message instead of a traceback,
a broken configuration that ends in the documented exit code, and a collector
that is wired up in the documented order and closed again on the way out.
"""

from __future__ import annotations

import logging
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import pytest
from typer.testing import CliRunner

from foresight.cli import app, main
from foresight.storage import db as storage_db

runner = CliRunner()


def _write_config(
    tmp_path: Path, channel_ids: str = "[1]", ai_enabled: bool = False
) -> Path:
    """Write a config file for a SQLite database below ``tmp_path``."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"""
[discord]
channel_ids = {channel_ids}

[database]
backend = "sqlite"
path = "{tmp_path / 'data' / 'foresight.db'}"

[ai]
enabled = {"true" if ai_enabled else "false"}
""",
        encoding="utf-8",
    )
    return config_path


class _FakeBot:
    """A bot that never talks to Discord."""

    def __init__(self, events: list[str]) -> None:
        self._events = events
        self.token: str | None = None

    async def start(self, token: str) -> None:
        self.token = token
        self._events.append("bot.start")

    async def close(self) -> None:
        self._events.append("bot.close")


class _FakeWriter:
    """A writer that records how the command drives it."""

    def __init__(self, repo: Any, events: list[str], **kwargs: Any) -> None:
        self.repo = repo
        self.events = events
        self.failure_path = kwargs.get("failure_path")
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True
        self.events.append("writer.start")

    async def stop(self) -> None:
        self.stopped = True
        self.events.append("writer.stop")

    def submit(self, op: Any) -> bool:
        return True


class _FakeMediaWorker:
    """A media worker that records what it was given."""

    def __init__(self, repo: Any, store: Any, events: list[str], **kwargs: Any) -> None:
        self.repo = repo
        self.store = store
        self.kwargs = kwargs
        self.events = events
        self.records: list[Any] = []
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True
        self.events.append("media.start")

    async def stop(self) -> None:
        self.stopped = True
        self.events.append("media.stop")

    def submit(self, record: Any) -> bool:
        self.records.append(record)
        return True


class _FakeAnalysisWorker:
    """An analysis worker that records the messages it was given."""

    def __init__(self, repo: Any, llm: Any, events: list[str], **kwargs: Any) -> None:
        self.repo = repo
        self.llm = llm
        self.kwargs = kwargs
        self.events = events
        self.submitted: list[tuple[int, str]] = []
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True
        self.events.append("analysis.start")

    async def stop(self) -> None:
        self.stopped = True
        self.events.append("analysis.stop")

    def submit(self, message_id: int, content: str) -> bool:
        self.submitted.append((message_id, content))
        return True


class _EngineProxy:
    """The real engine, with its disposal recorded."""

    def __init__(self, engine: Any, events: list[str]) -> None:
        self._engine = engine
        self._events = events

    def __getattr__(self, name: str) -> Any:
        return getattr(self._engine, name)

    async def dispose(self) -> None:
        self._events.append("engine.dispose")
        await self._engine.dispose()


@dataclass
class _Collector:
    """The fakes behind one ``foresight run``."""

    run: Callable[[], Any]
    events: list[str]
    replay_paths: list[Path | None]
    writers: list[_FakeWriter]
    media_workers: list[_FakeMediaWorker]
    analysis_workers: list[_FakeAnalysisWorker]
    bot: _FakeBot
    db_path: Path


@pytest.fixture
def ai_enabled(request: pytest.FixtureRequest) -> bool:
    """The collector runs without the AI, unless a test asks for it.

    A test asks for the AI with
    ``@pytest.mark.parametrize("ai_enabled", [True], indirect=True)``.
    """
    return bool(getattr(request, "param", False))


@pytest.fixture
def collector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ai_enabled: bool
) -> _Collector:
    """A ``foresight run`` whose Discord side is faked.

    The command is not started by the fixture: every test calls
    ``collector.run()`` itself, so that the test decides when the command
    runs — and can watch the log lines it writes while it runs.
    """
    events: list[str] = []
    replay_paths: list[Path | None] = []
    writers: list[_FakeWriter] = []
    media_workers: list[_FakeMediaWorker] = []
    analysis_workers: list[_FakeAnalysisWorker] = []
    config_path = _write_config(tmp_path, ai_enabled=ai_enabled)
    db_path = tmp_path / "data" / "foresight.db"

    bot = _FakeBot(events)

    def fake_build_bot(settings: Any, handler: Any) -> _FakeBot:
        return bot

    async def fake_recover(writer: Any, path: Path | None) -> int:
        replay_paths.append(path)
        events.append("replay")
        return 0

    def make_writer(repo: Any, **kwargs: Any) -> _FakeWriter:
        writer = _FakeWriter(repo, events, **kwargs)
        writers.append(writer)
        return writer

    def make_media_worker(repo: Any, store: Any, **kwargs: Any) -> _FakeMediaWorker:
        worker = _FakeMediaWorker(repo, store, events, **kwargs)
        media_workers.append(worker)
        return worker

    def make_analysis_worker(repo: Any, llm: Any, **kwargs: Any) -> _FakeAnalysisWorker:
        worker = _FakeAnalysisWorker(repo, llm, events, **kwargs)
        analysis_workers.append(worker)
        return worker

    real_create_engine = storage_db.create_engine

    def fake_create_engine(config: Any) -> Any:
        return _EngineProxy(real_create_engine(config), events)

    monkeypatch.setattr("foresight.bot.build_bot", fake_build_bot)
    monkeypatch.setattr("foresight.bot.require_token", lambda: "token")
    monkeypatch.setattr("foresight.writer.Writer", make_writer)
    monkeypatch.setattr("foresight.writer.recover_failed_batches", fake_recover)
    monkeypatch.setattr("foresight.storage.db.create_engine", fake_create_engine)
    monkeypatch.setattr("foresight.media_worker.MediaWorker", make_media_worker)
    monkeypatch.setattr("foresight.analysis.worker.AnalysisWorker", make_analysis_worker)
    monkeypatch.setattr(
        "foresight.storage.media.build_media_store", lambda config: "media-store"
    )

    def run() -> Any:
        return runner.invoke(app, ["run", "--config", str(config_path)])

    return _Collector(
        run=run,
        events=events,
        replay_paths=replay_paths,
        writers=writers,
        media_workers=media_workers,
        analysis_workers=analysis_workers,
        bot=bot,
        db_path=db_path,
    )


def _seed_pending_messages(db_path: Path, count: int = 2) -> None:
    """Put messages that wait for their analysis into the database."""
    import asyncio

    from foresight.config import DatabaseConfig
    from foresight.domain.models import MessageRecord, OpKind, WriteOp
    from foresight.storage.db import create_engine, init_db
    from foresight.storage.repository import Repository

    async def _run() -> None:
        engine = create_engine(DatabaseConfig(backend="sqlite", path=str(db_path)))
        await init_db(engine)
        repo = Repository(engine)
        await repo.apply_batch(
            [
                WriteOp(
                    OpKind.MESSAGE,
                    MessageRecord(
                        message_id=index + 1,
                        guild_id=10,
                        channel_id=1,
                        author_id=30,
                        content=f"waiting {index}",
                        message_type=0,
                        created_at=f"2026-11-1{index}T00:00:00+00:00",
                        captured_at="2026-11-11T00:00:00+00:00",
                    ),
                )
                for index in range(count)
            ]
        )
        await engine.dispose()

    asyncio.run(_run())


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


def test_init_db_reports_an_unusable_database(tmp_path: Path):
    config_path = _write_config(tmp_path)
    # A directory cannot be opened as a SQLite database.
    (tmp_path / "data" / "foresight.db").mkdir(parents=True)
    result = runner.invoke(app, ["init-db", "--config", str(config_path)])
    assert result.exit_code != 0
    assert "Traceback" not in result.output
    assert "error:" in result.stderr
    # The cause is kept, so that the operator sees why it failed.
    assert "unable to open database file" in result.stderr


def test_run_reports_missing_discord_token(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)
    config_path = _write_config(tmp_path)
    result = runner.invoke(app, ["run", "--config", str(config_path)])
    assert result.exit_code != 0
    assert "DISCORD_TOKEN" in result.output
    assert "Traceback" not in result.output


def test_run_reports_an_unusable_database(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("foresight.bot.require_token", lambda: "token")
    config_path = _write_config(tmp_path)
    # The parent of the database is a file, so the engine cannot be built.
    (tmp_path / "data").write_text("not a directory", encoding="utf-8")
    result = runner.invoke(app, ["run", "--config", str(config_path)])
    assert result.exit_code != 0
    assert "Traceback" not in result.output
    assert "error:" in result.stderr


def test_main_reports_configuration_error(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr(
        sys, "argv", ["foresight", "init-db", "--config", str(tmp_path / "nope.toml")]
    )
    with pytest.raises(SystemExit) as excinfo:
        main()
    assert excinfo.value.code == 2
    assert "configuration error" in capsys.readouterr().err


def test_main_reports_empty_channel_ids_as_a_configuration_error(
    tmp_path: Path, monkeypatch, capsys
):
    config_path = _write_config(tmp_path, channel_ids="[]")
    monkeypatch.setattr(sys, "argv", ["foresight", "run", "--config", str(config_path)])
    with pytest.raises(SystemExit) as excinfo:
        main()
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "configuration error" in err
    assert "channel_ids" in err


def test_run_replays_the_failure_file_next_to_the_database(collector: _Collector):
    result = collector.run()
    assert result.exit_code == 0, result.output
    assert collector.replay_paths == [collector.db_path.parent / "failed_batch.jsonl"]


def test_run_replays_the_failure_file_before_the_bot_starts(collector: _Collector):
    result = collector.run()
    assert result.exit_code == 0, result.output
    assert collector.events.index("replay") < collector.events.index("bot.start")


def test_run_replays_the_failure_file_before_the_writer_starts(collector: _Collector):
    result = collector.run()
    assert result.exit_code == 0, result.output
    # The replay must not race the background task of the writer.
    assert collector.events.index("replay") < collector.events.index("writer.start")


def test_run_gives_the_writer_the_failure_file_it_replays(collector: _Collector):
    result = collector.run()
    assert result.exit_code == 0, result.output
    assert collector.writers[0].failure_path == collector.replay_paths[0]


def test_run_starts_the_bot_with_the_token(collector: _Collector):
    result = collector.run()
    assert result.exit_code == 0, result.output
    assert collector.bot.token == "token"


def test_run_closes_the_bot_when_the_collector_stops(collector: _Collector):
    result = collector.run()
    assert result.exit_code == 0, result.output
    assert "bot.close" in collector.events


def test_run_closes_the_bot_before_it_stops_the_writer(collector: _Collector):
    result = collector.run()
    assert result.exit_code == 0, result.output
    # A closed bot cannot deliver an event to a writer that is already
    # stopped — such an operation is dropped.
    assert collector.events.index("bot.close") < collector.events.index("writer.stop")


def test_run_stops_the_writer_before_it_disposes_the_engine(collector: _Collector):
    result = collector.run()
    assert result.exit_code == 0, result.output
    assert collector.writers[0].stopped
    assert collector.events[-4:] == [
        "bot.close",
        "media.stop",
        "writer.stop",
        "engine.dispose",
    ]


def test_run_logs_that_the_collector_starts_and_stops(collector: _Collector, caplog):
    with caplog.at_level(logging.INFO, logger="foresight.cli"):
        result = collector.run()
    assert result.exit_code == 0, result.output
    messages = [record.getMessage() for record in caplog.records]
    assert any("starting" in message for message in messages)
    assert any("stopping" in message for message in messages)


def test_run_stops_the_writer_even_when_the_bot_cannot_be_closed(
    collector: _Collector, monkeypatch: pytest.MonkeyPatch
):
    async def failing_close() -> None:
        collector.events.append("bot.close")
        raise RuntimeError("the bot could not be closed")

    monkeypatch.setattr(collector.bot, "close", failing_close)
    result = collector.run()
    assert result.exit_code != 0
    # The bot could not be closed, but the writer and the engine are
    # released anyway, and in the documented order.
    assert collector.events[-3:] == ["media.stop", "writer.stop", "engine.dispose"]


def test_run_reports_a_failing_replay_and_releases_the_engine(
    collector: _Collector, monkeypatch: pytest.MonkeyPatch
):
    async def failing_recover(writer: Any, path: Path | None) -> int:
        raise OSError("the failure file cannot be read")

    monkeypatch.setattr("foresight.writer.recover_failed_batches", failing_recover)
    result = collector.run()
    assert result.exit_code != 0
    # The operator gets a message, not a traceback.
    assert "Traceback" not in result.output
    assert "error:" in result.stderr
    # The engine that was already created is released.
    assert "engine.dispose" in collector.events


def test_run_does_not_disguise_an_unrelated_schema_error(
    collector: _Collector, monkeypatch: pytest.MonkeyPatch
):
    async def failing_init_db(engine: Any) -> None:
        raise RuntimeError("a bug in the schema creation")

    monkeypatch.setattr("foresight.storage.db.init_db", failing_init_db)
    result = collector.run()
    # A bug is not reported as a database that cannot be opened: it
    # propagates.
    assert isinstance(result.exception, RuntimeError)
    assert "cannot open the database" not in result.output
    # The engine that was already created is still released.
    assert "engine.dispose" in collector.events


def test_init_db_does_not_disguise_an_unrelated_schema_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    async def failing_init_db(engine: Any) -> None:
        raise RuntimeError("a bug in the schema creation")

    monkeypatch.setattr("foresight.storage.db.init_db", failing_init_db)
    result = runner.invoke(app, ["init-db", "--config", str(_write_config(tmp_path))])
    assert isinstance(result.exception, RuntimeError)
    assert "cannot open the database" not in result.output


def test_run_starts_the_media_worker(collector: _Collector):
    """The attachments of a message are downloaded in the background."""
    result = collector.run()
    assert result.exit_code == 0, result.output
    assert collector.media_workers[0].started
    assert collector.media_workers[0].stopped


def test_run_gives_the_media_worker_the_size_limit_of_the_configuration(
    collector: _Collector,
):
    """The worker does not read the configuration: the limit comes with it."""
    result = collector.run()
    assert result.exit_code == 0, result.output
    assert collector.media_workers[0].kwargs["max_bytes"] == 100 * 1024 * 1024


def test_run_does_not_start_an_analysis_worker_when_the_ai_is_disabled(
    collector: _Collector,
):
    """A disabled AI is not called at all, so there is no worker for it."""
    result = collector.run()
    assert result.exit_code == 0, result.output
    assert collector.analysis_workers == []


@pytest.mark.parametrize("ai_enabled", [True], indirect=True)
def test_run_starts_the_analysis_worker_when_the_ai_is_enabled(
    collector: _Collector,
):
    result = collector.run()
    assert result.exit_code == 0, result.output
    assert collector.analysis_workers[0].started
    assert collector.analysis_workers[0].stopped


@pytest.mark.parametrize("ai_enabled", [True], indirect=True)
def test_run_stops_the_analysis_worker_before_the_media_worker(
    collector: _Collector,
):
    """The analysis is the slowest work: it stops first."""
    result = collector.run()
    assert result.exit_code == 0, result.output
    events = collector.events
    assert events.index("analysis.stop") < events.index("media.stop")
    assert events.index("media.stop") < events.index("writer.stop")


@pytest.mark.parametrize("ai_enabled", [True], indirect=True)
def test_run_queues_the_messages_that_wait_for_their_analysis(
    collector: _Collector,
):
    """A restart must not leave the messages that were never analysed.

    A message whose analysis never happened keeps the status `pending`,
    so the collector queues it again when it starts.
    """
    _seed_pending_messages(collector.db_path, count=2)

    result = collector.run()

    assert result.exit_code == 0, result.output
    submitted = collector.analysis_workers[0].submitted
    assert sorted(submitted) == [(1, "waiting 0"), (2, "waiting 1")]


def test_run_does_not_queue_anything_without_an_analysis_worker(
    collector: _Collector,
):
    """Without the AI there is no worker that could analyse a message."""
    _seed_pending_messages(collector.db_path, count=2)

    result = collector.run()

    assert result.exit_code == 0, result.output
    assert collector.analysis_workers == []
