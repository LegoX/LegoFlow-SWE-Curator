from __future__ import annotations

import subprocess
from pathlib import Path

from harbor.models.environment_type import EnvironmentType

from swegen.tools import harbor_runner


def test_run_harbor_agent_filters_local_dataset_by_task_id(
    tmp_path: Path, monkeypatch
) -> None:
    commands: list[list[str]] = []

    monkeypatch.setattr(harbor_runner, "harbor_cmd_base", lambda: ["harbor"])
    monkeypatch.setattr(harbor_runner.time, "time", lambda: 0)

    def fake_run(cmd: list[str], check: bool, capture_output: bool = False, text: bool = False):
        commands.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(harbor_runner.subprocess, "run", fake_run)

    code, result = harbor_runner.run_harbor_agent(
        "owner__repo-123",
        tmp_path / "dataset",
        tmp_path / "jobs",
        "nop",
        environment=EnvironmentType.DOCKER,
        capture_output=True,
    )

    assert code == 0
    assert result is None
    assert commands == [
        [
            "harbor",
            "run",
            "--agent",
            "nop",
            "-p",
            str(tmp_path / "dataset"),
            "-i",
            "owner__repo-123",
            "--jobs-dir",
            str(tmp_path / "jobs" / "owner__repo-123.nop.0"),
            "--env",
            "docker",
        ]
    ]
    assert "-t" not in commands[0]
