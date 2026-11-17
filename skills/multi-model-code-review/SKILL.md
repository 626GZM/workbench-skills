---
name: multi-model-code-review
description: "对一个 git 改动（分支、单个提交或未提交改动）做代码审查：取 diff、按目录分组并截断，交给一个模型审查，或用多个模型并跑后按位置合并去重、标注哪些模型都提到了。用户要“review 一下这段改动”“换几个模型一起看看有没有问题”“生成代码审查报告”时使用。只读，不改代码、不提交。"
metadata:
  requires:
    bins: ["python3", "git"]
---

# multi-model-code-review

把一段 git 改动整理成模型看得懂的 diff，让一个或多个模型挑问题，最后合并成一份带位置（`文件:行`）和严重程度的结果。除 `report` 外全部输出 JSON。

## 使用场景

- 提交前自查一段分支改动或未提交改动
- 同一份 diff 换几个模型各审一遍，只留下大家都认同或确实能证实的问题
- 把审查结果交给 AI 继续修，或渲染成 markdown 贴到评审里
- 在流水线里跑一次只读审查，按 `severity` 决定要不要阻断

## 用法

```bash
# 分支改动：默认从 origin/main 的分叉点算到 HEAD，用 Kimi 审一遍
python3 scripts/code_review.py review \
  --repo ~/repos/project-a --base origin/main --head HEAD --engine kimi

# 未提交改动（含暂存区和未跟踪的文本文件）
python3 scripts/code_review.py review \
  --repo ~/repos/project-a --uncommitted --engine deepseek

# 单个提交：先取父提交号，再把它当基线（--base 只接受合法的 ref / 提交号）
PARENT=$(git -C ~/repos/project-a rev-parse <提交号>^)
python3 scripts/code_review.py review \
  --repo ~/repos/project-a --base "$PARENT" --head <提交号> --engine codex

# 多个模型并跑，去重合并后存结果
python3 scripts/code_review.py batch \
  --repo ~/repos/project-a --base origin/main --engines kimi,codex \
  --out /tmp/review.json

# 把结果渲染成 markdown
python3 scripts/code_review.py report --input /tmp/review.json --out /tmp/review.md

# 只看会执行什么，不真的调模型
python3 scripts/code_review.py review \
  --repo ~/repos/project-a --engine kimi --dry-run
```

## 子命令

| 命令 | 作用 | 输出 |
|---|---|---|
| `review` | 取一次 diff，交给一个模型审查 | 结果 JSON |
| `batch` | 同一份 diff 交给多个模型并跑，按位置去重合并 | 结果 JSON |
| `report` | 把 `review` / `batch` 的结果渲染成 markdown | markdown 文本 |

### review 参数

| 参数 | 说明 |
|---|---|
| `--repo DIR` | 仓库目录，默认当前目录 |
| `--base REF` | 基线，默认 `origin/main`；用三点 diff，从分叉点算起 |
| `--head REF` | 目标，默认 `HEAD` |
| `--uncommitted` | 改为审查未提交改动；忽略 `--base` / `--head` |
| `--engine` | `claude` / `codex` / `deepseek` / `kimi`，默认 `kimi` |
| `--profile` | 模型权限档位，默认 `read`（只读） |
| `--max-files N` | 最多送审文件数，默认 40；超出时优先保留改动行数多的 |
| `--timeout N` | 单次模型调用超时秒数，默认 1200 |
| `--out FILE` | 结果 JSON 写到这里 |
| `--dry-run` | 只打印将执行的代理参数（argv / cwd / profile），不启动模型 |

`batch` 用 `--engines kimi,codex` 代替 `--engine`，其余参数相同。`report` 用 `--input` 指定结果 JSON。

## 提示词口径

- 只审查 diff 里出现的改动，diff 之外、没看到的代码不臆测。
- 只报能证实的问题：必须贴出证据行，并指出文件和新文件行号；拿不准的不报。
- 不评论风格偏好（命名喜好、缩进、注释多少）。
- 优先级：安全、正确性、并发、事务、空指针、边界、资源泄漏、错误处理。
- 每条给出可执行的改法，不只说“建议优化”。
- 输出固定为 JSON 数组：`file` / `line` / `severity` / `title` / `detail` / `suggestion`。

