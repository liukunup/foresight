import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "selfhost.sh"


def _prepare(tmp_path):
    for name in ("scripts/selfhost.sh", ".env.example", "config.example.toml"):
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / name, target)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(
        '#!/bin/sh\n'
        '[ "$1 $2" = "compose version" ] && exit 0\n'
        'printf "%s\\n" "$*" >> "$DOCKER_LOG"\n',
        encoding="utf-8",
    )
    docker.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["DOCKER_LOG"] = str(tmp_path / "docker.log")
    env.pop("DISCORD_TOKEN", None)
    env.pop("DISCORD_CHANNEL_IDS", None)
    return env


def _run(tmp_path, env):
    return subprocess.run(
        ["bash", str(tmp_path / "scripts" / "selfhost.sh")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _settings(path):
    result = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            result[key.strip()] = value.strip()
    return result


def test_makefile_exposes_the_one_click_target():
    content = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "bash scripts/selfhost.sh" in content
    assert ".PHONY" in content
    targets = {
        line.split(":", 1)[0]
        for line in content.splitlines()
        if line
        and not line.startswith((" ", "\t", "#", "."))
        and ":" in line
        and ":=" not in line
    }
    assert targets == {"selfhost", "status", "down", "restart", "clean", "uninstall"}


def test_selfhost_generates_secrets_initializes_and_starts(tmp_path):
    env = _prepare(tmp_path)
    env["DISCORD_TOKEN"] = "discord-token"
    env["DISCORD_CHANNEL_IDS"] = "111, 222"

    result = _run(tmp_path, env)
    assert result.returncode == 0, result.stderr

    settings = _settings(tmp_path / ".env")
    assert settings["DISCORD_TOKEN"] == "discord-token"
    assert settings["DISCORD_CHANNEL_IDS"] == "111,222"
    assert settings["COMPOSE_PROFILES"] == "mysql,rustfs"
    assert settings["FORESIGHT_DATABASE_BACKEND"] == "mysql"
    assert settings["FORESIGHT_MEDIA_BACKEND"] == "s3"
    assert settings["FORESIGHT_S3_ENDPOINT_URL"] == "http://rustfs:9000"
    assert len(settings["FORESIGHT_API_TOKEN"]) >= 32
    assert len(settings["FORESIGHT_DB_PASSWORD"]) >= 32
    assert len(settings["FORESIGHT_S3_ACCESS_KEY_ID"]) >= 16
    assert settings["FORESIGHT_S3_ACCESS_KEY_ID"].isalnum()
    assert settings["FORESIGHT_S3_ACCESS_KEY_ID"] == settings["FORESIGHT_S3_ACCESS_KEY_ID"].upper()
    assert len(settings["FORESIGHT_S3_SECRET_ACCESS_KEY"]) >= 32
    assert (tmp_path / "config.toml").exists()
    assert (tmp_path / ".env").stat().st_mode & 0o077 == 0

    commands = (tmp_path / "docker.log").read_text(encoding="utf-8").splitlines()
    assert commands[0] == "compose up -d mysql rustfs"
    assert commands[1] == "compose run --rm foresight init-db"
    assert commands[2] == "compose up -d"
    assert commands[3] == "compose ps"

    assert "ForeSight 自托管已就绪" in result.stdout
    assert settings["FORESIGHT_API_TOKEN"] in result.stdout
    assert "MESSAGE CONTENT INTENT" in result.stdout


def test_selfhost_reuses_existing_secrets(tmp_path):
    env = _prepare(tmp_path)
    env["DISCORD_TOKEN"] = "discord-token"
    env["DISCORD_CHANNEL_IDS"] = "111"

    assert _run(tmp_path, env).returncode == 0
    first = _settings(tmp_path / ".env")
    assert _run(tmp_path, env).returncode == 0
    second = _settings(tmp_path / ".env")

    for key in (
        "FORESIGHT_API_TOKEN",
        "FORESIGHT_DB_PASSWORD",
        "FORESIGHT_S3_ACCESS_KEY_ID",
        "FORESIGHT_S3_SECRET_ACCESS_KEY",
    ):
        assert second[key] == first[key]


def test_selfhost_rejects_invalid_channel_ids_before_starting_docker(tmp_path):
    env = _prepare(tmp_path)
    env["DISCORD_TOKEN"] = "discord-token"
    env["DISCORD_CHANNEL_IDS"] = "not-a-number"

    result = _run(tmp_path, env)
    assert result.returncode != 0
    assert "频道 ID 必须是数字" in result.stderr
    assert not (tmp_path / "docker.log").exists()


def test_selfhost_requires_credentials_in_non_interactive_runs(tmp_path):
    env = _prepare(tmp_path)

    result = _run(tmp_path, env)
    assert result.returncode != 0
    assert "DISCORD_TOKEN" in result.stderr
    assert not (tmp_path / "docker.log").exists()


def _make(tmp_path, target, env):
    shutil.copy(ROOT / "Makefile", tmp_path / "Makefile")
    (tmp_path / ".env").write_text("DISCORD_TOKEN=token\n", encoding="utf-8")
    (tmp_path / "config.toml").write_text("[discord]\n", encoding="utf-8")
    data = tmp_path / "data" / "media"
    data.mkdir(parents=True)
    (data / "file").write_text("attachment", encoding="utf-8")
    return subprocess.run(
        ["make", target],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_make_restart_restarts_the_stack_without_touching_files(tmp_path):
    env = _prepare(tmp_path)
    result = _make(tmp_path, "restart", env)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "docker.log").read_text(encoding="utf-8").splitlines() == [
        "compose --profile mysql --profile rustfs restart"
    ]
    assert (tmp_path / ".env").exists()
    assert (tmp_path / "config.toml").exists()
    assert (tmp_path / "data" / "media" / "file").exists()


def test_make_clean_keeps_configuration_and_volumes(tmp_path):
    env = _prepare(tmp_path)
    result = _make(tmp_path, "clean", env)
    assert result.returncode == 0, result.stderr
    commands = (tmp_path / "docker.log").read_text(encoding="utf-8").splitlines()
    assert commands == ["compose --profile mysql --profile rustfs down --remove-orphans"]
    assert not (tmp_path / "data").exists()
    assert (tmp_path / ".env").exists()
    assert (tmp_path / "config.toml").exists()


def test_make_uninstall_removes_volumes_configuration_and_image(tmp_path):
    env = _prepare(tmp_path)
    result = _make(tmp_path, "uninstall", env)
    assert result.returncode == 0, result.stderr
    commands = (tmp_path / "docker.log").read_text(encoding="utf-8").splitlines()
    assert commands[0] == "compose --profile mysql --profile rustfs down -v --remove-orphans"
    assert commands[1] == "image rm liukunup/foresight:latest"
    assert not (tmp_path / ".env").exists()
    assert not (tmp_path / "config.toml").exists()
    assert not (tmp_path / "data").exists()
