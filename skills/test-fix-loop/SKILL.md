---
name: test-fix-loop
description: 跑测试、读失败、让模型改、再跑，反复到通过或轮次用完。当用户说“把这个单测修到过”“编译过不了帮我看”“这条用例一直挂”，或者想让 AI 自己把一次失败的构建/测试修好时使用。只修这次失败的原因，不重构、不加功能。
metadata:
  requires:
    bins: ["python3", "git"]
---

# test-fix-loop

## 使用场景

一次测试或构建失败，报错很明确，你不想手动来回改：

- “把这个单测修到过”
- “编译挂了，看看哪里有问题，改掉”
- “这条 e2e 用例一直失败，帮我定位并修”

不适合：需求还没实现（那是写代码，不是修失败）、失败原因是环境/权限/网络（脚本会停在“信息不足”）。

## 用法

### 修一次失败的测试

```
用户：把 core 模块的 FooTest 修到过
AI：
1. 跑 test_fix_loop.py run --cmd "mvn -q -pl core -Dtest=FooTest test" --cwd .
2. 读 JSON：rounds 里每轮有 exit、summary、failures
3. ok=true 就报“修好了，改了哪些文件、跑了几轮、各轮耗时”
4. ok=false 就报 reason 和最后一轮的失败，不要自己再手改
```

### 只想看诊断

```
test_fix_loop.py run --cmd "npm test -- --run" --analyze-only
```

模型只给分析，不写文件。

### 允许改业务代码

默认 `--scope test` 只能改 `src/test` 下的测试代码。确认是业务代码写错时才用：

```
test_fix_loop.py run --cmd "mvn -q test" --scope any
```

### 只跑一次看失败

```
test_fix_loop.py run --cmd "pytest -x" --dry-run
```

不调模型，只输出解析出的失败条目，适合先确认命令本身对不对。

## 输出

JSON，关键字段：

| 字段 | 含义 |
|---|---|
| `ok` | 最终是否通过 |
| `reason` | `通过` / `轮次用完仍失败` / `连续两轮失败一模一样` / `模型调用失败：…` |
| `rounds[]` | 每轮的退出码、耗时、失败摘要、模型说了什么、改了哪些文件 |
| `changed_files` | 全程改动过的文件（git 仓库才有） |

退出码：0 通过，1 未通过，2 参数或环境问题。

## 注意

- 命令用 shell 执行，注意引号；复杂命令建议写成一个脚本再传 `--cmd ./run-tests.sh`。
- 最多 5 轮，默认 3 轮。两轮失败一模一样时提前停，避免白烧额度。
- 模型不跑完整测试套件，只改代码；重跑由脚本负责，所以每轮改动都会被验证。
- 结果里会有模型原话，可以据此判断它是真修好了还是在猜。
