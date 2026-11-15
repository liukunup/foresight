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


def test_script_refuses_an_empty_target_dir(tmp_path: Path):
    """An empty target must not become a directory in the root.

    The target is removed before the skill is copied into it, so an
    empty target would remove a directory of the root.
    """
    result = subprocess.run(
        [
            "bash",
            str(SCRIPT),
            "--target-dir",
            "",
            "--connection-file",
            str(tmp_path / "connection.toml"),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "target" in result.stderr
    assert not Path("/foresight-analytics").exists()


def test_script_refuses_the_root_as_the_target_dir(tmp_path: Path):
    """The root is not a target: the skill would be removed from it."""
    result = subprocess.run(
        [
            "bash",
            str(SCRIPT),
            "--target-dir",
            "/",
            "--connection-file",
            str(tmp_path / "connection.toml"),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "target" in result.stderr
    assert not Path("/foresight-analytics").exists()


def test_the_connection_file_is_not_readable_by_others(tmp_path: Path):
    """The connection file belongs to the operator alone.

    The file names the environment variable that holds the token, and
    it is written with the mode of a private file.
    """
    target = tmp_path / "skills"
    connection = tmp_path / "config" / "foresight" / "connection.toml"
    result = _install(target, connection)
    assert result.returncode == 0, result.stderr
    assert connection.stat().st_mode & 0o777 == 0o600


def test_the_connection_file_holds_the_path_of_the_helper(tmp_path: Path):
    """The helper is called with a path that does not depend on the
    directory the agent happens to work in."""
    target = tmp_path / "skills"
    connection = tmp_path / "config" / "foresight" / "connection.toml"
    result = _install(target, connection)
    assert result.returncode == 0, result.stderr
    content = connection.read_text(encoding="utf-8")
    assert "helper_path" in content
    assert str(target / "foresight-analytics" / "scripts" / "query.py") in content


def test_the_skill_calls_the_helper_with_a_path_of_its_own():
    """The helper sits next to the skill, not in the working directory.

    The command of the skill must work from any directory the agent
    works in.
    """
    skill = (
        Path(__file__).resolve().parents[2]
        / "skills"
        / "foresight-analytics"
        / "SKILL.md"
    )
    content = skill.read_text(encoding="utf-8")
    assert "python3 scripts/query.py" not in content
    assert "helper_path" in content
