# AI agent Python 10 PR 小样本实验报告

## 摘要

本次实验基于当前 `SWE-Lego-Live/swegen` 接入的 SWEgen 基线运行：

- `SWE-Lego-Live/swegen`: `b5e2501ed8ccaa74b79671953ad87a6a1f13fca8`
- `subblock/swegen/repos/swegen`: `SWE-Lego-Live-SWEgen` `9a61d06875f6630f34457d80a9a54c1ee94254ef`
- 参考操作说明：`SWE-Lego-Live/subblock/swegen/CLAUDE.md`

目标是让当前 AI agent 运行一个小量 Python PR 实验：使用 `GITHUB_TOKEN` 进行 PR 收集尝试，使用可用的 LLM API 配置完成 `swegen create`，并至少产出 1 个有效 SWE task。成功标准是实验输出目录中的 `verifiable_tasks.txt` 至少写入一个 task ID。

最终结果：

- LLM preflight：通过
- `swegen create` fresh generation：通过
- 验证通过的 task：`tox-dev__tox-3813`
- NOP/Oracle：NOP reward=0，Oracle reward=1
- manifest：`artifacts/experiments/ai-agent-python10-2026-06-01-rerun2/swe_tasks/py-cc/verifiable_tasks.txt`
- `SWE-Lego-Live-SWEgen` 源码修改：无

## 环境与参数

本次使用的关键参数：

| 项 | 值 |
|---|---|
| GitHub | 使用环境变量 `GITHUB_TOKEN`，并映射到 `GITHUB_TOKENS` 给收集脚本使用 |
| OpenAI-compatible base URL | `https://yunwu.ai/` |
| OpenAI model | `gpt-5.4` |
| Anthropic-compatible model | `claude-opus-4-6` |
| Docker socket | `DOCKER_HOST=unix:///var/run/docker.sock` |
| `swegen create` source 下限 | `--min-source-files 1` |

LLM preflight 命令使用 `swegen.llm_env` 的配置路径进行验证。结果：

```text
openai_model gpt-5.4
openai_base https://yunwu.ai/v1
openai_preflight=ok
anthropic_model claude-opus-4-6
anthropic_base https://yunwu.ai
```

这说明后续实验没有被 LLM API 短缺或无效凭证阻塞。

## PR 收集

实验目录：

```bash
artifacts/experiments/ai-agent-python10-2026-06-01-rerun/
```

使用 `GITHUB_TOKEN` 运行收集脚本：

```bash
GITHUB_TOKENS="$GITHUB_TOKEN" \
python tools/collect_prs_wo_image.py \
  --languages python \
  --repo_num 2 \
  --max_prs_per_repo 10 \
  --output_dir artifacts/experiments/ai-agent-python10-2026-06-01-rerun/collected_prs \
  --disable_progress_bar
```

脚本确认 token 可用：

```text
Token valid: ghp_qF3F...9Yd3 (user: ChaofanTao, remaining: 4985)
```

但之后 GitHub 搜索阶段反复出现：

```text
All tokens exhausted. Waiting ...
```

运行超过 5 分钟仍未产出 `python_pr_ids.txt`，因此终止本轮实时收集。为了继续验证 SWEgen 主流程，本次使用仓库已有 Python PR 候选固定 10 个输入。这个现象说明实时 PR 收集在单 token、小样本场景下仍可能受 GitHub Search API 或脚本内部 token backoff 影响；它没有阻塞后续 `swegen create` 的验证。

固定的 10 个 Python PR 输入如下：

```text
tox-dev/tox:pr-3813
tox-dev/tox:pr-3814
AnswerDotAI/RAGatouille:pr-157
tox-dev/tox:pr-3810
electricitymaps/electricitymaps-contrib:pr-8113
tox-dev/tox:pr-3803
morpheus65535/bazarr:pr-2691
tox-dev/tox:pr-3804
tox-dev/tox:pr-3800
electricitymaps/electricitymaps-contrib:pr-8119
```

