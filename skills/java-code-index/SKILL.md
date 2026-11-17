---
name: java-code-index
description: "给 Java 仓库建静态代码地图，快速回答“这个接口在哪实现”“这个类被谁调用”“改这个方法会影响哪些地方”。遍历源码解析包/类/方法/注解，落 index.json，并提供查找、调用关系、Spring 接口清单和影响面分析。纯静态解析，不调用模型。"
metadata:
  requires:
    bins: ["python3"]
---

# java-code-index

Java 代码索引。不跑编译、不连数据库、不调模型：遍历 `src/main/java`，解析 package / import / 类 / 方法 / 注解，把结果写成一份 `index.json`，之后用几条查询命令回答“在哪实现”“谁在用”“改了会怎样”。

## 使用场景

- 拿到一个陌生 Java 仓库，先 `build` 出代码地图，再按类名或接口路径定位实现
- 排查某个方法被哪些地方调用，评估修改范围
- 改一个文件前，先看谁 import 了它、谁实现了同一个接口、谁调了它的方法
- 从 Spring controller 反查完整的 HTTP 方法 + 路径 + Java 方法签名，写接口文档或做联调
- 用 `*` 通配批量找命名规律相同的类（如 `*Controller`、`*Mapper`）

## 安装

整个 `java-code-index/` 目录拷到 skills 目录即可，无需额外依赖：

```bash
cp -r java-code-index ~/.config/agent-skills/
# 或用软链
ln -s "$PWD/java-code-index" ~/.config/agent-skills/java-code-index
```

脚本只依赖 Python 标准库，不需要 `git`、`javaparser` 或任何第三方包。

## 用法

```bash
# 1. 建索引（默认源码目录 src/main/java，默认输出 .wbkit-out/java-index）
python3 scripts/java_index.py build --repo ~/repos/project-a

# 自定义源码目录和输出目录
python3 scripts/java_index.py build --repo ~/repos/project-a \
  --src src/main/java --out .wbkit-out/java-index

# 2. 按类名 / 方法名 / 注解模糊查，支持 * 通配
python3 scripts/java_index.py find --repo ~/repos/project-a --query OrderService
python3 scripts/java_index.py find --repo ~/repos/project-a --query '*Controller'
python3 scripts/java_index.py find --repo ~/repos/project-a --query '@RestController'

# 3. 谁调用了这个方法（启发式）
python3 scripts/java_index.py callers --repo ~/repos/project-a --method pay
python3 scripts/java_index.py callers --repo ~/repos/project-a --method OrderService.pay

# 4. 这个方法调用了谁（启发式）
python3 scripts/java_index.py callees --repo ~/repos/project-a --method OrderService#pay

# 5. Spring 接口清单，可按路径前缀过滤
python3 scripts/java_index.py api --repo ~/repos/project-a
python3 scripts/java_index.py api --repo ~/repos/project-a --path /orders

# 6. 改这个文件可能影响谁
python3 scripts/java_index.py impact --repo ~/repos/project-a \
  --file src/main/java/com/example/order/OrderService.java
```

查询类命令会先读 `--out` 下的 `index.json`；没有索引时会在内存里临时构建一份（要求仓库有 `src/main/java`），不写盘。想让结果稳定可复现，先跑一次 `build`。

## 子命令

| 命令 | 参数 | 输出 |
|---|---|---|
| `build` | `--repo` / `--src` / `--out` | 写 `index.json`，返回 `stats` 和 `parse_errors` |
| `find` | `--query` | `hits[]`：类或方法，带 `file:line` |
| `callers` | `--method` | `targets[]`（目标方法声明）+ `callers[]`（调用点） |
| `callees` | `--method` | `targets[]` + `callees[]`（方法体里的调用名与同名声明） |
| `api` | `--path`（可选前缀） | `controllers[]` + 每个 controller 的 `endpoints[]` |
| `impact` | `--file` | `impact[]`：`reason`（import/extends/implements/sibling_impl/call） |

`--method` 支持三种写法：`pay`（只按方法名）、`OrderService.pay`、`com.example.order.OrderService#pay`。

## 输出 JSON 结构

`index.json`：

```json
{
  "repo": "~/repos/project-a",
  "src": "src/main/java",
  "generated_at": "2026-11-17T10:30:00",
  "classes": [
    {
      "fqn": "com.example.order.OrderService",
      "file": "src/main/java/com/example/order/OrderService.java",
      "kind": "class",
      "line": 5,
      "package": "com.example.order",
      "extends": [],
      "implements": ["OrderApi"],
      "annotations": [{"name": "Service", "args": ""}],
      "modifiers": ["public"],
      "imports": ["java.util.List", "com.example.order.dto.Order"],
      "methods": [
        {
          "name": "pay",
          "line": 12,
          "returns": "Order",
          "params": [{"type": "Long", "name": "id"}],
          "annotations": [{"name": "Transactional", "args": ""}],
          "modifiers": ["public"],
          "signature": "Order pay(Long id)",
          "end": 20
        }
      ]
    }
  ],
  "parse_errors": [{"file": "src/main/java/com/example/order/Broken.java", "error": "第 3 行：花括号不配平"}],
  "stats": {"files": 42, "classes": 57, "methods": 312, "parse_errors": 1}
}
```

