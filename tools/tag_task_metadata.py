#!/usr/bin/env python3
"""Difficulty scoring + 4-tag metadata tagging for SWE task datasets.

This is the single, canonical tagging tool for LegoFlow Curator. It scores difficulty
(static, no API) and generates the 4-tag metadata (LLM) in one pass, operating
over unified JSONL datasets (`<datasets-dir>/<id>/tasks.jsonl`).

Methodology:
  * Difficulty: the 5-dimension weighted, log-scaled model defined in
      `legoflow_curator.scoring` (`score_from_text`) — the same scorer `legoflow-curator create`
      uses, so difficulty is identical across the pipeline and the databoard.
  * Tags: exactly 4 in order [language, area, topic, bug_class] via an LLM,
      where area ∈ {backend, frontend, fullstack, cli, library, framework}.

Each record in tasks.jsonl provides: instance_id, problem_statement, patch, and
optionally test_patch. Output is written to `<datasets-dir>/<id>/tags.jsonl`,
one JSON object per task; the run is resumable (already-tagged instance_ids are
skipped).

The datasets directory is resolved from (in priority order):
  1. --datasets-dir CLI flag
  2. TAGGING_DATASETS_DIR environment variable
  3. ./datasets relative to the current working directory
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any, TypeVar

import requests

# Difficulty scoring is defined once in the legoflow-curator package and shared with
# `legoflow-curator create`. Prefer the co-located package so this tool always uses the
# same scorer it ships with, regardless of any other installed legoflow_curator.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from legoflow_curator.scoring import score_from_text

DATASET_IDS = ["self_made", "swe_rebench", "swe_rebench_v2", "openswe_filtered", "scale_swe"]


def resolve_datasets_dir(cli_value: str | None) -> Path:
    """Resolve the datasets root from CLI flag, env var, or CWD default."""
    if cli_value:
        return Path(cli_value).expanduser()
    env_value = os.environ.get("TAGGING_DATASETS_DIR")
    if env_value:
        return Path(env_value).expanduser()
    return Path.cwd() / "datasets"

AREA_TAGS = {"backend", "frontend", "fullstack", "cli", "library", "framework"}
TAG_COUNT = 4

MAX_INSTRUCTION_CHARS = 4000
MAX_PATCH_CHARS = 12000
MAX_TEST_CHARS = 8000
TAG_COMPLETION_TOKENS = 1024
T = TypeVar("T")

TAG_SYSTEM_PROMPT = """You generate SWE task tags.

IMPORTANT: You MUST respond with a valid JSON object ONLY. No markdown, no explanation outside JSON.
Do not think step by step. Output the JSON object immediately.

TAGS:
Generate exactly 4 tags in this order:
1. Primary programming language, e.g. "python", "javascript", "typescript", "go", "rust", "java", "ruby", "cpp"
2. Tier/area. Choose ONE from: "backend", "frontend", "fullstack", "cli", "library", "framework"
3. Framework/library name, e.g. "fastapi", "django", "react", "nextjs", "axios", "express"; OR a specific topic, e.g. "http", "async", "testing"
4. Bug class: a domain-independent short label for the defect mechanism, e.g. "missing-fallback", "incomplete-validation", "wrong-default", "type-handling-inconsistency", "missing-metadata-propagation"

The bug class should describe the logical failure mode, not the affected framework.

Examples:
- FastAPI backend bug caused by missing default fallback: ["python", "backend", "fastapi", "missing-fallback"]
- Next.js UI bug caused by incorrect state propagation: ["typescript", "frontend", "nextjs", "missing-state-propagation"]
- Python CLI bug caused by incomplete option parsing: ["python", "cli", "argparse", "incomplete-parsing"]

