from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_dockerfile_exists_and_uses_non_root_user():
    content = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "python:3.13-slim" in content
    assert "USER foresight" in content
    assert 'ENTRYPOINT ["foresight"]' in content


def test_compose_exposes_port_8000_and_persists_data():
    content = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert '"8000:8000"' in content
    assert "./data:/app/data" in content
    assert 'command: ["both"]' in content


def test_env_example_lists_required_secrets():
    content = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "DISCORD_TOKEN=" in content
    assert "FORESIGHT_API_TOKEN=" in content
