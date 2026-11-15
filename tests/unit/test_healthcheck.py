"""Tests for the liveness probe of the container.

The probe is what the container tells the orchestrator with: it must
answer on every deployment that is supported, and it must never fill the
log of the container with a traceback.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest

from foresight.healthcheck import (
    DEFAULT_PORT,
    PROBE_PATH,
    main,
    probe,
    resolve_port,
)


def _write_config(tmp_path: Path, port: int) -> Path:
    """Write a configuration that names the port of the API."""
    path = tmp_path / "config.toml"
    path.write_text(
        f"""
[discord]
channel_ids = [1]

[database]
backend = "sqlite"
path = "{tmp_path / 'data' / 'foresight.db'}"

[api]
host = "127.0.0.1"
port = {port}
""",
        encoding="utf-8",
    )
    return path


def test_the_probe_uses_the_port_of_the_environment(monkeypatch: pytest.MonkeyPatch):
    """The port of the environment moves the API, and the probe follows."""
    monkeypatch.setenv("FORESIGHT_API_PORT", "9001")
    assert resolve_port() == 9001


def test_the_probe_uses_the_port_of_the_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Without the environment the configuration has the last word."""
    config = _write_config(tmp_path, 9100)
    monkeypatch.delenv("FORESIGHT_API_PORT", raising=False)
    monkeypatch.setenv("FORESIGHT_CONFIG", str(config))
    assert resolve_port() == 9100


def test_the_probe_falls_back_to_the_default_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A deployment without a port gets the default one."""
    monkeypatch.delenv("FORESIGHT_API_PORT", raising=False)
    monkeypatch.setenv("FORESIGHT_CONFIG", str(tmp_path / "nope.toml"))
    assert resolve_port() == DEFAULT_PORT


def test_the_probe_asks_the_liveness_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The probe asks the route that needs no token."""
    asked: list[str] = []

    class _Response:
        status = 200

        def __enter__(self) -> "_Response":
            return self

        def __exit__(self, *args: Any) -> None:
            return None

    def fake_urlopen(url: str, timeout: float = 0) -> _Response:
        asked.append(url)
        return _Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    assert probe(port=8123) is True
    assert asked == [f"http://127.0.0.1:8123{PROBE_PATH}"]


def test_the_probe_fails_when_the_service_does_not_answer(
    monkeypatch: pytest.MonkeyPatch,
):
    """A service that is not there is a failed probe and not a crash."""
    def fake_urlopen(url: str, timeout: float = 0) -> Any:
        raise OSError("the connection is refused")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    assert probe(port=8123) is False


def test_the_probe_leaves_with_the_code_of_the_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    """The container reads the exit code of the probe.

    A failed probe must not print a traceback: the log of the container
    would fill up with the same traceback every few seconds.
    """
    def fake_urlopen(url: str, timeout: float = 0) -> Any:
        raise OSError("the connection is refused")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setenv("FORESIGHT_API_PORT", "8123")

    with pytest.raises(SystemExit) as caught:
        main()

    assert caught.value.code == 1
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    assert captured.err == ""


def test_the_probe_succeeds_when_the_service_answers(
    monkeypatch: pytest.MonkeyPatch,
):
    class _Response:
        status = 200

        def __enter__(self) -> "_Response":
            return self

        def __exit__(self, *args: Any) -> None:
            return None

    monkeypatch.setattr(
        "urllib.request.urlopen", lambda url, timeout=0: _Response()
    )
    monkeypatch.setenv("FORESIGHT_API_PORT", "8123")

    with pytest.raises(SystemExit) as caught:
        main()

    assert caught.value.code == 0
