---
name: todo-scanner
description: "静态扫描代码仓库里的 TODO / FIXME / XXX / HACK、调试残留、空 catch、被吞掉的异常和注释掉的代码块，按严重程度与责任人汇总成可跟踪的清单；也能只报本次改动新增的条目。纯静态扫描，不调用模型、不改文件。"
metadata:
  requires:
    bins: ["python3", "git"]
---

# todo-scanner

把散落在代码里的遗留问题扫出来，变成一张能派活、能核对的清单。只读文件和 git，不写任何源码。

## 使用场景

- 接手一个仓库，想知道还有多少 TODO、都压在谁身上
- 提交 review 前，只确认"这次改动有没有新埋 TODO / 调试语句"
- 迭代收尾，按严重度和作者统计剩余遗留问题
- 把扫描结果交给别的流程继续处理（JSON 输出）

## 用法

```bash
# 全量扫描（默认扫常见代码扩展名，跳过 node_modules / target / dist 等目录）
python3 scripts/scan_todos.py scan --repo ~/repos/project-a

# 只扫 Java / Python / TypeScript，跳过多余目录，最多 300 条
python3 scripts/scan_todos.py scan --repo ~/repos/project-a \
  --ext .java,.py,.ts --exclude node_modules,target,dist --limit 300

# 存成 JSON，再渲染成 markdown 清单（默认按文件分组）
python3 scripts/scan_todos.py scan --repo ~/repos/project-a > /tmp/todos.json
python3 scripts/scan_todos.py report --input /tmp/todos.json --out /tmp/todos.md

# 按责任人分组，方便派活
python3 scripts/scan_todos.py report --input /tmp/todos.json --group-by author

# 只看本次改动相对 origin/main 新增的条目
python3 scripts/scan_todos.py diff --repo ~/repos/project-a --base origin/main

# 按类型 / 作者 / 年龄分桶
python3 scripts/scan_todos.py stats --repo ~/repos/project-a
```

## 子命令

| 命令 | 说明 | 输出 |
|---|---|---|
| `scan` | 全量扫描仓库 | JSON：条目数组 + 统计 |
| `report` | 把扫描结果渲染成 markdown 清单 | markdown（`--out` 时同时写文件） |
| `diff` | 只报相对 `--base`（默认 `origin/main`）新增的条目，对比的是 merge-base | JSON，结构同 `scan`，另带 `base` / `merge_base` |
| `stats` | 按类型、作者、年龄分桶 | JSON |

公共参数：

- `--repo DIR`：仓库目录，默认当前目录
- `--ext .java,.py,.ts`：扫描的扩展名，逗号分隔；传了就整套替换默认值
- `--exclude node_modules,target,dist`：整棵跳过的目录；传了就整套替换默认值
- `--limit N`：最多返回多少条，默认 500（先排序再截断，并给出 `truncated`）

## 识别的类型（kind）

| kind | 识别方式 | 严重度 |
|---|---|---|
| `todo` | 注释里的 `TODO`、`XXX`、`待办`、`待改` | medium |
| `fixme` | 注释里的 `FIXME` | high |
| `hack` | 注释里的 `HACK` | high |
| `debug` | `console.log(`、`printStackTrace(`、`System.out.println(` | high |
| `commented_code` | 连续 5 行以上、且整体像代码的注释块 | low |
| `empty_catch` | `catch (...) { }` 块里没有任何可执行语句 | high |
| `swallowed_exception` | `catch` / `except` 非空，但不 `throw` / `raise` / `return`，也只打印或 `pass` | high |

`tag` 保留原始标记或具体来源（`TODO`、`FIXME`、`XXX`、`HACK`、`CONSOLE_LOG`、`PRINT_STACK_TRACE`、`SYSTEM_OUT`、`EMPTY_CATCH`、`SWALLOWED_EXCEPTION`、`COMMENTED_CODE`），便于自己再细分。

标记只认注释里的内容：字符串字面量里的 `"TODO"` 不会误报；注释里的 `console.log` 算“注释掉的代码”，不算调试残留。同一行的空 catch 和 `printStackTrace` 只保留信息量更大的结构类条目。

## 输出 JSON

`scan` / `diff`：

```json
{
  "repo": "/home/me/repos/project-a",
  "files": 128,
  "base": "origin/main",
  "merge_base": "3f2a1c9...",
  "entries": [
    {
      "file": "src/main/java/com/example/OrderService.java",
      "line": 42,
      "kind": "fixme",
      "tag": "FIXME",
      "text": "并发下会重复扣款",
      "author": "张三",
      "commit": "9a8b7c6d5e4f3a2b1c0d",
      "age_days": 12,
      "severity": "high"
    }
  ],
  "total": 1,
  "matched": 1,
  "truncated": false
}
```

- `author` / `commit` / `age_days` 来自逐行 `git blame -L line,line --line-porcelain`；不在 git 仓库、文件未入库或 blame 失败时，前两项为空字符串，`age_days` 为 `null`，不影响其它字段。
- `matched` 是截断前的候选条数，`total` 是实际返回条数。

`stats`：

```json
{
  "repo": "/home/me/repos/project-a",
  "files": 128,
  "total": 37,
  "matched": 37,
  "truncated": false,
  "by_kind": {"todo": 20, "fixme": 9, "debug": 5, "commented_code": 3},
  "by_author": {"张三": 22, "李四": 15},
  "by_age": {"0-7": 4, "8-30": 11, "31-90": 12, "91-180": 7, "180+": 3, "unknown": 0},
  "by_severity": {"high": 17, "medium": 17, "low": 3}
}
```

## 报告

`report` 输出 markdown，包含总条数、严重度汇总，以及按 `--group-by` 分好的表格：

```markdown
## src/main/java/com/example/OrderService.java（2 条）

| 行 | 类型 | 严重度 | 责任人 | 提交 | 年龄 | 内容 |
|---|---|---|---|---|---|---|
| 42 | fixme | high | 张三 | 9a8b7c6d | 12 天 | 并发下会重复扣款 |
```

`--group-by` 支持 `file`（默认）、`author`、`kind`、`severity`。表头里的 `文件` 列只在非 file 分组时出现；未取到 blame 的责任人显示为 `未归属`。

## 配置

没有配置文件，也不读 `wbkit` 的配置：扫描范围全部由命令行参数决定（`--repo`、`--ext`、`--exclude`、`--limit`）。不同项目可以把常用参数写进自己的脚本或 CI 任务里。

## 注意

- 纯静态扫描，不调用任何模型；需要 AI 帮忙归类、判断该不该修时，可把 JSON 交给 `multi-agent-run` 的只读档位。
- 砍掉了内部工作台原有的规范检查、workflow 依赖和编排语义，这里只做通用代码扫描。
- `diff` 对比的是 `--base` 与 `HEAD` 的 merge-base，未跟踪的新文件整篇算新增；基线 ref 不存在时直接报错（退出码 2）。
- `commented_code` 用启发式判断"像代码"：要求连续注释里有分号、括号、赋值、控制关键字等信号，并跳过版权头和 javadoc 参数块，宁可漏报也不刷屏。
- `empty_catch` / `swallowed_exception` 只解析 `{ }` 结构的 catch 块和 Python 的 `except` 块，其它语言的结构写法可能识别不到。
- 逐行 blame 会启动 git 子进程，仓库特别大时建议配 `--limit`，或先用 `diff` 只看增量。
- 单个文件超过 2 MB 或含二进制内容会被跳过。

## 依赖

- `python3`（3.8+）
- `git`（取作者和提交；没有 git 也能扫，只是 blame 字段为空）
- 无需第三方 Python 包
