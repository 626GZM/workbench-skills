---
name: daily-report
description: "把工作日历目录里某一天的工作记录汇总成可以直接复制给领导的日报：明细版（今日工作 + 明日计划）或固定条数的简版，支持按一句话改稿和“以后都这样”的固定要求。当用户要写日报、整理今天做了什么、把工作记录变成汇报文字时使用。"
metadata:
  requires:
    bins: ["python3", "git"]
---

# daily-report

读一个手写友好的「工作日历目录」，把某一天的工作记录汇总成日报。明细版纯规则拼接，简版可以让模型按样例仿写，失败自动回退规则版。

## 使用场景

- 一天结束，要把几个需求的进展汇总成一段能直接发给领导的文字
- 记录都在 `timeline.jsonl` 里，但不想自己组织语言和口径
- 同一版日报想换句话强调某一条（`revise`）
- 有些格式要求（“每条都带进度”“不要写文件名”）每次都要提，用 `rules` 存下来

## 输入目录结构

工作区就是一个普通目录，每个需求一个子目录，目录名随意：

```
workspace/
  task-a/
    meta.json
    timeline.jsonl
  task-b/
    meta.json
    timeline.jsonl
```

`meta.json`（UTF-8，字段都可选，但 `title` 建议填）：

```json
{
  "title": "【REQ-101】订单导出优化",
  "stage": "开发",
  "modules": ["导出任务", "模板配置"],
  "progress": {"开发": 80},
  "plan": ["继续开发", "补充导出失败提示"]
}
```

| 字段 | 说明 |
|---|---|
| `title` | 需求名；写成 `【REQ-101】订单导出优化` 时，日报里显示为 `REQ-101 订单导出优化` |
| `code` | 可选，需求编号；`title` 里没有 `【】` 时拼成 `编号 标题` |
| `stage` | 当前阶段名，如 `需求分析` / `开发` / `自测` |
| `modules` | 功能模块清单；简版按模块拆条，一条一个模块 |
| `progress` | 阶段名 → 百分比；用当前阶段的百分比出进度 |
| `plan` | 可选，明日计划；不填就按 `progress` 里没到 100% 的阶段推“继续 xx” |

`timeline.jsonl` 每行一条记录：

```json
{"at": "2026-11-17T10:00:00", "role": "写代码-T1", "ok": true, "engine": "kimi", "note": "完成导出任务列表接口"}
```

| 字段 | 说明 |
|---|---|
| `at` | 必填，ISO 时间；按它的日期归到某一天 |
| `note` | 必填，日报的唯一素材，写“做成了什么” |
| `ok` | 可选；显式为 `false` 的记录（失败、重试）不写进日报 |
| `role` / `engine` | 可选，只做追溯，不输出到日报 |

写记录时就已经用对外口径，生成时只做轻量清洗：工程词换人话、计数类丢掉、等待 / 卡住 / 失败 / 返工丢掉。没有记录的一天不会编内容。

## 用法

```bash
# 明细版：按需求分组，当前进度 + 今天做了什么 + 明日计划（纯规则，不调模型）
python3 scripts/daily_report.py build --workspace ~/workbench/daily --date 2026-11-17

# 简版：正好 6 条，模型按样例仿写；没装 CLI / 没 key / 失败自动回退规则版
python3 scripts/daily_report.py build --workspace ~/workbench/daily --date 2026-11-17 \
  --style brief --n 6 --engine kimi

# 另存到文件（detail 写 markdown，brief 写逐行文本）
python3 scripts/daily_report.py build --workspace ~/workbench/daily --date 2026-11-17 \
  --style brief --out /tmp/daily-2026-11-17.txt

# 可选：把当天提交也算进日报（走 wbkit.git，不裸调 git）
python3 scripts/daily_report.py build --workspace ~/workbench/daily --date 2026-11-17 \
  --git-repo ~/repos/order-service --git-branch feature/export

# 按一句话改已有日报，只改说到的地方
python3 scripts/daily_report.py revise --text /tmp/daily-2026-11-17.txt \
  --ask "把第 2 条写得更具体" --engine kimi

# 固定要求（“以后都这样”）
python3 scripts/daily_report.py rules add --rules ~/workbench/daily-rules.json --text "每条都带进度"
python3 scripts/daily_report.py rules list --rules ~/workbench/daily-rules.json
python3 scripts/daily_report.py rules drop --rules ~/workbench/daily-rules.json --index 0
```

