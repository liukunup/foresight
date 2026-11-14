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


def build_prompt(content: str, translation_target: str) -> str:
    """The prompt that asks for the analysis of one message."""
    system = SYSTEM_PROMPT.format(target=translation_target)
    return f"{system}\n\nMessage:\n{content}"


def build_strict_prompt(content: str, translation_target: str) -> str:
    """The prompt of the second attempt, when the answer was not JSON.

    The model wraps its answer in markdown fences often enough that one
    stricter try is worth it.
    """
    return build_prompt(content, translation_target) + STRICT_SUFFIX
