from pathlib import Path

from legoflow_curator.create.claude_code_runner import _build_sdk_runtime_env


def test_sdk_runtime_env_isolates_home_and_mirrors_auth(tmp_path: Path) -> None:
    task_dir = tmp_path / "tasks" / "example"
    task_dir.mkdir(parents=True)

    env, claude_home = _build_sdk_runtime_env(
        "runtime-key",
        "http://127.0.0.1:4123",
        "claude-opus-4-8",
        task_dir,
    )

    assert env["ANTHROPIC_API_KEY"] == "runtime-key"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "runtime-key"
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:4123"
    assert env["ANTHROPIC_MODEL"] == "claude-opus-4-8"
    assert claude_home == task_dir.parent / ".legoflow-curator" / "claude-home"
    assert env["HOME"] == str(claude_home)
    assert env["CLAUDE_CONFIG_DIR"] == str(claude_home / ".claude")
    assert claude_home.is_dir()


def test_sdk_runtime_env_honors_explicit_isolated_home(
    tmp_path: Path, monkeypatch
) -> None:
    custom_home = tmp_path / "isolated-home"
    monkeypatch.setenv("LEGOFLOW_CURATOR_CLAUDE_HOME", str(custom_home))

    env, claude_home = _build_sdk_runtime_env(None, None, None, tmp_path / "task")

    assert claude_home == custom_home
    assert env == {
        "HOME": str(custom_home),
        "CLAUDE_CONFIG_DIR": str(custom_home / ".claude"),
    }