## 子命令

| 命令 | 说明 |
|---|---|
| `build` | `--style detail`（默认）出 markdown 明细；`--style brief` 出 `--n` 条简版 |
| `revise` | `--text` 给日报文件、`--ask` 给一句话要求，只改说到的地方，返回改好的全文 |
| `rules add` | 追加一条固定要求（`--text`），生成和改稿时拼进提示词 |
| `rules list` | 列出固定要求 |
| `rules drop` | 删一条，`--index` 从 0 开始 |

`build` 的 `--engine` 只影响简版，可选 `claude` / `codex` / `deepseek` / `kimi`，默认 `kimi`。

## 输出 JSON

`build --style detail`：

```json
{"date": "2026-11-17", "source": "rule", "markdown": "# 日报 ...", "out": null}
```

`build --style brief`：

```json
{"date": "2026-11-17", "source": "ai", "lines": ["1. [REQ-101 订单导出优化] ..."], "out": null}
```

- `source`：`ai` 模型仿写成功 / `rule` 规则版 / `none` 这一天没有记录
- `none` 时 `lines` 为 `[]`，并多一个 `"message": "这一天没有记录到进展"`
- `out`：给了 `--out` 就是写入的绝对路径，否则 `null`

`revise`：`{"source": "ai", "text": "改好的全文"}`；`rules`：`{"path": "规则文件路径", "rules": ["..."]}`。

退出码：成功 `0`；参数错、工作区不存在、日期格式不对、日报文件不存在等输入错误 `2`，错误信息是 stderr 上的 `{"error": "..."}`。

## 日报写作规则（已写进提示词，规则版也遵守）

- 只写做成了什么，带范围或数量（完成哪几个接口、覆盖哪几条链路），不写“讨论 N 个问题”“评审 N 轮”这类计数
- 不出现 e2e / dev / test / 联调环境 / 冒烟这些工程词：跟前端对接口说“与前端联调”，本地跑通说“本地自测”，环境里跑通说“验证通过”
- 不写等待、卡住、失败重试、延期、返工；还在做的写推进方向和进展
- 进度只出 `已完成` / `进行中 70%` / `80%` / `90%` 三档
- 不编造：当天做了什么只能来自当天的记录
- 不提 AI、模型、工具、自动化

## 简版的两条路径

1. **AI 仿写**：`agent.run(engine, prompt, cwd, profile='docs')`，提示词里带样例、格式、口径、固定要求和当天事实。要求模型返回正好 `n` 条编号行；返回条数不对、超时、限流、没装 CLI、没配 key 都算失败。
2. **规则回退**：按需求分组，每条记录配一个功能模块（没有模块清单就用当前阶段），形如
   `1. [REQ-101 订单导出优化] 导出任务：完成导出任务列表接口，进度：进行中 80%`；
   条数在需求之间轮流分，最多 `n` 条。记录不足 `n` 条时按实际条数输出，**不编造**。

## 配置

- 固定要求文件默认 `~/.config/workbench-skills/daily-rules.json`（跟 wbkit 的配置目录一致），也可以用 `--rules` 指到别处
- 模型配置沿用 wbkit：`models.<engine>.cli`、`models.<engine>.model`，密钥走 `cfg.api_key()`（环境变量或 `secrets.json`）
- 简版调用用 `docs` profile：模型只能读，不能改工作区

## 注意

原内部工作台里绑死私有语义的部分都砍掉了：

- 不再读 `workflow.json`，也不再 import `workflow` / `e2e` / `server`；阶段、等待、评审这些内部状态改由 `meta.json` 的 `stage` / `progress` / `plan` 表达
- 联调 / 测试问题、验证结果不再从内部数据库读，改成写在 `timeline.jsonl` 的 `note` 里
- 当天提交默认不看；需要时显式给 `--git-repo` / `--git-branch`，用 `wbkit.git` 只读查询
- 不做定时推送、不写状态文件；只生成文本

## 依赖

- `python3`（3.8+），只用标准库
- 简版和 `revise`：至少装一家 AI 编程 CLI（claude / codex），或配好 deepseek / kimi 的 key；不可用时简版回退规则版，`revise` 直接报错
- `--git-repo`：`git`
