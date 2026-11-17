---
name: doc-review-comments
description: "像审代码一样逐段审文档：把意见锚定到原文某一段，逐条交给模型改并保留前后 diff，可以逐条接受或退回再改。用户要“审一下这份方案”“把这几条意见改进去”“看看这次改了什么”时使用。只改指定文档，不提交。"
metadata:
  requires:
    bins: ["python3"]
---

# doc-review-comments

把你的意见挂在文档的某一段原文上，再让模型逐条照改。每条意见都有自己的状态和改动说明，改之前自动留一份原文备份，改完能看 unified diff，最后逐条接受或退回再改。

## 使用场景

- 方案、设计、需求文档写完后逐段过一遍，把“这里没说重试几次”这类意见挂到对应段落
- 攒了若干条意见后一次性交给模型改，避免自己一句句找位置
- 改完先看 diff，再决定哪条收下、哪条退回重改
- 文档里有一张 `| 编号 | 问题 | 可选方向 |` 的勾选表，先列出来给人点选

## 用法

```bash
# 1. 在原文一段上挂意见：锚点必须是原文里真实存在的一段（存前 40 字）
python3 scripts/doc_review.py add \
  --dir ~/workbench/req-101 --doc 方案设计.md \
  --anchor "失败后直接返回错误码" \
  --text "这里没说失败重试几次" --section "3.2 接口约定"

# 2. 看当前有哪些意见（可按状态过滤）
python3 scripts/doc_review.py list --dir ~/workbench/req-101
python3 scripts/doc_review.py list --dir ~/workbench/req-101 --status open

# 3. 先看会发给模型的提示词，不动文件
python3 scripts/doc_review.py apply \
  --dir ~/workbench/req-101 --doc 方案设计.md --engine kimi --dry-run

# 4. 逐条交给模型改（默认只改还没改过的意见）
python3 scripts/doc_review.py apply \
  --dir ~/workbench/req-101 --doc 方案设计.md --engine kimi

# 5. 只重跑某一条（比如模型失败，或改完还想再改）
python3 scripts/doc_review.py apply \
  --dir ~/workbench/req-101 --doc 方案设计.md --id C-01 --engine kimi

# 6. 看这次改动和上一版备份的 diff
python3 scripts/doc_review.py diff --dir ~/workbench/req-101 --doc 方案设计.md
python3 scripts/doc_review.py diff --dir ~/workbench/req-101 --doc 方案设计.md \
  --backup 20261117-153012

# 7. 逐条决定
python3 scripts/doc_review.py accept --dir ~/workbench/req-101 --id C-01
python3 scripts/doc_review.py reject --dir ~/workbench/req-101 --id C-02 --reason "口径还没定"

# 8. 解析文档里的勾选表，把选项列出来供人点选
python3 scripts/doc_review.py select \
  --dir ~/workbench/req-101 --doc 方案设计.md --table "| 编号 | 问题 | 可选方向 |"
```

## 子命令

| 命令 | 作用 | 输出 |
|---|---|---|
| `add` | 在原文一段上挂一条意见；锚点找不到时报错并给最接近的 3 个候选 | 意见 JSON |
| `list` | 列出意见，可按 `open` / `resolved` 过滤 | 意见列表 JSON |
| `apply` | 逐条让模型按意见改文档，写回 `resolution`，输出前后 diff | 结果 JSON |
| `accept` | 这条意见已接受，状态改为 `resolved` | 意见 JSON |
| `reject` | 这条意见不采纳，原因写进 `resolution`，状态改为 `resolved` | 意见 JSON |
| `diff` | 当前文档对某个备份（默认最近一版）的 unified diff | diff 文本 |
| `select` | 按表头定位文档里的勾选表，拆出编号、问题、可选项 | 表格 JSON |

### apply 参数

| 参数 | 说明 |
|---|---|
| `--dir DIR` | 文档所在目录，数据写到 `DIR/.review/` |
| `--doc FILE` | 目录里的 md 文件名 |
| `--id C-01` | 只改这一条；已改过的也能用 `--id` 重跑 |
| `--engine` | `claude` / `codex` / `deepseek` / `kimi`，默认 `kimi` |
| `--timeout N` | 单次模型调用超时秒数，默认 1800 |
| `--dry-run` | 只打印提示词，不启动模型、不写备份、不动文件 |

