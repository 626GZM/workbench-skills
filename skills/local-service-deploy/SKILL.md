---
name: local-service-deploy
description: "按一份 services.json 构建、启动、停止本地服务：查进程和端口、跑健康检查、看日志、重启。当用户要“把本地服务起起来”“改完代码重启某个服务”“看看服务为什么没起来”“盯一下日志”时使用。配置驱动，不绑定任何具体公司的服务。"
metadata:
  requires:
    bins: ["python3"]
---

# local-service-deploy

把「构建并起停本地服务、看日志、健康检查」做成通用工具。所有命令输出 JSON，方便 AI 判断下一步；`logs -f` 例外，跟 `tail -f` 一样流式输出。

服务怎么构建、怎么启动、监听哪个端口、健康检查地址、日志写到哪里，全部来自 `services.json`，脚本里没有任何写死的服务名或构建命令。

## 使用场景

- 本地要起一套服务：先 `build`，再 `start --wait 120`，等健康检查通过
- 改完代码只重启一个服务：`restart --service payment-service --skip-build`
- 服务起不来：`status` 看进程、端口、健康检查和最近 20 行日志，`logs -n 200` 看全量
- 想知道现在哪些服务活着、端口有没有在听：`list`
- 停止本地环境：`stop`，按 pid 文件先 TERM 后 KILL

## 用法

```bash
# 列出所有服务、进程状态、端口是否在听
python3 scripts/service_deploy.py list --config ./services.json

# 构建（跑配置里的 build，返回耗时和退出码；失败带 stderr 末尾）
python3 scripts/service_deploy.py build --service order-service --config ./services.json

# 后台启动，轮询 health 直到通或超时（默认取配置 wait，60 秒）
python3 scripts/service_deploy.py start --service order-service --config ./services.json --wait 90

# 停止（先 TERM，stop_timeout 秒后还在就 KILL）
python3 scripts/service_deploy.py stop --service order-service --config ./services.json

# 重启：stop + build + start；--skip-build 只重启不构建
python3 scripts/service_deploy.py restart --service order-service --config ./services.json --skip-build

# 进程、端口、health、最近 20 行日志
python3 scripts/service_deploy.py status --service order-service --config ./services.json

# 日志末尾 200 行；-f 持续跟随（不是 JSON）
python3 scripts/service_deploy.py logs --service order-service --config ./services.json -n 200
python3 scripts/service_deploy.py logs --service order-service --config ./services.json -f
```

`--config` 不传时依次找 `./services.json`、`~/.config/workbench-skills/services.json`。

## 配置文件

`services.json` 完整示例：

```json
{
  "state_dir": "~/.local/state/local-service-deploy",
  "services": {
    "order-service": {
      "dir": "~/repos/order-service",
      "build": "mvn -q -DskipTests package",
      "start": "java -jar target/app.jar --server.port=8081",
      "port": 8081,
      "health": "http://localhost:8081/actuator/health",
      "log": "/tmp/order-service.log",
      "env": {"SPRING_PROFILES_ACTIVE": "local"},
      "cwd": "~/repos/order-service",
      "shell": "/bin/zsh"
    },
    "payment-service": {
      "dir": "~/repos/payment-service",
      "build": "go build -o bin/payment ./cmd/payment",
      "start": "./bin/payment --port 8082",
      "port": 8082,
      "health": "http://127.0.0.1:8082/healthz",
      "wait": 30
    }
  }
}
```

### 字段

顶层：

| 字段 | 必填 | 说明 |
|---|---|---|
| `services` | 是 | 服务表：对象形式（服务名 → 配置）或数组形式（每项带 `name`） |
| `state_dir` | 否 | pid 文件和默认日志的根目录，默认 `~/.local/state/local-service-deploy` |

每个服务：

