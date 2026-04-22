# Adaptive Parameter Tuning for SWE-gen Pipeline

## Overview

为 SWE-Lego-Live-SWEgen 项目增加 AI agent（Claude Code）自适应调参能力。AI agent 定期监控各语言的 SWE 数据产出效率，根据成功率自主调整 collect PR 和 swegen create 的参数，并在 PR 池不足时自动补充。

目标：体现 AI agent 的灵活决策能力，而非固定脚本执行。

## 运行模式

- **定期监控 + 按需调参**：AI agent 每 30 分钟检查各语言产出状态，仅在发现问题时调整参数
- **持续运行，关注效率**：不设硬性目标数，重点保持各语言产出效率在合理范围
- **默认不重启**：参数修改后等当前轮次自然结束，下一轮生效。仅连续 3 个周期零成功时允许重启

## inputs.yaml 结构

```yaml
global:
  monitor_interval_min: 30
  pr_pool_min_threshold: 100
  collect_pr_defaults:
    repo_num: 100
    max_prs_per_repo: 50
  restart_policy:
    allowed: true
    zero_success_cycles: 3

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
    status: {}
  ts:
    enabled: true
    params:
      timeout: 3600
      cc_timeout: 3000
      n_concurrent: 16
    status: {}
  go:
    enabled: true
    params:
      timeout: 3200
      cc_timeout: 2400
      n_concurrent: 16
    status: {}
  c:
    enabled: true
    params:
      timeout: 3200
      cc_timeout: 2400
      n_concurrent: 16
    status: {}
  cpp:
    enabled: true
    params:
      timeout: 3200
      cc_timeout: 2400
      n_concurrent: 16
    status: {}
  java:
    enabled: true
    params:
      timeout: 3200
      cc_timeout: 2400
      n_concurrent: 16
    status: {}
  rust:
    enabled: true
    params:
      timeout: 3600
      cc_timeout: 3000
      n_concurrent: 16
    status: {}
```

## 可调参数（仅 3 个）

| 参数 | 含义 | 最小值 | 最大值 | 步长 | 默认值 |
|------|------|--------|--------|------|--------|
| timeout | swegen create 单 case 超时（秒） | 2400 | 5400 | 400 | 3200 (Rust/TS: 3600) |
| cc_timeout | Claude Code session 超时（秒） | 1800 | 4200 | 300 | 2400 (Rust/TS: 3000) |
| n_concurrent | 并发数 | 4 | 32 | 4 | 16 |

## 调参规则

### 触发条件与动作

| 场景 | 检测条件 | 动作 |
|------|---------|------|
| 成功率偏低 | `success_rate < 0.15`（连续 2 个周期） | `timeout += 400` 或 `cc_timeout += 300` |
| 成功率健康 | `success_rate >= 0.25` | 维持当前参数不变 |
| PR 池即将耗尽 | `pr_pool_remaining < pr_pool_min_threshold` | 触发 collect PR 补充该语言 |
| 产出停滞 | `zero_success_streak >= 3` | 允许重启该语言的 create 脚本 |
| 资源浪费 | `success_rate > 0.4` 且 `n_concurrent < 24` | `n_concurrent += 4` |

### 调参节奏约束

- 同一语言的参数调整间隔 ≥ 2 个监控周期（≥ 60 分钟）
- 每次最多调整 1 个参数（避免多变量同时变化无法归因）
- 所有决策记录到 `logs/adaptive_decisions.jsonl`

## 监控周期流程

每 30 分钟，AI agent 按顺序执行：

1. **采集状态** — 读取各语言 `verifiable_tasks.txt` 统计成功数，读取 batch state JSON 统计失败数，计算 `success_rate`，更新 `inputs.yaml` 的 `status`
2. **判断是否调参** — 按规则表决策，如需调参则修改 `inputs.yaml` 的 `params`，写一条记录到 `logs/adaptive_decisions.jsonl`
3. **判断是否补充 PR** — 检查各语言 `pr_pool_remaining`，低于阈值时触发 collect

## collect PR 补充逻辑

当某语言 `pr_pool_remaining < 100` 时：

```bash
python tools/collect_prs_wo_image.py \
  --languages {lang} \
  --repo_num 100 \
  --max_prs_per_repo 50 \
  --output_dir ./artifacts/collected_prs
```

collect 完成后，AI agent 需要：
- 将新收集的 PR 与 batch state 中已处理的 PR 去重
- 未处理的 PR 写入新的 input-ids-file 供下一轮 create 使用
- 更新 `inputs.yaml` 中的 `pr_pool_remaining`

## 日志格式

`logs/adaptive_decisions.jsonl` 每行一条：

```json
{"timestamp": "2026-04-22T14:30:00Z", "lang": "rust", "action": "adjust_param", "param": "timeout", "old": 3600, "new": 4000, "reason": "success_rate 0.08 for 2 consecutive cycles"}
{"timestamp": "2026-04-22T14:30:00Z", "lang": "cpp", "action": "collect_pr", "pr_pool_before": 12, "reason": "pr_pool_remaining below threshold 20"}
```

## create 脚本改造

现有 `scripts/create_{lang}.sh` 从硬编码改为读取 `inputs.yaml`：

- 新增 `scripts/read_params.py`：从 inputs.yaml 读取指定语言的参数，输出 shell 变量（约 20 行）
- 各 create 脚本调用 `eval $(python scripts/read_params.py --lang {lang})` 获取 `$TIMEOUT`, `$CC_TIMEOUT`, `$N_CONCURRENT`

## 整体工作流

```
用户启动 → create_all_bg.sh 拉起 8 个语言的 create 脚本
                ↓
         各脚本从 inputs.yaml 读参数运行
                ↓
    ┌──── AI agent 每 30min 监控 ────┐
    │  1. 采集状态 → 更新 status     │
    │  2. 判断调参 → 修改 params     │
    │  3. 判断补 PR → 触发 collect   │
    └────────────────────────────────┘
                ↓
         某语言脚本自然结束
                ↓
         下一轮用新 params 启动
                ↓
         极端情况：连续 3 周期零成功 → AI agent 主动重启该语言脚本
```

## 文件变更清单

### 新增

| 文件 | 用途 |
|------|------|
| `inputs.yaml` | 完整的配置+状态文件（替换当前空文件） |
| `scripts/read_params.py` | 从 inputs.yaml 读参数输出 shell 变量 |
| `logs/adaptive_decisions.jsonl` | 调参决策日志（运行时生成） |

### 修改

| 文件 | 改动 |
|------|------|
| `scripts/create_{lang}.sh` (×8) | 从硬编码改为读 inputs.yaml |
| `scripts/create_all_bg.sh` | 适配新的参数读取方式 |
| `README.md` | 增加自适应调参机制说明 |
| `CLAUDE.md` | 增加 AI agent 监控调参操作指南 |

### 不改动

- `swegen` CLI 核心代码（参数通过命令行传入）
- `outputs.yaml`（下游接口不变）
- 评分、验证逻辑
