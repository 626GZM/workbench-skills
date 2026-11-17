---
name: java-layer-review
description: "扫描本次改动新增的 Java 代码，找出框架已经做了、代码里又写一遍的防御性冗余、分层越界和常见坏味道。只报 diff 新增行，命中给证据行；规则可开关、严重级别可调，输出 JSON。"
metadata:
  requires:
    bins: ["python3", "git"]
---

# java-layer-review

针对 Java 后端的改动检查工具。核心问题是“框架已经替你做了，你又写了一遍”：拦截器已经挡住未登录、全局异常处理已经兜住异常、请求体已经 `@Validated` 校验、时间由数据库默认值维护，代码里却再写一次。顺带检查 controller/service/dao 的分层越界和 N+1、拼接 SQL 等坏味道。

只读 git 差异，**只报本次新增行**，不改代码。命中不等于一定错，`why` 说明框架已经做了什么，`fix` 给出改法；确实需要保留就在代码旁写清原因。

## 使用场景

- 提 MR 前自查，先看自己新写的代码有没有重复防御
- code review 时快速定位分层问题和坏味道，拿到准确行号
- 团队统一分层习惯：关掉不认同的规则，或把某条降为提示
- 接手别人分支时，只看这个分支新增的部分

## 用法

```bash
# 工作区未提交的改动（含未跟踪的新 Java 文件），最常用
python3 scripts/layer_review.py review --repo ~/repos/project-a --uncommitted

# 和主分支比：只查 base..head 之间新增的行
python3 scripts/layer_review.py review --repo ~/repos/project-a --base origin/main --head HEAD

# 带团队规则配置，并把结果存成文件
python3 scripts/layer_review.py review --repo ~/repos/project-a \
    --base origin/main --rules ~/.java-layer-review.json --out /tmp/layer.json

# 看内置规则
python3 scripts/layer_review.py rules list
python3 scripts/layer_review.py rules explain controller-direct-dao

# 导出默认配置，按团队习惯改
python3 scripts/layer_review.py rules init --out ~/.java-layer-review.json
```

## 子命令

| 命令 | 说明 | 输出 |
|---|---|---|
| `review` | 检查 diff 新增行；`--uncommitted` 时看工作区，否则看 `--base..--head` | 命中数组 |
| `rules list` | 内置规则清单 | 规则数组 |
| `rules explain <id>` | 单条规则的说明和正则 | 单个规则对象 |
| `rules init --out FILE` | 导出默认配置（可选 `--force` 覆盖） | `{out, rules}` |

`review` 参数：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--repo` | 必填 | 仓库目录 |
| `--base` | `origin/main` | 基准分支，取 `base..head` 的新增行 |
| `--head` | `HEAD` | 目标提交 |
| `--uncommitted` | 关 | 看工作区未提交改动，未跟踪的新 `.java` 整份算新增 |
| `--rules` | 无 | 规则配置文件；不传用内置默认 |
| `--out` | 无 | 把结果另存为 JSON，文件内容与 stdout 一致 |

退出码：`0` 表示检查跑完（有命中也是 0），`2` 表示参数或 git 出错。

## 输出 JSON 结构

`review` 输出数组，按文件、行号排序：

```json
[
  {
    "rule": "controller-direct-dao",
    "severity": "high",
    "file": "src/main/java/com/example/order/controller/OrderController.java",
    "line": 23,
    "snippet": "orderMapper.insert(item);",
    "why": "controller 直连 mapper/dao 跳过了 service 的事务和业务规则，分层被穿透。",
    "fix": "改为调用 service 方法。"
  }
]
```

字段：`rule` 规则 id、`severity` 生效的严重级别、`file` 相对路径、`line` 新文件行号、`snippet` 命中行（截断 200 字）、`why` 为什么框架已经处理、`fix` 建议改法。

`rules list` / `rules explain` 的字段：`id` / `title` / `severity` / `enabled` / `why` / `fix`，`explain` 多一个 `patterns`（正则源码，便于判断误报）。

## 内置规则

| id | 说明 | 默认 severity | 生效层 |
|---|---|---|---|
| `entity-null-check` | 实体类里重复判空 | low | entity/PO/DO |
| `login-user-null-check` | 已登录接口重复判登录用户为空 | medium | 全部 |
| `controller-try-catch` | controller 里重复 try-catch 兜底 | medium | controller |
| `manual-field-validation` | 请求体已 `@Validated` 仍手写逐字段校验 | medium | controller |
| `manual-timestamp` | 手写 `new Date()` / `setCreateTime()` | low | 全部 |
| `controller-business-logic` | controller 里写循环、多分支、事务 | medium | controller |
| `controller-direct-dao` | controller 直接调 mapper/dao | high | controller |
| `service-returns-entity` | service 层直接返回 Entity/PO | medium | service |
| `swallowed-exception-in-tx` | 事务方法里 catch 异常不抛 | high | service |
| `n-plus-one-query` | 循环里查数据库（N+1） | high | 全部 |
| `magic-string-compare` | 魔法值字符串直接比较 | low | 全部 |
| `sql-concat` | 字符串拼接 SQL | high | 全部 |

分层判断：类名后缀（`Controller` / `Service` / `Mapper` / `Entity` 等）优先，其次看目录（`/controller/`、`/service/`、`/mapper/`、`/entity/`）。`/src/test/` 下的代码整份跳过。

## 配置

`rules init` 导出的文件是 JSON，用户只写要改的项，没写的保持默认：

```json
{
  "version": 1,
  "rules": {
    "controller-direct-dao": { "enabled": false },
    "magic-string-compare": { "severity": "high" },
    "n-plus-one-query": { "enabled": true, "severity": "medium" }
  }
}
```

- `enabled: false` 关掉某条规则；也可以直接写 `"规则id": false`
- `severity` 只能是 `high` / `medium` / `low`
- 未知规则 id 会报错，先用 `rules list` 查
- 团队习惯不同就改这份文件，不用改脚本

## 注意

- **只报不改**：不修改工作区、不提交、不推送；`--out` 只写检查结果文件。
- **只报新增行**：老代码里的同类写法不会报，避免一上来几百条。
- **命中不等于错**：例如确实要查询历史数据的表、必须自己控制回滚的补偿逻辑，可能就是要这么写，在代码旁注明原因即可。
- 事务吞异常和 N+1 依赖行上下文（diff 前后约 20 行、向上找方法声明），方法很长或跨文件调用时可能漏报；这是提示工具，不替代 review。
- 正则按常见写法匹配，变量名差异大时可能漏报；觉得某条误报多，先在配置里关掉。
- 已砍掉：内部个人样式文档路径、外部 workflow 依赖、自动改代码。

## 依赖

- `python3`（3.8+）
- `git`
- 只用标准库，无需第三方包