## 小量生成验证

最终成功的实验目录：

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

注意：报告中省略 API key 明文，只记录参数形态。

### 中间尝试：`AnswerDotAI/RAGatouille#157`

在第一轮输入顺序中，`AnswerDotAI/RAGatouille#157` 进入 Claude SDK 并生成了 task artifact。该过程证明 LLM API 与 Claude Code SDK 可以正常工作。该 PR 的 NOP 成功得到 reward=0，但 Oracle 多次得到 reward=0，原因是 task 运行时需要编译 `colbert-ai` 的 C++ extension，容器内 `cc1plus` 被 OOM kill。

该失败属于候选 PR 的环境资源复杂度问题，不是 LLM API 短缺，也不是 SWEgen 主流程错误。因此后续将同一 10 PR 集合重排，让较轻量的 `tox-dev/tox#3813` 优先验证。

### 成功样本：`tox-dev/tox#3813`

`tox-dev/tox#3813` fresh generation 成功：

```text
✓ Skeleton generated in 48.1s
✓ Task generated and validated in 287.8s
✓ Skipping harbor validation (CC already validated)
✓ CC NOP: expected reward=0, actual reward=0
✓ CC Oracle: expected reward=1, actual reward=1
```

Harbor NOP：

```text
py-cc • nop
Trials: 1
Exceptions: 0
Mean: 0.000
Reward: 0.0
```

Harbor Oracle：

```text
py-cc • oracle
Trials: 1
Exceptions: 0
Mean: 1.000
Reward: 1.0
```

最终 manifest：

```text
tox-dev__tox-3813
```

manifest 路径：

```bash
artifacts/experiments/ai-agent-python10-2026-06-01-rerun2/swe_tasks/py-cc/verifiable_tasks.txt
```

## 成功标准检查

| 检查项 | 结果 |
|---|---|
| 使用环境变量 `GITHUB_TOKEN` 做 PR 收集尝试 | 通过，token 校验成功；实时收集因 Search/API backoff 未产出 |
| 使用指定 LLM API 配置 preflight | 通过 |
| `swegen create` 不再因 API 短缺停止 | 通过 |
| `--min-source-files 1` | 通过 |
| 至少 1 个有效 SWE task | 通过，`tox-dev__tox-3813` |
| `verifiable_tasks.txt` 写入 task ID | 通过 |
| NOP/Oracle 验证 | 通过，NOP=0、Oracle=1 |
| 是否发现 SWEgen 源码错误 | 未发现 |

## 结论

1. 使用新的 `yunwu.ai` LLM 配置后，`swegen create` 可以越过 LLM preflight，并成功驱动 Claude SDK 完成 task generation。
2. `tox-dev/tox#3813` 的 fresh generation 在 10 PR 小样本输入中成功产出 verified SWE task，满足 `verifiable_tasks.txt` 写入 task ID 的成功标准。
3. `AnswerDotAI/RAGatouille#157` 的失败暴露的是候选 PR 资源复杂度问题：依赖 `colbert-ai` 的 C++ extension 编译在容器中 OOM，不应归因为 API 或 SWEgen 主流程。
4. `DOCKER_HOST=unix:///var/run/docker.sock` 对当前机器是必要环境项；否则 Harbor 可能检查默认 `/tmp/podman-fresh.sock` 并误报 Docker daemon 不可用。
5. 本次没有修改 `SWE-Lego-Live-SWEgen` 源码；只更新实验报告。生成 artifacts 保留在本地，不纳入提交。

## 后续建议

建议将快速验证流程固化到 `SWE-Lego-Live/subblock/swegen/quick-verify.md`，并从 `SWE-Lego-Live/subblock/swegen/CLAUDE.md` 链接过去。新的 AI agent 应先执行快速验证，确认环境、LLM、Docker/Harbor 和一个固定小样本 task 能跑通，再进入大规模 PR 收集与批量生成。