- `kind` 取值：`class` / `interface` / `enum` / `record`；内部类的 `fqn` 用 `Outer.Inner` 形式。
- `extends` / `implements` 已去掉泛型参数，保留类名（可能是简单名或全名）。
- `signature` 是拼出来的可读签名，`returns` 已去掉泛型。
- 每个方法记录 `end` 行（方法体结束行），方便直接定位范围。

`find` 的命中项：

```json
{
  "kind": "method",
  "name": "pay",
  "class": "com.example.order.OrderService",
  "signature": "Order pay(Long id)",
  "file": "src/main/java/com/example/order/OrderService.java",
  "line": 12,
  "location": "src/main/java/com/example/order/OrderService.java:12"
}
```

`api` 的返回：

```json
{
  "count": 2,
  "controllers": [
    {
      "class": "com.example.order.OrderController",
      "file": "src/main/java/com/example/order/OrderController.java",
      "line": 8,
      "base_path": "/orders",
      "endpoints": [
        {"http": "GET", "path": "/orders/{id}", "method": "get",
         "signature": "Order get(Long id)", "line": 18,
         "location": "src/main/java/com/example/order/OrderController.java:18",
         "annotations": ["GetMapping", "PathVariable"]}
      ]
    }
  ]
}
```

`callers` / `callees` / `impact` 都带一个 `note` 字段，明确写清结果是怎么来的、哪里可能漏。

## 解析是怎么做的

自带轻量解析器，三步：

1. **去噪**：把注释、字符串、字符、文本块替换成等长空格，换行和偏移保持不变。所以注释或字符串里出现的 `class Fake {` 不会干扰结构扫描。
2. **括号配平**：从文件顶层开始扫描 `;` 和 `{`，用配平找到每个类体和每个方法体的边界，递归进入内部类。
3. **头部解析**：从成员头部里，先用规则剥掉注解，再定位方法名、参数和返回类型；注解参数从原始文本里取回，因此 `@GetMapping("/orders/{id}")` 的字符串不会丢。

特意覆盖：泛型（`Map<String, List<Order>>`）、方法级泛型（`<R> R map(...)`）、内部类、注解带参数和嵌套注解、多行方法签名、`throws`、接口默认方法、抽象方法、enum、record、Lombok 注解（只当普通注解记录，不做展开）。

## 解析限制

- **调用关系是启发式的**：`callers` / `callees` 只按方法名做文本匹配，不解析变量类型、不做重载区分、不跟踪接口到实现的分派。同名方法会混在一起，可能漏报（动态代理、反射、AOP、MQ 消费入口）也可能误报。
- **不覆盖框架行为**：Spring 的依赖注入、MyBatis 的 XML SQL、注解处理器生成的代码都不在静态文本里，索引看不到。
- **单文件解析失败不中断**：花括号不配平、文件超过 2 MB、疑似二进制等情况会跳过该文件，并记到 `parse_errors`；其余文件照常入索引。
- **`extends` / `implements` 保留原名去泛型**，跨模块继承链只到名字层面，不解析依赖 jar。
- **只扫源码目录**：默认 `src/main/java`，`target`、`build`、`node_modules` 等目录整体跳过；不处理 `src/test/java`。
- 索引是某一时刻的快照，源码改动后要重新 `build`（查询命令在索引缺失时会临时构建，但不会更新磁盘上的旧索引）。

## 配置

不需要配置文件，全部走命令行参数：

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--repo` | `.` | 仓库根目录 |
| `--src` | `src/main/java` | 源码目录，相对 `--repo` |
| `--out` | `.wbkit-out/java-index` | 索引输出目录，相对 `--repo` |

## 注意

- 全部输出 JSON，错误走 stderr 且退出码为 2（目录不存在、索引损坏、文件找不到、查询为空）。
- 纯只读：只写 `--out` 下的 `index.json`，不改源码、不碰 git。
- `impact` 只认显式 `import`、`extends` / `implements`、同一接口的兄弟实现、以及同名方法调用；同包未 import、反射注入等会漏，结果里的 `note` 会提示。
- 建议在 CI 或本地先 `build` 一次，后续查询直接复用，避免每次临时解析整仓。

## 依赖

- `python3`（3.8+）
- 只用标准库
