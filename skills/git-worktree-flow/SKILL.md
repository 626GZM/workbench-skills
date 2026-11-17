---
name: git-worktree-flow
description: "给一个 git 仓库拉一套并行开发工作区：建 worktree、列出各目录的分支与脏状态、安全删除、清理失效记录、把基线合进某个工作区分支。当用户要在同一个仓库上同时开发多个分支、给 AI 或另一个任务单独开一份代码目录时使用。"
metadata:
  requires:
    bins: ["python3", "git"]
---

# git-worktree-flow

基于 `git worktree` 的并行开发目录管理。所有子命令输出 JSON，方便调用方直接读字段做下一步判断。

## 使用场景

- 同一个仓库要同时开发多个分支，又不想来回切换、不想互相踩到未提交改动
- 给另一个任务（另一个人或一个 AI）单独开一份代码目录，各自检出不同分支
- 收工后确认哪些目录还在、分支对不对、有没有没提交的改动
- 某个工作区分支落后了，把最新基线合进去；有冲突先拿到冲突文件清单，不冲动改工作区

## 用法

```bash
# 建工作区：分支不存在就从 origin/main 拉出来
python3 scripts/worktree_flow.py create \
  --repo ~/repos/project-a \
  --branch feature/x \
  --path ~/repos/project-a-wt/x \
  --base origin/main

# 分支已存在：直接检出到新目录
python3 scripts/worktree_flow.py create \
  --repo ~/repos/project-a --branch feature/y --path ~/repos/project-a-wt/y

# 只要一份临时目录、不占用分支
python3 scripts/worktree_flow.py create \
  --repo ~/repos/project-a --branch spike --path /tmp/spike --detach

# 看这个仓库所有工作区：路径、分支、HEAD、脏文件数
python3 scripts/worktree_flow.py list --repo ~/repos/project-a

# 删除工作区；有未提交改动会拒绝
python3 scripts/worktree_flow.py remove --repo ~/repos/project-a --path ~/repos/project-a-wt/x

# 确认丢弃改动再删
python3 scripts/worktree_flow.py remove --repo ~/repos/project-a --path ~/repos/project-a-wt/x --force

# 目录已被手工删掉，清理残留记录
python3 scripts/worktree_flow.py prune --repo ~/repos/project-a

# 把基线合进某个工作区的分支
python3 scripts/worktree_flow.py sync \
  --repo ~/repos/project-a \
  --path ~/repos/project-a-wt/x \
  --base origin/main
```

## 子命令

| 命令 | 必填参数 | 输出 |
|---|---|---|
| `create` | `--repo` `--branch` `--path` | `path` / `branch` / `detached` / `base` / `head` |
| `list` | `--repo` | `count` / `worktrees[]` |
| `remove` | `--repo` `--path` | `removed` / `branch` / `forced` |
| `prune` | `--repo` | `pruned[]` / `count` / `worktrees[]` |
| `sync` | `--repo` `--path` `--base` | `status` / `branch` / `base` / `before` / `after` / `fast_forward` / `files[]` |

`create` 的可选参数：`--base`（新分支起点，缺省当前 HEAD）、`--detach`（分离 HEAD）。
`remove` 的可选参数：`--force`（丢弃未提交改动强制删除）。

## 输出 JSON 结构

`list` 的每条 `worktrees[]`：

```json
{
  "path": "/home/me/repos/project-a-wt/x",
  "head": "3f2a1c9...",
  "branch": "feature/x",
  "detached": false,
  "bare": false,
  "locked": null,
  "prunable": null,
  "exists": true,
  "tracked": 0,
  "untracked": 0,
  "dirty": false
}
```

`sync` 的 `status`：

| 值 | 含义 |
|---|---|
| `up-to-date` | 基线已包含在当前分支里，什么都没做 |
| `merged` | 已合入；`fast_forward` 为 `true` 表示是快进 |
| `conflict` | 预演发现冲突，`files` 是冲突文件；工作区、HEAD 均未改动 |
| `dirty` | 工作区有未提交改动，未执行合并，`files` 是这些文件 |

出错时（不是仓库、分支名非法、基线不存在、删除被拒等）向 stderr 打一行 `{"error": "..."}`，退出码 2。

## 配置

无。仓库目录、分支名、基线全部由命令行传入，不读全局配置。

## 注意

- 已去掉内部工作台的历史约定：不再自动生成 `<开发分支>-ai` 这类“AI 分支”，不再搬运主工作区未提交的改动，也不再有需求目录 / orchestrator 编排。/ 分支名完全由调用方通过 `--branch` 决定，工具只做机械的 worktree 操作。
- 分支名统一走校验：`-` 开头、含 `..`、含空白或结尾是 `/`、`.lock` 的直接拒绝。
- 同一分支不能被两个目录同时检出（git 的限制），`create` 遇到已检出的分支会让 git 报错，不会偷偷改名。
- `remove` 默认拒绝有未提交改动（含未跟踪文件）的目录；`--force` 才会丢弃。主工作区不允许用该命令删除。
- `sync` 只做“基线 → worktree 分支”单向合并，不推送、不 rebase。合并前用 `git merge-tree` 预演，冲突或工作区脏时直接返回 JSON，不产生半成品合并。
- `prune` 只清理记录，不动任何现存目录里的文件。

## 依赖

- `python3`（3.8+）
- `git`（建议 2.38+，`sync` 的冲突预演用到 `git merge-tree --write-tree`）
- 无需第三方包
