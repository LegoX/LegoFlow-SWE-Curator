# AI agent Python 10 PR 小样本实验报告

## 摘要

本次实验使用当前 AI agent 跑通了一个 Python 小样本 SWEgen 验证流程：

- 使用环境变量 `GITHUB_TOKEN` 完成 Python PR 收集。
- 使用指定 LLM API 配置完成 `swegen create`。
- 使用 `--min-source-files 1` 生成并验证至少 1 个 SWE task。
- `verifiable_tasks.txt` 写入 task ID：`tox-dev__tox-3813`。

最终成功信号：

```text
CC NOP: expected reward=0, actual reward=0
CC Oracle: expected reward=1, actual reward=1
```

## 环境与参数

| 项 | 值 |
|---|---|
| GitHub token | 环境变量 `GITHUB_TOKEN` / `GITHUB_TOKENS` |
| OpenAI-compatible base URL | `https://yunwu.ai/` |
| OpenAI model | `gpt-5.4` |
| Anthropic-compatible model | `claude-opus-4-6` |
| Docker socket | `DOCKER_HOST=unix:///var/run/docker.sock` |
| SWEgen 参数 | `--min-source-files 1` |

LLM preflight 结果：

```text
openai_model gpt-5.4
openai_base https://yunwu.ai/v1
openai_preflight=ok
anthropic_model claude-opus-4-6
anthropic_base https://yunwu.ai
```

## PR 收集结果

使用环境变量里的 `GITHUB_TOKEN` 运行 Python PR 收集：

```bash
GITHUB_TOKENS="$GITHUB_TOKEN" \
GITHUB_REQUIRE_PROXY_ISOLATION=0 \
python tools/collect_prs_wo_image.py \
  --languages python \
  --repo_num 1 \
  --max_prs_per_repo 10 \
  --max-candidate-repos 30 \
  --output_dir artifacts/experiments/ai-agent-python10-2026-06-01-collect-local-sync/collected_prs \
  --disable_progress_bar \
  --force-recheck-all
```

收集结果：

```text
Loaded 1 tokens from GITHUB_TOKENS/GITHUB_TOKEN env
Token valid: ghp_qF3F...9Yd3 (user: ChaofanTao, remaining: 4359)
[python] Found 30 candidate repos (searched 121)
[python] Completed: 1/1 qualifying repos
Repos: 121 searched -> 30 candidates -> 1 with qualifying PRs
PRs: 55 scanned -> 50 merged -> 8 qualifying
```

输出文件：

```bash
artifacts/experiments/ai-agent-python10-2026-06-01-collect-local-sync/collected_prs/python_pr_ids.txt
```

成功收集到的 Python PR：

```text
webrecorder/cdxj-indexer:pr-19
webrecorder/cdxj-indexer:pr-18
webrecorder/cdxj-indexer:pr-9
webrecorder/cdxj-indexer:pr-12
webrecorder/cdxj-indexer:pr-14
webrecorder/cdxj-indexer:pr-13
webrecorder/cdxj-indexer:pr-10
webrecorder/cdxj-indexer:pr-3
```

## SWE Task 生成验证

小量生成验证目录：

```bash
artifacts/experiments/ai-agent-python10-2026-06-01-rerun2/
```

运行命令：

```bash
DOCKER_HOST=unix:///var/run/docker.sock \
GITHUB_TOKEN="$GITHUB_TOKEN" \
GITHUB_TOKENS="$GITHUB_TOKEN" \
OPENAI_API_BASE_URL=https://yunwu.ai/ \
OPENAI_MODEL=gpt-5.4 \
ANTHROPIC_BASE_URL=https://yunwu.ai/ \
ANTHROPIC_MODEL=claude-opus-4-6 \
PYTHONPATH=src \
python -c 'from swegen.cli import app; app()' create \
  --input-ids-file artifacts/experiments/ai-agent-python10-2026-06-01-rerun2/collected_prs/python_pr_ids.txt \
  --max-pr 1 \
  --n-concurrent 1 \
  --output artifacts/experiments/ai-agent-python10-2026-06-01-rerun2/swe_tasks/py-cc \
  --state-dir artifacts/experiments/ai-agent-python10-2026-06-01-rerun2/state \
  --timeout 2400 \
  --cc-timeout 1800 \
  --no-require-issue \
  --min-source-files 1 \
  --max-source-files 10 \
  --docker-prune-batch 0 \
  --verbose
```

成功生成并验证的 task：

```text
tox-dev__tox-3813
```

关键输出：

```text
✓ Skeleton generated in 48.1s
✓ Task generated and validated in 287.8s
✓ CC NOP: expected reward=0, actual reward=0
✓ CC Oracle: expected reward=1, actual reward=1
```

manifest 路径：

```bash
artifacts/experiments/ai-agent-python10-2026-06-01-rerun2/swe_tasks/py-cc/verifiable_tasks.txt
```

manifest 内容：

```text
tox-dev__tox-3813
```

## 成功标准检查

| 检查项 | 结果 |
|---|---|
| 使用 `GITHUB_TOKEN` 收集 Python PR | 通过，8 个 PR |
| LLM API preflight | 通过 |
| `swegen create` fresh generation | 通过 |
| `--min-source-files 1` | 通过 |
| `verifiable_tasks.txt` 写入 task ID | 通过，`tox-dev__tox-3813` |
| NOP/Oracle 验证 | 通过，NOP=0、Oracle=1 |

## 结论

本次小样本实验确认：在当前环境中，`SWE-Lego-Live-SWEgen` 可以使用环境变量 `GITHUB_TOKEN` 收集 Python PR，并使用指定 LLM API 配置完成 `swegen create`。最终产出 1 个可验证 SWE task：`tox-dev__tox-3813`。
