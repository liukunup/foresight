"""Tests for the AI analysis subsystem.

The client is tested against a mocked OpenAI compatible endpoint, and the
worker against a real database: the analysis row it writes and the status
it sets on the message are what the rest of the service reads.
"""

import asyncio
import json
import time

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from foresight.analysis.llm import LlmClient, LlmError, LlmPermanentError
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


async def _wait_for_depth(expected: float, timeout: float = 10.0) -> None:
    """Wait until the gauge of the queue shows the expected depth."""
    deadline = time.monotonic() + timeout
    while _sample("foresight_ai_queue_depth") != expected:
        if time.monotonic() > deadline:
            raise AssertionError(
                "the depth of the queue did not reach "
                f"{expected}: {_sample('foresight_ai_queue_depth')}"
            )
        await asyncio.sleep(0.01)


class _BrokenLlm:
    """A client that fails in the way the test asks it to fail."""

    model = "broken-model"

    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls = 0

    async def analyze(self, content: str):
        self.calls += 1
        raise self.error


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
    with pytest.raises(LlmPermanentError):
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


async def test_worker_marks_failed_after_retries(tmp_path, monkeypatch):
    monkeypatch.setattr("foresight.analysis.worker._sleep", _no_wait)
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


async def test_llm_client_clamps_the_sentiment_score():
    """The score is a confidence between zero and one.

    The column holds a number between zero and one, and the model does
    not always answer with one: a score of 99 is a confidence of one.
    """
    with respx.mock:
        respx.post("https://llm.example/v1/chat/completions").mock(
            return_value=_response(
                {
                    "translation": "你好",
                    "sentiment": "positive",
                    "sentiment_score": 99,
                    "issues": [],
                }
            )
        )
        client = LlmClient(_config())
        result = await client.analyze("hello")
    assert result.sentiment_score == 1.0


async def test_llm_client_clamps_a_negative_sentiment_score():
    """A score below zero is a confidence of zero."""
    with respx.mock:
        respx.post("https://llm.example/v1/chat/completions").mock(
            return_value=_response(
                {
                    "translation": "你好",
                    "sentiment": "negative",
                    "sentiment_score": -3,
                    "issues": [],
                }
            )
        )
        client = LlmClient(_config())
        result = await client.analyze("hello")
    assert result.sentiment_score == 0.0


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


async def test_llm_client_counts_the_tokens_of_an_answer_it_cannot_use():
    """The call was paid for, even when the answer cannot be parsed."""
    prompt_before = _sample("foresight_ai_tokens_total", {"type": "prompt"})
    completion_before = _sample("foresight_ai_tokens_total", {"type": "completion"})

    with respx.mock:
        route = respx.post("https://llm.example/v1/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": "not json"}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                },
            )
        )
        client = LlmClient(_config())
        with pytest.raises(LlmError):
            await client.analyze("hello")

    # The answer is asked for again with the strict prompt, so the two
    # calls are the ones the tokens are counted for.
    assert route.call_count == 2
    assert _sample("foresight_ai_tokens_total", {"type": "prompt"}) == prompt_before + 20
    assert (
        _sample("foresight_ai_tokens_total", {"type": "completion"})
        == completion_before + 10
    )


async def test_llm_client_uses_one_client_for_every_call():
    """One client for the life of the client: the connections are pooled."""
    clients: list[httpx.AsyncClient] = []

    def factory(**kwargs) -> httpx.AsyncClient:
        client = httpx.AsyncClient(**kwargs)
        clients.append(client)
        return client

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
        client = LlmClient(_config(), client_factory=factory)
        await client.analyze("hello")
        await client.analyze("hello again")
        assert len(clients) == 1
        assert not clients[0].is_closed
        await client.close()

    assert clients[0].is_closed


