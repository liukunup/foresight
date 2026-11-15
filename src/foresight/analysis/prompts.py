"""Prompt templates for message analysis."""

from __future__ import annotations

PROMPT_VERSION = "v1"

SYSTEM_PROMPT = (
    "You analyse chat messages. Always answer with a single JSON object and "
    "no extra text. The JSON object must contain exactly these keys: "
    '"translation", "sentiment", "sentiment_score", "issues". '
    '"translation" is the message translated into {target}. '
    '"sentiment" is one of "positive", "neutral", "negative", "mixed". '
    '"sentiment_score" is a number between 0 and 1. '
    '"issues" is an array of short strings listing the problems, requests or '
    "feedback the message raises; use an empty array when there are none."
)

STRICT_SUFFIX = (
    "\n\nIMPORTANT: reply with raw JSON only. Do not wrap it in markdown fences "
    "and do not add any explanation."
)


def build_system_prompt(translation_target: str, *, strict: bool = False) -> str:
    """The system prompt of the analysis.

    The prompt is the system prompt of the request: it tells the model what
    to answer and in which shape, and it does not carry the message itself.

    The strict variant is the prompt of the second attempt: it asks for raw
    JSON, for the model that wrapped its answer in markdown fences.
    """
    system = SYSTEM_PROMPT.format(target=translation_target)
    if strict:
        return system + STRICT_SUFFIX
    return system


def build_user_prompt(content: str) -> str:
    """The message that is analysed.

    The message is the user message of the request: it is the text the
    analysis is about.
    """
    return f"Message:\n{content}"
