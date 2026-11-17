# workbench-skills

一组可独立安装、可组合使用的 Agent Skill，把日常开发里那些重复动作做成命令行工具：查仓库、审代码、审文档、追需求阶段、写日报、排查线上问题、整理测试反馈、看 e2e 结果。

每个 skill 都是自包含的：`SKILL.md` 描述什么时候用、怎么用，`scripts/` 下是可执行的 Python 脚本（只用标准库），拷走一个目录就能用。

兼容 Claude Code、Codex CLI、Trae、LobeHub 等支持 Agent Skill 的环境。

## Skill 清单

### 需求与文档

| Skill | 做什么 |
|---|---|
| [prd-doc-ingest](skills/prd-doc-ingest/) | 把 PRD（md / html / txt / docx / pdf）和图片整理进干净目录，抽出目标、验收条件、接口约定、待确认问题 |
| [doc-review-comments](skills/doc-review-comments/) | 像审代码一样逐段审文档：意见锚定原文，逐条让模型改，保留 diff，可接受或退回 |
| [doc-consistency-review](skills/doc-consistency-review/) | 跨文档查接口缺失、命名不统一、断链、空章节、残留 TODO，并可对照代码验证 |
| [req-stage-workflow](skills/req-stage-workflow/) | 需求阶段看板：查进度、按顺序推进、跳过阶段、登记变更、导出 markdown |
| [task-brief-handoff](skills/task-brief-handoff/) | 用一份 brief.json 记录目标、验收、待办、决定、踩坑，生成交接简报 |

### 代码与审查

| Skill | 做什么 |
|---|---|
| [repo-git-inspect](skills/repo-git-inspect/) | 只读查 git：分支、脏文件、提交、diff、merge base、fetch |
| [git-worktree-flow](skills/git-worktree-flow/) | 同一仓库多分支并行开发：建/列/删 worktree、清理失效记录、合基线 |
| [multi-model-code-review](skills/multi-model-code-review/) | 审 git 改动，可单模型或多模型并跑后合并去重、标注一致性 |
| [multi-agent-run](skills/multi-agent-run/) | 无头调用 Claude Code / Codex / DeepSeek / Kimi 跑一次任务并取回结果 |
| [test-fix-loop](skills/test-fix-loop/) | 跑测试、读失败、让模型改、再跑，反复到通过；只修这次失败 |
| [java-code-index](skills/java-code-index/) | Java 代码地图：接口在哪、谁调用我、改这里影响谁 |
| [java-layer-review](skills/java-layer-review/) | 扫新增 Java 代码里的重复防御、分层越界、常见坏味道，规则可开关 |
| [todo-scanner](skills/todo-scanner/) | 扫 TODO/FIXME/HACK、调试残留、空 catch、注释掉的代码，按人和严重程度汇总 |

### 运行与排查

| Skill | 做什么 |
|---|---|
| [online-issue-investigate](skills/online-issue-investigate/) | 线上告警自动抠线索、查日志、对代码只读排查，出根因和止血建议 |
| [test-issue-triage](skills/test-issue-triage/) | 把一堆测试反馈拆条、逐条定归属（后端/不是 bug/前端/信息不够），可选直接改 |
| [e2e-artifact-view](skills/e2e-artifact-view/) | 只读看 e2e 产物：运行列表、失败原因、通过率趋势、账号脱敏、内容打码 |
| [local-service-deploy](skills/local-service-deploy/) | 按 services.json 构建、启动、停止本地服务，看端口、健康检查和日志 |

### 汇报与度量

| Skill | 做什么 |
|---|---|
| [daily-report](skills/daily-report/) | 把当天工作记录汇总成能直接发的日报：明细版或固定条数简版 |
| [dev-metrics-report](skills/dev-metrics-report/) | 从 runs.jsonl 和 git 算效率指标，出周报和异常信号 |
| [docs-fulltext-search](skills/docs-fulltext-search/) | 给目录建全文索引，按关键词找文档和片段（中文友好） |
| [model-quota-check](skills/model-quota-check/) | 查 Kimi / Codex / DeepSeek 剩余额度，判断还能不能跑 |

## 安装

把需要的 skill 目录整个拷到你的工具能识别的位置即可，例如：

```bash
# Claude Code
cp -r skills/repo-git-inspect ~/.claude/skills/

# Trae
cp -r skills/daily-report ~/.trae-cn/skills/
```

目录里的 `scripts/_lib/` 是构建时同步进来的公共底座，拷贝时会一起带上，不需要额外安装依赖。所有脚本只依赖 Python 3.9+ 标准库。

## 配置

默认不需要配置。需要改路径、模型、密钥时，按优先级取：

1. 环境变量 `WB_CONFIG` 指向的 JSON
2. 当前目录的 `.wbkit.json`
3. `~/.config/workbench-skills/config.json`

```bash
cp config/config.example.json ~/.config/workbench-skills/config.json
```

模型密钥放环境变量，或写进 `~/.config/workbench-skills/secrets.json`（脚本会以 600 权限写入）：

```bash
export DEEPSEEK_API_KEY=...
export KIMI_API_KEY=...
```

不配密钥也能用大部分只读功能；需要模型的能力会自动回退到规则版或明确报错。

## 本仓库的开发

```bash
python3 tools/sync_lib.py         # 把 lib/wbkit 同步到各 skill 的 scripts/_lib
python3 tools/validate_skill.py   # 校验 SKILL.md 格式与脚本 --help
python3 -m unittest discover -s tests -t .   # 全量测试
python3 tools/check_drift.py      # 检查同步产物是否有漂移（CI 用）
```

`lib/wbkit` 是唯一真源，不要直接改 `skills/*/scripts/_lib/`，改完跑 `sync_lib.py`。

## License

MIT
