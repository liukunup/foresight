"""The liveness probe of the container.

The probe asks the API whether it is alive. It has to answer on every
deployment that is supported: the port comes from the environment or
from the configuration, and the probe carries no token — the route it
asks sits outside `/api` and says nothing about the dump.

The probe never prints a traceback. A failed probe is a failed probe:
the health log of the container would fill up with the same traceback
every few seconds, and the traceback says less than the exit code.
"""

from __future__ import annotations

import os
import sys
import urllib.request

from foresight.config import ConfigError, load_config

# The port the API listens on when nothing says otherwise.
DEFAULT_PORT = 8000

# The route that answers the probe. It sits outside `/api`, so the
# bearer token of the API does not apply to it.
PROBE_PATH = "/healthz"

# How long the probe waits for the answer, in seconds.
PROBE_TIMEOUT = 4.0


def resolve_port() -> int:
    """The port the API listens on.

    The environment has the last word: `FORESIGHT_API_PORT` moves the
    API, and the probe has to follow it. Without it the port of the
    configuration is used, and without a configuration the default.
    """
    raw = os.environ.get("FORESIGHT_API_PORT", "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    try:
        config = load_config(os.environ.get("FORESIGHT_CONFIG") or None)
    except ConfigError:
        # A configuration that cannot be read is not the end of the
        # probe: the default port is the best guess there is.
        return DEFAULT_PORT
    return config.api.port


def probe(port: int | None = None) -> bool:
    """Ask the API whether it is alive."""
    if port is None:
        port = resolve_port()
    url = f"http://127.0.0.1:{port}{PROBE_PATH}"
    try:
        with urllib.request.urlopen(url, timeout=PROBE_TIMEOUT) as response:
            return response.status == 200
    except Exception:
        # Every failure is a failed probe: a refused connection, a
        # timeout, a service that is not there yet. The reason belongs
        # in the log of the service, not in a traceback of the probe.
        return False


def main() -> None:
    """Run the probe and leave with the code the container reads."""
    sys.exit(0 if probe() else 1)
