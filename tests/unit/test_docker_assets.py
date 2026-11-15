from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_dockerfile_exists_and_uses_non_root_user():
    content = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "python:3.13-slim" in content
    assert "USER foresight" in content
    assert 'ENTRYPOINT ["foresight"]' in content


def test_compose_exposes_port_8000_and_persists_data():
    content = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    # The mapping itself, not a comment that mentions it: the host port
    # is moved by FORESIGHT_PORT, the port of the container is not.
    assert '"${FORESIGHT_PORT:-8000}:8000"' in content
    assert "./data:/app/data" in content
    assert 'command: ["both"]' in content


def test_env_example_lists_required_secrets():
    content = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "DISCORD_TOKEN=" in content
    assert "FORESIGHT_API_TOKEN=" in content


def test_the_health_check_probes_the_liveness_route():
    """The probe must work on every supported configuration.

    The probe asks the route that needs no token, and it asks it on the
    port the API really listens on.
    """
    content = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    _, _, healthcheck = content.partition("HEALTHCHECK")
    assert healthcheck, "the image has no health check"
    assert "foresight-healthcheck" in healthcheck
    # The metrics endpoint is guarded by its own token: a probe that
    # asks it would fail on every deployment that sets one.
    assert "/metrics" not in healthcheck


def test_the_image_installs_the_locked_dependencies():
    """The build must not resolve the versions again.

    Without the lock file every build resolves the declared ranges
    again, and the image of today is not the image of tomorrow.
    """
    content = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "uv.lock" in content
    assert "uv sync --frozen" in content


def test_the_runtime_image_does_not_carry_the_sources():
    """The package is installed in the virtualenv.

    A copy of the sources beside it can drift away from the installed
    package, and the image carries everything twice.
    """
    content = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    _, _, runtime = content.partition("AS runtime")
    assert runtime, "the image has no runtime stage"
    assert "src" not in runtime


def test_compose_gives_the_data_directory_to_the_service():
    """The service writes as uid 10001: the directory must belong to it.

    The directory of the host belongs to the user of the host, so the
    compose file gives it to the user of the container before the
    service starts. Without that the collector cannot write.
    """
    content = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "chown 10001:10001 /app/data" in content
    assert "service_completed_successfully" in content
