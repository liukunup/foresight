"""OpenAI compatible client used for translation, sentiment and issue extraction."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, cast

import httpx

from foresight.analysis.prompts import (
    PROMPT_VERSION,
    build_system_prompt,
    build_user_prompt,
)
from foresight.config import AiConfig
from foresight.metrics import (
    ai_request_duration_seconds,
    ai_requests_total,
    ai_tokens_total,
)

logger = logging.getLogger(__name__)

VALID_SENTIMENTS = ("positive", "neutral", "negative", "mixed")

# The four results of the analysis. An answer that does not carry all of
# them is not the analysis of the message: it is answered again, with the
# strict prompt.
EXPECTED_KEYS = ("translation", "sentiment", "sentiment_score", "issues")

# The status codes that are worth another attempt: the endpoint was busy
# or it asked us to slow down. Every other 4xx is a mistake of the request
# itself, and it does not get better by trying again.
RETRYABLE_STATUS = frozenset({408, 429})


class LlmError(Exception):
    """Raised when the model call fails or the answer cannot be parsed."""


class LlmPermanentError(LlmError):
    """Raised when another attempt cannot help.

    A missing key or a disabled AI is a mistake of the configuration: the
    call is refused right away, instead of being retried.
    """


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
        self._client: httpx.AsyncClient | None = None

    @property
    def endpoint(self) -> str:
        """The URL of the chat completion endpoint."""
        base = self._config.base_url.rstrip("/")
        return f"{base}/chat/completions"

    @property
    def model(self) -> str:
        """The name of the model the analyses are made with."""
        return self._config.model

    async def __aenter__(self) -> "LlmClient":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.close()

    async def close(self) -> None:
        """Close the client of the endpoint."""
        client = self._client
        self._client = None
        if client is not None:
            await client.aclose()

    def _get_client(self) -> httpx.AsyncClient:
        """The client of the endpoint.

        One client for the life of the client: the connections are pooled
        and every call reuses them, instead of paying for a handshake of
        its own.
        """
        if self._client is None:
            self._client = cast(
                httpx.AsyncClient,
                self._client_factory(timeout=self._config.timeout_seconds),
            )
        return self._client

    async def analyze(self, content: str) -> LlmResult:
        """Analyse one message.

        Raises :class:`LlmError` when the call itself failed, and when the
        model did not answer with a usable JSON object twice. A
        :class:`LlmPermanentError` is raised when another attempt cannot
        help.
        """
        if not self._config.enabled:
            raise LlmPermanentError("the AI analysis is disabled")
        if not self._config.api_key:
            raise LlmPermanentError("FORESIGHT_LLM_API_KEY is not set")
        started = time.perf_counter()
        target = self._config.translation_target
        answer = await self._call(
            build_system_prompt(target), build_user_prompt(content)
        )
        if answer is None:
            # The model answered with prose, with a fenced JSON object or
            # with an object of the wrong shape: it gets one more chance,
            # with a prompt that asks for raw JSON.
            logger.warning(
                "the model did not answer with a usable JSON object, asking again"
            )
            answer = await self._call(
                build_system_prompt(target, strict=True),
                build_user_prompt(content),
            )
        if answer is None:
            raise LlmError("the model did not return a usable answer")
        parsed, body = answer
        return _to_result(parsed, body, self._config.model, _elapsed_ms(started))

    async def _call(
        self, system_prompt: str, user_prompt: str
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """Ask the model once.

        Returns the parsed answer together with the raw body, or None when
        the model answered with something that is not a usable answer.
        """
        started = time.perf_counter()
        try:
            body = await self._post(system_prompt, user_prompt)
            # The tokens are counted for every answer that came back, even
            # for one that cannot be used: the call was paid for anyway.
            _count_tokens(body)
            parsed = _parse_json(_content_of(body))
        except LlmError:
            ai_requests_total.labels(status="failed").inc()
            raise
        else:
            if parsed is None or not _has_expected_keys(parsed):
                ai_requests_total.labels(status="failed").inc()
                return None
            ai_requests_total.labels(status="ok").inc()
            return parsed, body
        finally:
            ai_request_duration_seconds.observe(time.perf_counter() - started)

    async def _post(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        """Send one chat completion request."""
        payload = {
            "model": self._config.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": f"Bearer {self._config.api_key}",
            "Content-Type": "application/json",
        }
        client = self._get_client()
        try:
            response = await client.post(
                self.endpoint, json=payload, headers=headers
            )
            response.raise_for_status()
            body = response.json()
        except (httpx.InvalidURL, httpx.UnsupportedProtocol) as exc:
            # The url of the endpoint is wrong, or it speaks another
            # protocol: another attempt cannot make it right.
            raise LlmPermanentError(f"the endpoint url is not usable: {exc}") from exc
        except httpx.HTTPStatusError as exc:
            raise _status_error(exc) from exc
        except httpx.TransportError as exc:
            # A transport error is a hiccup of the network: another attempt
            # can still succeed.
            raise LlmError(f"the endpoint could not be reached: {exc}") from exc
        except httpx.HTTPError as exc:
            raise LlmError(str(exc)) from exc
        except ValueError as exc:
            raise LlmError(f"the endpoint answered with a broken body: {exc}") from exc
        except Exception as exc:
            raise LlmError(str(exc)) from exc
        if not isinstance(body, dict):
            raise LlmError("unexpected response shape")
        return body


def _status_error(exc: httpx.HTTPStatusError) -> LlmError:
    """The error of a request the endpoint answered with a status code.

    A request the endpoint refuses does not get better by trying again:
    the key is wrong or the request itself is malformed. A server error and
    a request that came too early do get another attempt.
    """
    status = exc.response.status_code
    if status >= 500 or status in RETRYABLE_STATUS:
        return LlmError(f"the endpoint answered with {status}: {exc}")
    return LlmPermanentError(f"the endpoint refused the request with {status}: {exc}")


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


def _has_expected_keys(parsed: dict[str, Any]) -> bool:
    """Whether the answer carries the four results of the analysis.

    An answer without them is not an answer: it is the JSON of something
    else, and it must not be stored as an empty analysis.
    """
    return all(key in parsed for key in EXPECTED_KEYS)


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
