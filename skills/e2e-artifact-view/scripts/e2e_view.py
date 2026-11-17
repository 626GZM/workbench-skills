#!/usr/bin/env python3
"""e2e 运行产物只读视图。

只读产物目录，不跑测试、不连内部系统、不登录：
  <root>/<运行目录>/run.json
  <root>/<运行目录>/snaps/*.png
  <root>/<运行目录>/logs/*.log

子命令：
  runs     列出运行记录（时间、环境、状态、通过/失败数、耗时）
  show     一次运行的详情：步骤、用例、失败原因、证据路径
  cases    用例级明细，可按状态过滤
  summary  聚合统计：通过率趋势、失败最多的用例、失败原因归类
  accounts 测试账号配置的脱敏视图（密码显示为 ***）
  mask     把手机号/身份证/邮箱/token 打码后输出

输出一律 JSON；不改任何产物文件。
"""
import argparse
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path


class ViewError(Exception):
    """产物读不了、参数不合法等可预期错误。"""


def emit(data):
    print(json.dumps(data, ensure_ascii=False, indent=2))


# ---------- 状态归一 ----------

STATUS_ALIASES = {
    'passed': 'passed', 'pass': 'passed', 'ok': 'passed', 'success': 'passed',
    'succeeded': 'passed', '通过': 'passed', '成功': 'passed',
    'failed': 'failed', 'fail': 'failed', 'failure': 'failed', 'error': 'failed',
    'broken': 'failed', '失败': 'failed', '不通过': 'failed',
    'skipped': 'skipped', 'skip': 'skipped', 'pending': 'skipped',
    'blocked': 'skipped', '跳过': 'skipped',
}


def norm_status(value):
    """把各种状态写法归成 passed / failed / skipped / unknown。"""
    if value is None:
        return 'unknown'
    return STATUS_ALIASES.get(str(value).strip().lower(), 'unknown')


def derive_status(cases):
    """没有显式状态时按用例结果推导。"""
    statuses = [c['status'] for c in cases]
    if 'failed' in statuses:
        return 'failed'
    if 'passed' in statuses:
        return 'passed'
    if 'skipped' in statuses:
        return 'skipped'
    return 'unknown'


# ---------- 时间 ----------

