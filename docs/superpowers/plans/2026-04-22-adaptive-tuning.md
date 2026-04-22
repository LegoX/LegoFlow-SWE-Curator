# Adaptive Parameter Tuning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add AI agent adaptive parameter tuning to the SWE-gen pipeline so Claude Code can monitor success rates and adjust create/collect parameters per language.

**Architecture:** `inputs.yaml` is the single source of truth for per-language parameters and status. A `read_params.py` helper lets shell scripts read from it. All 8 `create_{lang}.sh` scripts are refactored to read params from `inputs.yaml` instead of hardcoding. README.md and CLAUDE.md are updated with the new mechanism.

**Tech Stack:** Python 3.12, PyYAML, Bash

---

### Task 1: Create `scripts/read_params.py`

**Files:**
- Create: `repos/SWE-gen/scripts/read_params.py`
- Test: `repos/SWE-gen/tests/test_read_params.py`

- [ ] **Step 1: Write the failing test**

Create `repos/SWE-gen/tests/test_read_params.py`:

```python
import subprocess
import tempfile
import os
import pytest

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "scripts", "read_params.py")


def _write_yaml(tmp_path, content):
    p = tmp_path / "inputs.yaml"
    p.write_text(content)
    return str(p)


def test_read_params_py(tmp_path):
    yaml_content = """\
global:
  monitor_interval_min: 30
  pr_pool_min_threshold: 100
languages:
  py:
    enabled: true
    params:
      timeout: 3200
      cc_timeout: 2400
      n_concurrent: 16
"""
    yaml_path = _write_yaml(tmp_path, yaml_content)
    result = subprocess.run(
        ["python", SCRIPT, "--lang", "py", "--inputs-yaml", yaml_path],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert "TIMEOUT=3200" in result.stdout
    assert "CC_TIMEOUT=2400" in result.stdout
    assert "N_CONCURRENT=16" in result.stdout


def test_read_params_rust_defaults(tmp_path):
    yaml_content = """\
global:
  monitor_interval_min: 30
languages:
  rust:
    enabled: true
    params:
      timeout: 4000
      cc_timeout: 3300
      n_concurrent: 20
"""
    yaml_path = _write_yaml(tmp_path, yaml_content)
    result = subprocess.run(
        ["python", SCRIPT, "--lang", "rust", "--inputs-yaml", yaml_path],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert "TIMEOUT=4000" in result.stdout
    assert "CC_TIMEOUT=3300" in result.stdout
    assert "N_CONCURRENT=20" in result.stdout


def test_read_params_missing_lang(tmp_path):
    yaml_content = """\
global:
  monitor_interval_min: 30
languages:
  py:
    enabled: true
    params:
      timeout: 3200
      cc_timeout: 2400
      n_concurrent: 16
"""
    yaml_path = _write_yaml(tmp_path, yaml_content)
    result = subprocess.run(
        ["python", SCRIPT, "--lang", "java", "--inputs-yaml", yaml_path],
        capture_output=True, text=True,
    )
    assert result.returncode != 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd repos/SWE-gen && python -m pytest tests/test_read_params.py -v`
Expected: FAIL (script does not exist yet)

- [ ] **Step 3: Write minimal implementation**

Create `repos/SWE-gen/scripts/read_params.py`:

```python
#!/usr/bin/env python3
"""Read per-language params from inputs.yaml and output shell variables."""
import argparse
import sys
from pathlib import Path

import yaml

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--lang", required=True, help="Language key (py, js, ts, go, c, cpp, java, rust)")
    parser.add_argument("--inputs-yaml", default="inputs.yaml", help="Path to inputs.yaml")
    args = parser.parse_args()

    yaml_path = Path(args.inputs_yaml)
    if not yaml_path.exists():
        print(f"Error: {yaml_path} not found", file=sys.stderr)
        sys.exit(1)

    with open(yaml_path) as f:
        config = yaml.safe_load(f)

    langs = config.get("languages", {})
    if args.lang not in langs:
        print(f"Error: language '{args.lang}' not found in {yaml_path}", file=sys.stderr)
        sys.exit(1)

    params = langs[args.lang].get("params", {})
    print(f"TIMEOUT={params.get('timeout', 3200)}")
    print(f"CC_TIMEOUT={params.get('cc_timeout', 2400)}")
    print(f"N_CONCURRENT={params.get('n_concurrent', 16)}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd repos/SWE-gen && python -m pytest tests/test_read_params.py -v`
Expected: All 3 tests PASS

- [ ] **Step 5: Commit**

```bash
cd repos/SWE-gen
git add scripts/read_params.py tests/test_read_params.py
git commit -m "feat: add read_params.py helper to read inputs.yaml"
```

---

### Task 2: Populate `inputs.yaml` with full config

