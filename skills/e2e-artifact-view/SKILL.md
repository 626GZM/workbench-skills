---
name: e2e-artifact-view
description: "只读查看 e2e 运行产物：列出历史运行、看一次运行的步骤与失败原因、按状态筛用例、汇总通过率趋势和失败最多的用例、脱敏查看测试账号、给日志和报告打码。当用户问“昨天 e2e 跑得怎么样”“这条用例为什么挂了”“最近失败集中在哪”，或要把测试结果整理成人能看的视图时使用。不跑测试。"
metadata:
  requires:
    bins: ["python3"]
---

# e2e-artifact-view

把 e2e 运行产物（`run.json` + 截图/日志）整理成 JSON 视图：运行列表、单次详情、用例明细、聚合统计、账号脱敏、文本打码。

全程只读：不跑测试、不连内部系统、不登录、不改产物文件。

## 使用场景

- 一晚上的定时回归跑了一堆，想看哪几次挂了、挂了多少条
- 某条用例失败，想知道报错原文和对应截图/日志在哪
- 只要失败的用例明细，转给开发去修
- 出一份周报：通过率趋势、最常挂的用例、失败原因分布
- 把测试账号配置发给别人前，先确认密码都打码了
- 产物里的日志带手机号、邮箱、token，要贴出来之前先打码

## 用法

```bash
# 最近 20 次运行（时间、环境、状态、通过/失败数、耗时），新的在前
python3 scripts/e2e_view.py runs --root ~/e2e-artifacts

# 只看 dev 环境，最多 5 条
python3 scripts/e2e_view.py runs --root ~/e2e-artifacts --env dev --limit 5

# 一次运行的详情：步骤、每个用例的状态、失败原因、证据路径
python3 scripts/e2e_view.py show --run ~/e2e-artifacts/2026-11-17-101500-local

# 用例明细；只要失败的
python3 scripts/e2e_view.py cases --run ~/e2e-artifacts/2026-11-17-101500-local --status failed

# 近 7 天聚合：通过率趋势、失败最多的 5 条用例、失败原因归类
python3 scripts/e2e_view.py summary --root ~/e2e-artifacts --since 7d --top 5

# 测试账号配置的脱敏视图（密码显示为 ***）
python3 scripts/e2e_view.py accounts --config ~/e2e-artifacts/accounts.json

# 给一个文件打码后输出（手机号/身份证/邮箱/token）
python3 scripts/e2e_view.py mask --file ~/e2e-artifacts/2026-11-17-101500-local/logs/api.log
```

## 子命令

| 命令 | 参数 | 输出 |
|---|---|---|
| `runs` | `--root DIR`，`[--env local\|dev\|test]`，`[--limit 20]` | 运行记录数组：`id` / `env` / `status` / `started_at` / `duration_seconds` / `counts` / `pass_rate` |
| `show` | `--run RUN_DIR` | `run`（同上一行）+ `steps[]`：每步 `name` / `status` / `counts` / `cases[]` |
| `cases` | `--run RUN_DIR`，`[--status failed]` | 扁平用例数组：`step` / `id` / `title` / `status` / `error` / `evidence` / `missing_evidence` |
| `summary` | `--root DIR`，`[--since 7d]`，`[--top 5]` | 汇总 `counts` / `pass_rate` / `trend[]` / `top_failed_cases[]` / `failure_categories[]` |
| `accounts` | `--config FILE` | 账号数组：`code` / `name` / `purpose` / `account` / `envs` / `password`（固定 `***`） |
| `mask` | `--file FILE` | `changed` / `counts`（各类打码数量）/ `text`（打码后的全文） |

- `runs` 默认最新在前；`--limit 0` 表示全部。
- `cases --status` 可选 `passed` / `failed` / `skipped` / `unknown`，不传返回全部。
- `summary --since` 支持 `30m` / `24h` / `7d` / `2w`，也接受 `2026-11-01` 这样的日期。
- 出错时往 stderr 打 `{"error": "..."}`，退出码 2。

## 产物目录结构

一个根目录下每个子目录是一次运行：

```
root/
  2026-11-17-101500-local/
    run.json
    snaps/a.png
    logs/step-1.log
  2026-11-17-210000-dev/
    run.json
```

`run.json`：

```json
{
  "env": "local",
  "started_at": "2026-11-17 10:15:00",
  "finished_at": "2026-11-17 10:16:02",
  "status": "failed",
  "steps": [
    {
      "name": "登录",
      "status": "passed",
      "cases": [
        {"id": "case-01", "title": "正常账号能登录", "status": "passed",
         "error": "", "evidence": ["snaps/login-ok.png"]}
      ]
    },
    {
      "name": "下单",
      "status": "failed",
      "cases": [
        {"id": "case-02", "title": "库存不足时不能下单", "status": "failed",
         "error": "expected 400, got 200",
         "evidence": ["snaps/order-fail.png", "logs/step-2.log"]}
      ]
    }
  ]
}
```

约定：

