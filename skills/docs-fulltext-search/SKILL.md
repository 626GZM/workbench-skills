---
name: docs-fulltext-search
description: "给一个目录建全文索引，然后按关键词搜索文件和内容。当用户问“哪个文档讲过 XX”“这段配置在哪”“帮我在这堆 md 里找某个接口名”，或者需要在大量文档/代码里定位相关片段时使用。索引只是缓存，随时可重建。"
metadata:
  requires:
    bins: ["python3"]
---

# docs-fulltext-search

基于 SQLite FTS5 的目录全文搜索。索引库默认在 `~/.local/state/workbench-skills/fileindex.db`，
可在配置里改 `index.db_path`。

## 使用场景

- 需求文档、设计稿、代码混在一个目录里，要按关键词定位
- 只记得半句话，想找原文档
- 想知道某个目录里有多少 md / 多少字

## 用法

```bash
# 建索引（首次或目录有大批改动时）
python3 scripts/search_files.py build --root /path/to/docs

# 只索引 markdown 和 python
python3 scripts/search_files.py build --root /path/to/docs --ext md,py

# 搜索
python3 scripts/search_files.py search --root /path/to/docs --query "渠道审批" --limit 10

# 只在 markdown 里搜
python3 scripts/search_files.py search --root /path/to/docs --query "接口" --ext md

# 看索引状态
python3 scripts/search_files.py stats --root /path/to/docs

# 删掉某目录的索引
python3 scripts/search_files.py drop --root /path/to/docs
```

## 输出

`search` 返回：

```json
{
  "root": "/path/to/docs",
  "query": "渠道审批",
  "count": 2,
  "hits": [
    {"path": "02-方案设计.md", "size": 38210, "mtime": 1763456789, "snippet": "…审批节点需要…"}
  ]
}
```

`build` 返回 `{"root": "...", "files": 128, "bytes": 3145728}`。

## 注意

- 索引是缓存，删了不影响任何原文件。
- 中文用 FTS5 的 unicode61 分词，检索不准时自动退回子串匹配。
- 跳过 `.git`、`node_modules`、`target`、`dist`、`.venv` 等目录；单文件超过 4MB 只记元数据不进全文。

## 依赖

- `python3`（3.8+，需带 sqlite3 且编译了 FTS5；标准安装的 Python 都带）
- 无需第三方包