`severity` 取值：`blocker`（数据错误、安全漏洞、线上故障）、`major`（明确的逻辑或契约缺陷）、`minor`（边界或健壮性不足）、`nit`（可改可不改）。

## 输出 JSON 结构

`review`：

```json
{
  "ok": true,
  "engine": "kimi",
  "seconds": 42,
  "models": ["kimi-for-coding"],
  "repo": "~/repos/project-a",
  "range": {"mode": "branch", "base": "origin/main", "head": "HEAD"},
  "files": [{"path": "src/order.py", "dir": "src", "add": 12, "del": 3, "truncated": false}],
  "groups": [{"dir": "src", "files": ["src/order.py"]}],
  "skipped": [{"path": "package-lock.json", "reason": "超出 --max-files"}],
  "prompt_chars": 18342,
  "issues": [
    {"file": "src/order.py", "line": 88, "severity": "major", "title": "空指针风险",
     "detail": "证据行 + 触发场景 → 实际结果 → 期望结果", "suggestion": "可执行的改法",
     "engine": "kimi"}
  ],
  "issue_count": 1,
  "log": "/tmp/agent-xxxx.log"
}
```

`batch` 在 `review` 的基础上多两个字段：

```json
{
  "engines": ["kimi", "codex"],
  "runs": [{"engine": "kimi", "ok": true, "seconds": 40, "issue_count": 2},
           {"engine": "codex", "ok": false, "error": "超时（1200 秒）"}],
  "issues": [
    {"file": "src/order.py", "line": 88, "severity": "blocker", "title": "空指针风险",
     "detail": "…", "suggestion": "…", "agreement": ["codex", "kimi"], "agreement_count": 2}
  ]
}
```

合并规则：`(file, line, 标题去掉空白和标点后小写)` 相同即视为同一条，严重程度取最高，详情和建议取偏长的那份；`agreement` 列出提到它的模型，`agreement_count` 是家数。

模型输出不是合法 JSON 时 `ok=false`，`error` 里写明原因，`raw_output` 给出原始输出的落盘路径（有 `--out` 时放在它旁边，否则放临时目录）。

## 配置

模型和权限档位由 `wbkit` 的配置提供，按以下优先级查找：

1. 环境变量 `WB_CONFIG`
2. 当前目录 `.wbkit.json`
3. `~/.config/workbench-skills/config.json`
4. 内置默认

```json
{
  "models": {
    "kimi": {"cli": "claude", "model": "kimi-for-coding", "key_env": "KIMI_API_KEY"},
    "codex": {"cli": "codex", "model": ""}
  }
}
```

key 只走环境变量或 `secrets.json`（权限 600），不写进配置文件。

## 注意

- 只读：不修改文件、不提交、不推送；`--profile read` 下模型只能用读文件、grep 和只读 git 命令。
- `--uncommitted` 下未跟踪的文本文件会补成“全新增”的 diff；二进制文件跳过，列在 `skipped` 里。
- 单文件 diff 超过 500 行或 24000 字符会截断，提示词里和结果里都有 `truncated` 标注。
- `batch` 用线程并行调多个模型；某一家超时、限流或没装 CLI 时只记在 `runs` 里，不影响其他家。
- 去重是保守的精确匹配（文件、行号、归一化标题），不做模糊聚类，避免把不同问题误合并。
- 如果要接外部审查工具：用 `--engine` 换成你自己的 CLI 包装（在 `wbkit.agent` 的引擎名上加一层），不要在本脚本里塞第二套进程管理。
- 原内部工作台的发布 / 评论接口、工作流状态、外部审查 CLI 依赖都已砍掉，这里只负责“取 diff → 调模型 → 合并 → 出报告”。

## 依赖

- `python3`（3.8+）
- `git`
- 至少一个可用引擎：本机已登录的 `claude` / `codex`，或配置了 key 的 `deepseek` / `kimi`
- 无需第三方 Python 包