**Files:**
- Modify: `repos/SWE-gen/inputs.yaml`

- [ ] **Step 1: Write the full inputs.yaml**

Replace `repos/SWE-gen/inputs.yaml` with:

```yaml
# Adaptive parameter tuning configuration for SWE-gen pipeline.
# AI agent (Claude Code) reads status and adjusts params per language.

global:
  monitor_interval_min: 30
  pr_pool_min_threshold: 100
  collect_pr_defaults:
    repo_num: 100
    max_prs_per_repo: 50
  restart_policy:
    allowed: true
    zero_success_cycles: 3
  param_bounds:
    timeout: { min: 2400, max: 5400, step: 400 }
    cc_timeout: { min: 1800, max: 4200, step: 300 }
    n_concurrent: { min: 4, max: 32, step: 4 }

languages:
  py:
    enabled: true
    params:
      timeout: 3200
      cc_timeout: 2400
      n_concurrent: 16
    status:
      total_success: 0
      total_failed: 0
      total_filtered: 0
      pr_pool_remaining: 0
      last_round_success: 0
      last_round_failed: 0
      success_rate: 0.0
      zero_success_streak: 0
      last_checked: null
  js:
    enabled: true
    params:
      timeout: 3200
      cc_timeout: 2400
      n_concurrent: 16
    status:
      total_success: 0
      total_failed: 0
      total_filtered: 0
      pr_pool_remaining: 0
      last_round_success: 0
      last_round_failed: 0
      success_rate: 0.0
      zero_success_streak: 0
      last_checked: null
  ts:
    enabled: true
    params:
      timeout: 3600
      cc_timeout: 3000
      n_concurrent: 16
    status:
      total_success: 0
      total_failed: 0
      total_filtered: 0
      pr_pool_remaining: 0
      last_round_success: 0
      last_round_failed: 0
      success_rate: 0.0
      zero_success_streak: 0
      last_checked: null
  go:
    enabled: true
    params:
      timeout: 3200
      cc_timeout: 2400
      n_concurrent: 16
    status:
      total_success: 0
      total_failed: 0
      total_filtered: 0
      pr_pool_remaining: 0
      last_round_success: 0
      last_round_failed: 0
      success_rate: 0.0
      zero_success_streak: 0
      last_checked: null
  c:
    enabled: true
    params:
      timeout: 3200
      cc_timeout: 2400
      n_concurrent: 16
    status:
      total_success: 0
      total_failed: 0
      total_filtered: 0
      pr_pool_remaining: 0
      last_round_success: 0
      last_round_failed: 0
      success_rate: 0.0
      zero_success_streak: 0
      last_checked: null
  cpp:
    enabled: true
    params:
      timeout: 3200
      cc_timeout: 2400
      n_concurrent: 16
    status:
      total_success: 0
      total_failed: 0
      total_filtered: 0
      pr_pool_remaining: 0
      last_round_success: 0
      last_round_failed: 0
      success_rate: 0.0
      zero_success_streak: 0
      last_checked: null
  java:
    enabled: true
    params:
      timeout: 3200
      cc_timeout: 2400
      n_concurrent: 16
    status:
      total_success: 0
      total_failed: 0
      total_filtered: 0
      pr_pool_remaining: 0
      last_round_success: 0
      last_round_failed: 0
      success_rate: 0.0
      zero_success_streak: 0
      last_checked: null
  rust:
    enabled: true
    params:
      timeout: 3600
      cc_timeout: 3000
      n_concurrent: 16
    status:
      total_success: 0
      total_failed: 0
      total_filtered: 0
      pr_pool_remaining: 0
      last_round_success: 0
      last_round_failed: 0
      success_rate: 0.0
      zero_success_streak: 0
      last_checked: null
```

- [ ] **Step 2: Verify YAML is valid**

Run: `cd repos/SWE-gen && python -c "import yaml; yaml.safe_load(open('inputs.yaml'))"`
Expected: No error

- [ ] **Step 3: Commit**

```bash
cd repos/SWE-gen
git add inputs.yaml
git commit -m "feat: populate inputs.yaml with adaptive tuning config"
```

---

### Task 3: Refactor all 8 `create_{lang}.sh` scripts

**Files:**
- Modify: `repos/SWE-gen/scripts/create_py.sh`
- Modify: `repos/SWE-gen/scripts/create_js.sh`
- Modify: `repos/SWE-gen/scripts/create_ts.sh`
- Modify: `repos/SWE-gen/scripts/create_go.sh`
- Modify: `repos/SWE-gen/scripts/create_c.sh`
- Modify: `repos/SWE-gen/scripts/create_cpp.sh`
- Modify: `repos/SWE-gen/scripts/create_java.sh`
- Modify: `repos/SWE-gen/scripts/create_rust.sh`