- 运行目录名建议 `YYYY-MM-DD-HHMMSS-<env>`（也认 `YYYYMMDD-HHMMSS`），没有 `started_at` 时用它推断时间；再没有就用目录 mtime。
- `status` 认 `passed / failed / skipped`（也认 `pass / ok / success / fail / error / skip` 和中文「通过 / 失败 / 跳过」），其余值按 `unknown` 处理并按用例结果推导。
- `steps` 可以省略；顶层直接给 `cases` 时当成一个名为「全部」的步骤。
- `evidence` 里的相对路径相对运行目录；文件不存在时进 `missing_evidence`。
- `duration_seconds` 没给时用 `finished_at - started_at` 算。

## 输出 JSON 结构

`runs`：

```json
{
  "root": "/abs/root",
  "count": 2,
  "runs": [
    {
      "id": "2026-11-17-101500-local",
      "path": "/abs/root/2026-11-17-101500-local",
      "env": "local",
      "status": "failed",
      "started_at": "2026-11-17 10:15:00",
      "finished_at": "2026-11-17 10:16:02",
      "time": 1763345700,
      "duration_seconds": 62,
      "counts": {"total": 2, "passed": 1, "failed": 1, "skipped": 0, "unknown": 0},
      "pass_rate": 0.5
    }
  ]
}
```

`show`：`{"run": {…}, "steps": [{"name", "status", "counts", "cases": [{"id", "title", "status", "error", "evidence", "missing_evidence"}]}]}`。

`summary`：

```json
{
  "root": "/abs/root",
  "since": "7d",
  "cutoff": "2026-11-10 10:20:00",
  "runs": 3,
  "counts": {"total": 9, "passed": 6, "failed": 3, "skipped": 0, "unknown": 0},
  "pass_rate": 0.6667,
  "trend": [
    {"date": "2026-11-16", "runs": 1, "total": 3, "passed": 2, "failed": 1, "pass_rate": 0.6667}
  ],
  "top_failed_cases": [
    {"id": "case-02", "title": "库存不足时不能下单", "step": "下单", "failed": 2,
     "last_seen": "2026-11-17 10:15:00"}
  ],
  "failure_categories": [
    {"category": "超时", "count": 1, "ratio": 0.3333, "samples": ["等待元素超时：下单按钮"]}
  ]
}
```

- `pass_rate` = `passed / (passed + failed)`；一条都没执行（全跳过）时为 `null`。
- `trend` 按运行开始日期分组，日期升序。
- `top_failed_cases` 按失败次数降序，次数相同看最近一次时间；条数由 `--top` 控制。
- `failure_categories` 固定输出 `超时 / 断言失败 / 环境错误 / 数据问题 / 未知` 五项。

## 失败归类口径

按 `error` 里的关键词匹配，先命中先算：

| 归类 | 关键词（不区分大小写） |
|---|---|
| `超时` | `timeout`、`timed out`、`超时`、`deadline`、`ETIMEDOUT` |
| `断言失败` | `assert`、`expected`、`期望`、`断言`、`mismatch` |
| `环境错误` | `connect`、`refused`、`ECONN`、`network`、`网络`、`DNS`、`unavailable`、`502/503/504`、`upstream` |
| `数据问题` | `数据`、`不存在`、`not found`、`404`、`duplicate`、`重复`、`invalid` |
| `未知` | 以上都没命中，或 `error` 为空 |

## 账号配置

`accounts --config FILE` 读的是这样一份 JSON（结构与内部工作台的私密文件对齐，但**不会**发起登录）：

```json
{
  "identities": {
    "admin": {
      "name": "管理端账号",
      "purpose": "测管理端权限",
      "login": {"account": "admin01", "password": "******"}
    }
  },
  "environments": {
    "dev": {
      "identities": {
        "guest": {"name": "访客账号", "purpose": "只读校验", "token": "Bearer ******"}
      }
    }
  }
}
```

- 顶层 `identities` 记为共用（`envs: ["共用"]`）；`environments.<环境>.identities` 记为该环境。
- 输出只保留代号、名称、用途、用户名、所在环境；有密码时 `password` 固定输出 `***`，token 一律不输出。
- `purpose` 依次取 `purpose / role / why`，都没有就留空。

## 注意

从内部工作台拆分时，这些能力**没有搬**过来：

- **真实登录 / 鉴权流程**：不读私密文件、不调登录接口、不换 token；账号配置只做结构与脱敏展示。
- **内部接口调用**：没有接口地址、环境清单、看板服务，所有数据只来自你给的产物目录。
- **workflow / server 依赖**：不读 `workflow.json`、不连服务端，运行目录就是唯一事实来源。
- **用例概要与计划绑定**：砍掉，只按 `run.json` 里实际写下的步骤和用例展示。
- 不做历史清理、不重跑、不改产物；`mask` 只输出到 stdout，不覆盖原文件。
- 打码是尽力而为：手机号、18 位身份证、邮箱、`Bearer` 与常见 `token/secret/password/api_key` 赋值会被处理，其它自定义格式需要自己再检查。

## 依赖

- `python3`（3.8+），只用标准库
- 不需要 git、不需要网络、不需要任何第三方包