async def test_llm_client_asks_again_when_the_answer_has_the_wrong_shape():
    """A JSON answer without the four keys is not an answer either."""
    with respx.mock:
        route = respx.post("https://llm.example/v1/chat/completions").mock(
            side_effect=[
                _response({"result": {"translation": "你好"}}),
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


async def test_llm_client_raises_when_the_answer_has_the_wrong_shape():
    """An answer with the wrong shape is a failure, not an empty analysis."""
    with respx.mock:
        route = respx.post("https://llm.example/v1/chat/completions").mock(
            return_value=_response({"result": {"translation": "你好"}})
        )
        client = LlmClient(_config())
        with pytest.raises(LlmError):
            await client.analyze("hello")
    assert route.call_count == 2


async def test_llm_client_raises_a_permanent_error_for_a_rejected_request():
    """A request the endpoint refuses does not get better by trying again."""
    with respx.mock:
        route = respx.post("https://llm.example/v1/chat/completions").mock(
            return_value=httpx.Response(401, text="nope")
        )
        client = LlmClient(_config())
        with pytest.raises(LlmPermanentError):
            await client.analyze("hello")
    assert route.call_count == 1


async def test_llm_client_raises_a_permanent_error_for_an_unusable_url():
    """A url the client cannot use is a mistake of the configuration."""
    client = LlmClient(_config(base_url="not-a-url"))
    with pytest.raises(LlmPermanentError):
        await client.analyze("hello")


async def test_llm_client_does_not_call_the_model_when_the_ai_is_disabled():
    """A disabled AI is not called at all."""
    with respx.mock:
        route = respx.post("https://llm.example/v1/chat/completions").mock(
            return_value=_response(
                {
                    "translation": "你好",
                    "sentiment": "neutral",
                    "sentiment_score": 0.5,
                    "issues": [],
                }
            )
        )
        client = LlmClient(_config(enabled=False))
        with pytest.raises(LlmError):
            await client.analyze("hello")
    assert not route.called


async def test_llm_client_sends_the_system_prompt_as_a_system_message():
    """The prompt of the analysis is a system prompt, not a user message."""
    with respx.mock:
        route = respx.post("https://llm.example/v1/chat/completions").mock(
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
        await client.analyze("the login times out")

    sent = json.loads(route.calls[0].request.content)["messages"]
    assert sent[0]["role"] == "system"
    assert "JSON" in sent[0]["content"]
    assert sent[1]["role"] == "user"
    assert "the login times out" in sent[1]["content"]


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


async def test_worker_skips_an_empty_message(db_engine):
    """An empty message is marked as skipped, and not left pending.

    The message is handed over even when it carries no text: the worker
    is the one that decides, and it must mark the message as skipped —
    a message that stays `pending` is queued again on every restart.
    """
    repo = Repository(db_engine)
    await repo.apply_batch([WriteOp(OpKind.MESSAGE, _message())])

    with respx.mock:
        route = respx.post("https://llm.example/v1/chat/completions").mock(
            return_value=_response({"translation": "你好"})
        )
        worker = AnalysisWorker(repo, LlmClient(_config()), max_concurrency=1)
        await worker.start()
        worker.submit(1, "")
        await worker.stop()
        assert not route.called

    async with db_engine.connect() as conn:
        status = (
            await conn.execute(select(messages.c.analysis_status))
        ).scalar_one()
    assert status == "skipped"


async def test_worker_skips_a_message_that_is_only_a_url(db_engine):
    """A link and nothing else carries nothing to analyse."""
    repo = Repository(db_engine)
    await repo.apply_batch([WriteOp(OpKind.MESSAGE, _message())])

    with respx.mock:
        route = respx.post("https://llm.example/v1/chat/completions").mock(
            return_value=_response({"translation": "你好"})
        )
        worker = AnalysisWorker(repo, LlmClient(_config()), max_concurrency=1)
        await worker.start()
        worker.submit(1, "https://example.com/a-very-long-link")
        await worker.stop()
        assert not route.called

    async with db_engine.connect() as conn:
        status = (
            await conn.execute(select(messages.c.analysis_status))
        ).scalar_one()
    assert status == "skipped"


async def test_worker_skips_a_message_that_is_only_emoji(db_engine):
    """Emojis and nothing else carry nothing to analyse."""
    repo = Repository(db_engine)
    await repo.apply_batch([WriteOp(OpKind.MESSAGE, _message())])

    with respx.mock:
        route = respx.post("https://llm.example/v1/chat/completions").mock(
            return_value=_response({"translation": "你好"})
        )
        worker = AnalysisWorker(repo, LlmClient(_config()), max_concurrency=1)
        await worker.start()
        worker.submit(1, "😀😀")
        await worker.stop()
        assert not route.called

    async with db_engine.connect() as conn:
        status = (
            await conn.execute(select(messages.c.analysis_status))
        ).scalar_one()
    assert status == "skipped"


async def test_worker_analyses_a_message_with_a_link_in_it(db_engine):
    """A message that carries text is analysed, link or not."""
    repo = Repository(db_engine)
    await repo.apply_batch([WriteOp(OpKind.MESSAGE, _message())])

    with respx.mock:
        route = respx.post("https://llm.example/v1/chat/completions").mock(
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
        worker.submit(1, "look at https://example.com")
        await worker.stop()
        assert route.called

    async with db_engine.connect() as conn:
        status = (
            await conn.execute(select(messages.c.analysis_status))
        ).scalar_one()
    assert status == "done"


async def test_worker_marks_the_message_as_failed(db_engine, monkeypatch):
    monkeypatch.setattr("foresight.analysis.worker._sleep", _no_wait)
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


async def test_worker_fails_a_message_after_an_unexpected_error(db_engine):
    """An error the worker does not expect does not strand the message."""
    repo = Repository(db_engine)
    await repo.apply_batch([WriteOp(OpKind.MESSAGE, _message())])

    llm = _BrokenLlm(RuntimeError("boom"))
    worker = AnalysisWorker(repo, llm, max_concurrency=1, sleep=_no_wait)
    await worker.start()
    worker.submit(1, "hello")
    await worker.stop()

    async with db_engine.connect() as conn:
        row = (await conn.execute(select(message_analysis))).one()
        status = (
            await conn.execute(select(messages.c.analysis_status))
        ).scalar_one()
    assert status == "failed"
    assert "boom" in row.error


async def test_worker_does_not_retry_a_permanent_error(db_engine):
    """A missing key is a configuration error: one attempt is enough."""
    repo = Repository(db_engine)
    await repo.apply_batch([WriteOp(OpKind.MESSAGE, _message())])

    llm = _BrokenLlm(LlmPermanentError("FORESIGHT_LLM_API_KEY is not set"))
    worker = AnalysisWorker(repo, llm, max_concurrency=1)
    await worker.start()
    worker.submit(1, "hello")
    await worker.stop()

    assert llm.calls == 1
    async with db_engine.connect() as conn:
        row = (await conn.execute(select(message_analysis))).one()
        status = (
            await conn.execute(select(messages.c.analysis_status))
        ).scalar_one()
    assert status == "failed"
    assert "FORESIGHT_LLM_API_KEY" in row.error


async def test_worker_waits_between_the_attempts(db_engine, monkeypatch):
    """The worker waits through its own seam, not through the stdlib."""
    delays: list[float] = []

    async def record(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr("foresight.analysis.worker._sleep", record)
    repo = Repository(db_engine)
    await repo.apply_batch([WriteOp(OpKind.MESSAGE, _message())])

    llm = _BrokenLlm(RuntimeError("boom"))
    worker = AnalysisWorker(repo, llm, max_concurrency=1)
    await worker.start()
    worker.submit(1, "hello")
    await worker.stop()

    assert delays == [1.0, 2.0, 4.0]


async def test_worker_retries_the_analysis_three_times(db_engine):
    """One attempt and three retries are four attempts.

    The spec asks for three retries (spec §10.3), and the write queue
    counts them the same way: a message that failed is not given up on
    before the third retry.
    """
    repo = Repository(db_engine)
    await repo.apply_batch([WriteOp(OpKind.MESSAGE, _message())])

    llm = _BrokenLlm(RuntimeError("boom"))
    worker = AnalysisWorker(repo, llm, max_concurrency=1, sleep=_no_wait)
    await worker.start()
    worker.submit(1, "hello")
    await worker.stop()

    assert llm.calls == 4

    async with db_engine.connect() as conn:
        status = (
            await conn.execute(select(messages.c.analysis_status))
        ).scalar_one()
    assert status == "failed"


async def test_worker_fails_a_message_the_model_answered_with_the_wrong_shape(
    db_engine, monkeypatch
):
    """A JSON answer without the four keys is not a successful analysis."""
    monkeypatch.setattr("foresight.analysis.worker._sleep", _no_wait)
    repo = Repository(db_engine)
    await repo.apply_batch([WriteOp(OpKind.MESSAGE, _message())])

    with respx.mock:
        respx.post("https://llm.example/v1/chat/completions").mock(
            return_value=_response({"result": {"translation": "你好"}})
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
    assert status == "failed"
    assert row.error
    assert row.translated_text is None


async def test_worker_publishes_the_depth_of_the_queue(db_engine):
    """The gauge counts the messages that wait, not the entries of the queue."""
    repo = Repository(db_engine)
    await repo.apply_batch([WriteOp(OpKind.MESSAGE, _message())])

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
        # The gauge of the registry is process wide, so the test compares
        # the values with each other: it must not depend on what an
        # earlier test left behind.
        before = _sample("foresight_ai_queue_depth")
        assert worker.submit(1, "hello") is True
        assert _sample("foresight_ai_queue_depth") == before + 1
        await _wait_for_depth(before)
        assert _sample("foresight_ai_queue_depth") == before
        await worker.stop()
        # Nothing waits any more: the end markers of the queue are not
        # work, so they must not show up as a backlog.
        assert _sample("foresight_ai_queue_depth") == before


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


async def test_the_analysis_worker_stops_with_a_full_queue(db_engine):
    """The shutdown must not hang on a backlog.

    The worker puts the end of the queue behind the work that is still
    to be done, so a long backlog would keep the process alive for a
    long time — and the SIGTERM of the container would wait for it. The
    wait is bounded, and the workers are cancelled when the bound is
    reached.
    """

    class _SlowLlm:
        model = "slow-model"

        async def analyze(self, content: str):
            await asyncio.sleep(30)
            raise AssertionError("the analysis must not have been awaited")

    repo = Repository(db_engine)
    worker = AnalysisWorker(
        repo, _SlowLlm(), max_concurrency=1, drain_timeout=0.5
    )
    await worker.start()
    worker.submit(1, "hello")
    started = time.monotonic()
    await asyncio.wait_for(worker.stop(), timeout=20)
    elapsed = time.monotonic() - started

    assert elapsed < 10


async def test_worker_reports_the_messages_that_wait(db_engine):
    """The command waits for the queue to be empty before it ends."""
    repo = Repository(db_engine)

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
        assert worker.pending() == 0
        assert worker.submit(1, "hello") is True
        # The worker cannot take the message before the event loop gets a
        # turn, so the message is still waiting here.
        assert worker.pending() == 1
        await worker.stop()

    # Nothing waits any more: the end markers of the queue are not work.
    assert worker.pending() == 0
