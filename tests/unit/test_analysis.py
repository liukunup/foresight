"""Tests for the AI analysis subsystem.

The client is tested against a mocked OpenAI compatible endpoint, and the
worker against a real database: the analysis row it writes and the status
it sets on the message are what the rest of the service reads.
"""

import asyncio
import json

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from foresight.analysis.llm import LlmClient, LlmError
from foresight.analysis.prompts import PROMPT_VERSION
from foresight.analysis.worker import AnalysisWorker
from foresight.config import AiConfig
from foresight.domain.models import MessageRecord, OpKind, WriteOp
from foresight.metrics import REGISTRY
from foresight.storage.db import init_db
from foresight.storage.repository import Repository
from foresight.storage.tables import message_analysis, messages


def _response(payload: dict) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [
                {"message": {"content": json.dumps(payload, ensure_ascii=False)}}
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        },
    )


def _config(**overrides) -> AiConfig:
    values = {
        "enabled": True,
        "base_url": "https://llm.example/v1",
        "model": "test-model",
        "api_key": "key",
    }
    values.update(overrides)
    return AiConfig(**values)


def _message(message_id: int = 1) -> MessageRecord:
    return MessageRecord(
        message_id=message_id,
        guild_id=10,
        channel_id=20,
        author_id=30,
        content="hello",
        message_type=0,
        created_at="2026-11-11T10:00:00+00:00",
        captured_at="2026-11-11T10:00:00+00:00",
    )


def _sample(name: str, labels: dict[str, str] | None = None) -> float:
    """The current value of a metric of the process wide registry."""
    value = REGISTRY.get_sample_value(name, labels)
    return 0.0 if value is None else value


async def _no_wait(delay: float) -> None:
    """Stand in for the sleep between the attempts of the worker."""


async def test_llm_client_parses_json_payload():
    config = AiConfig(
        enabled=True,
        base_url="https://llm.example/v1",
        model="test-model",
        api_key="key",
    )
    with respx.mock:
        respx.post("https://llm.example/v1/chat/completions").mock(
            return_value=_response(
                {
                    "translation": "你好",
                    "sentiment": "negative",
                    "sentiment_score": 0.8,
                    "issues": ["登录超时"],
                }
            )
        )
        client = LlmClient(config)
        result = await client.analyze("login always times out")
    assert result.translation == "你好"
    assert result.sentiment == "negative"
    assert result.issues == ["登录超时"]


async def test_llm_client_raises_on_non_json():
    config = AiConfig(enabled=True, base_url="https://llm.example/v1", api_key="key")
    with respx.mock:
        respx.post("https://llm.example/v1/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={"choices": [{"message": {"content": "not json"}}]},
            )
        )
        client = LlmClient(config)
        with pytest.raises(LlmError):
            await client.analyze("hello")


async def test_llm_client_raises_without_api_key():
    client = LlmClient(AiConfig(enabled=True, base_url="https://llm.example/v1"))
    with pytest.raises(LlmError):
        await client.analyze("hello")


async def test_worker_stores_analysis(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'a.db'}")
    await init_db(engine)
    repo = Repository(engine)

    with respx.mock:
        respx.post("https://llm.example/v1/chat/completions").mock(
            return_value=_response(
                {
                    "translation": "你好",
                    "sentiment": "positive",
                    "sentiment_score": 0.9,
                    "issues": [],
                }
            )
        )
        client = LlmClient(
            AiConfig(enabled=True, base_url="https://llm.example/v1", api_key="key")
        )
        worker = AnalysisWorker(repo, client, max_concurrency=1)
        await worker.start()
        worker.submit(1, "good morning")
        await asyncio.sleep(0.5)
        await worker.stop()

    async with engine.connect() as conn:
        row = (
            await conn.execute(select(message_analysis).where(message_analysis.c.message_id == 1))
        ).one()
    assert row.sentiment == "positive"
    assert row.translated_text == "你好"
    await engine.dispose()


