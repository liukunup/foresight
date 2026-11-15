"""Logging configuration shared by all entry points."""

from __future__ import annotations

import logging

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"

# Handler installed by the previous ``setup_logging`` call. It is the only
# handler this module owns, so it is the only one that may be closed.
_managed_handler: logging.Handler | None = None


def setup_logging(level: str = "INFO") -> None:
    """Configure the root logger.

    The requested level is applied even when the root logger already has
    handlers (e.g. on a second ``_load()`` call or under a test runner).

    Handlers installed by a previous ``setup_logging`` call are replaced.
    Every other handler is kept as it is: ``basicConfig(force=True)`` closes
    the handlers it removes, so a handler that owns a resource (a
    ``FileHandler``, for instance) would be left closed and unusable. Such
    handlers are therefore detached before the call and re-attached after it.
    """
    global _managed_handler

    root = logging.getLogger()
    preserved: list[logging.Handler] = []
    for handler in root.handlers[:]:
        root.removeHandler(handler)
        if handler is _managed_handler:
            handler.close()
        else:
            preserved.append(handler)

    logging.basicConfig(level=level.upper(), format=_FORMAT, force=True)
    _managed_handler = root.handlers[0] if root.handlers else None

    for handler in preserved:
        root.addHandler(handler)
