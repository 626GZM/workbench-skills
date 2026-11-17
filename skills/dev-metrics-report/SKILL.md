---
name: dev-metrics-report
description: "从本地 runs.jsonl 和 git 仓库算开发效率指标（次数、成功率、耗时、按环节/引擎聚合、缓存占比、提交量），输出 JSON 或一份可直接阅读的周报/月报。当用户想复盘‘时间花在哪’‘哪个环节最费’‘这周产出多少’，或需要异常信号和改进建议时使用。不连接任何内部系统。"
metadata:
  requires:
    bins: ["python3", "git"]
---

# dev-metrics-report

从本地记录里算开发效率指标，出周报 / 月报。只读本地文件，不连看板、不连服务端、不发通知。

## 使用场景

- 一周结束，想复盘时间主要花在哪些环节
- 想知道哪个环节单次最费、哪个引擎成功率高
- 想把本地 AI 调用记录和 git 提交量放在一起看
- 需要一份带异常信号和可执行建议的 Markdown 报告

## 输入数据

### runs.jsonl

每行一条本地运行记录，UTF-8：

```json
{"at":"2026-11-17T10:00:00","role":"方案设计","engine":"kimi","ok":true,"seconds":420,"tokens_in":1000,"tokens_out":500,"cache_read":20000}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `at` | 是 | ISO 时间；没有合法 `at` 的行会被跳过 |
| `role` | 否 | 环节，建议用 `需求分析` / `方案设计` / `开发` / `自测` / `联调` / `测试`；缺省记作 `未标注` |
| `engine` | 否 | 引擎名，如 `claude` / `codex` / `deepseek` / `kimi`；缺省记作 `未标注` |
| `ok` | 否 | 是否成功，缺省按成功 |
| `seconds` | 否 | 本次耗时（秒），缺省 0 |
| `tokens_in` | 否 | 输入 token，缺省 0 |
| `tokens_out` | 否 | 输出 token，缺省 0 |
| `cache_read` | 否 | 缓存读 token，缺省 0 |

坏 JSON 行、非对象行、没有合法 `at` 的行都会被跳过，不会中断统计。

### git 仓库（可选）

给 `--repo` 时只读查询提交记录，统计提交数、增删行、活跃天数、每日提交曲线和按作者分布。

## 用法

```bash
# 汇总：总次数、成功率、总耗时、按 role / engine 聚合、缓存读占比 top 5
python3 scripts/metrics_report.py summary --runs ~/workbench/runs.jsonl \
  --since 2026-11-01 --until 2026-11-17

# 时间序列：按天（默认）或按 ISO 周
python3 scripts/metrics_report.py trend --runs ~/workbench/runs.jsonl \
  --by week --since 2026-11-01

# git 统计：提交数、增删行、活跃天数、每日提交曲线、按作者
python3 scripts/metrics_report.py git --repo ~/repos/project-a \
  --since 2026-11-01 --author Alice

# 周报 / 月报：给人看的 Markdown；可同时带上 git 统计
python3 scripts/metrics_report.py report --runs ~/workbench/runs.jsonl \
  --repo ~/repos/project-a --since 2026-11-01 --out /tmp/dev-metrics-2026-11.md
