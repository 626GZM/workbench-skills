---
name: test-issue-triage
description: "测试或前端一次性发来一堆问题（聊天记录、文档、一段文字）时，拆成一条条独立问题，逐条只读排查定归属（后端 bug / 不是 bug / 前端问题 / 信息不够）、给出根因和改法，并可让模型直接改后端问题。问题清单落在本地 issues.json，全程不绑定任何内部工作台。"
metadata:
  requires:
    bins: ["python3", "git"]
---

# test-issue-triage

把测试 / 前端甩来的一整段反馈变成一条条可跟踪的问题：拆分 → 落盘 → 只读排查 → 需要时改后端代码。

## 使用场景

- 测试在群里发了一大段问题，带编号或按空行分段，要拆成一条条分别处理
- 一条问题说不清是谁的锅：先只读排查，判成后端 bug / 不是 bug / 前端问题 / 信息不够
- 判成后端问题后，让模型直接在指定仓库里改，并给测试一段能直接转发的回复
- 想留一份问题台账：谁报的、状态到哪、根因和回复是什么

公开版一次只处理一条：没有队列、没有锁、不开后台线程，`diagnose` / `fix` 都是前台跑完返回。

## 用法

```bash
# 一整段反馈拆成多条（纯规则，不调模型、不花 token），输出 JSON 数组
python3 scripts/test_issue.py split --text feedback.txt

# 内容很乱（聊天记录、口语化）：先让模型拆，没 key / 失败自动回退规则拆
cat chat.txt | python3 scripts/test_issue.py split --stdin --ai --engine kimi --source 联调

# 手工记一条，编号按来源自动递增（测试→ISSUE-01，联调→FE-01）
python3 scripts/test_issue.py add --store issues.json --title "导出订单接口报 500" \
  --detail bug.md --api "POST /orders/export" --source 测试

# 列出待处理
python3 scripts/test_issue.py list --store issues.json --status 待处理

# 只读排查一条（仓库可重复；文档目录默认取 issues.json 所在目录）
python3 scripts/test_issue.py diagnose --store issues.json --id ISSUE-01 \
  --repo ~/repos/order-service --docs ~/workbench/docs --engine kimi

# 按排查结论改后端代码；--commit 才提交（不推送）
python3 scripts/test_issue.py fix --store issues.json --id ISSUE-01 \
  --repo ~/repos/order-service --engine kimi --commit

# 只看会发给代理的参数，不真跑、不改文件
python3 scripts/test_issue.py fix --store issues.json --id ISSUE-01 \
  --repo ~/repos/order-service --engine kimi --dry-run

# 改状态
python3 scripts/test_issue.py status --store issues.json --id ISSUE-01 --set 待验证
```

## 子命令

| 命令 | 参数 | 说明 |
|---|---|---|
| `split` | `--text FILE` 或 `--stdin`，`[--ai] [--engine] [--source]` | 拆成一条条，输出 JSON 数组 `[{title, detail, api}]`；带编号按编号拆，否则按空行拆；去重、丢开场白、自动抽接口 |
| `add` | `--store`，`--title`，`[--detail FILE]`，`[--api]`，`[--source]` | 新记一条，编号按来源前缀递增 |
| `list` | `--store`，`[--status]` | 输出 `{count, store, issues[]}` |
| `diagnose` | `--store`，`--id`，`--repo DIR`（可重复），`[--docs DIR]`（可重复），`[--engine]` | 只读排查，写回 `verdict / cause / plan / reply / files` |
| `fix` | `--store`，`--id`，`--repo DIR`（可重复），`[--docs]`，`[--engine]`，`[--commit]`，`[--dry-run]` | 只改 `--repo` 指向的工作区；`--commit` 提交到当前分支，**从不推送** |
| `status` | `--store`，`--id`，`--set STATUS` | 状态：`待处理 / 处理中 / 待验证 / 已修复 / 已关闭` |

## 问题清单（issues.json）

