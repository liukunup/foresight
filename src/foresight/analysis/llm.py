"""OpenAI compatible client used for translation, sentiment and issue extraction."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from foresight.analysis.prompts import PROMPT_VERSION, build_prompt, build_strict_prompt
from foresight.config import AiConfig
from foresight.metrics import (
    ai_request_duration_seconds,
    ai_requests_total,
    ai_tokens_total,
)

logger = logging.getLogger(__name__)

VALID_SENTIMENTS = ("positive", "neutral", "negative", "mixed")


class LlmError(Exception):
    """Raised when the model call fails or the answer cannot be parsed."""


@dataclass
class LlmResult:
    """The analysis of one message, as the model answered it."""

    translation: str | None = None
    sentiment: str | None = None
    sentiment_score: float | None = None
    issues: list[str] = field(default_factory=list)
    model: str = ""
    prompt_version: str = PROMPT_VERSION
    latency_ms: int = 0
    usage: dict[str, int] = field(default_factory=dict)


class LlmClient:
    """Calls an OpenAI compatible chat completion endpoint.

    One call answers every question of the analysis: the translation, the
    sentiment, its score and the issues the message raises.
    """

    def __init__(self, config: AiConfig, client_factory: Any = None) -> None:
        self._config = config
        self._client_factory = client_factory or httpx.AsyncClient

    @property
    def endpoint(self) -> str:
        """The URL of the chat completion endpoint."""
        base = self._config.base_url.rstrip("/")
        return f"{base}/chat/completions"

    @property
    def model(self) -> str:
        """The name of the model the analyses are made with."""
        return self._config.model

    async def analyze(self, content: str) -> LlmResult:
        """Analyse one message.

        Raises :class:`LlmError` when the call itself failed, and when the
        model answered with something that is not JSON twice.
        """
        if not self._config.api_key:
            raise LlmError("FORESIGHT_LLM_API_KEY is not set")
        started = time.perf_counter()
        target = self._config.translation_target
        answer = await self._call(build_prompt(content, target))
        if answer is None:
            # The model answered with prose or with a fenced JSON object:
            # it gets one more chance, with a prompt that asks for raw JSON.
            logger.warning("the model did not answer with JSON, asking again")
            answer = await self._call(build_strict_prompt(content, target))
        if answer is None:
            raise LlmError("the model did not return valid JSON")
        parsed, body = answer
        return _to_result(parsed, body, self._config.model, _elapsed_ms(started))

    async def _call(self, prompt: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """Ask the model once.

        Returns the parsed answer together with the raw body, or None when
        the model answered with something that is not JSON.
        """
        started = time.perf_counter()
        try:
            body = await self._post(prompt)
            parsed = _parse_json(_content_of(body))
        except LlmError:
            ai_requests_total.labels(status="failed").inc()
            raise
        else:
            if parsed is None:
                ai_requests_total.labels(status="failed").inc()
                return None
            ai_requests_total.labels(status="ok").inc()
            _count_tokens(body)
            return parsed, body
        finally:
            ai_request_duration_seconds.observe(time.perf_counter() - started)

    async def _post(self, prompt: str) -> dict[str, Any]:
        """Send one chat completion request."""
        payload = {
            "model": self._config.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": f"Bearer {self._config.api_key}",
            "Content-Type": "application/json",
        }
        try:
            async with self._client_factory(
                timeout=self._config.timeout_seconds
            ) as client:
                response = await client.post(
                    self.endpoint, json=payload, headers=headers
                )
                response.raise_for_status()
                body = response.json()
        except Exception as exc:
            raise LlmError(str(exc)) from exc
        if not isinstance(body, dict):
            raise LlmError("unexpected response shape")
        return body


def _content_of(body: dict[str, Any]) -> str:
    """The answer of the model, as text."""
    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LlmError("unexpected response shape") from exc
    return content if isinstance(content, str) else ""


def _parse_json(text: str) -> dict[str, Any] | None:
    """The JSON object of an answer, or None when there is none.

    The model wraps its answer in markdown fences often enough that the
    fences are removed before the answer is given up on.
    """
    if not text:
        return None
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = candidate.strip("`")
        if candidate.lower().startswith("json"):
            candidate = candidate[4:]
        candidate = candidate.strip()
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _to_result(
    parsed: dict[str, Any], body: dict[str, Any], model: str, latency_ms: int
) -> LlmResult:
    """Turn the answer of the model into a result.

    A sentiment outside the four known values becomes `neutral`: the column
    is queried by those values, so a free-form string would break the
    analysis.
    """
    sentiment = parsed.get("sentiment")
    if sentiment not in VALID_SENTIMENTS:
        sentiment = "neutral"
    score = parsed.get("sentiment_score")
    try:
        score = float(score) if score is not None else None
    except (TypeError, ValueError):
        score = None
    issues = parsed.get("issues")
    if not isinstance(issues, list):
        issues = []
    translation = parsed.get("translation")
    if not isinstance(translation, str):
        translation = None
    usage = _usage_of(body)
    return LlmResult(
        translation=translation,
        sentiment=sentiment,
        sentiment_score=score,
        issues=[str(issue) for issue in issues],
        model=model,
        prompt_version=PROMPT_VERSION,
        latency_ms=latency_ms,
        usage={
            "prompt_tokens": _as_int(usage.get("prompt_tokens")),
            "completion_tokens": _as_int(usage.get("completion_tokens")),
        },
    )


def _count_tokens(body: dict[str, Any]) -> None:
    """Count the tokens the call consumed."""
    usage = _usage_of(body)
    prompt_tokens = _as_int(usage.get("prompt_tokens"))
    completion_tokens = _as_int(usage.get("completion_tokens"))
    if prompt_tokens:
        ai_tokens_total.labels(type="prompt").inc(prompt_tokens)
    if completion_tokens:
        ai_tokens_total.labels(type="completion").inc(completion_tokens)


def _usage_of(body: dict[str, Any]) -> dict[str, Any]:
    """The token counts of the endpoint, when it reported any."""
    usage = body.get("usage")
    return usage if isinstance(usage, dict) else {}


def _as_int(value: Any) -> int:
    """A number of tokens, or zero when the endpoint reported none."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _elapsed_ms(started: float) -> int:
    """The milliseconds that passed since the given moment."""
    return int((time.perf_counter() - started) * 1000)