async def test_worker_marks_failed_after_retries(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'a.db'}")
    await init_db(engine)
    repo = Repository(engine)

    with respx.mock:
        route = respx.post("https://llm.example/v1/chat/completions").mock(
            return_value=httpx.Response(500, text="boom")
        )
        client = LlmClient(
            AiConfig(enabled=True, base_url="https://llm.example/v1", api_key="key")
        )
        worker = AnalysisWorker(repo, client, max_concurrency=1)
        await worker.start()
        worker.submit(1, "hello")
        await asyncio.sleep(1.5)
        await worker.stop()
        assert route.call_count >= 2

    async with engine.connect() as conn:
        row = (
            await conn.execute(select(message_analysis).where(message_analysis.c.message_id == 1))
        ).one()
    assert row.error
    await engine.dispose()


async def test_llm_client_falls_back_to_neutral_for_an_unknown_sentiment():
    """The column is queried by the four known values, so the rest is neutral."""
    with respx.mock:
        respx.post("https://llm.example/v1/chat/completions").mock(
            return_value=_response(
                {
                    "translation": "你好",
                    "sentiment": "happy",
                    "sentiment_score": 0.5,
                    "issues": [],
                }
            )
        )
        client = LlmClient(_config())
        result = await client.analyze("hello")
    assert result.sentiment == "neutral"


async def test_llm_client_asks_again_with_a_strict_prompt():
    """An answer that is not JSON gets one more chance with a strict prompt."""
    with respx.mock:
        route = respx.post("https://llm.example/v1/chat/completions").mock(
            side_effect=[
                httpx.Response(
                    200,
                    json={"choices": [{"message": {"content": "sorry, no JSON"}}]},
                ),
                _response(
                    {
                        "translation": "你好",
                        "sentiment": "neutral",
                        "sentiment_score": 0.5,
                        "issues": [],
                    }
                ),
            ]
        )
        client = LlmClient(_config())
        result = await client.analyze("hello")

    assert route.call_count == 2
    assert result.translation == "你好"
    # The second call asks for raw JSON, so that the model stops wrapping
    # its answer in markdown fences.
    second_prompt = json.loads(route.calls[1].request.content)["messages"][0]["content"]
    assert "raw JSON" in second_prompt


async def test_llm_client_does_not_call_the_model_without_an_api_key():
    """A call without a key would be refused by the endpoint anyway."""
    with respx.mock:
        route = respx.post("https://llm.example/v1/chat/completions").mock(
            return_value=_response({"translation": "你好"})
        )
        client = LlmClient(_config(api_key=""))
        with pytest.raises(LlmError):
            await client.analyze("hello")
    assert not route.called


async def test_llm_client_counts_the_requests_and_the_tokens():
    requests_before = _sample("foresight_ai_requests_total", {"status": "ok"})
    prompt_before = _sample("foresight_ai_tokens_total", {"type": "prompt"})
    completion_before = _sample("foresight_ai_tokens_total", {"type": "completion"})
    duration_before = _sample(
        "foresight_ai_request_duration_seconds_count"
    )

    with respx.mock:
        respx.post("https://llm.example/v1/chat/completions").mock(
            return_value=_response(
                {
                    "translation": "你好",
                    "sentiment": "neutral",
                    "sentiment_score": 0.5,
                    "issues": [],
                }
            )
        )
        client = LlmClient(_config())
        await client.analyze("hello")

    assert _sample("foresight_ai_requests_total", {"status": "ok"}) == requests_before + 1
    assert _sample("foresight_ai_tokens_total", {"type": "prompt"}) == prompt_before + 10
    assert (
        _sample("foresight_ai_tokens_total", {"type": "completion"})
        == completion_before + 5
    )
    assert (
        _sample("foresight_ai_request_duration_seconds_count") == duration_before + 1
    )


