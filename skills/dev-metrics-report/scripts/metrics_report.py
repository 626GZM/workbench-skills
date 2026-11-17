#!/usr/bin/env python3
"""开发度量与用量统计：从本地 runs.jsonl 和 git 仓库算开发效率指标。

只读本地记录，不连接任何内部系统。除 report 输出 Markdown 外，其余子命令输出 JSON。
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / '_lib'))

from wbkit import git  # noqa: E402

DEFAULT_ROLE = '未标注'
DEFAULT_ENGINE = '未标注'
ROLE_SLOW_FACTOR = 2.0      # 某环节平均耗时超过总体平均的倍数阈值
FAIL_RATE_LIMIT = 0.20      # 失败率阈值
CACHE_RATIO_LIMIT = 0.80    # 缓存读占总输入的比例阈值
DAY_SECONDS_LIMIT = 8 * 3600  # 单日累计耗时阈值（秒）


class MetricsError(Exception):
    """输入不合法或数据读不了。"""


def emit(data):
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _naive(t):
    """统一成不带时区的本地时间，避免带时区和不带时区的记录相减报错。"""
    if t is None:
        return None
    if t.tzinfo is not None:
        return t.astimezone().replace(tzinfo=None)
    return t


def parse_bound(text, end=False):
    """解析 --since / --until。

    只给日期（YYYY-MM-DD）时：since 取当天 0 点，until 取当天 23:59:59（含当天）；
    给完整 ISO 时间时按原样。统一转成本地无时区时间。
    """
    if not text:
        return None
    text = str(text).strip()
    try:
        if len(text) == 10:
            t = datetime.strptime(text, '%Y-%m-%d')
            return t.replace(hour=23, minute=59, second=59) if end else t
        return _naive(datetime.fromisoformat(text.replace('Z', '+00:00')))
    except ValueError:
        raise MetricsError('时间格式不对：%r（要 2026-11-01 或 ISO 时间）' % text)


def period(since=None, until=None):
    """把输入的时间边界回显成规范格式，便于调用方核对。"""
    s, u = parse_bound(since), parse_bound(until, end=True)
    return {'since': s.isoformat() if s else None, 'until': u.isoformat() if u else None}


def _parse_at(text):
    if not text:
        return None
    try:
        return _naive(datetime.fromisoformat(str(text).replace('Z', '+00:00')))
    except ValueError:
        return None


def _num(value):
    """把数字字段转成非负浮点；缺失或非法按 0 处理。"""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return 0.0
    return n if n > 0 else 0.0


def _normalize(raw, at):
    return {
        'at': at,
        'role': str(raw.get('role') or DEFAULT_ROLE),
        'engine': str(raw.get('engine') or DEFAULT_ENGINE),
        'ok': bool(raw.get('ok', True)),
        'seconds': _num(raw.get('seconds')),
        'tokens_in': int(_num(raw.get('tokens_in'))),
        'tokens_out': int(_num(raw.get('tokens_out'))),
        'cache_read': int(_num(raw.get('cache_read'))),
    }


def load_runs(path, since=None, until=None):
    """读 runs.jsonl，按时间过滤；坏行和没有合法 at 的行直接跳过。"""
    p = Path(path).expanduser()
    if not p.is_file():
        raise MetricsError('runs 文件不存在：%s' % path)
    since_t, until_t = parse_bound(since), parse_bound(until, end=True)
    rows = []
    for line in p.read_text(encoding='utf-8', errors='replace').splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except ValueError:
            continue
        if not isinstance(raw, dict):
            continue
        at = _parse_at(raw.get('at'))
        if at is None:
            continue
        if since_t and at < since_t:
            continue
        if until_t and at > until_t:
            continue
        rows.append(_normalize(raw, at))
    rows.sort(key=lambda r: r['at'])
    return rows


def _group(rows, key):
    """按 role / engine 聚合：次数、耗时、平均耗时、失败数。"""
    buckets = {}
    for r in rows:
        name = r[key]
        b = buckets.setdefault(name, {'count': 0, 'seconds': 0.0, 'failed': 0})
        b['count'] += 1
        b['seconds'] += r['seconds']
        if not r['ok']:
            b['failed'] += 1
    out = []
    for name, b in buckets.items():
        out.append({
            key: name,
            'count': b['count'],
            'seconds': round(b['seconds'], 1),
            'hours': round(b['seconds'] / 3600, 2),
            'avg_seconds': round(b['seconds'] / b['count'], 1),
            'failed': b['failed'],
        })
    out.sort(key=lambda x: (-x['seconds'], x[key]))
    return out


def _cache_top(rows, limit=5):
    """按 role 汇总缓存读占比，取前 limit 个。占比分母是 tokens_in + cache_read。"""
    buckets = {}
    for r in rows:
        b = buckets.setdefault(r['role'], {'cache_read': 0, 'tokens_in': 0})
        b['cache_read'] += r['cache_read']
        b['tokens_in'] += r['tokens_in']
    out = []
    for role, b in buckets.items():
        total_input = b['tokens_in'] + b['cache_read']
        out.append({
            'role': role,
            'cache_read': b['cache_read'],
            'total_input': total_input,
            'ratio': round(b['cache_read'] / total_input, 4) if total_input else 0.0,
        })
    out.sort(key=lambda x: (-x['ratio'], -x['cache_read'], x['role']))
    return out[:limit]


def summarize(rows):
    """总次数、成功率、总耗时、按 role / engine 聚合、缓存读 top 5。"""
    total = len(rows)
    ok = sum(1 for r in rows if r['ok'])
    total_seconds = sum(r['seconds'] for r in rows)
    tokens_in = sum(r['tokens_in'] for r in rows)
    tokens_out = sum(r['tokens_out'] for r in rows)
    cache_read = sum(r['cache_read'] for r in rows)
    total_input = tokens_in + cache_read
    return {
        'total': total,
        'ok': ok,
        'failed': total - ok,
        'success_rate': round(ok / total, 4) if total else 0.0,
        'total_seconds': round(total_seconds, 1),
        'total_hours': round(total_seconds / 3600, 2),
        'avg_seconds': round(total_seconds / total, 1) if total else 0.0,
        'tokens': {
            'in': tokens_in,
            'out': tokens_out,
            'cache_read': cache_read,
            'total_input': total_input,
        },
        'cache_ratio': round(cache_read / total_input, 4) if total_input else 0.0,
        'by_role': _group(rows, 'role'),
        'by_engine': _group(rows, 'engine'),
        'cache_read_top': _cache_top(rows),
    }


def _bucket(at, by):
    if by == 'week':
        year, week, _ = at.isocalendar()
        return '%04d-W%02d' % (year, week)
    return at.strftime('%Y-%m-%d')


def build_trend(rows, by='day'):
    """时间序列：按天或按 ISO 周聚合次数、耗时、失败数。"""
    buckets = {}
    for r in rows:
        name = _bucket(r['at'], by)
        b = buckets.setdefault(name, {'count': 0, 'seconds': 0.0, 'failed': 0})
        b['count'] += 1
        b['seconds'] += r['seconds']
        if not r['ok']:
            b['failed'] += 1
    points = []
    for name in sorted(buckets):
        b = buckets[name]
        points.append({
            'bucket': name,
            'count': b['count'],
            'seconds': round(b['seconds'], 1),
            'hours': round(b['seconds'] / 3600, 2),
            'failed': b['failed'],
        })
    return {'by': by, 'points': points}


def _git_time_args(since, until):
    args = []
    if since:
        args.append('--since=%s' % parse_bound(since).strftime('%Y-%m-%dT%H:%M:%S'))
    if until:
        args.append('--until=%s' % parse_bound(until, end=True).strftime('%Y-%m-%dT%H:%M:%S'))
    return args


def git_stats(repo, since=None, until=None, author=None):
    """git 统计：提交数、增删行、活跃天数、每日提交曲线、按作者统计。"""
    root = git.repo_root(str(Path(repo).expanduser()))
    if not root:
        raise MetricsError('不是 git 仓库：%s' % repo)
    args = ['log', '--pretty=format:%x1e%H%x1f%an%x1f%ad', '--date=short', '--numstat']
    args.extend(_git_time_args(since, until))
    if author:
        args.append('--author=%s' % author)
    # 空仓库里 git log 会非 0 退出，这里按“没有提交”处理
    text = git.git(root, *args, check=False) or ''

    commits = 0
    added = 0
    deleted = 0
    days = {}
    authors = {}
    for rec in text.split('\x1e'):
        if not rec.strip():
            continue
        lines = rec.strip('\n').splitlines()
        if not lines:
            continue
        meta = lines[0].split('\x1f')
        if len(meta) != 3:
            continue
        _, name, day = meta
        commits += 1
        days[day] = days.get(day, 0) + 1
        au = authors.setdefault(name, {'author': name, 'commits': 0, 'added': 0,
                                      'deleted': 0, 'days': set()})
        au['commits'] += 1
        au['days'].add(day)
        for line in lines[1:]:
            parts = line.split('\t')
            if len(parts) != 3:
                continue
            add_text, del_text, _ = parts
            if add_text == '-' or del_text == '-':  # 二进制文件没有行数
                continue
            try:
                a, d = int(add_text), int(del_text)
            except ValueError:
                continue
            added += a
            deleted += d
            au['added'] += a
            au['deleted'] += d

    by_author = [{
        'author': v['author'],
        'commits': v['commits'],
        'added': v['added'],
        'deleted': v['deleted'],
        'active_days': len(v['days']),
    } for v in authors.values()]
    by_author.sort(key=lambda x: (-x['commits'], x['author']))

    return {
        'repo': root,
        'period': period(since, until),
        'author': author or None,
        'commits': commits,
        'added': added,
        'deleted': deleted,
        'changed_lines': added + deleted,
        'active_days': len(days),
        'daily': [{'date': d, 'commits': c} for d, c in sorted(days.items())],
        'by_author': by_author,
    }


def anomaly_signals(rows):
    """异常信号：规则写死，每条都带数值和阈值，方便解释。"""
    signals = []
    total = len(rows)
    if not total:
        return signals

    overall = sum(r['seconds'] for r in rows) / total
    # 1. 某环节平均耗时超过总体平均的 2 倍
    for g in _group(rows, 'role'):
        if overall > 0 and g['avg_seconds'] > overall * ROLE_SLOW_FACTOR:
            signals.append({
                'rule': 'role_avg_over_2x',
                'role': g['role'],
                'detail': '「%s」平均耗时 %.0f 秒，是总体平均 %.0f 秒的 %.1f 倍（阈值 2 倍）'
                          % (g['role'], g['avg_seconds'], overall, g['avg_seconds'] / overall),
                'value': g['avg_seconds'],
                'threshold': round(overall * ROLE_SLOW_FACTOR, 1),
            })

    # 2. 总体失败率超过 20%
    failed = sum(1 for r in rows if not r['ok'])
    rate = failed / total
    if rate > FAIL_RATE_LIMIT:
        signals.append({
            'rule': 'failure_rate_over_20',
            'detail': '失败率 %.1f%%（%d/%d，阈值 20%%）' % (rate * 100, failed, total),
            'value': round(rate, 4),
            'threshold': FAIL_RATE_LIMIT,
        })

    # 3. 缓存读占总输入超过 80%
    tokens_in = sum(r['tokens_in'] for r in rows)
    cache_read = sum(r['cache_read'] for r in rows)
    total_input = tokens_in + cache_read
    if total_input and cache_read / total_input > CACHE_RATIO_LIMIT:
        ratio = cache_read / total_input
        signals.append({
            'rule': 'cache_ratio_over_80',
            'detail': '缓存读占总输入 %.1f%%（%d/%d，阈值 80%%）'
                      % (ratio * 100, cache_read, total_input),
            'value': round(ratio, 4),
            'threshold': CACHE_RATIO_LIMIT,
        })

    # 4. 单日累计耗时超过 8 小时
    day_seconds = {}
    for r in rows:
        day = r['at'].date()
        day_seconds[day] = day_seconds.get(day, 0.0) + r['seconds']
    for day in sorted(day_seconds):
        seconds = day_seconds[day]
        if seconds > DAY_SECONDS_LIMIT:
            signals.append({
                'rule': 'day_over_8h',
                'date': day.isoformat(),
                'detail': '%s 累计耗时 %.1f 小时（阈值 8 小时）' % (day.isoformat(), seconds / 3600),
                'value': round(seconds / 3600, 2),
                'threshold': 8,
            })
    return signals


def _fmt_seconds(seconds):
    if seconds >= 3600:
        return '%.1f 小时' % (seconds / 3600)
    if seconds >= 60:
        return '%.0f 分钟' % (seconds / 60)
    return '%.0f 秒' % seconds


def _period_text(p):
    since, until = p.get('since'), p.get('until')
    if since and until:
        return '%s ~ %s' % (since[:10], until[:10])
    if since:
        return '%s 起' % since[:10]
    if until:
        return '截至 %s' % until[:10]
    return '全部记录'


def _suggestions(signals):
    rules = {s['rule'] for s in signals}
    out = []
    if 'role_avg_over_2x' in rules:
        slow = max((s for s in signals if s['rule'] == 'role_avg_over_2x'),
                   key=lambda s: s['value'])
        out.append('「%s」明显偏慢，考虑拆分任务、补充模板或换更合适的引擎。' % slow['role'])
    if 'failure_rate_over_20' in rules:
        out.append('失败率偏高，按环节看失败最集中的地方，先排查可复现的失败原因。')
    if 'cache_ratio_over_80' in rules:
        out.append('缓存读占比很高，留意上下文是否膨胀过度；过长会话可以分段重开。')
    if 'day_over_8h' in rules:
        out.append('存在单日投入超过 8 小时的记录，确认是否批量重跑或异常兜底。')
    if not out:
        out.append('本期指标平稳，保持当前节奏；继续积累记录后再看趋势。')
    return out


def render_report(rows, git_data=None, p=None):
    """生成给人看的 Markdown：概览 / 时间分布 / 最费环节 / 异常信号 / 建议。"""
    p = p or {'since': None, 'until': None}
    s = summarize(rows)
    signals = anomaly_signals(rows)
    lines = ['# 开发度量报告（%s）' % _period_text(p), '']

    lines.append('## 本期概览')
    lines.append('')
    lines.append('- 记录次数：%d 次，成功 %d 次，失败 %d 次，成功率 %.1f%%'
                 % (s['total'], s['ok'], s['failed'], s['success_rate'] * 100))
    if s['total']:
        lines.append('- 累计耗时：%s，平均每次 %s'
                     % (_fmt_seconds(s['total_seconds']), _fmt_seconds(s['avg_seconds'])))
        lines.append('- Token：输入 %s / 输出 %s / 缓存读 %s，缓存占输入 %.1f%%'
                     % (format(s['tokens']['in'], ','), format(s['tokens']['out'], ','),
                        format(s['tokens']['cache_read'], ','), s['cache_ratio'] * 100))
    else:
        lines.append('- 本期没有记录')
    if git_data:
        lines.append('- 代码提交：%d 次，新增 %d 行、删除 %d 行，活跃 %d 天'
                     % (git_data['commits'], git_data['added'], git_data['deleted'],
                        git_data['active_days']))
    lines.append('')

    lines.append('## 时间都花在哪')
    lines.append('')
    if s['by_role'] and s['total_seconds'] > 0:
        lines.append('| 环节 | 次数 | 耗时（小时） | 占比 |')
        lines.append('|---|---:|---:|---:|')
        for g in s['by_role']:
            share = g['seconds'] / s['total_seconds']
            lines.append('| %s | %d | %.2f | %.1f%% |'
                         % (g['role'], g['count'], g['hours'], share * 100))
    else:
        lines.append('本期没有可统计的耗时。')
    lines.append('')

    lines.append('## 哪些环节最费')
    lines.append('')
    if s['by_role']:
        slow = sorted(s['by_role'], key=lambda g: (-g['avg_seconds'], g['role']))[:5]
        for g in slow:
            lines.append('- %s：平均 %s/次，共 %d 次，失败 %d 次'
                         % (g['role'], _fmt_seconds(g['avg_seconds']), g['count'], g['failed']))
    else:
        lines.append('本期没有可比较的环节。')
    lines.append('')

    lines.append('## 异常信号')
    lines.append('')
    if signals:
        for sig in signals:
            lines.append('- %s' % sig['detail'])
    else:
        lines.append('- 未触发任何异常规则。')
    lines.append('')

    lines.append('## 建议')
    lines.append('')
    for tip in _suggestions(signals):
        lines.append('- %s' % tip)
    return '\n'.join(lines) + '\n'


def cmd_summary(a):
    rows = load_runs(a.runs, a.since, a.until)
    data = summarize(rows)
    data['runs'] = str(Path(a.runs).expanduser().resolve())
    data['period'] = period(a.since, a.until)
    emit(data)


def cmd_trend(a):
    rows = load_runs(a.runs, a.since, a.until)
    data = build_trend(rows, a.by)
    data['runs'] = str(Path(a.runs).expanduser().resolve())
    data['period'] = period(a.since, a.until)
    emit(data)


def cmd_git(a):
    emit(git_stats(a.repo, a.since, a.until, a.author or None))


def cmd_report(a):
    rows = load_runs(a.runs, a.since, a.until)
    git_data = git_stats(a.repo, a.since, a.until) if a.repo else None
    text = render_report(rows, git_data, period(a.since, a.until))
    if a.out:
        out = Path(a.out).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding='utf-8')
        print('已写入：%s' % out.resolve(), file=sys.stderr)
    print(text, end='')


def main(argv=None):
    p = argparse.ArgumentParser(
        description='开发度量与用量统计：从本地 runs.jsonl 和 git 仓库算指标，出周报/月报')
    sub = p.add_subparsers(dest='cmd', required=True)

    s = sub.add_parser('summary', help='汇总：次数、成功率、耗时、按 role/engine 聚合、缓存 top 5')
    s.add_argument('--runs', required=True, help='runs.jsonl 路径')
    s.add_argument('--since', default='', help='开始时间，如 2026-11-01')
    s.add_argument('--until', default='', help='结束时间；只给日期时含当天')
    s.set_defaults(fn=cmd_summary)

    s = sub.add_parser('trend', help='时间序列：按天或按周')
    s.add_argument('--runs', required=True, help='runs.jsonl 路径')
    s.add_argument('--by', choices=('day', 'week'), default='day')
    s.add_argument('--since', default='', help='开始时间，如 2026-11-01')
    s.add_argument('--until', default='', help='结束时间；只给日期时含当天')
    s.set_defaults(fn=cmd_trend)

    s = sub.add_parser('git', help='git 统计：提交数、增删行、活跃天数、按作者')
    s.add_argument('--repo', required=True, help='git 仓库目录')
    s.add_argument('--since', default='', help='开始时间，如 2026-11-01')
    s.add_argument('--until', default='', help='结束时间；只给日期时含当天')
    s.add_argument('--author', default='', help='只统计某个作者')
    s.set_defaults(fn=cmd_git)

    s = sub.add_parser('report', help='生成 Markdown 报告（概览/耗时分布/最费环节/异常信号/建议）')
    s.add_argument('--runs', required=True, help='runs.jsonl 路径')
    s.add_argument('--repo', default='', help='可选，附带 git 统计')
    s.add_argument('--since', default='', help='开始时间，如 2026-11-01')
    s.add_argument('--until', default='', help='结束时间；只给日期时含当天')
    s.add_argument('--out', default='', help='可选，把报告写到文件')
    s.set_defaults(fn=cmd_report)

    a = p.parse_args(argv)
    try:
        a.fn(a)
    except (MetricsError, git.GitError, OSError) as e:
        print(json.dumps({'error': str(e)}, ensure_ascii=False), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
