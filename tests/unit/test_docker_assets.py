import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_dockerfile_exists_and_uses_non_root_user():
    content = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    # The version of the base image is not pinned by the test: it is
    # Dependabot's business to move it. What matters is that it is a
    # slim image of the Python the project declares.
    assert re.search(r"FROM python:3\.\d+-slim", content), content
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


def test_env_example_lists_the_ports_and_the_configuration():
    """The README tells the operator to set them: the example has to."""
    content = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "FORESIGHT_PORT=" in content
    assert "FORESIGHT_CONFIG=" in content
    assert "FORESIGHT_API_HOST=" in content
    assert "FORESIGHT_API_PORT=" in content


@pytest.mark.parametrize(
    ("profile", "service", "image", "host", "port", "volume"),
    [
        ("postgresql", "postgres", "postgres:17-alpine", "postgres", "5432", "postgres-data"),
        ("mysql", "mysql", "mysql:8.4", "mysql", "3306", "mysql-data"),
    ],
)
def test_compose_runs_each_database_after_a_healthy_service(
    profile, service, image, host, port, volume
):
    """One compose file covers every database, selected by profile."""
    content = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert f"image: {image}" in content
    assert f"  {service}:" in content
    assert f'profiles: ["{profile}"]' in content
    assert "condition: service_healthy" in content
    assert "condition: service_completed_successfully" in content
    assert "chown 10001:10001 /app/data" in content
    assert '"${FORESIGHT_PORT:-8000}:8000"' in content
    assert 'command: ["both"]' in content
    assert "${FORESIGHT_DB_PASSWORD:-}" in content
    assert f"{volume}:/var/lib/" in content
    assert "\nvolumes:\n" in content
    assert f"\n  {volume}:" in content

    settings = (ROOT / "config.example.toml").read_text(encoding="utf-8")
    assert f'host = "{host}"' in settings
    assert f"port = {port}" in settings


def test_compose_selects_rustfs_with_a_profile():
    content = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "image: rustfs/rustfs:" in content
    assert 'profiles: ["rustfs"]' in content
    assert "rustfs-data:/data" in content
    assert "mc mb --ignore-existing s3/foresight" in content
    assert "${FORESIGHT_S3_ACCESS_KEY_ID:-}" in content
    assert "${FORESIGHT_S3_SECRET_ACCESS_KEY:-}" in content


def test_compose_initializes_the_bucket_with_a_generic_service_name():
    """The bucket init service is not tied to the rustfs profile name."""
    content = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "  s3-init:" in content
    assert "rustfs-bucket" not in content
    assert "image: minio/mc:" in content
    assert "minio/minio" not in content


def test_no_minio_backend_support():
    """MinIO is not advertised as a storage backend, only as a client tool."""
    paths = [ROOT / "README.md"]
    paths += list((ROOT / "src").rglob("*.py"))
    paths += list((ROOT / "docs").rglob("*.md"))
    for path in paths:
        assert "minio" not in path.read_text(encoding="utf-8").lower(), path


def test_env_example_lists_the_profiles():
    content = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "COMPOSE_PROFILES=" in content
    assert "mysql,rustfs" in content
    assert "FORESIGHT_DATABASE_BACKEND=" in content
    assert "FORESIGHT_MEDIA_BACKEND=" in content
    assert "DISCORD_CHANNEL_IDS=" in content


def test_compose_passes_the_env_file_to_the_service():
    content = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "env_file:" in content
    assert "path: .env" in content


def test_config_example_documents_every_backend():
    content = (ROOT / "config.example.toml").read_text(encoding="utf-8")
    assert 'backend = "sqlite"' in content
    assert "sqlite | postgresql | mysql" in content
    assert 'backend = "local"' in content
    assert 'endpoint_url = "http://rustfs:9000"' in content
