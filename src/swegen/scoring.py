"""Difficulty scoring for SWE-gen tasks.

This module is the single source of truth for difficulty scoring. Both callers
share the exact same algorithm, weights, and thresholds:

  * `swegen create` (via `score_task`, reading a task directory on disk), and
  * the dataset tagger `tools/tag_task_metadata.py` (via `score_from_text`,
    operating on patch/test diff text).

Methodology (weighted, log-scaled; no API calls):
  Five dimensions are each scaled to 1.0-5.0, combined by weight, then mapped to
  a final 1.0-10.0 score.

  | dimension              | weight | signal                                  |
  | ---------------------- | ------ | --------------------------------------- |
  | patch_scope            | 0.30   | changed lines + files + hunks           |
  | logic_complexity       | 0.25   | new defs/classes + control-flow adds    |
  | context_breadth        | 0.20   | distinct directories touched            |
  | test_complexity        | 0.15   | test lines + test files                 |
  | instruction_complexity | 0.10   | instruction length                      |

Score buckets: easy <= 4.0, medium <= 7.0, hard > 7.0.
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field


class TaskScore(BaseModel):
    task_id: str
    score: float = 0.0  # 1.0-10.0
    label: str = "medium"  # easy/medium/hard
    dim_scores: dict[str, float] = Field(default_factory=dict)
    patch_files: int = 0
    patch_lines: int = 0
    patch_hunks: int = 0
    test_files: int = 0
    test_lines: int = 0
    instruction_chars: int = 0
    directories: int = 0


# Dimension weights (canonical calibration, shared by all callers).
WEIGHTS = {
    "patch_scope": 0.30,
    "logic_complexity": 0.25,
    "context_breadth": 0.20,
    "test_complexity": 0.15,
    "instruction_complexity": 0.10,
}


# ---------------------------------------------------------------------------
# Core scoring (single implementation used by every caller)
# ---------------------------------------------------------------------------

def _scale(raw: float, *, easy: float, hard: float) -> float:
    """Log-scale a raw dimension signal into the 1.0-5.0 range."""
    if raw <= easy:
        return 1.0
    if raw >= hard:
        return 5.0
    ratio = math.log1p(raw - easy) / math.log1p(hard - easy)
    return round(1.0 + ratio * 4.0, 2)


def score_from_metrics(
    *,
    patch_lines: int,
    patch_files: int,
    patch_hunks: int,
    new_defs: int,
    control_flow: int,
    directories: int,
    test_lines: int,
    test_files: int,
    instr_chars: int,
) -> dict[str, Any]:
    """Compute difficulty score/label from already-extracted metrics.

    This is the one place the scoring formula, weights, and label thresholds
    live. Text- and disk-based entry points both funnel their metrics here.
    """
    dim_scores = {
        "patch_scope": _scale(
            patch_lines + patch_files * 8 + patch_hunks * 3, easy=20, hard=260
        ),
        "logic_complexity": _scale(
            new_defs * 10 + control_flow * 4 + patch_lines, easy=20, hard=220
        ),
        "context_breadth": _scale(
            directories * 15 + patch_files * 4 + patch_hunks, easy=15, hard=120
        ),
        "test_complexity": _scale(test_lines + test_files * 10, easy=35, hard=350),
        "instruction_complexity": _scale(instr_chars, easy=1500, hard=12000),
    }
    weighted_sum = sum(dim_scores[name] * WEIGHTS[name] for name in WEIGHTS)
    final_score = round(1.0 + (weighted_sum - 1.0) * 2.05, 1)
    final_score = max(1.0, min(10.0, final_score))
    if final_score <= 4.0:
        label = "easy"
    elif final_score <= 7.0:
        label = "medium"
    else:
        label = "hard"
    return {
        "difficulty_score": final_score,
        "difficulty_label": label,
        "dim_scores": dim_scores,
        "patch_stats": {"lines": patch_lines, "hunks": patch_hunks, "files": patch_files},
    }


# ---------------------------------------------------------------------------
# Diff text parsing (used by both entry points)
# ---------------------------------------------------------------------------

_CONTROL_FLOW_RE = re.compile(
    r"\b(if|elif|else|for|while|try|except|with|match|case|switch|catch)\b"
)
_NEW_DEF_RE = re.compile(r"^\+\s*(def|async\s+def|class|function|const|let|var)\b")


def parse_patch_text(patch_text: str) -> dict[str, Any]:
    """Parse a unified diff into the metrics the scorer consumes."""
    files = 0
    hunks = 0
    additions = 0
    deletions = 0
    dirs: set[str] = set()
    new_defs = 0
    control_flow = 0

    for line in patch_text.splitlines():
        if line.startswith("diff --git "):
            files += 1
            parts = line.split()
            if len(parts) >= 4:
                current_file = parts[3].removeprefix("b/")
                parent = str(Path(current_file).parent)
                if parent and parent != ".":
                    dirs.add(parent)
            continue
        if line.startswith("@@"):
            hunks += 1
            continue
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            additions += 1
            if _NEW_DEF_RE.search(line):
                new_defs += 1
            if _CONTROL_FLOW_RE.search(line):
                control_flow += 1
        elif line.startswith("-"):
            deletions += 1

    return {
        "files": files,
        "hunks": hunks,
        "additions": additions,
        "deletions": deletions,
        "dirs": dirs,
        "new_defs": new_defs,
        "control_flow": control_flow,
    }


# ---------------------------------------------------------------------------
# Text entry point (used by tools/tag_task_metadata.py over JSONL datasets)
# ---------------------------------------------------------------------------

def score_from_text(patch_text: str, test_text: str, instruction: str) -> dict[str, Any]:
    """Score difficulty from patch, test-patch, and instruction text."""
    p = parse_patch_text(patch_text)
    t = parse_patch_text(test_text) if test_text else {"additions": 0, "deletions": 0, "files": 0}
    return score_from_metrics(
        patch_lines=p["additions"] + p["deletions"],
        patch_files=p["files"],
        patch_hunks=p["hunks"],
        new_defs=p["new_defs"],
        control_flow=p["control_flow"],
        directories=len(p["dirs"]),
        test_lines=t["additions"] + t["deletions"],
        test_files=t["files"],
        instr_chars=len(instruction),
    )


# ---------------------------------------------------------------------------
# Disk entry point (used by `swegen create` over task directories)
# ---------------------------------------------------------------------------

def _count_test_files(task_dir: Path) -> tuple[int, int]:
    """Count test files and total test lines in a task's tests/ directory."""
    tests_dir = task_dir / "tests"
    if not tests_dir.exists():
        return 0, 0
    test_files = 0
    test_lines = 0
    for f in tests_dir.rglob("*"):
        if f.is_file() and f.name != "test.sh":
            test_files += 1
            try:
                test_lines += len(f.read_text(errors="replace").splitlines())
            except Exception:
                pass
    return test_files, test_lines