async def test_worker_marks_the_message_as_done(db_engine):
    repo = Repository(db_engine)
    await repo.apply_batch([WriteOp(OpKind.MESSAGE, _message())])

    with respx.mock:
        respx.post("https://llm.example/v1/chat/completions").mock(
            return_value=_response(
                {
                    "translation": "你好",
                    "sentiment": "positive",
                    "sentiment_score": 0.9,
                    "issues": ["登录超时"],
                }
            )
        )
        worker = AnalysisWorker(repo, LlmClient(_config()), max_concurrency=1)
        await worker.start()
        assert worker.submit(1, "good morning") is True
        await worker.stop()

    async with db_engine.connect() as conn:
        row = (await conn.execute(select(message_analysis))).one()
        status = (
            await conn.execute(select(messages.c.analysis_status))
        ).scalar_one()
    assert row.sentiment == "positive"
    assert row.sentiment_score == 0.9
    assert row.translated_text == "你好"
    # The issues are stored as a JSON array of strings.
    assert json.loads(row.issues_json) == ["登录超时"]
    assert row.model == "test-model"
    assert row.prompt_version == PROMPT_VERSION
    assert row.error is None
    assert status == "done"


async def test_worker_skips_a_message_without_content(db_engine):
    """A message of one character has nothing to analyse."""
    repo = Repository(db_engine)
    await repo.apply_batch([WriteOp(OpKind.MESSAGE, _message())])

    with respx.mock:
        route = respx.post("https://llm.example/v1/chat/completions").mock(
            return_value=_response({"translation": "你好"})
        )
        worker = AnalysisWorker(repo, LlmClient(_config()), max_concurrency=1)
        await worker.start()
        worker.submit(1, "a")
        await worker.stop()
        assert not route.called

    async with db_engine.connect() as conn:
        status = (
            await conn.execute(select(messages.c.analysis_status))
        ).scalar_one()
        rows = (await conn.execute(select(message_analysis))).all()
    assert status == "skipped"
    assert rows == []


async def test_worker_marks_the_message_as_failed(db_engine, monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _no_wait)
    repo = Repository(db_engine)
    await repo.apply_batch([WriteOp(OpKind.MESSAGE, _message())])

    with respx.mock:
        respx.post("https://llm.example/v1/chat/completions").mock(
            return_value=httpx.Response(500, text="boom")
        )
        worker = AnalysisWorker(repo, LlmClient(_config()), max_concurrency=1)
        await worker.start()
        worker.submit(1, "hello")
        await worker.stop()

    async with db_engine.connect() as conn:
        row = (await conn.execute(select(message_analysis))).one()
        status = (
            await conn.execute(select(messages.c.analysis_status))
        ).scalar_one()
    assert row.error
    assert row.sentiment is None
    assert status == "failed"


async def test_worker_publishes_the_depth_of_the_queue(db_engine, monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _no_wait)
    repo = Repository(db_engine)
    await repo.apply_batch([WriteOp(OpKind.MESSAGE, _message())])

    depth_before = _sample("foresight_ai_queue_depth")

    with respx.mock:
        respx.post("https://llm.example/v1/chat/completions").mock(
            return_value=_response(
                {
                    "translation": "你好",
                    "sentiment": "neutral",
                    "sentiment_score": 0.5,
                    "issues": [],
                }
            )
        )
        worker = AnalysisWorker(repo, LlmClient(_config()), max_concurrency=1)
        await worker.start()
        worker.submit(1, "hello")
        await worker.stop()

    # The queue is empty again: the work is done.
    assert _sample("foresight_ai_queue_depth") == depth_before


async def test_worker_refuses_a_message_when_the_queue_is_full(db_engine):
    """A full queue drops the message instead of growing without end."""
    repo = Repository(db_engine)
    worker = AnalysisWorker(repo, LlmClient(_config()), queue_maxsize=1)
    assert worker.submit(1, "hello") is True
    assert worker.submit(2, "hello") is False


async def test_worker_refuses_a_message_after_it_was_stopped(db_engine):
    """A stopped worker has no tasks left to do the work."""
    repo = Repository(db_engine)
    worker = AnalysisWorker(repo, LlmClient(_config()))
    await worker.start()
    await worker.stop()
    assert worker.submit(1, "hello") is False
