---
name: doc-consistency-review
description: "做文档一致性检查和单篇评审：跨 Markdown 查接口路径缺失、实体命名不统一、断链、空章节、残留 TODO，并能对照代码目录验证类名/接口是否存在；还可调用模型评审单篇文档或就文档提问。当用户说“帮我把这几篇文档对一遍”“设计文档有没有漏”“评审这篇方案”时使用。"
metadata:
  requires:
    bins: ["python3"]
---

# doc-consistency-review

文档自查工具。`check` 纯静态、不联网；`review` / `ask` 走 `wbkit.agent.run`。
所有 `check` 输出都是 JSON 数组，字段固定，方便 AI 直接引用文件、行号和证据。

## 使用场景

- 方案设计和接口设计写完后，跨文档对一遍：同一接口是不是只写在部分文档里
- 提交评审前自查：断链、空章节、`TODO` / `待补充` 这类半成品痕迹
- 文档里的类名、接口路径和代码对不上，快速定位是文档写错还是代码没实现
- 只需要意见时，用 `review` 让模型逐条给问题；有具体疑问用 `ask` 问答

## 用法

```bash
# 跨文档一致性检查（只扫 Markdown）
python3 scripts/doc_check.py check --dir ./docs

# 同时对照代码目录，检查文档提到的类名 / 接口路径是否存在
python3 scripts/doc_check.py check --dir ./docs \
  --code ~/repos/project-a --code ~/repos/project-b

# 单篇评审，模型返回 JSON；默认引擎 kimi
python3 scripts/doc_check.py review --file ./docs/02-方案设计.md --engine kimi

# 评审结果落盘（解析失败时原始输出写到 out.json.raw.txt）
python3 scripts/doc_check.py review --file ./docs/02-方案设计.md --engine claude \
  --out ./review.json

# 就文档提问，纯文本回答
python3 scripts/doc_check.py ask --file ./docs/01-需求理解.md \
  --question "下单流程分几步？失败后怎么处理？"
```

## 子命令

| 命令 | 主要参数 | 输出 |
|---|---|---|
| `check` | `--dir` 必填，`--code` 可多次传入 | 问题数组 `[]`，无问题为空数组 |
| `review` | `--file` 必填，`--engine`、`--out` | `{ok, file, engine, count, reviews[]}` |
| `ask` | `--file`、`--question` 必填，`--engine` | 纯文本回答（错误信息走 stderr） |

`--help` 正常；参数缺失或非法时退出码为 `2`。

## check 规则

| kind | severity | 说明 |
|---|---|---|
| `api-path-partial` | low | `/api/...` 路径只在部分文档出现，报出缺失的文档 |
| `naming-inconsistent` | medium | 同一实体多种写法，如 `order_no` / `orderNo` / `OrderNo`，归一后比较 |
| `broken-link` | medium | 文档内相对链接指向的文件不存在（外链、锚点、绝对路径跳过） |
| `empty-section` | low | 标题下既没有正文也没有下级标题 |
| `todo-residue` | low | 残留 `TODO` / `FIXME` / `待补充` / `待完善` 等标记 |
| `code-missing` | medium | 传了 `--code` 时，文档提到的类名或 `/api` 路径在代码里全文找不到 |

每条问题结构：

```json
{
  "kind": "api-path-partial",
  "severity": "low",
  "file": "02-方案设计.md",
  "line": 42,
  "message": "接口路径 /api/orders 只在 1/2 篇文档出现，缺失：01-需求理解.md",
  "evidence": "/api/orders"
}
```

说明：

- 代码块（``` 围起来的部分）会被跳过，避免把示例当结论。
- 接口路径规则至少要有两篇文档提到接口才会触发，单篇文档不报。
- 命名规则只比较含下划线或内部大写的组合词，普通英文单词不参与。

## review 输出

模型被要求只返回 JSON 数组，每个元素：

```json
[
  {
    "severity": "high",
    "section": "3.2 接口约定",
    "quote": "失败后直接返回错误码",
    "problem": "没说是否重试、重试几次",
    "suggestion": "补充失败后的重试策略和上限"
  }
]
```

`review` 会容忍 Markdown 代码块和前后解释，自动截取第一个合法的 JSON 数组。
解析失败时返回 `{ok: false, error, raw_output}`，`raw_output` 是模型原始输出的落盘路径。

## 配置

- 文档目录和代码目录都从命令行传，不写死在配置里。
- 模型引擎（`claude` / `codex` / `deepseek` / `kimi`）沿用 wbkit 配置：`models.<engine>.cli` 可指定可执行文件，密钥走环境变量或 `cfg.set_secret()`，不写进仓库。
- 单测通过模块级 `_runner` 注入假模型，不启动任何 CLI。

## 注意

- `check` 是启发式检查，只提示可疑点，不自动改文档；误报以 `evidence` 为准人工判断。
- `--code` 是全文子串匹配，不是语法分析；大仓库会跳过 `.git`、`node_modules`、`target` 等目录和超大文件。
- `review` / `ask` 会把整篇文档放进提示词，超长文档请先拆分。
- 不修改 `lib/` 和 `tools/`，也不依赖第三方包。

## 依赖

- `python3`（3.8+）
- `check` 无需其它依赖
- `review` / `ask` 需要本机装好对应模型 CLI，并按 wbkit 配置好密钥
