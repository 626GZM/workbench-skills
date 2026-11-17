---
name: online-issue-investigate
description: "收到一条线上告警（或一整段告警原文）时，自动抠线索、查日志、对着线上代码只读排查，产出“现象 / 根因 / 证据 / 影响范围 / 修复建议 / 止血建议”的报告。只读排查，不改代码、不提交。"
metadata:
  requires:
    bins: ["python3", "git"]
---

# online-issue-investigate

给一条线上告警，把“查日志、看代码、出结论”这一套做完。全程只读：不改代码、不提交、不连数据库。

每个问题一个目录（`<issues_root>/OL-YYYYMMDD-01/`）：

- `issue.json`：线索、状态、结构化结论
- `告警原文.txt`：粘进来的原文（已打码）
- `日志摘要.md`：查到的日志（已打码）+ 用到的查询
- `堆栈代码.md`：按日志里的“类名:行号”截好的代码
- `排查报告.md`：给人看的结论

## 使用场景

- 收到一条线上告警，想知道“到底怎么回事、影响多大、要不要改代码”
- 手里只有一段告警原文 / 报错堆栈，想快速把 traceId、服务、异常类名抠出来
- 排查前先确认线上代码是哪一版（`origin/release` 的只读检出），再让 AI 对着代码判断
- 排查完把结论存档，第二天回看“当时怎么定位的”

## 用法

```bash
# 1. 先看看能从告警里抠出什么（不落盘、不调模型）
python3 scripts/online_issue.py parse-alert --alert ~/alerts/alert.txt \
  --services order-service,payment-service

# 也可以直接从 stdin 喂告警原文
cat ~/alerts/alert.txt | python3 scripts/online_issue.py parse-alert

# 2. 建一个问题（自动解析告警并落 issue.json）
python3 scripts/online_issue.py create --title "下单接口大量 500" \
  --alert ~/alerts/alert.txt --env prod --services order-service --minutes 60

# 3. 排查（同步执行，进度打到 stderr）。--repo 可重复，--base release 用只读检出
python3 scripts/online_issue.py investigate OL-20260101-01 \
  --repo ~/repos/order-service --repo ~/repos/payment-service --base release

# 已经有现成代码目录时，不必走 git
python3 scripts/online_issue.py investigate OL-20260101-01 \
  --code ~/workspace/order-service-backup

# 指定模型；不传就按 claude → codex → deepseek → kimi 取第一个可用的
python3 scripts/online_issue.py investigate OL-20260101-01 --engine kimi --timeout 2400

# 4. 看问题 / 看报告
python3 scripts/online_issue.py list
python3 scripts/online_issue.py show OL-20260101-01
```

## 子命令

| 命令 | 说明 |
|---|---|
| `parse-alert --alert FILE [--services a,b]` | 从告警原文抠 env / traceId / 服务 / 时间 / 异常类名 / 关键词，输出 JSON |
| `create --title ... --alert FILE [--env prod] [--root DIR] [--services a,b] [--minutes 60]` | 建问题目录（编号按天递增），告警打码后落盘 |
| `list [--root DIR]` | 列问题，按编号倒序 |
| `investigate OL-xxx [--root DIR] [--engine kimi] [--repo DIR ...] [--base release] [--code DIR ...] [--timeout 2400]` | 查日志 → 准备只读代码 → 截堆栈代码 → AI 排查 → 写三份 md 并回写 issue.json |
| `show OL-xxx [--root DIR]` | 输出 `issue.json` 和 `排查报告.md` 正文 |

## 输出 JSON 结构

`parse-alert`：

```json
{
  "env": "prod",
  "trace": "0123456789abcdef0123456789abcdef",
  "traces": ["0123456789abcdef0123456789abcdef"],
  "services": ["order-service"],
  "time": "2026-11-17 10:00:00",
  "end": 1763345400,
  "exceptions": ["IllegalStateException"],
  "keywords": ["IllegalStateException"]
}
```

`issue.json`：

```json
{
  "id": "OL-20260101-01",
  "title": "下单接口大量 500",
  "env": "prod",
  "status": "待排查",
  "created_at": "2026-01-01T10:00:00",
  "minutes": 60,
  "trace": "0123456789abcdef0123456789abcdef",
  "services": ["order-service"],
  "keywords": ["IllegalStateException"],
  "detail": "补充说明",
  "alert_file": "告警原文.txt",
  "report": {
    "summary": "一句话结论", "confirmed": "日志里看到的", "cause": "根因",
    "evidence": ["日志时间 / 代码位置"], "impact": "影响范围",
    "need_code_change": true, "fix_plan": "怎么改", "files": ["服务/路径#方法"],
    "workaround": "止血建议", "data_fix_sql": "要修数据时的 SQL（没执行）",
    "confidence": "高", "engine": "claude", "at": "2026-01-01T10:05:00",
    "log": "logs/ai-20260101-100500-claude.log"
  },
  "log": [{"at": "...", "status": "待排查", "note": "创建"}]
}
```

状态机：`待排查 → 排查中 → 待处理`（排查成功）；失败回到 `待排查`，错误写 `issue.json` 的 `error` 字段。

AI 必须输出固定 JSON：

```json
{"summary": "", "confirmed": "", "cause": "", "evidence": [], "impact": "",
 "need_code_change": true, "fix_plan": "", "files": [], "workaround": "",
 "data_fix_sql": "", "confidence": "高|中|低"}
```