不给 `--id` 时，只处理状态 `open` 且 `resolution` 为空（还没改过）的意见。模型失败的意见会保持 `open`，下次还会重试。

## 意见数据结构

`<dir>/.review/comments.json`：

```json
{
  "doc": "方案设计.md",
  "comments": [
    {
      "id": "C-01",
      "doc": "方案设计.md",
      "anchor": "失败后直接返回错误码，不重试",
      "section": "3.2 接口约定",
      "text": "这里没说失败重试几次",
      "status": "open",
      "resolution": "3.2 接口约定：补充失败后最多重试 3 次、间隔指数退避"
    }
  ]
}
```

- 顶层 `doc` 是最近一次操作的文档；每条意见自带 `doc`，所以一个目录里可以同时审多份文档。
- `anchor` 存原文片段前 40 字，`add` 时必须在原文里命中。
- `status` 只有 `open` / `resolved`：`apply` 后仍是 `open`（等你决定），`accept` / `reject` 才变 `resolved`。
- `resolution` 是模型对这条的说明（改了哪一段、怎么改的）；`reject` 的原因也追加在这里。
- 模型调用失败时该条多一个 `error` 字段，状态保持 `open`，不影响其他条。

## 输出 JSON 结构

`add` / `accept` / `reject` 输出单条意见。`list`：

```json
{
  "ok": true,
  "dir": "~/workbench/req-101",
  "doc": "方案设计.md",
  "count": 2,
  "counts": {"open": 1, "resolved": 1},
  "comments": []
}
```

`apply`：

```json
{
  "ok": true,
  "doc": "方案设计.md",
  "engine": "kimi",
  "dry_run": false,
  "backup": "/path/to/.review/backup/20261117-153012.md",
  "applied_count": 1,
  "failed_count": 0,
  "applied": [
    {"id": "C-01", "ok": true, "changed": true,
     "resolution": "3.2 接口约定：补充失败后最多重试 3 次",
     "diff": "--- 改之前\n+++ 改之后\n...", "warnings": [], "error": null}
  ],
  "diff": "--- 改之前\n+++ 改之后\n..."
}
```

`--dry-run` 时 `applied` 为空，改由 `calls` 给出 `id` / `profile` / `prompt_chars` / `prompt`。

`select`：

```json
{
  "ok": true,
  "doc": "方案设计.md",
  "header": ["编号", "问题", "可选方向"],
  "count": 1,
  "rows": [
    {"id": "Q1", "question": "重试次数怎么定",
     "options": ["3 次", "5 次", "不重试"]}
  ]
}
```

`diff` 只打印 unified diff 文本；没有变化时打印“（没有变化）”。找不到备份或锚点时报错，错误 JSON 打到 stderr 并以退出码 2 结束。

## 注意

- 只改 `--doc` 指定的这一个文件；提示词里写死“其他段落一个字都不要动”。
- 目标段按“锚点所在段落（空行分隔）”计算；一段之外改动超过 5 行时，`applied[].warnings` 里会给提示，方便你确认模型是不是改多了。
- 模型返回必须带 `{"how": "改了哪一段、怎么改的"}`。模型改了文件却没给说明时，这一条会回滚到改前内容并记为失败，避免留下来路不明的改动。
- `apply` 一次运行开始时把原文备份到 `.review/backup/<时间戳>.md`；同一秒内多次运行会加 `-2`、`-3` 后缀。
- 模型超时、限流、没装 CLI：该条保持 `open` 并在 `error` 里写明原因，其他条继续跑；整体 `ok=false`、退出码 1。
- `select` 只解析文档里的表并列出选项，不回写“你的选择”列、不写决定日志——这部分交互由调用方自己处理。
- 已砍掉原内部工作台的 workflow 状态、常驻 server、看板上的勾选交互和通知；这里只有命令行和数据文件。
- 密钥一律走 `wbkit` 配置（环境变量或 `secrets.json`），数据文件里不存 key。

## 依赖

- `python3`（3.8+）
- 至少一个可用引擎：本机已登录的 `claude` / `codex`，或配置了 key 的 `deepseek` / `kimi`
- 无需第三方 Python 包
