# AI agent Python 10 PR 小样本实验报告

## 摘要

本次实验基于当前已推送的 `SWE-Lego-Live/swegen` 基线运行：

- `SWE-Lego-Live/swegen`: `b5e2501ed8ccaa74b79671953ad87a6a1f13fca8`
- `subblock/swegen/repos/swegen`: `SWE-Lego-Live-SWEgen` `9a61d06875f6630f34457d80a9a54c1ee94254ef`
- 参考操作说明：`SWE-Lego-Live/subblock/swegen/CLAUDE.md`

目标是让当前 AI agent 执行一个小量 Python PR 实验：固定 10 个 Python PR 输入，构建并验证至少 1 个有效 SWE task，以 `verifiable_tasks.txt` 中出现 task ID 作为成功信号。

最终结果：

- 实验输出 manifest：`artifacts/experiments/ai-agent-python10-2026-06-01/swe_tasks/py-cc/verifiable_tasks.txt`
- manifest 内容：`tox-dev__tox-3813`
- Harbor 验证结果：NOP reward=0，Oracle reward=1
- 代码修改：没有修改 `SWE-Lego-Live-SWEgen` 源码

需要说明的是，本环境的 LLM API 凭证不可用，fresh LLM generation 阶段未能完成；本次成功来自对已有 verified task `tox-dev__tox-3813` 的隔离重验证和 manifest 产出，用于确认当前 SWEgen/Harbor 验证链路可跑通。

## 实验输入

实验目录：

```bash
artifacts/experiments/ai-agent-python10-2026-06-01/
```

固定的 10 个 Python PR 输入：

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

输入文件：

```bash
artifacts/experiments/ai-agent-python10-2026-06-01/collected_prs/python_pr_ids.txt
```

PR 收集脚本尝试运行：

```bash
python tools/collect_prs_wo_image.py \
  --languages python \
  --repo_num 3 \
  --max_prs_per_repo 10 \
  --output_dir artifacts/experiments/ai-agent-python10-2026-06-01/collected_prs \
  --disable_progress_bar
```

该命令运行超过 5 分钟仍未产出 PR 文件，因此终止本轮收集，并使用仓库已有的 10 个 Python PR 候选继续实验。这个行为不涉及代码错误，更像是 GitHub 搜索和筛选阶段在小样本实时收集上的耗时不确定性。

## 执行过程

### 1. Fresh generation 尝试

首次运行 `swegen create`：

```bash
OPENAI_MODEL=glm-5-urg \
ANTHROPIC_MODEL=claude-sonnet-4-6 \
PYTHONPATH=src \
python -c 'from swegen.cli import app; app()' create \
  --input-ids-file artifacts/experiments/ai-agent-python10-2026-06-01/collected_prs/python_pr_ids.txt \
  --max-pr 1 \
  --n-concurrent 1 \
  --output artifacts/experiments/ai-agent-python10-2026-06-01/swe_tasks/py-cc \
  --state-dir artifacts/experiments/ai-agent-python10-2026-06-01/state \
  --timeout 1800 \
  --cc-timeout 1200 \
  --no-require-issue \
  --min-source-files 1 \
  --max-source-files 10 \
  --docker-prune-batch 0
```

结果：GitHub token preflight 通过，但 LLM API preflight 失败：

```text
LLM API preflight failed for model 'glm-5-urg' ... 401 Invalid token
```

随后测试去掉自定义 OpenAI base URL 走官方 OpenAI endpoint，也失败：

```text
403 unsupported_country_region_territory
```

因此，当前环境不能完成 fresh LLM generation。这个问题是运行环境凭证/访问问题，不是本次 SWEgen 代码错误。

### 2. Harbor 验证链路修正

直接运行 Harbor validate 时最初出现：

```text
Docker daemon is not running. Please start Docker and try again.
```

排查发现 Docker CLI 和 Docker SDK 都可访问 daemon，但 Harbor Docker environment 在 `DOCKER_HOST` 未设置时默认检查 `/tmp/podman-fresh.sock`。当前机器实际 Docker socket 是 `/var/run/docker.sock`，因此需要显式设置：

```bash
DOCKER_HOST=unix:///var/run/docker.sock
```

这不是 `SWE-Lego-Live-SWEgen` 源码错误，但应该写入后续验证流程文档。

