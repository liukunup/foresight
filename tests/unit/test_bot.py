"""Tests for the discord.py wiring.

The Discord layer itself is out of scope for the test suite, but
:func:`~foresight.bot.require_token` is pure configuration logic with a
specified failure mode, so it is worth covering on its own.
"""

from __future__ import annotations

import pytest

from foresight.bot import require_token


def test_require_token_returns_the_configured_token(monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "a-discord-token")
    assert require_token() == "a-discord-token"


def test_require_token_rejects_a_missing_token(monkeypatch):
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="DISCORD_TOKEN"):
        require_token()


@pytest.mark.parametrize("value", ["", "   "])
def test_require_token_rejects_an_empty_token(monkeypatch, value):
    monkeypatch.setenv("DISCORD_TOKEN", value)
    with pytest.raises(RuntimeError, match="DISCORD_TOKEN"):
        require_token()