All 8 scripts follow the same refactoring pattern: replace hardcoded timeout/cc-timeout/N_CONCURRENT with values read from `inputs.yaml` via `read_params.py`.

- [ ] **Step 1: Refactor `create_py.sh`**

Replace the parameter section (lines 20-37) of `repos/SWE-gen/scripts/create_py.sh`. The new script:

```bash
#!/bin/bash
cd "$(dirname "$0")/.."
PROJECT_ROOT="$(pwd)"
source swegen-env2/bin/activate
source scripts/load_runtime_env.sh
echo 'activate swegen-env2'

load_runtime_env

uv pip install -e .

export OPENAI_MODEL="${OPENAI_MODEL:-glm-5-urg}"
export ANTHROPIC_MODEL="${ANTHROPIC_MODEL:-claude-sonnet-4-6}"

set -euo pipefail

cd "$(dirname "$0")"
mkdir -p logs/swegen-create

# Read params from inputs.yaml (adaptive tuning)
eval $(python "${PROJECT_ROOT}/scripts/read_params.py" --lang py --inputs-yaml "${PROJECT_ROOT}/inputs.yaml")
echo "TIMEOUT=${TIMEOUT} CC_TIMEOUT=${CC_TIMEOUT} N_CONCURRENT=${N_CONCURRENT}"

swegen create \
  --input-ids-file "${PROJECT_ROOT}/artifacts/collected_prs/python_pr_ids.txt" \
  --max-pr 5000 \
  --n-concurrent "${N_CONCURRENT}" \
  --output "${PROJECT_ROOT}/artifacts/swe_tasks/py-cc" \
  --state-dir .swegen-py \
  --timeout "${TIMEOUT}" \
  --cc-timeout "${CC_TIMEOUT}" \
  --no-require-issue \
  --min-source-files 3 \
  --max-source-files 10 \
  2>&1 | tee logs/swegen-create/cc_py_March.txt
```

- [ ] **Step 2: Refactor remaining 7 scripts**

Apply the same pattern to each script. The only differences per language are:
- `--lang` argument to `read_params.py`: `js`, `ts`, `go`, `c`, `cpp`, `java`, `rust`
- `--input-ids-file`: `javascript_pr_ids.txt`, `typescript_pr_ids.txt`, `go_pr_ids.txt`, `c_pr_ids.txt`, `cpp_pr_ids.txt`, `java_pr_ids.txt`, `rust_pr_ids.txt`
- `--output`: `js-cc`, `ts-cc`, `go-cc`, `c-cc`, `cpp-cc`, `java-cc`, `rust-cc`
- `--state-dir`: `.swegen-js`, `.swegen-ts`, `.swegen-go`, `.swegen-c`, `.swegen-cpp`, `.swegen-java`, `.swegen-rust`
- `--min-source-files`: 3 for py, 2 for all others
- Log file suffix: `cc_js_March.txt`, etc.

For `create_js.sh`, also remove the trailing Q&A comment block (lines 44-52).

- [ ] **Step 3: Verify scripts are syntactically valid**

Run: `for f in repos/SWE-gen/scripts/create_*.sh; do bash -n "$f" && echo "OK: $f" || echo "FAIL: $f"; done`
Expected: All 8 scripts report OK

- [ ] **Step 4: Commit**

```bash
cd repos/SWE-gen
git add scripts/create_py.sh scripts/create_js.sh scripts/create_ts.sh scripts/create_go.sh \
        scripts/create_c.sh scripts/create_cpp.sh scripts/create_java.sh scripts/create_rust.sh
git commit -m "refactor: create scripts read params from inputs.yaml"
```

---

### Task 4: Update `create_all_bg.sh`

**Files:**
- Modify: `repos/SWE-gen/scripts/create_all_bg.sh`

- [ ] **Step 1: Update the script**

Replace `repos/SWE-gen/scripts/create_all_bg.sh` with:

```bash
#!/bin/bash
cd "$(dirname "$0")/.."

set -euo pipefail
source swegen-env2/bin/activate
source scripts/load_runtime_env.sh

load_runtime_env

mkdir -p logs/swegen-create

echo "Starting create scripts (params from inputs.yaml)..."

start_one() {
    local lang="$1"
    nohup bash "scripts/create_${lang}.sh" > /dev/null 2>&1 &
    echo "${lang} PID: $!"
}

for lang in py go ts js c cpp java rust; do
    start_one "$lang"
done

echo "All create scripts started. Check logs/swegen-create/cc_*_March.txt"
```

- [ ] **Step 2: Commit**

```bash
cd repos/SWE-gen
git add scripts/create_all_bg.sh
git commit -m "refactor: update create_all_bg.sh for inputs.yaml workflow"
```

---

