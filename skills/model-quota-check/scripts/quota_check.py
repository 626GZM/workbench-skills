#!/usr/bin/env python3
"""查各家模型还剩多少额度（只读，不花额度）。

- Kimi：Kimi Code 用量接口（5 小时窗口、月度）
- Codex：本机 ~/.codex/sessions 里最近的 rate_limits 事件（ChatGPT 订阅）
- DeepSeek：余额接口（按量付费）

key 从环境变量（KIMI_API_KEY / DEEPSEEK_API_KEY）或 ~/.config/workbench-skills/secrets.json 读。
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / '_lib'))

from wbkit import config as cfg  # noqa: E402

CODEX_SESSIONS = Path.home() / '.codex' / 'sessions'
NEAR_FULL = 98


def _get(url, key, timeout=15):
    req = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + key})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _ts(text):
    try:
        return datetime.fromisoformat(str(text).replace('Z', '+00:00')).timestamp()
    except ValueError:
        return None


def kimi(conf):
    key = cfg.api_key('kimi')
    if not key:
        return {'engine': 'kimi', 'ok': False, 'error': '没有 key（KIMI_API_KEY 或 secrets.json）'}
    url = ((conf.get('models') or {}).get('kimi') or {}).get('usage_url') or \
        'https://api.kimi.com/coding/v1/usages'
    try:
        u = _get(url, key).get('usages') or {}
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {'engine': 'kimi', 'ok': False, 'error': str(e)[:120]}
    names = {'limit_5h': '5 小时', 'limit_month_total': '本月总量', 'limit_month_code': '本月编码'}
    windows = [{'name': names.get(k, k), 'used_percent': round(float(v.get('used_ratio') or 0) * 100, 1),
                'resets_at': _ts(v.get('reset_time'))}
               for k, v in u.items() if isinstance(v, dict)]
    return {'engine': 'kimi', 'ok': True, 'windows': windows, 'source': 'Kimi Code 用量接口'}


def _find(obj, key):
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            r = _find(v, key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find(v, key)
            if r is not None:
                return r
    return None


def codex(conf):
    """最近的 Codex 会话里最后一条 rate_limits（Codex 每次请求都会带回来）。"""
    files = sorted(CODEX_SESSIONS.glob('*/*/*/*.jsonl'),
                   key=lambda p: p.stat().st_mtime, reverse=True)[:30] if CODEX_SESSIONS.exists() else []
    for f in files:
        last = None
        for line in f.read_text(encoding='utf-8', errors='replace').splitlines():
            if '"rate_limits"' not in line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            found = _find(ev, 'rate_limits')
            if isinstance(found, dict) and found.get('primary'):
                last = found
        if last:
            windows = []
            for k, name in (('primary', '5 小时'), ('secondary', '一周')):
                w = last.get(k) or {}
                if w:
                    used, reset = float(w.get('used_percent') or 0), w.get('resets_at')
                    if reset and reset < time.time():
                        used, reset = 0.0, None
                    windows.append({'name': name, 'used_percent': used, 'resets_at': reset})
            return {'engine': 'codex', 'ok': True, 'windows': windows, 'plan': last.get('plan_type'),
                    'as_of': f.stat().st_mtime, 'source': '本机 Codex 最近一次请求带回的额度'}
    return {'engine': 'codex', 'ok': False, 'error': '本机 Codex 会话里还没有额度记录（用一次 Codex 就有了）'}


def deepseek(conf):
    key = cfg.api_key('deepseek')
    if not key:
        return {'engine': 'deepseek', 'ok': False, 'error': '没有 key（DEEPSEEK_API_KEY 或 secrets.json）'}
    spec = (conf.get('models') or {}).get('deepseek') or {}
    url = spec.get('balance_url') or 'https://api.deepseek.com/user/balance'
    try:
        info = (_get(url, key).get('balance_infos') or [{}])[0]
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {'engine': 'deepseek', 'ok': False, 'error': str(e)[:120]}
    return {'engine': 'deepseek', 'ok': True, 'balance': float(info.get('total_balance') or 0),
            'currency': info.get('currency', 'CNY'), 'granted': info.get('granted_balance'),
            'topped_up': info.get('topped_up_balance'), 'source': 'DeepSeek 余额接口'}


FUNCS = {'kimi': kimi, 'codex': codex, 'deepseek': deepseek}


def collect(vendors, conf):
    out = {}
    for v in vendors:
        fn = FUNCS.get(v)
        if not fn:
            out[v] = {'engine': v, 'ok': False, 'error': '不支持：%s（可选 %s）' % (v, '/'.join(FUNCS))}
            continue
        try:
            out[v] = fn(conf)
        except Exception as e:  # 某一家查不到不影响其他家
            out[v] = {'engine': v, 'ok': False, 'error': str(e)[:120]}
    return out


def blocked(data):
    """现在不能用的引擎：{引擎: 恢复时间戳}。"""
    now, out = time.time(), {}
    for engine, q in data.items():
        if not q.get('ok'):
            continue
        for w in q.get('windows') or []:
            if w['used_percent'] >= NEAR_FULL and (w.get('resets_at') or 0) > now:
                out[engine] = max(out.get(engine, 0), w['resets_at'])
        if engine == 'deepseek' and q.get('balance', 1) < 0.5:
            out[engine] = now + 24 * 3600
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description='查各家模型剩余额度')
    p.add_argument('--vendor', default='kimi,codex,deepseek', help='逗号分隔')
    p.add_argument('--blocked', action='store_true', help='只输出现在不能用的引擎')
    a = p.parse_args(argv)
    conf = cfg.load()
    vendors = [v.strip() for v in a.vendor.split(',') if v.strip()]
    data = collect(vendors, conf)
    if a.blocked:
        print(json.dumps(blocked(data), ensure_ascii=False, indent=2))
    else:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
