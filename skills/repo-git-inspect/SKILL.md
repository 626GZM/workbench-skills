---
name: repo-git-inspect
description: "只读查询 git 仓库：当前分支、未提交改动、提交列表、两点的差异、merge base、从 origin 拉取分支。当用户问“这个仓库现在什么状态”“这段改动和主分支差多少”“最近提交了哪些”，或需要在 review 前先摸清 diff 范围时使用。不改工作区、不提交、不推送。"
metadata:
  requires:
    bins: ["python3", "git"]
---

# repo-git-inspect

只读的 git 查询工具。所有命令输出 JSON，方便 AI 直接引用行号、哈希和文件列表。

## 使用场景

- review 前先确认改动范围：先 `merge-base`，再 `diff --stat`
- 判断工作区是否干净，决定能不能切分支
- 取一段提交的标题和作者，写变更说明
- 在 worktree 里确认当前分支对不对

## 用法

```bash
# 仓库概况：分支、HEAD、脏文件数、最近一次提交
python3 scripts/git_inspect.py info --repo /path/to/repo

# 未提交改动（tracked / untracked 分开）
python3 scripts/git_inspect.py dirty --repo /path/to/repo

# 最近 20 条提交
python3 scripts/git_inspect.py log --repo /path/to/repo --range origin/main..HEAD --limit 20

# 差异统计（看改了哪些文件、多少行）
python3 scripts/git_inspect.py diff --repo /path/to/repo --base origin/main --stat

# 完整 diff 文本（默认截断到 200KB）
python3 scripts/git_inspect.py diff --repo /path/to/repo --base origin/main

# 存成补丁文件
python3 scripts/git_inspect.py diff --repo /path/to/repo --base origin/main --output /tmp/change.patch

# 两个 ref 的 merge base
python3 scripts/git_inspect.py merge-base --repo /path/to/repo --a origin/main --b feature/x

# 区间内的提交号
python3 scripts/git_inspect.py commits --repo /path/to/repo --range origin/main..HEAD

# 从 origin 拉取分支（只拉，不推）
python3 scripts/git_inspect.py fetch --repo /path/to/repo --branches main,dev
```

## 子命令

| 命令 | 输出 |
|---|---|
| `info` | `branch` / `head` / `dirty` / `untracked` / `last_commit` / `last_fetch` |
| `branch` | 当前分支名 |
| `dirty` | `tracked[]` / `untracked[]` |
| `log` | `commits[]`：`hash` / `author` / `time` / `subject` |
| `diff` | `diff` 文本，或 `--stat` 的统计，或 `--output` 写文件 |
| `merge-base` | 共同祖先的提交号（没有则 `null`） |
| `commits` | 提交号数组 |
| `fetch` | 每个分支是否拉到（`true` / `false`） |
| `root` | 目录所属仓库根（不是仓库则 `null`） |

## 注意

- 只读：不提交、不切换分支、不推送。唯一的写操作是 `fetch` 更新远端引用。
- 分支名会做合法性校验，`-` 开头、含 `..` 或空白的直接拒绝。
- 其它 git 进程占着 `index.lock` 时会自动重试几次。

## 依赖

- `python3`（3.8+）
- `git`
- 无需第三方包
