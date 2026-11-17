---
name: prd-doc-ingest
description: "把一份 PRD（md / html / txt / docx / pdf）连同引用图片整理进干净的需求目录，并列出文件与大纲、抽出目标、范围、验收条件、接口约定和待确认问题，还能检查缺图、外链图片、空章节、标题跳级。当用户要导入需求文档、整理 PRD 素材，或想在开发前快速过一遍需求完整性时使用。"
metadata:
  requires:
    bins: ["python3"]
---

# prd-doc-ingest

需求文档进入开发前的第一道工序：把散落的 PRD 和图片收进一个干净目录，再把可以执行的要素抽出来。

翻译成文件就是：

- `<dest>/<文档名>.md`：正文，图片引用已改成目录内相对路径
- `<dest>/assets/`：`../`、绝对路径或 docx 里的图片集中放这里

只处理本地文件，不联网拉飞书 / 语雀文档，不调模型。要做语义归纳（补功能点、合并重复验收、生成更自然的问题），把输出交给 `multi-agent-run` 再跑一轮即可。

## 使用场景

- 拿到一份导出的 PRD，先 `ingest` 连同图片收进需求目录，再去写方案
- 评审前跑 `list` 看文件、图片数和章节结构，确认关键约定在哪一节
- 写测试用例前跑 `extract`，直接拿到验收条件和接口清单
- 交付前跑 `check`，把缺图、外链图片、空章节、没有验收条件这类硬伤先补掉

## 用法

```bash
# md：正文 + 本地图片一起复制，../ 和绝对路径的图落到 assets/
python3 scripts/prd_ingest.py ingest --src ~/docs/PRD.md --dir ~/reqs/REQ-101

# txt：自动包成 md
python3 scripts/prd_ingest.py ingest --src ~/docs/PRD.txt --dir ~/reqs/REQ-101

# html：去标签转 md，保留标题层级、列表、表格和图片
python3 scripts/prd_ingest.py ingest --src ~/docs/PRD.html --dir ~/reqs/REQ-101

# docx：只读 word/document.xml，保留标题层级、列表、表格，图片按顺序插入
python3 scripts/prd_ingest.py ingest --src ~/docs/PRD.docx --dir ~/reqs/REQ-101

# pdf：默认调 pdftotext -layout；也可以换成自己的命令
python3 scripts/prd_ingest.py ingest --src ~/docs/PRD.pdf --dir ~/reqs/REQ-101
python3 scripts/prd_ingest.py ingest --src ~/docs/PRD.pdf --dir ~/reqs/REQ-101 \
  --pdf-cmd 'my-pdf2txt --input {src} --output {out}'

# 目标目录非空时确认覆盖
python3 scripts/prd_ingest.py ingest --src ~/docs/PRD.md --dir ~/reqs/REQ-101 --force

# 目录清单：文件大小、行数、图片数 + 大纲，并标出验收 / 范围 / 接口 / 异常章节
python3 scripts/prd_ingest.py list --dir ~/reqs/REQ-101

# 抽要素，同时存一份 JSON
python3 scripts/prd_ingest.py extract --dir ~/reqs/REQ-101 --out ~/reqs/REQ-101/extract.json

# 残缺检查
python3 scripts/prd_ingest.py check --dir ~/reqs/REQ-101
```

## 子命令

| 命令 | 作用 | 主要输出 |
|---|---|---|
| `ingest` | 复制正文和图片，转换 txt / html / docx / pdf | `dest` / `title` / `files` / `images` / `warnings` |
| `list` | 列文件大小、行数、图片数，抽 `#` / `##` 大纲并给关键章节打标签 | `files[].size` / `files[].images` / `files[].sections[]` |
| `extract` | 抽目标、范围、验收、接口、疑问 | `goals` / `scope` / `acceptance` / `interfaces` / `questions` |
| `check` | 查缺图、外链图片、空章节、验收为空、标题跳级 | `ok` / `summary` / `issues[]` |

## 输出 JSON 结构

`ingest`：

```json
{
  "dest": "/home/me/reqs/REQ-101",
  "title": "订单导出需求",
  "files": ["PRD.md"],
  "images": 3,
  "warnings": ["图片不存在：images/old.png"]
}
```

`list`：

```json
{
  "dir": "/home/me/reqs/REQ-101",
  "files": [{
    "file": "PRD.md",
    "size": 2048,
    "lines": 42,
    "images": 3,
    "title": "订单导出需求",
    "sections": [
      {"line": 1, "level": 1, "title": "订单导出需求", "tags": []},
      {"line": 8, "level": 2, "title": "接口约定", "tags": ["接口"]},
      {"line": 20, "level": 2, "title": "验收条件", "tags": ["验收"]}
    ]
  }],
  "total_size": 2048,
  "total_images": 3,
  "assets": 2
}
```

`extract`：