def score_task(task_dir: Path, task_id: Optional[str] = None) -> TaskScore:
    """Score a single task directory.

    Reads solution/fix.patch, tests/, and instruction.md, then feeds the
    extracted metrics through the shared `score_from_metrics` scorer so the
    result matches the dataset tagger exactly.
    """
    tid = task_id or task_dir.name

    patch_path = task_dir / "solution" / "fix.patch"
    patch_text = patch_path.read_text(errors="replace") if patch_path.exists() else ""
    p = parse_patch_text(patch_text)

    test_files, test_lines = _count_test_files(task_dir)

    instr_path = task_dir / "instruction.md"
    instr_chars = 0
    if instr_path.exists():
        try:
            instr_chars = len(instr_path.read_text(errors="replace"))
        except Exception:
            pass

    patch_lines = p["additions"] + p["deletions"]
    result = score_from_metrics(
        patch_lines=patch_lines,
        patch_files=p["files"],
        patch_hunks=p["hunks"],
        new_defs=p["new_defs"],
        control_flow=p["control_flow"],
        directories=len(p["dirs"]),
        test_lines=test_lines,
        test_files=test_files,
        instr_chars=instr_chars,
    )

    return TaskScore(
        task_id=tid,
        score=result["difficulty_score"],
        label=result["difficulty_label"],
        dim_scores=result["dim_scores"],
        patch_files=p["files"],
        patch_lines=patch_lines,
        patch_hunks=p["hunks"],
        test_files=test_files,
        test_lines=test_lines,
        instruction_chars=instr_chars,
        directories=len(p["dirs"]),
    )


def score_tasks_batch(
    task_dirs: list[Path],
    task_ids: Optional[list[str]] = None,
) -> list[TaskScore]:
    """Score multiple task directories."""
    results = []
    for i, td in enumerate(task_dirs):
        tid = task_ids[i] if task_ids and i < len(task_ids) else None
        try:
            results.append(score_task(td, tid))
        except Exception as e:
            print(f"  Warning: failed to score {td.name}: {e}")
    return results


def update_task_toml_difficulty(task_dir: Path, score: TaskScore) -> None:
    """Update the difficulty field in task.toml with the computed score.

    Also adds a [scoring] section with the numeric score.
    """
    toml_path = task_dir / "task.toml"
    if not toml_path.exists():
        return

    content = toml_path.read_text()

    content = re.sub(
        r'difficulty\s*=\s*"[^"]*"',
        f'difficulty = "{score.label}"',
        content,
    )

    scoring_section = f"\n[scoring]\ndifficulty_score = {score.score}\ndifficulty_label = \"{score.label}\"\n"
    if "[scoring]" in content:
        content = re.sub(
            r'\[scoring\].*?(?=\n\[|\Z)',
            scoring_section.strip() + "\n",
            content,
            flags=re.DOTALL,
        )
    else:
        content = content.rstrip() + "\n" + scoring_section

    toml_path.write_text(content)