### Task 5: Update `README.md` with adaptive tuning section

**Files:**
- Modify: `repos/SWE-gen/README.md`

- [ ] **Step 1: Add adaptive tuning section to README.md**

Insert the following section before `## License` in `repos/SWE-gen/README.md`:

```markdown
## 自适应调参机制

AI agent（Claude Code）可以根据各语言的 SWE 数据产出效率，自动调整管线参数。

### 工作原理

1. **参数配置**：`inputs.yaml` 定义每种语言的 `timeout`、`cc_timeout`、`n_concurrent` 三个可调参数及运行状态
2. **定期监控**：AI agent 每 30 分钟检查各语言的成功率（从 `verifiable_tasks.txt` 和 batch state 统计）
3. **按需调参**：成功率偏低时增加超时，成功率高时提升并发，PR 池不足时自动补充
4. **安全约束**：每次仅调整 1 个参数，间隔 ≥ 60 分钟，参数有上下界限制

### 可调参数

| 参数 | 范围 | 步长 | 作用 |
|------|------|------|------|
| `timeout` | 2400-5400s | 400 | swegen create 单 case 超时 |
| `cc_timeout` | 1800-4200s | 300 | Claude Code session 超时 |
| `n_concurrent` | 4-32 | 4 | 并发数 |

### 使用方式

```bash
# 各语言 create 脚本自动从 inputs.yaml 读取参数
bash scripts/create_py.sh

# 或批量启动
bash scripts/create_all_bg.sh
```

调参决策记录在 `logs/adaptive_decisions.jsonl`。详见 `docs/superpowers/specs/2026-04-22-adaptive-tuning-design.md`。
```

- [ ] **Step 2: Commit**

```bash
cd repos/SWE-gen
git add README.md
git commit -m "docs: add adaptive tuning section to README"
```

---

### Task 6: Update `CLAUDE.md` with AI agent monitoring guide

**Files:**
- Modify: `repos/SWE-gen/CLAUDE.md`

- [ ] **Step 1: Add monitoring guide to CLAUDE.md**

Append the following section at the end of `repos/SWE-gen/CLAUDE.md`:

```markdown

## Adaptive Parameter Tuning

### Overview

You (the AI agent) monitor and tune the SWE-gen pipeline. Configuration and status live in `inputs.yaml`.

### Monitoring Cycle (every 30 minutes)

1. **Collect status**: Count verified tasks from `artifacts/swe_tasks/{lang}-cc/verifiable_tasks.txt`. Count failures from batch state in `artifacts/swe_tasks/{lang}-cc/.swegen-create-batch/`. Update `inputs.yaml` `status` fields.
2. **Decide tuning**: If `success_rate < 0.15` for 2 consecutive cycles, increase `timeout` (+400) or `cc_timeout` (+300). If `success_rate > 0.4` and `n_concurrent < 24`, increase `n_concurrent` (+4). If `success_rate >= 0.25`, do nothing.
3. **Check PR pool**: If `pr_pool_remaining < 100`, run `python tools/collect_prs_wo_image.py --languages {lang} --repo_num 100 --max_prs_per_repo 50 --output_dir ./artifacts/collected_prs`, then deduplicate against processed PRs and update the input-ids-file.

### Constraints

- Adjust at most 1 parameter per language per cycle
- Wait ≥ 2 cycles (60 min) between adjustments for the same language
- Parameter bounds: timeout [2400, 5400], cc_timeout [1800, 4200], n_concurrent [4, 32]
- Do NOT restart running create scripts unless `zero_success_streak >= 3`
- Log every decision to `logs/adaptive_decisions.jsonl`

### Reading params from inputs.yaml

```bash
eval $(python scripts/read_params.py --lang py --inputs-yaml inputs.yaml)
echo $TIMEOUT $CC_TIMEOUT $N_CONCURRENT
```

### Decision log format

```json
{"timestamp": "2026-04-22T14:30:00Z", "lang": "rust", "action": "adjust_param", "param": "timeout", "old": 3600, "new": 4000, "reason": "success_rate 0.08 for 2 consecutive cycles"}
```
```

- [ ] **Step 2: Commit**

```bash
cd repos/SWE-gen
git add CLAUDE.md
git commit -m "docs: add adaptive tuning guide to CLAUDE.md"
```

---

### Task 7: Create `logs/` directory and `.gitkeep`

**Files:**
- Create: `repos/SWE-gen/logs/.gitkeep`

- [ ] **Step 1: Create logs directory**

```bash
mkdir -p repos/SWE-gen/logs
touch repos/SWE-gen/logs/.gitkeep
```

- [ ] **Step 2: Commit**

```bash
cd repos/SWE-gen
git add logs/.gitkeep
git commit -m "chore: add logs directory for adaptive_decisions.jsonl"
```
