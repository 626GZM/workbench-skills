---
name: req-stage-workflow
description: "给一个需求或任意任务建本地阶段看板：查当前阶段和进度、按顺序推进、跳过阶段、登记需求变更、看时间线、导出 markdown。状态只存目标目录的 workflow.json，不连任何外部系统。当用户问“这个需求到哪一步了”“帮我建个阶段看板”“把它推进到下一阶段”时使用。"
metadata:
  requires:
    bins: ["python3"]
---

# req-stage-workflow

一个本地、可追踪的阶段看板。所有状态写在一个目录下的 `workflow.json`，不依赖工作流引擎、数据库或通知服务，目录拷走就能继续用。

## 使用场景

- 需求刚立项，先把阶段拆出来，后面每次开会都问“到哪一步了”
- 每天推进一个阶段，顺手留一句说明，事后能回看时间线
- 某个阶段这次不做（比如不需要单独联调），跳过并写清原因
- 需求中途加了一条变更，登记下来别丢
- 周会前导出一份 markdown 看板，直接贴到文档里

## 数据文件

`workflow.json` 放在 `--dir` 指定的目录里：

```json
{
  "title": "【REQ-101】订单导出优化",
  "stages": {
    "需求分析": "已确认",
    "方案设计": "进行中",
    "开发": "未开始",
    "自测": "未开始",
    "联调": "未开始",
    "测试": "未开始",
    "上线": "未开始"
  },
  "history": [
    {"at": "2026-11-17T10:00:00+08:00", "action": "stage", "stage": "方案设计",
     "from": "未开始", "to": "进行中", "note": ""}
  ],
  "changes": [
    {"id": "CH-01", "title": "新增导出模板", "status": "待分析", "at": "2026-11-17T10:05:00+08:00"}
  ]
}
```

`stages` 的键顺序就是阶段顺序。状态只有四种：`未开始` / `进行中` / `已确认` / `已跳过`。

## 用法

```bash
# 1. 建看板（目录不存在会自动建）
python3 scripts/stage_workflow.py init --dir ~/workbench/req-101 \
  --title "【REQ-101】订单导出优化"

# 2. 自定义阶段顺序
python3 scripts/stage_workflow.py init --dir ~/workbench/task-x \
  --title "导出接口重构" --stages "分析,实现,验证,上线"

# 3. 查当前阶段、进度、下一步
python3 scripts/stage_workflow.py status --dir ~/workbench/req-101

# 4. 推进当前阶段：未开始 → 进行中，进行中 → 已确认
python3 scripts/stage_workflow.py advance --dir ~/workbench/req-101
python3 scripts/stage_workflow.py advance --dir ~/workbench/req-101 \
  --stage 方案设计 --to 已确认 --note "方案评审通过"

# 5. 跳级（明确知道要越过前面的阶段）
python3 scripts/stage_workflow.py advance --dir ~/workbench/req-101 \
  --stage 测试 --to 进行中 --force

# 6. 跳过当前阶段，必须写原因
python3 scripts/stage_workflow.py skip --dir ~/workbench/req-101 \
  --stage 联调 --reason "本期不涉及前后端联调"

# 7. 需求变更：登记 / 处理完成
python3 scripts/stage_workflow.py change add --dir ~/workbench/req-101 \
  --title "新增导出模板"
python3 scripts/stage_workflow.py change done --dir ~/workbench/req-101 \
  --id CH-01 --summary "模板配置页已上线"

# 8. 时间线（默认最近 50 条，按时间从早到晚）
python3 scripts/stage_workflow.py history --dir ~/workbench/req-101 --limit 20

# 9. 导出 markdown 看板
python3 scripts/stage_workflow.py export --dir ~/workbench/req-101 \
  --out ~/workbench/req-101/board.md
```

## 子命令

| 命令 | 说明 |
|---|---|
| `init` | 建 `workflow.json`；`--stages` 用逗号分隔，缺省取配置 `stages` 或内置七阶段 |
| `status` | 当前阶段、每个阶段状态、进度百分比、下一步命令、未处理变更数 |
| `advance` | 推进一格；`--stage` 缺省取当前阶段，`--to` 缺省按当前状态往下一格；`--force` 允许跳级 |
| `skip` | 跳过当前阶段，`--reason` 必填 |
| `change add` | 登记一条变更，编号从 `CH-01` 开始自增 |
| `change done` | 把变更标成 `已处理`，可带 `--summary` |
| `history` | 时间线，`--limit` 取最近 N 条（默认 50） |
| `export` | 导出 markdown 看板到 `--out` |

## 阶段规则

- 状态枚举固定为 `未开始` / `进行中` / `已确认` / `已跳过`。
- **只按顺序走一格**：默认推进“第一个还没结束的阶段”。`advance` 指定了别的阶段会被拒绝，除非加 `--force`。
- **不能带着前面的阶段确认**：把某阶段改成 `已确认` 时，它前面的阶段必须都是 `已确认` 或 `已跳过`，否则拒绝；确认要越过就加 `--force`。
- 确认一个阶段后，自动把它后面第一个 `未开始` 的阶段置为 `进行中`，这样 `status` 总能给出当前阶段。
- `skip` 只能跳当前阶段，并且必须写原因。
- 进度百分比：`已确认` / `已跳过` 各算 1，`进行中` 算 0.5，`未开始` 算 0，按阶段数取整。
- 每次写 `workflow.json` 都是先写同目录临时文件再 `os.replace`，写一半中断不会留下坏文件。

## 输出 JSON

`status` 与所有会改变状态的命令都会输出同一套看板信息：

```json
{
  "file": "/absolute/path/workflow.json",
  "title": "【REQ-101】订单导出优化",
  "current": "方案设计",
  "progress": 21,
  "counts": {"total": 7, "done": 1, "skipped": 0, "in_progress": 1, "pending": 5},
  "stages": [{"name": "需求分析", "status": "已确认"}, {"name": "方案设计", "status": "进行中"}],
  "next": {"stage": "方案设计", "action": "确认完成", "command": "python3 scripts/stage_workflow.py advance ..."},
  "changes": {"total": 1, "open": 1}
}
```

- `advance` / `skip` 额外带 `stage` / `from` / `to` / `started` / `history_entry`
- `change add` / `change done` 返回 `change_id` 和 `change`
- `history` 返回 `total` / `count` / `history[]`
- `export` 返回 `out` / `bytes` / `markdown`
- 参数错、目录里没有看板、JSON 坏了、状态非法：stderr 输出 `{"error": "..."}`，退出码 `2`

## 配置

在 wbkit 的配置文件里加一项即可改默认阶段（优先级：命令行 `--stages` > 配置 `stages` > 内置七阶段）：

```json
{"stages": ["需求分析", "方案设计", "开发", "自测", "联调", "测试", "上线"]}
```

配置文件查找顺序沿用 `wbkit.config`：`WB_CONFIG` 环境变量 > 当前目录 `.wbkit.json` > `~/.config/workbench-skills/config.json`。

## 注意

相对原来的工作台实现，这里刻意砍掉了这些：

- 没有 orchestrator、server、AI 调度、通知推送和评审弹窗，只读写本地 JSON
- 阶段状态简化为纯字符串，不再记录审批人、各仓库 HEAD、状态快照和漂移检测
- 不检查代码、不跑流水线；`advance` 只看阶段顺序，内容是否真的做完由使用者自己判断
- 不做历史归档，`history` 就存在 `workflow.json` 里；条目很多时自行用 `--limit` 查看

## 依赖

- `python3`（3.8+），只用标准库
- 无需 git、网络或任何第三方包
