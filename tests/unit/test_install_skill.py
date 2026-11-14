import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "install_skill.sh"


def _install(target: Path, connection: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "bash",
            str(SCRIPT),
            "--target-dir",
            str(target),
            "--connection-file",
            str(connection),
            "--api-base-url",
            "http://127.0.0.1:8000",
        ],
        capture_output=True,
        text=True,
    )


def test_script_is_valid_bash():
    result = subprocess.run(
        ["bash", "-n", str(SCRIPT)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def test_script_copies_skill_and_writes_connection(tmp_path: Path):
    target = tmp_path / "skills"
    connection = tmp_path / "config" / "foresight" / "connection.toml"
    result = _install(target, connection)
    assert result.returncode == 0, result.stderr
    assert (target / "foresight-analytics" / "SKILL.md").exists()
    assert (target / "foresight-analytics" / "reference" / "schema.md").exists()
    assert connection.exists()
    content = connection.read_text(encoding="utf-8")
    assert "http://127.0.0.1:8000" in content
    assert "FORESIGHT_API_TOKEN" in content


def test_script_can_be_run_twice(tmp_path: Path):
    target = tmp_path / "skills"
    connection = tmp_path / "config" / "foresight" / "connection.toml"
    first = _install(target, connection)
    second = _install(target, connection)
    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert (target / "foresight-analytics" / "SKILL.md").exists()
    assert (target / "foresight-analytics" / "scripts" / "query.py").exists()
    assert "http://127.0.0.1:8000" in connection.read_text(encoding="utf-8")
