---
name: multi-agent-run
description: "在后台无头调用多个 AI 编程 CLI（Claude Code、Codex、DeepSeek、Kimi）跑一次任务并取回结果。用于把审查、核实、批改、写文档这类活派给另一个模型，或在脚本/流水线里让模型执行一次只读分析或写代码。支持权限档位（只读/核实/写代码/写文档）、超时与卡死检测、限流识别。不弹窗、不需要人工确认。"
metadata:
  requires:
    bins: ["python3"]
---

# multi-agent-run

一个命令把活派给任意一家模型的 CLI，拿回结构化结果。适合让主模型派子任务，也适合在脚本里跑一次无人值守的分析。

## 支持的引擎

| engine | 底层命令 | 认证方式 |
|---|---|---|
| `claude` | `claude -p` | 本机已登录的 Claude Code |
| `codex` | `codex exec` | 本机已登录的 Codex CLI |
| `deepseek` | `claude -p` + 换基地址 | 环境变量或 `secrets.json` 里的 key |
| `kimi` | `claude -p` + 换基地址 | 环境变量或 `secrets.json` 里的 key |

`deepseek` / `kimi` 借 Claude Code 的壳（能读文件、跑命令），只对这一个子进程替换 `ANTHROPIC_BASE_URL`、key 和模型，不影响你自己的 Claude Code 配置。

## 权限档位（profile）

无头运行不能弹窗问，白名单外的工具一律拒绝。

| profile | 能做什么 |
|---|---|
| `read` | 读文件、grep、只读 git 命令 |
| `verify` | 上面这些 + 跑 `mvn` / `gradle` + 只在 `src/test/**` 下写文件 |
| `coder` | 读写文件、跑命令、提交、派子任务 |
| `docs` | 只读，用来写文档（不含代码仓库写权限） |

## 用法

```bash
# 让 Kimi 只读分析一个仓库
python3 scripts/run_agent.py run \
  --engine kimi --profile read --cwd /path/to/repo \
  --prompt "总结这个模块的职责和外部依赖，200 字以内"

# 从文件读提示词，结果另存
python3 scripts/run_agent.py run \
  --engine claude --profile coder --cwd /path/to/repo \
  --prompt-file /tmp/task.md --log /tmp/run.log

# 用管道喂提示词
echo "列出 src 下所有 TODO" | python3 scripts/run_agent.py run --engine codex --profile read --cwd .

# 看本机哪些引擎可用
python3 scripts/run_agent.py check

# 存一把 key（写入 ~/.config/workbench-skills/secrets.json，权限 600）
python3 scripts/run_agent.py set-key --vendor deepseek --key sk-xxx
```

## 输出

成功时 JSON：

```json
{
  "ok": true,
  "engine": "kimi",
  "seconds": 42,
  "text": "模型的回答",
  "models": ["kimi-for-coding"],
  "log": "/tmp/agent-xxx.log",
  "session": null
}
```

失败时 `ok=false`，并给出 `error`：

- `超时（N 秒）`
- `没有回应：启动 N 秒了模型还没回一个字，多半是额度用完被限流`
- `卡住：N 秒没有任何新动作`
- 子进程非零退出时的 stderr 尾部

`quota=true` 表示这次失败像限流/欠费，换一家重试即可。

## 超时与中断

- `--timeout` 默认 1200 秒，到点连子孙进程一起杀掉。
- 启动后 150 秒还没有任何输出，判定为没有回应。
- 连续 600 秒没有新输出，判定为卡住。
- 不写 `.running` 标记残留，进程退出后自动清理。

## 配置

配置按以下优先级查找，只存路径不存 key：

1. 环境变量 `WB_CONFIG`
2. 当前目录 `.wbkit.json`
3. `~/.config/workbench-skills/config.json`
4. 内置默认

```json
{
  "models": {
    "claude": {"cli": "claude", "model": ""},
    "codex":  {"cli": "codex",  "model": ""},
    "deepseek": {"cli": "claude", "model": "deepseek-chat", "key_env": "DEEPSEEK_API_KEY"},
    "kimi":     {"cli": "claude", "model": "kimi-for-coding", "key_env": "KIMI_API_KEY"}
  }
}
```

`cli` 可以写绝对路径（比如桌面端自带、版本更高的 Claude Code）。

## 注意

- 提示词不进 argv，走 stdin，避免超长命令行和引号转义问题。
- macOS 上系统代理会自动补进子进程环境变量（`scutil --proxy`），程序坞启动的进程也能连上 Anthropic。
- key 只通过子进程环境变量传递，同一用户的其他进程在运行期间可能通过 `ps` 看到，按这个级别保护。
- 不做重试、不做模型轮换：失败时看 `quota` 换一家再调。

## 依赖

- `python3`（3.8+）
- 至少装一家 CLI：`claude` 或 `codex`
- 无需第三方 Python 包
