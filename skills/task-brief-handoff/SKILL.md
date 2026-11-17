---
name: task-brief-handoff
description: "接手或交接一个开发任务时，用一份 brief.json 记录目标、验收条件、约束、相关仓库、待办、关键决定和踩坑，自动读取 git 现状生成结构化简报或交接单。新会话不必重新翻整个仓库，适合任务开头、换人接手、阶段性收尾时使用。"
metadata:
  requires:
    bins: ["python3", "git"]
---

# task-brief-handoff

一个任务一个目录，目录里放 `brief.json`。它只记事实：要做什么、怎么算做完、有哪些限制、代码在哪、现在停在哪、做过哪些决定、踩过哪些坑。

`build` 把 `brief.json` 和 git 现状拼成一份 Markdown 简报；`handoff` 生成给下一个人的交接单。简报里没有的内容一律写“未记录”，不推测、不补写。

## 使用场景

- 新会话接手一个做到一半的任务，先读简报，不用把整个仓库重新翻一遍
- 换人 / 换模型 / 隔了几天继续做，需要知道“停在哪、别重做什么”
- 任务开头先把目标、验收条件、约束、仓库落下来，后面每次追加决定和踩坑
- 阶段性收尾，给接手人一张只含未完成项和风险的交接单

## 用法

```bash
# 1. 在任务目录建 brief.json（目录不存在会创建；重复执行只补缺失字段）
python3 scripts/brief_handoff.py init --dir ~/tasks/REQ-101 \
  --title "订单导出优化" \
  --background "导出任务改为异步执行，避免接口超时" \
  --goal "导出任务支持异步提交、进度查询和结果下载" \
  --status "导出提交和进度查询已完成，下载接口还没开始" \
  --acceptance "提交导出后立即返回任务号" \
  --acceptance "进度查询能看到百分比和当前状态" \
  --constraint "不改前端已有接口路径" \
  --repo ~/repos/order-service \
  --support "前端同学配合联调进度查询接口"

# 2. 干活过程中随手追加（同样的内容重复 add 不会写两遍）
python3 scripts/brief_handoff.py add --dir ~/tasks/REQ-101 \
  --kind decision --text "任务状态用状态机，不用布尔字段"
python3 scripts/brief_handoff.py add --dir ~/tasks/REQ-101 \
  --kind todo --text "补进度查询的单元测试"
python3 scripts/brief_handoff.py add --dir ~/tasks/REQ-101 \
  --kind pitfall --text "本地跑测试要先启动对象存储，否则 3 个用例失败"
python3 scripts/brief_handoff.py add --dir ~/tasks/REQ-101 \
  --kind fact --text "导出结果保留 7 天，由定时任务清理"
# 已经做完的待办，用 --done 标记（只对 todo 有效）
python3 scripts/brief_handoff.py add --dir ~/tasks/REQ-101 \
  --kind todo --text "建导出任务表" --done

# 3. 生成简报。不给 --git-repo 就用 brief.json 里登记的仓库
python3 scripts/brief_handoff.py build --dir ~/tasks/REQ-101 \
  --git-repo ~/repos/order-service --out ~/tasks/REQ-101/BRIEF.md

# 4. 让模型把散落记录整理成通顺文档；失败自动回退规则版
python3 scripts/brief_handoff.py build --dir ~/tasks/REQ-101 --engine kimi

# 5. 只想看 brief.json 和简报正文
python3 scripts/brief_handoff.py show --dir ~/tasks/REQ-101

# 6. 生成交接单
python3 scripts/brief_handoff.py handoff --dir ~/tasks/REQ-101 \
  --out ~/tasks/REQ-101/HANDOFF.md --engine kimi

# 程序消费：加 --json
python3 scripts/brief_handoff.py build --dir ~/tasks/REQ-101 --json
```

## 子命令

| 命令 | 说明 |
|---|---|
| `init --dir DIR [--title ...] [--background ...] [--goal ...] [--status ...] [--acceptance ...] [--constraint ...] [--repo ...] [--support ...]` | 建 `brief.json`；已存在时只补缺失字段和列表项，不覆盖已有内容 |
| `add --dir DIR --kind decision\|todo\|pitfall\|fact --text "..." [--done]` | 追加一条记录；同 `kind` 下同样文本只保留一条。`--done` 只对 `todo` 有效 |
| `build --dir DIR [--git-repo DIR ...] [--out FILE] [--engine kimi] [--json]` | 生成 Markdown 简报；`--git-repo` 可重复，省略时用 `brief.json` 里的 `repos` |
| `show --dir DIR [--json]` | 输出 `brief.json` 和规则版简报正文（含 git 现状） |
| `handoff --dir DIR --out FILE [--engine kimi] [--json]` | 生成交接单：已完成、未完成、卡在哪、风险、需要谁配合 |