Return format:
{"tags": ["language", "tier-or-area", "framework-or-topic", "bug-class"]}
"""

@dataclass(frozen=True)
class TaggerConfig:
    model: str
    api_key: str
    base_url: str
    timeout_sec: float = 120.0
    retries: int = 3
    retry_delay_sec: float = 2.0


# ---------------------------------------------------------------------------
# LLM tag generation (ported from harbor)
# ---------------------------------------------------------------------------

def _patch_changed_files(patch_text: str) -> list[str]:
    files: list[str] = []
    for line in patch_text.splitlines():
        if not line.startswith("diff --git "):
            continue
        parts = line.split()
        if len(parts) >= 4:
            files.append(parts[3].removeprefix("b/"))
    return files


def _sanitize_for_openai(text: str) -> str:
    return text.replace("\x00", "")


def build_tag_prompt(
    record: dict[str, Any],
    *,
    max_instruction: int = MAX_INSTRUCTION_CHARS,
    max_patch: int = MAX_PATCH_CHARS,
    max_test: int = MAX_TEST_CHARS,
    max_files: int = 100,
) -> str:
    instruction = (record.get("problem_statement") or "")[:max_instruction]
    patch_text = record.get("patch") or ""
    if len(patch_text) > max_patch:
        patch_text = patch_text[:max_patch] + "\n... (truncated)"
    changed_files = _patch_changed_files(patch_text)[:max_files]
    test_text = (record.get("test_patch") or "")[:max_test]

    return _sanitize_for_openai(
        "\n".join(
            [
                f"Task name: {record.get('instance_id', '')}",
                "",
                "Instruction:",
                instruction,
                "",
                "Changed files from solution patch:",
                "\n".join(f"- {p}" for p in changed_files) or "- none detected",
                "",
                "Patch excerpt:",
                patch_text,
                "",
                "Test metadata:",
                test_text or "No test snippets available.",
                "",
                'Generate the 4 tags for this task using the required order. Return only: {"tags": [...]}',
            ]
        )
    )


# Progressive truncation levels for the tag prompt. Some endpoints hang on
# mid-size prompts (4-12 KB) even though the same task tags fine with a shorter
# excerpt; when a normal-size prompt fails we retry with tighter caps. The final
# level is an aggressive floor for tasks whose patch/tests are very large.
TRUNCATION_LEVELS = [
    {"max_instruction": MAX_INSTRUCTION_CHARS, "max_patch": MAX_PATCH_CHARS, "max_test": MAX_TEST_CHARS, "max_files": 100},
    {"max_instruction": 1500, "max_patch": 3000, "max_test": 1500, "max_files": 40},
    {"max_instruction": 800, "max_patch": 1200, "max_test": 600, "max_files": 20},
    {"max_instruction": 500, "max_patch": 500, "max_test": 0, "max_files": 10},
    {"max_instruction": 400, "max_patch": 400, "max_test": 0, "max_files": 5},
]


def _strip_code_fence(content: str) -> str:
    stripped = content.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _extract_json(content: str) -> str:
    stripped = _strip_code_fence(content)
    start = stripped.find("{")
    if start == -1:
        raise RuntimeError("LLM response did not contain a JSON object")
    # scan for the first balanced {...} object so trailing text is ignored
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(stripped)):
        ch = stripped[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return stripped[start : i + 1]
    raise RuntimeError("LLM response did not contain a balanced JSON object")


def _normalize_tag(value: Any) -> str:
    if value is None:
        return ""
    tag = str(value).strip().lower()
    tag = re.sub(r"\s+", "-", tag)
    tag = re.sub(r"[^a-z0-9_.+-]+", "-", tag)
    tag = re.sub(r"-{2,}", "-", tag).strip("-")
    return tag


def normalize_tags(raw_tags: Any) -> list[str]:
    if not isinstance(raw_tags, list):
        raise RuntimeError("LLM response field 'tags' is not a list")
    if any(tag is None for tag in raw_tags):
        raise RuntimeError("LLM response field 'tags' contains null")
    tags = [_normalize_tag(tag) for tag in raw_tags]
    tags = [tag for tag in tags if tag]
    if len(tags) < TAG_COUNT:
        raise RuntimeError(f"LLM generated only {len(tags)} tags")
    tags = tags[:TAG_COUNT]
    if tags[1] not in AREA_TAGS:
        raise RuntimeError(f"LLM generated invalid area tag: {tags[1]}")
    return tags


def _chat_completion_content(config: TaggerConfig, system_prompt: str, user_prompt: str) -> str:
    base_url = config.base_url.rstrip("/")
    if not base_url:
        raise RuntimeError("OpenAI-compatible base URL is empty")
    response = requests.post(
        f"{base_url}/chat/completions",
        headers={
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": config.model,
            "messages": [
                {"role": "system", "content": _sanitize_for_openai(system_prompt)},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": TAG_COMPLETION_TOKENS,
            # Qwen3.6 endpoint leaks reasoning into content unless thinking is
            # explicitly disabled; this yields clean JSON directly.
            "chat_template_kwargs": {"enable_thinking": False},
        },
        timeout=config.timeout_sec,
    )
    response.raise_for_status()
    payload = response.json()
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise RuntimeError("LLM response did not contain choices")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        # some endpoints emit reasoning_content when thinking mode leaks
        content = message.get("reasoning_content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("LLM returned empty content")
    return content


def _with_retries(config: TaggerConfig, operation: Callable[[], T]) -> T:
    attempts = max(1, config.retries + 1)
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except Exception as exc:
            last_error = exc
            if attempt >= attempts:
                break
            time.sleep(config.retry_delay_sec * attempt)
    raise last_error or RuntimeError("LLM operation failed")


def generate_tags_for_record(record: dict[str, Any], config: TaggerConfig) -> list[str]:
    """Generate 4 tags, falling back to progressively shorter prompts.

    The first level uses the full-size excerpt. If the endpoint fails (e.g. it
    hangs on a mid-size prompt), each subsequent level truncates the patch,
    instruction, and test text further so the request can still complete.
    """
    last_error: Exception | None = None
    for level in TRUNCATION_LEVELS:
        prompt = build_tag_prompt(record, **level)

        def _once() -> list[str]:
            content = _chat_completion_content(config, TAG_SYSTEM_PROMPT, prompt)
            parsed = json.loads(_extract_json(content))
            if isinstance(parsed, list):
                return normalize_tags(parsed)
            if isinstance(parsed, dict):
                return normalize_tags(parsed.get("tags"))
            raise RuntimeError("LLM response is neither an object nor a tag list")

        try:
            return _with_retries(config, _once)
        except Exception as exc:
            last_error = exc
    raise last_error or RuntimeError("tag generation failed at all truncation levels")


def tag_one_record(record: dict[str, Any], config: TaggerConfig) -> dict[str, Any]:
    """Score difficulty (static) + generate 4 tags (LLM). Raises on failure."""
    scored = score_from_text(
        record.get("patch") or "",
        record.get("test_patch") or "",
        record.get("problem_statement") or "",
    )
    tags = generate_tags_for_record(record, config)
    return {
        "instance_id": record["instance_id"],
        "difficulty_score": scored["difficulty_score"],
        "difficulty_label": scored["difficulty_label"],
        "tags": tags,
        "bug_class": tags[3],
        "patch_stats": scored["patch_stats"],
    }


# ---------------------------------------------------------------------------
# Dataset driver
# ---------------------------------------------------------------------------

def tag_dataset(dataset_id: str, datasets_dir: Path, config: TaggerConfig, jobs: int, max_count: int | None) -> list[str]:
    dataset_dir = datasets_dir / dataset_id
    tasks_jsonl = dataset_dir / "tasks.jsonl"
    output_jsonl = dataset_dir / "tags.jsonl"

    if not tasks_jsonl.exists():
        print(f"ERROR: {tasks_jsonl} does not exist, skipping")
        return []

    print(f"\n{'='*80}\nTagging dataset: {dataset_id}\ninput: {tasks_jsonl}\noutput: {output_jsonl}\nconcurrency: {jobs}\n{'='*80}")

    tagged_ids: set[str] = set()
    if output_jsonl.exists():
        with output_jsonl.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    tagged_ids.add(json.loads(line)["instance_id"])
                except Exception:
                    pass
        print(f"already tagged (skipped): {len(tagged_ids)}")

    records: list[dict[str, Any]] = []
    with tasks_jsonl.open("r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            rec = json.loads(line)
            if rec["instance_id"] not in tagged_ids:
                records.append(rec)
                if max_count and len(records) >= max_count:
                    break

    if not records:
        print("nothing to tag, skipping")
        return []
    print(f"to tag: {len(records)}")

    completed = 0
    failed: list[str] = []
    write_lock = Lock()

    with output_jsonl.open("a", encoding="utf-8") as out:
        with ThreadPoolExecutor(max_workers=max(1, jobs)) as executor:
            futures = {executor.submit(tag_one_record, rec, config): rec for rec in records}
            for future in as_completed(futures):
                rec = futures[future]
                try:
                    tagged = future.result()
                    with write_lock:
                        out.write(json.dumps(tagged, ensure_ascii=False) + "\n")
                        out.flush()
                    completed += 1
                    if completed % 200 == 0:
                        print(f"  progress: {completed}/{len(records)} ({completed/len(records)*100:.1f}%)")
                except Exception as exc:
                    failed.append(rec["instance_id"])
                    if len(failed) <= 5:
                        print(f"  ERROR {rec['instance_id']}: {exc}")

    print(f"✓ {dataset_id}: succeeded {completed}, failed {len(failed)}")
    return failed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="harbor-style difficulty + 4-tag tagging (JSONL datasets)")
    parser.add_argument("--dataset", choices=[*DATASET_IDS, "all"], default="all")
    parser.add_argument("--model", default=os.environ.get("TAGGING_MODEL", "Qwen3.6-35B-A3B"))
    parser.add_argument("--api-key", default=os.environ.get("TAGGING_API_KEY", "dummy-cf"))
    parser.add_argument("--base-url", default=os.environ.get("TAGGING_API_BASE_URL", "http://llm.jierungogogo.com/v1"))
    parser.add_argument("--jobs", type=int, default=64)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--max-count", type=int, default=None, help="per-dataset cap (for testing)")
    parser.add_argument(
        "--datasets-dir",
        default=None,
        help="datasets root dir (default: $TAGGING_DATASETS_DIR or ./datasets)",
    )
    args = parser.parse_args(argv)

    datasets_dir = resolve_datasets_dir(args.datasets_dir)
    config = TaggerConfig(
        model=args.model,
        api_key=args.api_key,
        base_url=args.base_url,
        retries=args.retries,
    )
    datasets = DATASET_IDS if args.dataset == "all" else [args.dataset]

    all_failed: dict[str, list[str]] = {}
    for dataset_id in datasets:
        failed = tag_dataset(dataset_id, datasets_dir, config, args.jobs, args.max_count)
        if failed:
            all_failed[dataset_id] = failed

    # Auto-retry failed cases (lower concurrency is more stable)
    if all_failed:
        print(f"\n{'='*80}\nretrying failed cases (jobs=8)\n{'='*80}")
        retry_config = TaggerConfig(
            model=args.model, api_key=args.api_key, base_url=args.base_url, retries=5, retry_delay_sec=2.0
        )
        for dataset_id in list(all_failed):
            tag_dataset(dataset_id, datasets_dir, retry_config, 8, None)

    print("\n" + "=" * 80 + "\nall tagging complete\n" + "=" * 80)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

