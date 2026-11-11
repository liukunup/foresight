"""Logging configuration shared by all entry points."""

from __future__ import annotations

import logging


def setup_logging(level: str = "INFO") -> None:
    """Configure the root logger.

    ``force=True`` makes the call idempotent: the requested level is applied
    even when the root logger already has handlers (e.g. on a second
    ``_load()`` call or under a test runner).
    """
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        force=True,
    )