| 字段 | 必填 | 说明 |
|---|---|---|
| 服务名（对象键 / `name`） | 是 | 字母数字开头，可含 `._-`，最长 64；也用作 pid 和日志文件名 |
| `dir` | 是 | 服务代码目录，必须存在 |
| `start` | 是 | 启动命令，交给 shell 执行 |
| `build` | 否 | 构建命令；不填时 `build` 报错、`restart` 自动跳过构建 |
| `port` | 否 | 监听端口；用于启动前查占用、`list` / `status` 查是否在听 |
| `health` | 否 | 健康检查 URL，2xx 算通；不填则启动后只看进程是否还活着 |
| `log` | 否 | 日志路径，默认 `<state_dir>/logs/<服务名>.log` |
| `env` | 否 | 追加/覆盖的环境变量对象，只影响该服务的 build 和 start |
| `cwd` | 否 | 构建和启动的工作目录，默认取 `dir` |
| `shell` | 否 | 执行命令用的 shell，默认系统 `/bin/sh`；命令里有 `&&`、重定向时可指定 bash/zsh |
| `wait` | 否 | health 轮询超时秒数，默认 60；命令行 `--wait` 优先 |
| `build_timeout` | 否 | 构建超时秒数，默认 900 |
| `stop_timeout` | 否 | TERM 之后等多少秒再 KILL，默认 10 |

相对路径（`dir` / `log` / `cwd`）按配置文件所在目录解析，`~` 会展开。

## 输出结构

`list`：

```json
{
  "config": "/abs/path/services.json",
  "state_dir": "/abs/path/state",
  "services": [
    {
      "name": "order-service", "dir": "/abs/repos/order-service", "port": 8081,
      "port_listening": true,
      "process": {"pid": 12345, "alive": true, "uptime_seconds": 320, "stale": false},
      "health": "http://localhost:8081/actuator/health",
      "log": "/tmp/order-service.log"
    }
  ]
}
```

`build`：`{service, ok, exit_code, timed_out, seconds, cmd, cwd, log, stderr_tail}`；失败时 `exit_code` 非 0、`stderr_tail` 是末尾若干行。

`start`：`{service, ok, status, pid, port, healthy, port_listening, waited_seconds, health, log, pid_file}`；已经在跑时返回 `already_running: true` 和原 pid；超时或进程秒退时 `ok: false`，带 `error` 和 `log_tail`，并清理 pid 文件。

`stop`：`{service, pid, stopped, forced, seconds, port_listening}`；pid 记录失效（进程不在或 pid 被复用）时 `stale: true`，只删 pid 文件、不动进程。

`restart`：`{service, stop, build, start, ok}`，`build` 为 `{"skipped": true}` 表示没构建。

`status`：`{service, status, process, port, port_listening, health, log, log_tail}`，`status` 取 `running` / `stopped` / `stale`，`log_tail` 是最近 20 行。

`logs`：`{service, path, exists, bytes, lines, text}`。

出错（配置不合法、找不到服务、端口被占）时，向 stderr 输出 `{"error": "..."}`，退出码 2。

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 成功（`stop` 时本来就没在跑也算成功） |
| 1 | 操作失败：构建非 0、启动超时、进程秒退 |
| 2 | 配置或用法错误 |

## 注意

- 砍掉了内部工作台的私有逻辑：环境变量注入、内网 Maven 私服、部署脚本路径、workflow 依赖。需要额外环境变量就用每条服务的 `env`；需要换目录用 `cwd`。
- pid 文件放在 `<state_dir>/pids/<服务名>.pid`，内容是 JSON，含服务名、pid、启动时间（`started_at` 和 `ps` 的 `lstart`）和启动命令。停止前会校验这三样，`lstart` 对不上就判定 pid 被复用，**只删记录不杀进程**，避免误杀无关程序。
- 启动前只检查端口是否被占用，占用就报错退出，不会去停别的进程。
- 启动用新会话（`start_new_session`），停止时优先杀整个进程组，避免 shell 子进程残留。
- 构建输出会追加到服务日志，`status` / `logs` 能看到构建失败原因。
- 健康检查不走系统代理，只按 2xx 判定。
- 只操作 `services.json` 里登记过的服务；不扫系统进程。

## 依赖

- `python3`（3.8+）
- POSIX 的 `ps`、`kill`（macOS / Linux 自带）
- 无需第三方包