解析失败（不是 JSON、缺 `summary` / `cause`）会明确报错，并把模型原始输出留在问题目录的 `logs/` 下。

## 日志查询（可插拔）

查询逻辑不绑定任何云厂商：`logs.query_cmd` 是一个命令模板，脚本把它当 shell 命令跑，**stdout 每行一条日志**。每个查询词跑一次（traceId、关键词、服务名依次，最多 6 次）。

占位符：

| 占位符 | 说明 |
|---|---|
| `{query}` | 查询词原样替换，模板里自己加引号 |
| `{query_quoted}` | 查询词做 shell 转义，适合 `grep` / 位置参数 |
| `{minutes}` | 时间窗（分钟） |
| `{env}` | 环境：prod / uat / test |

命令退出码非 0 会记一条提示，但 stdout 里的行仍会收下。单次超时用 `logs.timeout`（默认 180 秒）。

### 默认：本地日志文件（grep 兜底）

不配 `query_cmd` 时，按 `logs.files` 里的路径读本地日志（支持 `~`、通配符、目录递归找 `*.log`），按关键词不区分大小写逐行匹配。这个模式不依赖任何外部服务，适合本机留了日志、或从别处导出了日志的情况。

### 换成 SLS / ELK / Loki

把 `query_cmd` 换成对应产品的查询命令即可（下面是示例，端口、账号、索引名按自己的环境改）：

```jsonc
// 阿里云 SLS：查询词进 body 的 query 字段
"query_cmd": "aliyun sls GetLogsV2 --project my-project --logstore app-log --body '{\"from\":\"now-{minutes}m\",\"to\":\"now\",\"query\":\"{query}\"}'"
```

```jsonc
// Elasticsearch / OpenSearch
"query_cmd": "curl -s \"$ES_URL/app-log-*/_search?q={query}&size=200\" | python3 -c 'import json,sys;[print(h[\"_source\"][\"message\"]) for h in json.load(sys.stdin)[\"hits\"][\"hits\"]]'"
```

```jsonc
// Grafana Loki（logcli）
"query_cmd": "logcli query '{app=\"order-service\"}' --regex {query_quoted} --since {minutes}m --limit 200 -o raw"
```

原则：命令把命中的日志按行打到 stdout；凭证、地址、项目名放环境变量或本地配置，不写进这个 skill。

## 线上代码

- `--repo <dir>`（可重复）：本地 git 仓库。配合 `--base release`（默认）会把 `origin/release` 检出到 `<issues_root>/_code/<仓库名>` 的 **detached worktree**，只读排查用，不动你当前工作区；同一个仓库可跨问题复用。拉取失败会退回本地已有的 `origin/release`，再不行退回当前工作区并提示。
- `--base ''`：不建 worktree，直接看仓库当前工作区（适合本地已经切到对应分支）。
- `--code <dir>`（可重复）：现成目录，直接用，不碰 git。
- 服务名取目录名；`--repo` 也可以写配置 `projects` 里的项目名。

堆栈里出现 `(OrderService.java:88)` 时，会在上面这些代码里按类名找文件，截前后 8 行写进 `堆栈代码.md` 并塞进提示词——省得模型满仓库翻。JDK、框架的类找不到就跳过。

## 配置

配置按底座约定查找（`WB_CONFIG` > `./.wbkit.json` > `~/.config/workbench-skills/config.json`）：

```json
{
  "issues_root": "~/workbench/online-issues",
  "services": ["order-service", "payment-service"],
  "logs": {
    "default_minutes": 60,
    "max_lines": 300,
    "timeout": 180,
    "query_cmd": "",
    "files": {
      "prod": ["~/logs/order-service/*.log", "~/logs/payment-service/*.log"],
      "uat": ["~/logs/uat/"]
    },
    "env_words": {
      "prod": ["prod", "生产", "线上"],
      "uat": ["uat", "预发"],
      "test": ["test", "测试"]
    }
  }
}
```

- `issues_root`：问题根目录，命令行 `--root` 优先。
- `services`：服务名候选。告警解析只在这个列表里挑服务，避免把线程名、包名当服务。
- `logs.env_words`：从告警原文判断环境用的关键词，可覆盖默认值。
- 模型 key 一律走 `wbkit` 的 secrets（环境变量或 `secrets.json`），配置文件里不写 key。

## 注意

原内部实现里这些没搬：

- **阿里云 SLS SDK / 内部 logstore / CloudSSO 登录态**：改成可插拔的 `logs.query_cmd`，本 skill 里不出现任何云厂商 SDK，也不管登录过期；换后端只改配置。
- **orchestrator 额度轮转、notifier 通知、后台线程**：`investigate` 改成同步执行，进度打 stderr；模型按 claude → codex → deepseek → kimi 取第一个可用的，失败就把错误写回 `issue.json`。
- **workflow 目录 / 内部服务清单**：服务候选、日志路径、问题根目录全部走配置或参数。
- **自动改代码（hotfix）**：不在本 skill 范围内，只出报告；`data_fix_sql` 只展示不执行。

模型调用统一走 `wbkit.agent.run(..., profile='read')`：只读工具白名单，模型看不到写权限。日志、告警原文、写进提示词的内容都先过一遍打码（手机号、身份证、银行卡、邮箱、Bearer / token）。

## 依赖

- `python3`（3.8+），只用标准库
- `git`（`--repo` + `--base` 才需要）
- 至少装一家 AI 编程 CLI（claude / codex），或配了 key 的 deepseek / kimi
