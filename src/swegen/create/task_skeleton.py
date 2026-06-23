from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from harbor.models.task.config import (
    AgentConfig,
    EnvironmentConfig,
    TaskConfig,
    VerifierConfig,
)

from .utils import strip_tests_prefix


@dataclass
class SkeletonParams:
    """Parameters for skeleton generation (all deterministic from git)."""

    repo_url: str
    head_sha: str
    base_sha: str
    pr_number: int


def generate_dockerfile(params: SkeletonParams) -> str:
    """
    Generate a minimal, language-agnostic Dockerfile skeleton.

    The skeleton contains:
    - Deterministic parts filled in (git clone, SHAs, bug.patch application)
    - TODO comments for Claude Code to fill in (runtime, deps, build)

    Claude Code will analyze the repo and fill in:
    - Language runtime installation
    - Package manager setup
    - Dependency installation
    - Build steps (if needed)
    - Post-patch rebuild (if needed)
    
    Git clone strategy:
    - Simple + robust: clone, then fetch the exact commit SHA.
    - NOTE: `head_sha` currently comes from the PR's HEAD branch tip (GitHub API).
    - If the PR was squash-merged/rebased, that commit may not be on any normal branch.
    - In that case, fetching `refs/pull/<n>/head` is a robust fallback without fetching ALL PR refs.
    """
    return f"""FROM ubuntu:24.04

RUN apt-get update && apt-get install -y \\
    git \\
    curl \\
    ca-certificates \\
    patch \\
    build-essential \\
    python3 \\
    python3-pip \\
    python3-venv \\
    python3-dev \\
    libffi-dev \\
    libssl-dev \\
    && rm -rf /var/lib/apt/lists/*

RUN curl -LsSf https://astral.sh/uv/install.sh | sh && \\
    mv /root/.local/bin/uv /usr/local/bin/uv

WORKDIR /app

RUN git clone {params.repo_url} src && \\
    cd src && \\
    (git fetch --depth 1 origin {params.head_sha} || git fetch --depth 1 origin "+refs/pull/{params.pr_number}/head:refs/remotes/origin/pr/{params.pr_number}") && \\
    git checkout --detach FETCH_HEAD && \\
    git submodule update --init --recursive

WORKDIR /app/src

ENV CI=true
RUN uv venv /opt/venv && \\
    (uv pip install --python /opt/venv/bin/python -e ".[test,dev]" || \\
     uv pip install --python /opt/venv/bin/python -e ".[tests,dev]" || \\
     uv pip install --python /opt/venv/bin/python -e ".[test]" || \\
     uv pip install --python /opt/venv/bin/python -e ".[dev]" || \\
     uv pip install --python /opt/venv/bin/python -e . || true) && \\
    if [ -f requirements.txt ]; then uv pip install --python /opt/venv/bin/python -r requirements.txt || true; fi && \\
    uv pip install --python /opt/venv/bin/python pytest pytest-asyncio pytest-cov

RUN git reset --hard

COPY bug.patch /tmp/bug.patch
RUN patch -p1 < /tmp/bug.patch && rm /tmp/bug.patch

RUN rm -rf /app/src/.git

ENV PATH="/opt/venv/bin:${{PATH}}"
WORKDIR /app/src
"""


def generate_test_sh(
    test_files: list[str],
) -> str:
    """
    Generate a minimal, language-agnostic test.sh skeleton.

    The skeleton contains:
    - Test file copy commands (deterministic)
    - TODO for Claude Code to fill in the actual test command

    Claude Code will analyze the repo and fill in:
    - Test framework detection
    - Correct test command with specific file paths
    """
    # Build copy commands for test files
    if test_files:
        copy_lines = []
        for tf in test_files:
            # Handle common test directory prefixes
            source_path = strip_tests_prefix(tf)
            target_dir = str(Path(tf).parent)
            copy_lines.append(f'mkdir -p "{target_dir}"')
            copy_lines.append(f'cp "/tests/{source_path}" "{tf}"')
        copy_commands = "\n".join(copy_lines)

    else:
        copy_commands = "# No test files to copy"
    test_command = "pytest -q " + " ".join(f'"{tf}"' for tf in test_files)
    if not test_files:
        test_command = 'echo "No test files were extracted" >&2; false'

    return f"""#!/bin/bash

cd /app/src

export CI=true
source /opt/venv/bin/activate

# Copy HEAD test files from /tests (overwrites BASE state)
{copy_commands}

uv pip install -e . --no-deps >/dev/null 2>&1 || true
{test_command}
test_status=$?

if [ $test_status -eq 0 ]; then
  echo 1 > /logs/verifier/reward.txt
else
  echo 0 > /logs/verifier/reward.txt
fi
exit "$test_status"
"""


def generate_solve_sh() -> str:
    """Generate solution/solve.sh script (same for all tasks)."""
    return """#!/bin/bash

set -euo pipefail
cd /app/src

patch -p1 < /solution/fix.patch
"""


def generate_instruction_md(instruction_data: dict) -> str:
    """Generate instruction.md file for Harbor format."""
    return instruction_data["instruction"]


def generate_task_toml(instruction_data: dict) -> str:
    """Generate task.toml config file for Harbor format.

    Uses Harbor's TaskConfig for proper serialization and validation.
    """
    config = TaskConfig(
        metadata={
            "difficulty": instruction_data.get("difficulty", "medium"),
            "category": instruction_data.get("category", "bugfix"),
            "tags": instruction_data.get("tags", []),
        },
        verifier=VerifierConfig(timeout_sec=600.0),
        agent=AgentConfig(timeout_sec=600.0),
        environment=EnvironmentConfig(
            build_timeout_sec=600.0,
            cpus=1,
            memory_mb=2048,
            storage_mb=10240,
        ),
    )
    return config.model_dump_toml()
