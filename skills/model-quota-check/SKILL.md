---
name: model-quota-check
description: "查各家 AI 模型还剩多少额度：Kimi Code 的 5 小时/月度窗口、Codex 的订阅限额、DeepSeek 的账户余额。当用户问“还能不能跑”“哪家快用完了”“DeepSeek 还剩多少钱”，或者在批量派活前需要判断用哪家模型时使用。只读，不花额度。"
metadata:
  requires:
    bins: ["python3"]
---

# model-quota-check

一次性查清三家模型的剩余额度，输出 JSON。适合在批量调用模型前做一次前置检查。

## 使用场景

- 跑一批任务之前，先看哪家还有量
- 某次调用失败，判断是限流还是代码问题
- 想知道 DeepSeek 余额够不够这个月

## 用法

```bash
# 三家一起查
python3 scripts/quota_check.py

# 只查一家
python3 scripts/quota_check.py --vendor deepseek

# 只输出“现在不能用”的，附带恢复时间
python3 scripts/quota_check.py --blocked
```

## 输出

```json
{
  "kimi": {
    "engine": "kimi", "ok": true,
    "windows": [{"name": "5 小时", "used_percent": 42.5, "resets_at": 1763456789}],
    "source": "Kimi Code 用量接口"
  },
  "codex": {
    "engine": "codex", "ok": true,
    "windows": [{"name": "5 小时", "used_percent": 100.0, "resets_at": 1763456789}],
    "plan": "plus"
  },
  "deepseek": {
    "engine": "deepseek", "ok": true,
    "balance": 12.34, "currency": "CNY"
  }
}
```

`--blocked` 输出 `{"codex": 1763456789}` 这种形式上，键是不能用的引擎，值是预计恢复时间戳。

## 数据从哪来

| 引擎 | 来源 | 需要什么 |
|---|---|---|
| Kimi | `api.kimi.com` 用量接口 | `KIMI_API_KEY` |
| Codex | 本机 `~/.codex/sessions` 里最近一次请求带回的 `rate_limits` | 用过一次 Codex 登录态 |
| DeepSeek | `api.deepseek.com` 余额接口 | `DEEPSEEK_API_KEY` |

key 优先从环境变量读，其次从 `~/.config/workbench-skills/secrets.json`（权限 600）。

## 注意

- 只读，不消耗额度。
- 某一家的接口挂了不影响其他家，会单独返回 `ok: false`。
- Codex 的额度是本地记录，如果最近没用过 Codex，数据可能是旧的。

## 依赖

- `python3`（3.8+）
- 网络：访问各家的额度接口
- 无需第三方包