```json
{
  "dir": "/home/me/reqs/REQ-101",
  "files": ["PRD.md"],
  "goals": [{"text": "支持批量导出订单", "file": "PRD.md", "line": 5}],
  "scope": {
    "in": [{"text": "订单导出", "file": "PRD.md", "line": 10}],
    "out": [{"text": "财务对账", "file": "PRD.md", "line": 11}]
  },
  "acceptance": [{"id": "AC-01", "text": "导出的文件 30 秒内生成", "file": "PRD.md", "line": 24}],
  "interfaces": [{"method": "GET", "path": "/orders/export", "note": "查询导出任务",
                  "file": "PRD.md", "line": 16}],
  "questions": [{"text": "单次导出上限多少条？", "file": "PRD.md", "line": 30}],
  "warnings": []
}
```

`check`：

```json
{
  "dir": "/home/me/reqs/REQ-101",
  "files": ["PRD.md"],
  "ok": false,
  "summary": {"files": 1, "issues": 2, "by_type": {"missing_image": 1, "empty_section": 1}},
  "issues": [
    {"type": "missing_image", "file": "PRD.md", "line": 12,
     "ref": "images/old.png", "message": "图片引用不存在：images/old.png"},
    {"type": "empty_section", "file": "PRD.md", "line": 30,
     "title": "异常处理", "message": "章节是空的：异常处理"}
  ]
}
```

`issues[].type` 取值：`missing_image` / `remote_image` / `empty_section` / `missing_acceptance` / `acceptance_empty` / `heading_jump`。

## 支持格式

| 格式 | 处理方式 |
|---|---|
| `.md` / `.markdown` | 原样复制正文，复制本地图片；`../` 或绝对路径的图落到 `assets/` 并改写引用 |
| `.html` / `.htm` | 去标签转 md，保留标题层级、列表、表格和图片 `src`；图片复制规则同 md |
| `.txt` / `.text` | 包成 `# 标题 + 正文` 的 md |
| `.docx` | `zipfile` 解 `word/document.xml`，抽段落、标题层级、列表和表格；`word/media` 图片按出现顺序插入 |
| `.pdf` | 不解析二进制，调外部命令转文本（默认 `pdftotext -layout`），失败提示先转 md |

`--src` 传目录时，只导入该目录第一层的 md / html / txt / docx / pdf，不递归。

## 目录约定

```text
<dest>/
  <文档名>.md      # 每个源文档转成一份 md
  assets/          # ../、绝对路径、docx 内嵌的图片
  <子目录>/         # md 里原本就在同级的图片按原相对路径保留
```

md 里本来就是同目录或子目录的图片，复制后引用保持原样；只有跨目录（`../`）和绝对路径的图片会被搬到 `assets/` 并改写。`http/https/data:` 等网络图片不动。

## 抽取规则

- `list` 的 `sections` 只读 `#` / `##`，标题里含「验收」「范围」「接口」「异常」就写进 `tags`
- 目标：目标 / 需求目标 / 项目目标 / 业务目标 / 价值 / 收益 小节里的条目
- 范围：范围 / 需求范围 / 业务范围 小节；带「不包含 / 不含 / 不做 / 范围外 / 暂不」的进 `out`，其余进 `in`；小节内出现「不包含」子标题时，后续条目都算 `out`
- 验收条件：验收 / 验收标准 / 验收要点 / 测试要点 小节里的条目，按出现顺序编号 `AC-01`；没有该小节时用「验收 / 必须 / 不得 / 至少 / 不超过」等字样兜底
- 接口约定：markdown 表格里「方法 + 路径」两列，或任何位置出现的 `GET /path` 写法
- 待确认问题：含「待确认 / 待定 / 待补充 / 未确认 / TBD」或整行以 `？` / `?` 结尾的条目
- `check` 的空章节指标题下既没有正文也没有子章节；验收为空指有验收小节但没有任何内容

## 限制

- PDF 不做版面还原，`pdftotext` 抽出来的表格可能错行；重要表格建议先手工转成 md。也可以把 `--pdf-cmd` 换成团队自己的转换脚本，支持 `{src}` 和 `{out}` 占位符
- HTML 只做结构化降级：不执行脚本、不还原样式和绝对定位，复杂布局（多栏、卡片）会退化成顺序段落
- docx 只读正文 XML：不处理批注、修订痕迹、页眉页脚和合并单元格；图片只认 `word/media` 里被关系表引用的
- 图片只处理本地文件引用，不下载网络图片
- 抽取是纯规则：同名条目会去重，但语义合并、隐含验收、跨章节归纳都做不到。需要这些能力时把 `extract.json` 交给 `multi-agent-run`，让模型在规则结果上补一轮
- `check` 只报结构问题，不判断业务对错

## 注意

- 这里砍掉了内部工作台的 workflow 状态、server 路由、AI 调用、飞书 / 语雀在线拉取和 PDF 页面截图；只保留「整理目录 + 规则抽取」
- 重复导入到非空目录必须加 `--force`，否则直接报错，避免覆盖手工改过的正文
- 代码块里的 `#` 和图片引用不参与大纲、抽取和检查

## 依赖

- `python3`（3.8+）
- 标准库：`argparse` / `html.parser` / `json` / `re` / `shlex` / `shutil` / `subprocess` / `zipfile` / `xml.etree` / `urllib.parse`
- 可选：`pdftotext`（来自 poppler），只有导入 PDF 时才需要
- 不装第三方包
