"""discord.py wiring: intents, event registration, startup.

The callbacks below are the only place that talks to discord.py. They hand
the payload to the :class:`~foresight.events.EventHandler`, whose methods are
synchronous: they only map an event and queue a write operation, so there is
nothing to await.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import discord
from discord.ext import commands

from foresight.events import EventHandler

logger = logging.getLogger(__name__)


def build_intents() -> discord.Intents:
    intents = discord.Intents.default()
    intents.messages = True
    intents.message_content = True
    intents.reactions = True
    intents.guilds = True
    return intents


def build_bot(config: Any, handler: EventHandler) -> commands.Bot:
    bot = commands.Bot(command_prefix="!foresight ", intents=build_intents())

    @bot.event
    async def on_message(message: discord.Message) -> None:
        if bot.user is not None and message.author.id == bot.user.id:
            return
        handler.handle_message(message)

    @bot.event
    async def on_raw_message_edit(payload: discord.RawMessageUpdateEvent) -> None:
        handler.handle_raw_edit(payload)

    @bot.event
    async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent) -> None:
        handler.handle_raw_delete(payload)

    @bot.event
    async def on_raw_reaction_add(payload: discord.RawReactionActionEvent) -> None:
        handler.handle_raw_reaction_add(payload)

    @bot.event
    async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent) -> None:
        handler.handle_raw_reaction_remove(payload)

    return bot


def require_token() -> str:
    token = os.environ.get("DISCORD_TOKEN", "").strip()
    if not token:
        raise RuntimeError("DISCORD_TOKEN environment variable is required")
    return token