```

## 子命令

| 命令 | 说明 | 输出 |
|---|---|---|
| `summary` | 总次数、成功率、总耗时、按 role 聚合（次数/耗时/平均/失败数）、按 engine 聚合、缓存读占比 top 5 | JSON |
| `trend` | 时间序列：每桶的次数、耗时、失败数；`--by day` 按天，`--by week` 按 ISO 周 | JSON |
| `git` | 提交数、新增/删除行、活跃天数、每日提交曲线、按作者统计 | JSON |
| `report` | 本期概览 / 时间都花在哪 / 哪些环节最费 / 异常信号 / 建议 | Markdown |

时间参数：

- `--since 2026-11-01`：从当天 0 点开始（含）
- `--until 2026-11-17`：只给日期时含当天整天；给完整 ISO 时间时按该时刻
- 都不给就是全部记录

## 输出 JSON

`summary`：

```json
{
  "runs": "/path/to/runs.jsonl",
  "period": {"since": "2026-11-01T00:00:00", "until": "2026-11-17T23:59:59"},
  "total": 42,
  "ok": 38,
  "failed": 4,
  "success_rate": 0.9048,
  "total_seconds": 25200.0,
  "total_hours": 7.0,
  "avg_seconds": 600.0,
  "tokens": {"in": 50000, "out": 20000, "cache_read": 300000, "total_input": 350000},
  "cache_ratio": 0.8571,
  "by_role": [
    {"role": "开发", "count": 20, "seconds": 12000.0, "hours": 3.33, "avg_seconds": 600.0, "failed": 1}
  ],
  "by_engine": [
    {"engine": "kimi", "count": 12, "seconds": 7200.0, "hours": 2.0, "avg_seconds": 600.0, "failed": 0}
  ],
  "cache_read_top": [
    {"role": "开发", "cache_read": 100000, "total_input": 110000, "ratio": 0.9091}
  ]
}
```

- `by_role` / `by_engine` 按总耗时降序
- `cache_read_top` 按 `cache_read / (tokens_in + cache_read)` 降序，最多 5 条
- 空数据时计数为 0、比率为 `0.0`、数组为空，不报错

`trend`：

```json
{
  "runs": "/path/to/runs.jsonl",
  "period": {"since": "2026-11-01T00:00:00", "until": null},
  "by": "day",
  "points": [
    {"bucket": "2026-11-17", "count": 8, "seconds": 3600.0, "hours": 1.0, "failed": 1}
  ]
}
```

`--by week` 时 `bucket` 形如 `2026-W47`（ISO 周）。

`git`：

```json
{
  "repo": "/path/to/repo",
  "period": {"since": "2026-11-01T00:00:00", "until": null},
  "author": null,
  "commits": 15,
  "added": 320,
  "deleted": 45,
  "changed_lines": 365,
  "active_days": 6,
  "daily": [{"date": "2026-11-17", "commits": 3}],
  "by_author": [
    {"author": "Alice", "commits": 10, "added": 200, "deleted": 30, "active_days": 5}
  ]
}
```

- 空仓库、或时间过滤后没有提交时，计数为 0、数组为空
- 二进制文件的增删行不计入

## 异常信号规则（写死、可解释）

`report` 会逐条检查，命中就把数值和阈值写进「异常信号」：

| rule | 触发条件 |
|---|---|
| `role_avg_over_2x` | 某 role 平均耗时 > 总体平均耗时 × 2 |
| `failure_rate_over_20` | 总体失败率 > 20% |
| `cache_ratio_over_80` | `cache_read / (tokens_in + cache_read)` > 80% |
| `day_over_8h` | 单日累计耗时 > 8 小时 |

均为严格大于。建议由命中的规则推出：环节偏慢建议拆分或换引擎，失败率偏高建议排查失败原因，缓存占比高建议关注上下文膨胀，单日过长建议确认是否批量重跑。

## 报告的五个小节

- **本期概览**：记录次数、成功率、累计耗时、Token、可选的 git 提交量
- **时间都花在哪**：按 role 的耗时表格和占比
- **哪些环节最费**：按平均单次耗时排序，最多 5 条，带失败次数
- **异常信号**：命中的规则和具体数值；没有命中时明确写「未触发任何异常规则」
- **建议**：由异常信号推出的可执行建议；指标平稳时给保持类建议

`report` 默认把 Markdown 打到标准输出；给了 `--out` 会同时写文件，写文件的提示走 stderr，不混进正文。

## 注意

原内部工作台里绑死私有语义的部分都砍掉了：

- 不读内部看板、不连 server、不跑 workflow、不做 AI 调度、不发通知
- 数据只来自本地 `runs.jsonl` 和 `--repo` 指定的 git 仓库
- 时间统一按本地时间处理；带时区的输入会先转成本地时间再比较
- `report` 是唯一输出人读文本的子命令，其余全部输出 JSON
- 空文件、空仓库、坏行都不会让命令崩溃；输入错误时退出码 `2`，stderr 上是 `{"error": "..."}`

## 依赖

- `python3`（3.8+），只用标准库
- `git`（仅在用 `--repo` / `git` 子命令时需要）