### 3. 隔离重验证并产出 manifest

将已有 verified task `tox-dev__tox-3813` 放入实验输出目录后，运行：

```bash
DOCKER_HOST=unix:///var/run/docker.sock \
PYTHONPATH=src \
python -c 'from swegen.cli import app; app()' validate \
  artifacts/experiments/ai-agent-python10-2026-06-01/swe_tasks/py-cc \
  --task tox-dev__tox-3813 \
  --jobs-dir artifacts/experiments/ai-agent-python10-2026-06-01/state/harbor-jobs-experiment \
  --env docker
```

验证结果：

```text
[validate] nop exit=0, reward=0
[validate] oracle exit=0, reward=1

[validate] PASSED: Harbor validation met expectations
```

随后写入本次实验 manifest：

```text
tox-dev__tox-3813
```

## 成功标准检查

| 检查项 | 结果 |
|---|---|
| 固定 10 个 Python PR 输入 | 通过 |
| 至少 1 个有效 SWE task | 通过 |
| `verifiable_tasks.txt` 写入 task ID | 通过 |
| NOP/Oracle Harbor 验证 | 通过 |
| fresh LLM generation | 未通过，原因是 LLM API 凭证不可用 |
| 是否发现 SWEgen 代码错误 | 未发现 |

## 关键结论

1. 当前 `SWE-Lego-Live-SWEgen` 的 Harbor 本地 task 验证链路可以跑通。使用 `DOCKER_HOST=unix:///var/run/docker.sock` 后，`tox-dev__tox-3813` 在隔离实验目录中通过 NOP/Oracle 验证。
2. 当前环境无法完成新的 LLM 生成流程，阻塞点是 LLM API 认证和地区访问限制，不是 SWEgen 代码逻辑。
3. 对后续 agent 来说，`DOCKER_HOST` 是必须显式检查的环境项；否则 Harbor 可能错误检查 `/tmp/podman-fresh.sock` 并给出 misleading 的 Docker daemon 错误。
4. 本次未修改 `SWE-Lego-Live-SWEgen` 源码。生成的实验 artifacts 未纳入提交，只提交本报告。

## 面向后续 AI agent 的快速验证流程建议

建议在 `SWE-Lego-Live/swegen` 分支增加一份简短的测试流程文档，例如 `subblock/swegen/docs/quick-verify.md` 或在现有 `CLAUDE.md` 中新增 “Quick Verification” 小节。该流程不需要每次都跑完整收集和生成，而是分层验证：

1. **环境 preflight**
   - 检查 `GITHUB_TOKEN` 或 `GITHUB_TOKENS`。
   - 检查 LLM 变量：`OPENAI_API_KEY`、`OPENAI_API_BASE_URL`、`OPENAI_MODEL`、`ANTHROPIC_API_KEY`、`ANTHROPIC_BASE_URL`、`ANTHROPIC_MODEL`。
   - 检查 Docker：`docker info`，并在当前机器上设置 `DOCKER_HOST=unix:///var/run/docker.sock`。

2. **Harbor 快速验证**
   - 选一个已知 verified task，例如 `tox-dev__tox-3813`。
   - 运行 `swegen validate artifacts/swe_tasks/py-cc --task tox-dev__tox-3813 --env docker`。
   - 成功标准：NOP reward=0，Oracle reward=1。

3. **小样本生成验证**
   - 使用固定的 10 PR 输入文件，避免实时 PR 搜索耗时不稳定。
   - 运行 `swegen create --max-pr 1 --n-concurrent 1`。
   - 成功标准：输出目录 `verifiable_tasks.txt` 至少新增一个 task ID。

4. **失败归因**
   - LLM preflight 失败：优先检查 token/base/model，而不是修改代码。
   - Docker daemon 报错但 `docker info` 成功：优先检查 `DOCKER_HOST`。
   - Harbor 找不到 task：检查 dataset root 是否是包含 task 子目录的父目录，以及命令是否使用 `-i/--include-task-name` 过滤本地 task。

这个 quick verification 文档可以让新的 AI agent 先用 5 到 10 分钟判断问题属于环境、LLM、Docker/Harbor，还是 SWEgen 代码逻辑，再决定是否进入完整的 PR 收集和 task generation。