def parse_ts(value):
    """解析时间：epoch 数字、ISO、'YYYY-MM-DD HH:MM:SS'；统一成本地 naive datetime。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value)
        except (OSError, OverflowError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r'\d{10}(?:\.\d+)?', text):
        return datetime.fromtimestamp(float(text))
    if text.endswith(('Z', 'z')):
        text = text[:-1] + '+00:00'
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ('%Y-%m-%d %H:%M:%S', '%Y/%m/%d %H:%M:%S', '%Y-%m-%d'):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


def fmt_dt(dt):
    return dt.strftime('%Y-%m-%d %H:%M:%S') if dt else None


NAME_PATTERNS = (
    (re.compile(r'^(\d{4}-\d{2}-\d{2})-(\d{6})'), '%Y-%m-%d%H%M%S'),
    (re.compile(r'^(\d{8})-(\d{6})'), '%Y%m%d%H%M%S'),
)


def time_from_name(name):
    """从运行目录名推断开始时间；认 2026-11-17-101500 和 20261117-101500。"""
    for pat, fmt in NAME_PATTERNS:
        m = pat.match(name)
        if m:
            try:
                return datetime.strptime(m.group(1) + m.group(2), fmt)
            except ValueError:
                continue
    return None


def parse_since(text, now=None):
    """'7d' / '24h' / '30m' / '2w' 或具体日期，返回 cutoff；空串返回 None。"""
    text = (text or '').strip()
    if not text:
        return None
    now = now or datetime.now()
    m = re.fullmatch(r'(\d+)\s*([mhdw])', text, re.I)
    if m:
        unit = {'m': 60, 'h': 3600, 'd': 86400, 'w': 7 * 86400}[m.group(2).lower()]
        return now - timedelta(seconds=int(m.group(1)) * unit)
    dt = parse_ts(text)
    if dt:
        return dt
    raise ViewError('看不懂的时间：%s（示例：7d / 24h / 2026-11-01）' % text)


# ---------- 读产物 ----------

def read_run(run_dir):
    """读一次运行的 run.json，返回 (目录, 数据)。"""
    d = Path(run_dir).expanduser()
    f = d / 'run.json'
    if not f.is_file():
        raise ViewError('没有 run.json：%s' % d)
    try:
        data = json.loads(f.read_text(encoding='utf-8'))
    except (OSError, ValueError) as e:
        raise ViewError('run.json 读不了：%s（%s）' % (f, e))
    if not isinstance(data, dict):
        raise ViewError('run.json 顶层要是一个对象：%s' % f)
    return d, data


def iter_run_dirs(root):
    """根目录下带 run.json 的子目录，按名字排序。"""
    root = Path(root).expanduser()
    if not root.is_dir():
        raise ViewError('目录不存在：%s' % root)
    out = []
    for d in sorted(root.iterdir()):
        if d.name.startswith('.') or not d.is_dir():
            continue
        if (d / 'run.json').is_file():
            out.append(d)
    return out


def norm_cases(raw, run_dir):
    """把一个步骤的用例列表整理成统一结构，证据路径检查是否存在。"""
    out = []
    if not isinstance(raw, list):
        return out
    for i, c in enumerate(raw):
        if not isinstance(c, dict):
            continue
        evid = c.get('evidence') or []
        if isinstance(evid, str):
            evid = [evid]
        evidence, missing = [], []
        for item in evid:
            if not isinstance(item, str) or not item.strip():
                continue
            rel = item.strip()
            p = Path(rel)
            target = p if p.is_absolute() else run_dir / p
            (evidence if target.exists() else missing).append(rel)
        out.append({
            'id': str(c.get('id') or 'case-%02d' % (i + 1)),
            'title': str(c.get('title') or ''),
            'status': norm_status(c.get('status')),
            'error': str(c.get('error') or '').strip(),
            'evidence': evidence,
            'missing_evidence': missing,
        })
    return out


def norm_steps(run_dir, data):
    """整理步骤；没有 steps 但顶层有 cases 时当成一个「全部」步骤。"""
    raw = data.get('steps')
    if not isinstance(raw, list) or not raw:
        if isinstance(data.get('cases'), list):
            raw = [{'name': '全部', 'status': data.get('status'), 'cases': data['cases']}]
        else:
            raw = []
    steps = []
    for i, s in enumerate(raw):
        if not isinstance(s, dict):
            continue
        cases = norm_cases(s.get('cases'), run_dir)
        status = norm_status(s.get('status'))
        if status == 'unknown':
            status = derive_status(cases)
        steps.append({
            'name': str(s.get('name') or 'step-%02d' % (i + 1)),
            'status': status,
            'cases': cases,
        })
    return steps


def count_cases(cases):
    counts = {'total': len(cases), 'passed': 0, 'failed': 0, 'skipped': 0, 'unknown': 0}
    for c in cases:
        counts[c['status']] = counts.get(c['status'], 0) + 1
    return counts


def pass_rate(counts):
    """通过率 = passed / (passed + failed)；全跳过或没用例时 None。"""
    executed = counts['passed'] + counts['failed']
    return round(counts['passed'] / executed, 4) if executed else None


def run_started(d, data):
    dt = parse_ts(data.get('started_at')) or time_from_name(d.name)
    if dt:
        return dt
    try:
        return datetime.fromtimestamp(d.stat().st_mtime)
    except OSError:
        return None


def run_view(d, data):
    """运行级别的摘要：环境、状态、时间、耗时、计数、通过率。"""
    steps = norm_steps(d, data)
    cases = [c for s in steps for c in s['cases']]
    counts = count_cases(cases)
    status = norm_status(data.get('status'))
    if status not in ('passed', 'failed', 'skipped'):
        status = derive_status(cases)
    started = run_started(d, data)
    finished = parse_ts(data.get('finished_at'))
    duration = data.get('duration_seconds')
    if not isinstance(duration, (int, float)) or isinstance(duration, bool):
        duration = int((finished - started).total_seconds()) if (started and finished) else None
    return {
        'id': d.name,
        'path': str(d.resolve()),
        'env': str(data.get('env') or 'local'),
        'status': status,
        'started_at': fmt_dt(started),
        'finished_at': fmt_dt(finished),
        'time': int(started.timestamp()) if started else None,
        'duration_seconds': int(duration) if duration is not None else None,
        'counts': counts,
        'pass_rate': pass_rate(counts),
    }


def list_runs(root, env=None, limit=20):
    """列出运行，新的在前；limit <= 0 表示全部。"""
    rows = []
    for d in iter_run_dirs(root):
        try:
            _, data = read_run(d)
        except ViewError:
            continue
        view = run_view(d, data)
        if env and view['env'] != env:
            continue
        rows.append(view)
    rows.sort(key=lambda r: r['time'] or 0, reverse=True)
    if limit and limit > 0:
        rows = rows[:limit]
    return rows


# ---------- 失败归类 ----------

CATEGORIES = ('超时', '断言失败', '环境错误', '数据问题', '未知')
CATEGORY_RULES = (
    ('超时', re.compile(r'timeout|timed\s*out|超时|deadline|ETIMEDOUT', re.I)),
    ('断言失败', re.compile(r'assert|\bexpected\b|期望|断言|mismatch', re.I)),
    ('环境错误', re.compile(r'connect|refused|ECONN|network|网络|DNS|unavailable|'
                          r'\b50[234]\b|upstream', re.I)),
    ('数据问题', re.compile(r'数据|不存在|not\s+found|\b404\b|duplicate|重复|invalid', re.I)),
)


def classify(error):
    """按关键词把失败原因归到五类，先命中先算。"""
    text = error or ''
    for label, pat in CATEGORY_RULES:
        if pat.search(text):
            return label
    return '未知'


# ---------- 打码 ----------

MASK_RULES = (
    ('phone', re.compile(r'(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)'), r'\1****\2'),
    ('id_card', re.compile(r'(?<![\dXx])(\d{6})\d{8}(\d{3}[\dXx])(?![\dXx])'), r'\1********\2'),
    ('email', re.compile(r'([\w.+-]{1,2})[\w.+-]*@([\w-]+(?:\.[\w-]+)+)'), r'\1***@\2'),
    ('token', re.compile(r'(?i)\b(bearer)\s+[A-Za-z0-9._\-]{8,}'), r'\1 ***'),
    ('token', re.compile(r'(?i)\b(token|secret|password|passwd|api[_-]?key|authorization|'
                         r'access[_-]?token)(["\']?\s*[:=]\s*["\']?)[A-Za-z0-9._\-/+]{6,}'),
     r'\1\2***'),
)


def mask_text(text):
    """打码手机号、身份证、邮箱、token。返回 (打码后文本, 各类数量)。"""
    counts = {'phone': 0, 'id_card': 0, 'email': 0, 'token': 0}
    out = text or ''
    for kind, pat, rep in MASK_RULES:
        out, n = pat.subn(rep, out)
        counts[kind] += n
    return out, counts


# ---------- 账号配置 ----------

def accounts_view(path):
    """测试账号的脱敏视图：只给代号、名称、用途、用户名、环境，密码固定 ***。"""
    p = Path(path).expanduser()
    if not p.is_file():
        raise ViewError('配置文件不存在：%s' % p)
    try:
        data = json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError) as e:
        raise ViewError('配置文件读不了：%s（%s）' % (p, e))
    if not isinstance(data, dict):
        raise ViewError('配置文件顶层要是一个对象：%s' % p)

    rows = {}

    def add(code, rec, env_label):
        rec = rec if isinstance(rec, dict) else {}
        row = rows.setdefault(code, {
            'code': code,
            'name': rec.get('name') or code,
            'purpose': rec.get('purpose') or rec.get('role') or rec.get('why') or '',
            'account': '',
            'envs': [],
            'password': '',
            'auth': 'none',
        })
        login = rec.get('login') if isinstance(rec.get('login'), dict) else {}
        if login.get('account'):
            row['account'] = str(login['account'])
        if login.get('password'):
            row['password'] = '***'
            row['auth'] = 'password'
        if rec.get('token') or rec.get('authorization'):
            row['auth'] = 'token' if row['auth'] == 'none' else row['auth']
        if env_label not in row['envs']:
            row['envs'].append(env_label)

    for code, rec in (data.get('identities') or {}).items():
        add(str(code), rec, '共用')
    for env, group in (data.get('environments') or {}).items():
        group = group if isinstance(group, dict) else {}
        for code, rec in (group.get('identities') or {}).items():
            add(str(code), rec, str(env))

    ordered = sorted(rows.values(), key=lambda r: (r['code'], r['envs']))
    return {'config': str(p.resolve()), 'count': len(ordered), 'identities': ordered}


# ---------- 子命令 ----------

def cmd_runs(a):
    rows = list_runs(a.root, env=a.env or None, limit=a.limit)
    emit({'root': str(Path(a.root).expanduser().resolve()), 'count': len(rows), 'runs': rows})


def cmd_show(a):
    d, data = read_run(a.run)
    view = run_view(d, data)
    steps = norm_steps(d, data)
    out_steps = []
    for s in steps:
        out_steps.append({
            'name': s['name'],
            'status': s['status'],
            'counts': count_cases(s['cases']),
            'cases': s['cases'],
        })
    emit({'run': view, 'steps': out_steps})


def cmd_cases(a):
    d, data = read_run(a.run)
    wanted = norm_status(a.status) if a.status else ''
    if a.status and wanted == 'unknown':
        raise ViewError('状态只能是 passed / failed / skipped / unknown：%s' % a.status)
    rows = []
    for s in norm_steps(d, data):
        for c in s['cases']:
            if wanted and c['status'] != wanted:
                continue
            rows.append({
                'step': s['name'],
                'id': c['id'],
                'title': c['title'],
                'status': c['status'],
                'error': c['error'],
                'evidence': c['evidence'],
                'missing_evidence': c['missing_evidence'],
            })
    emit({'run': d.name, 'status_filter': a.status or '', 'count': len(rows), 'cases': rows})


def summarize(root, since='7d', top=5, now=None):
    """聚合统计：通过率趋势、失败最多的用例、失败原因归类。"""
    now = now or datetime.now()
    cutoff = parse_since(since, now)
    picked = []
    for d in iter_run_dirs(root):
        try:
            _, data = read_run(d)
        except ViewError:
            continue
        view = run_view(d, data)
        if cutoff and (view['time'] is None or datetime.fromtimestamp(view['time']) < cutoff):
            continue
        picked.append((view, norm_steps(d, data)))

    totals = {'total': 0, 'passed': 0, 'failed': 0, 'skipped': 0, 'unknown': 0}
    trend = {}
    failures = []
    for view, steps in picked:
        cases = [c for s in steps for c in s['cases']]
        counts = count_cases(cases)
        for k in totals:
            totals[k] += counts[k]
        day = (datetime.fromtimestamp(view['time']).strftime('%Y-%m-%d')
               if view['time'] else '未知')
        row = trend.setdefault(day, {'date': day, 'runs': 0, 'total': 0,
                                     'passed': 0, 'failed': 0, 'skipped': 0, 'unknown': 0})
        row['runs'] += 1
        for k in ('total', 'passed', 'failed', 'skipped', 'unknown'):
            row[k] += counts[k]
        for s in steps:
            for c in s['cases']:
                if c['status'] == 'failed':
                    failures.append((view, s, c))

    trend_rows = []
    for day in sorted(trend):
        row = trend[day]
        trend_rows.append({
            'date': row['date'],
            'runs': row['runs'],
            'total': row['total'],
            'passed': row['passed'],
            'failed': row['failed'],
            'skipped': row['skipped'],
            'unknown': row['unknown'],
            'pass_rate': pass_rate(row),
        })

    grouped = {}
    for view, s, c in failures:
        key = (c['id'], c['title'])
        row = grouped.setdefault(key, {
            'id': c['id'], 'title': c['title'], 'step': s['name'],
            'failed': 0, 'last_seen': None, '_ts': None,
        })
        row['failed'] += 1
        if view['time'] and (row['_ts'] is None or view['time'] > row['_ts']):
            row['_ts'] = view['time']
            row['last_seen'] = view['started_at']
    top_rows = sorted(grouped.values(), key=lambda r: (-r['failed'], -(r['_ts'] or 0)))[:max(top, 0)]
    for row in top_rows:
        row.pop('_ts', None)

    buckets = {label: {'count': 0, 'samples': []} for label in CATEGORIES}
    for _, _, c in failures:
        label = classify(c['error'])
        bucket = buckets.setdefault(label, {'count': 0, 'samples': []})
        bucket['count'] += 1
        sample = ' '.join(c['error'].split())[:120]
        if sample and sample not in bucket['samples'] and len(bucket['samples']) < 3:
            bucket['samples'].append(sample)
    total_failed = len(failures)
    categories = [{
        'category': label,
        'count': buckets[label]['count'],
        'ratio': round(buckets[label]['count'] / total_failed, 4) if total_failed else 0.0,
        'samples': buckets[label]['samples'],
    } for label in CATEGORIES]

    return {
        'root': str(Path(root).expanduser().resolve()),
        'since': since,
        'cutoff': fmt_dt(cutoff),
        'runs': len(picked),
        'counts': totals,
        'pass_rate': pass_rate(totals),
        'trend': trend_rows,
        'top_failed_cases': top_rows,
        'failure_categories': categories,
    }


def cmd_summary(a):
    emit(summarize(a.root, since=a.since, top=a.top))


def cmd_accounts(a):
    emit(accounts_view(a.config))


def cmd_mask(a):
    p = Path(a.file).expanduser()
    if not p.is_file():
        raise ViewError('文件不存在：%s' % p)
    try:
        text = p.read_text(encoding='utf-8', errors='replace')
    except OSError as e:
        raise ViewError('文件读不了：%s（%s）' % (p, e))
    masked, counts = mask_text(text)
    emit({'file': str(p.resolve()), 'changed': masked != text, 'counts': counts, 'text': masked})


def main(argv=None):
    p = argparse.ArgumentParser(description='e2e 运行产物只读视图，输出 JSON')
    sub = p.add_subparsers(dest='cmd', required=True)

    s = sub.add_parser('runs', help='列出运行记录（新的在前）')
    s.add_argument('--root', required=True, help='产物根目录')
    s.add_argument('--env', default='', help='只看某个环境，比如 local / dev / test')
    s.add_argument('--limit', type=int, default=20, help='最多几条，0 表示全部')
    s.set_defaults(fn=cmd_runs)

    s = sub.add_parser('show', help='一次运行的详情')
    s.add_argument('--run', required=True, help='运行目录（含 run.json）')
    s.set_defaults(fn=cmd_show)

    s = sub.add_parser('cases', help='用例级明细')
    s.add_argument('--run', required=True, help='运行目录（含 run.json）')
    s.add_argument('--status', default='', help='passed / failed / skipped / unknown')
    s.set_defaults(fn=cmd_cases)

    s = sub.add_parser('summary', help='聚合统计：通过率趋势、失败最多的用例、失败归类')
    s.add_argument('--root', required=True, help='产物根目录')
    s.add_argument('--since', default='7d', help='时间窗口：7d / 24h / 2026-11-01')
    s.add_argument('--top', type=int, default=5, help='失败最多的用例取前几条')
    s.set_defaults(fn=cmd_summary)

    s = sub.add_parser('accounts', help='测试账号配置的脱敏视图')
    s.add_argument('--config', required=True, help='账号配置文件（JSON）')
    s.set_defaults(fn=cmd_accounts)

    s = sub.add_parser('mask', help='把手机号/身份证/邮箱/token 打码后输出')
    s.add_argument('--file', required=True, help='要打码的文件')
    s.set_defaults(fn=cmd_mask)

    a = p.parse_args(argv)
    try:
        a.fn(a)
    except ViewError as e:
        print(json.dumps({'error': str(e)}, ensure_ascii=False), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