`--engine` 可选 `claude` / `codex` / `deepseek` / `kimi` / `none`。不给 `--engine` 时只出规则版；`none` 也是规则版。模型调用失败、超时、返回内容缺小节，都会自动回退规则版，并在 JSON 的 `source` / `error` 里说明。

## brief.json 结构

```json
{
  "version": 1,
  "title": "订单导出优化",
  "background": "导出任务改为异步执行，避免接口超时",
  "goal": "导出任务支持异步提交、进度查询和结果下载",
  "acceptance": ["提交导出后立即返回任务号", "进度查询能看到百分比和当前状态"],
  "constraints": ["不改前端已有接口路径"],
  "repos": ["~/repos/order-service"],
  "status": "导出提交和进度查询已完成，下载接口还没开始",
  "support": ["前端同学配合联调进度查询接口"],
  "todos": [{"text": "补进度查询的单元测试", "done": false, "at": "2026-01-01T10:00:00"}],
  "decisions": [{"text": "任务状态用状态机，不用布尔字段", "at": "2026-01-01T10:00:00"}],
  "pitfalls": [{"text": "本地跑测试要先启动对象存储", "at": "2026-01-01T10:00:00"}],
  "facts": [{"text": "导出结果保留 7 天", "at": "2026-01-01T10:00:00"}],
  "created_at": "2026-01-01T09:00:00",
  "updated_at": "2026-01-01T10:00:00"
}
```

## 输出 JSON 结构

`build` / `handoff` 加 `--json` 时：

```json
{
  "dir": "/abs/path/to/task",
  "markdown": "# 交接简报：...\n",
  "source": "rule",
  "engine": "kimi",
  "error": "模型调用失败时的原因，规则版为空字符串",
  "out": "/abs/path/to/BRIEF.md",
  "brief": {},
  "repos": [
    {
      "path": "/abs/path/to/repo",
      "root": "/abs/path/to/repo",
      "ok": true,
      "branch": "main",
      "dirty": ["src/main/java/.../ExportService.java"],
      "untracked": ["notes.md"],
      "commits": [{"hash": "abc1234", "time": "2026-01-01 10:00:00 +0800", "subject": "加导出任务表"}],
      "reason": ""
    }
  ]
}
```

仓库不存在、不是 git 仓库或读取失败时，该条 `ok` 为 `false`，`reason` 写明原因，简报里对应位置会注明“跳过”，不中断生成。

`init` / `add` 输出：

```json
{
  "dir": "/abs/path/to/task",
  "path": "/abs/path/to/task/brief.json",
  "created": true,
  "changed": true,
  "brief": {}
}
```

`add` 额外带 `kind`、`added`（是否新增）、`count`（该 kind 当前条数）。

## 简报小节

`build` 固定输出：项目背景、目标、验收条件、当前状态（git 分支 / 脏文件 / 最近 5 条提交）、待办、关键决定、踩坑记录、下一步。约束写在“目标”下面，事实写在“项目背景”下面。

`handoff` 固定输出：已完成、未完成、卡在哪、风险、需要谁配合。已完成和未完成严格按 `todos[].done` 区分，不会把已完成的再列进未完成。

## 配置

- 模型 key 走底座 `wbkit` 的约定：环境变量或 `~/.config/workbench-skills/secrets.json`，配置文件里不写 key。
- 本 skill 没有自己的配置项；任务目录由 `--dir` 指定，仓库路径写在 `brief.json` 的 `repos` 里或由 `--git-repo` 传入。

## 注意

- git 只读：只看分支、未提交改动、最近提交，不提交、不推送、不切换分支。
- 模型只做文字整理。提示词明确要求：不编造简报里没有的事实，没写的写“未记录”，禁止写“按计划推进”这类空话。
- 模型输出必须包含固定小节标题，缺任何一个都回退规则版，避免交接文档结构漂移。
- `init` 和 `add` 都是幂等的：重复执行不会产生重复字段或重复记录。
- 相比内部工作台实现，砍掉了 `workflow.json`、模板渲染、内部角色名和 server 依赖；任务状态、角色分工全部由 `brief.json` 的自由文本和待办表达。

## 依赖

- `python3`（3.8+），只用标准库
- `git`（读取仓库现状；没有仓库时自动跳过）
- 模型整理功能需要本机装好 `claude` / `codex`，或配了 key 的 `deepseek` / `kimi`
