"""The end to end smoke test of the service.

One Discord message goes in and everything that has to come out of it
comes out: the collector maps the event and hands it to the writer and to
the analysis worker, the writer stores the message, the worker asks the
model and stores the answer, and the API serves both.

Every other test covers one part of the chain; this one covers the seams
between the parts, which is where a break hides best.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import respx
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from foresight.analysis.llm import LlmClient
from foresight.analysis.worker import AnalysisWorker
from foresight.api.app import create_app
from foresight.config import AiConfig, DatabaseConfig
from foresight.events import EventHandler
from foresight.storage.db import create_engine, init_db
from foresight.storage.repository import Repository
from foresight.storage.tables import message_analysis, messages
from foresight.writer import Writer

# The ids of the fake Discord objects.
GUILD_ID = 10
CHANNEL_ID = 20
AUTHOR_ID = 30
MESSAGE_ID = 100

# The content of the message that goes through the service.
CONTENT = "login always times out"

# The answer of the fake model: the four results of an analysis.
ANALYSIS = {
    "translation": "登录总是超时",
    "sentiment": "negative",
    "sentiment_score": 0.8,
    "issues": ["登录超时"],
}


def _fake_message(content: str) -> SimpleNamespace:
    """A message shaped like the one discord.py delivers.

    The message is a plain object and not a real one of the Discord
    library: the adapter layer is exactly the thing that lets the
    collector be driven without a connection to Discord.
    """
    return SimpleNamespace(
        id=MESSAGE_ID,
        content=content,
        type=0,
        created_at=datetime(2026, 11, 11, tzinfo=timezone.utc),
        edited_at=None,
        author=SimpleNamespace(
            id=AUTHOR_ID,
            name="alice",
            global_name="Alice",
            display_name="alice",
            discriminator="0",
            bot=False,
            avatar=None,
            created_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
            joined_at=None,
            roles=[],
        ),
        channel=SimpleNamespace(
            id=CHANNEL_ID,
            name="general",
            topic=None,
            type="text",
            position=1,
            guild=SimpleNamespace(id=GUILD_ID, name="Foresight", member_count=1),
        ),
        guild=SimpleNamespace(id=GUILD_ID, name="Foresight", member_count=1),
        reference=None,
        mentions=[],
        attachments=[],
        embeds=[],
    )


def _collector_config() -> SimpleNamespace:
    """The configuration of a collector that watches one channel."""
    return SimpleNamespace(
        discord=SimpleNamespace(channel_ids=(CHANNEL_ID,)),
        media=SimpleNamespace(backend="local"),
    )


def _api_config(db_path: Path) -> SimpleNamespace:
    """The configuration of an API that reads the database at `db_path`."""
    return SimpleNamespace(
        api=SimpleNamespace(host="127.0.0.1", port=8000, metrics_token=""),
        database=SimpleNamespace(
            backend="sqlite",
            path=str(db_path),
            sqlalchemy_url=f"sqlite+aiosqlite:///{db_path}",
            readonly_sqlalchemy_url=(
                f"sqlite+aiosqlite:///file:{db_path}?mode=ro&uri=true"
            ),
        ),
        media=SimpleNamespace(backend="local"),
    )


async def _wait_for_the_message(engine: AsyncEngine, message_id: int) -> None:
    """Wait until the message is in the database.

    The wait is bounded: a message that never arrives is a failure of the
    test and not a wait that never ends.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 5.0
    while True:
        async with engine.connect() as conn:
            found = await conn.execute(
                select(messages.c.message_id).where(
                    messages.c.message_id == message_id
                )
            )
            if found.first() is not None:
                return
        if loop.time() >= deadline:
            raise AssertionError(f"message {message_id} never reached the database")
        await asyncio.sleep(0.01)


def _model_answers(engine: AsyncEngine) -> Any:
    """The answer of the fake model, once the message is in the database.

    The analysis marks the message as done, and that status belongs to a
    row that has to exist. A real model takes a moment to answer, and the
    writer writes the message in the meantime — so the fake one waits for
    the message as well.
    """

    async def answer(request: httpx.Request) -> httpx.Response:
        await _wait_for_the_message(engine, MESSAGE_ID)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": json.dumps(ANALYSIS, ensure_ascii=False)}}
                ]
            },
        )

    return answer


async def test_a_discord_message_reaches_the_api(tmp_path, monkeypatch):
    """A message goes into the service and comes back out of the API.

    The whole chain is exercised: the event is mapped and handed over,
    the writer stores it, the analysis worker asks the model and stores
    the answer, and the API serves the message and its analysis.
    """
    monkeypatch.setenv("FORESIGHT_API_TOKEN", "test-token")

    db_path = tmp_path / "e2e.db"
    engine = create_engine(DatabaseConfig(backend="sqlite", path=str(db_path)))
    await init_db(engine)
    repo = Repository(engine)
    writer = Writer(repo, batch_size=10, flush_interval_ms=20)
    await writer.start()
    llm = LlmClient(
        AiConfig(
            enabled=True,
            base_url="https://llm.example/v1",
            model="test-model",
            api_key="key",
        )
    )
    analysis = AnalysisWorker(repo, llm, max_concurrency=1)
    await analysis.start()

    try:
        with respx.mock:
            route = respx.post("https://llm.example/v1/chat/completions").mock(
                side_effect=_model_answers(engine)
            )
            handler = EventHandler(
                _collector_config(),
                writer,
                analysis_sink=analysis.submit,
            )
            # The collector handles the event on the callback of Discord:
            # the call is synchronous and the work happens in the
            # background.
            handler.handle_message(_fake_message(CONTENT))
            # The worker stops when its queue is empty, so the analysis is
            # written before the test reads the database.
            await analysis.stop()
            await writer.stop()

            # The model was asked, and the answer was stored.
            assert route.called, "the model was never asked"
            async with engine.connect() as conn:
                stored = (await conn.execute(select(messages))).one()
                analysed = (await conn.execute(select(message_analysis))).one()
            assert stored.message_id == MESSAGE_ID
            assert stored.content == CONTENT
            assert stored.channel_id == CHANNEL_ID
            assert stored.author_id == AUTHOR_ID
            assert stored.analysis_status == "done"
            assert analysed.sentiment == "negative"
            assert analysed.sentiment_score == 0.8
            assert analysed.translated_text == ANALYSIS["translation"]
            assert json.loads(analysed.issues_json) == ANALYSIS["issues"]

            # The API serves the message and its analysis.
            app = create_app(_api_config(db_path), engine=engine)
            transport = ASGITransport(app=app)
            headers = {"Authorization": "Bearer test-token"}
            async with AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                found = await client.get(
                    "/api/messages", headers=headers, params={"q": "times out"}
                )
                assert found.status_code == 200
                body = found.json()
                assert body["total"] == 1
                assert body["items"][0]["content"] == CONTENT
                assert body["items"][0]["analysis_status"] == "done"

                detail = await client.get(
                    f"/api/messages/{MESSAGE_ID}", headers=headers
                )
                assert detail.status_code == 200
                assert detail.json()["analysis"]["sentiment"] == "negative"

                overview = await client.get("/api/stats/overview", headers=headers)
                assert overview.status_code == 200
                assert overview.json()["total_messages"] == 1
    finally:
        # The resources are released even when the test fails, so that
        # nothing outlives the loop of the test. Stopping the worker and
        # the writer twice is harmless: they are stopped already.
        await analysis.stop()
        await writer.stop()
        await llm.close()
        await engine.dispose()