```json
{
  "issues": [
    {
      "id": "ISSUE-01",
      "source": "测试",
      "title": "导出订单接口报 500",
      "detail": "ids=[] 时必现",
      "api": "POST /orders/export",
      "status": "待处理",
      "verdict": "backend",
      "cause": "导出时没校验空列表",
      "plan": "OrderService#export 加空列表校验，回归测 ids=[]",
      "reply": "是后端的问题，空列表没挡住，已经定位。",
      "files": ["order-service/src/OrderService.java#export"],
      "summary": "加了空列表校验和单测",
      "commits": ["a1b2c3d"],
      "log": [{"at": "2026-11-17 10:00:00", "action": "diagnose", "verdict": "backend"}]
    }
  ]
}
```

- 用 `--store FILE`，或在配置里写 `issues.store`；都没给时用当前目录 `issues.json`。
- `verdict / cause / plan / reply` 是顶层字段，`diagnose` / `fix` 直接写回；`files / summary / commits` 有值才加。
- `fix` 判成 `backend` 时状态自动转 `待验证`，其它归属不动状态。

## 判定口径

| verdict | 什么时候用 | 怎么处理 |
|---|---|---|
| `backend` | 后端代码的问题：返回不对、校验不对、缺字段、报错 | `fix` 在 `--repo` 工作区改代码，并补一个覆盖该问题的回归测试 |
| `not_bug` | 按需求和方案本来就是这样 | 不改代码，引用需求或验收条件原文（如 AC-xx）解释 |
| `frontend` | 后端返回没问题，是前端展示 / 交互问题 | 不改代码，写清前端哪里不对，方便转给前端 |
| `need_info` | 信息不够判断 | 不改代码，写清还要对方补什么：复现步骤、测试数据、环境、截图、请求和返回 |

- 模型给的值不在配置的 `issues.verdicts` 里，一律回落 `need_info`。
- `reply` 像当面说话：几句大白话，不写排查过程、不堆术语。

## 配置

配置按底座约定查找（`WB_CONFIG` > `./.wbkit.json` > `~/.config/workbench-skills/config.json`）：

```json
{
  "issues": {
    "store": "~/workbench/issues/issues.json",
    "engine": "kimi",
    "sources": {"测试": "ISSUE", "联调": "FE"},
    "verdicts": ["backend", "not_bug", "frontend", "need_info"]
  },
  "models": {
    "kimi": {"cli": "claude", "model": ""}
  }
}
```

- `issues.sources`：来源到编号前缀的映射，默认 `测试→ISSUE`、`联调→FE`。
- `issues.verdicts`：归属枚举，改提示词口径时要一起改。
- `issues.engine`：默认引擎，默认 `claude`，可选 `codex / deepseek / kimi`（见 multi-agent-run skill）。
- 密钥一律走环境变量或 `~/.config/workbench-skills/secrets.json`，配置文件里不写 key。

## 注意

原内部工作台里这些能力**没有搬**，按需在外面自己接：

- **`workflow.json` / `orchestrator` / `userchanges` / `codeindex`**：全部砍掉，换成独立的 `issues.json` + 直接给仓库路径，不再有需求目录、worktree、AI 分支、需求上下文。
- **云效 projex**：拉缺陷、回写缺陷状态、登记工时、建子任务全砍，问题只来自 `split` / `add`。
- **e2e 重跑**：修完不再自动触发 e2e，需要回归就自己跑 e2e-test skill。
- **队列 / 锁 / 后台线程 / 断点恢复**：公开版一次处理一条，前台跑完返回；不要并发对同一个仓库跑 `fix`。
- **推送**：`fix --commit` 只提交到当前分支，不 push；要推自己推。
- **截图 base64 落盘**：改成把截图路径写进 `detail`，模型用 Read 自己看。
- `diagnose` 只读（profile=`read`），模型不会改文件；`fix` 用 profile=`coder`，会改 `--repo` 下的文件。
- `fix --commit` 用 `git add -A` 提交工作区现有改动，仓库最好在开跑前保持干净，别把无关改动一起提交。

## 依赖

- `python3`（3.8+），只用标准库
- `git`（仅 `fix --commit` 需要）
- `diagnose` / `fix` / `split --ai`：本机装了 claude / codex，或配了 deepseek / kimi 的 key
