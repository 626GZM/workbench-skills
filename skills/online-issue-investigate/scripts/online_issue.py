#!/usr/bin/env python3
"""线上告警只读排查：解析告警 → 查日志 → 准备线上代码 → 截堆栈代码 → AI 只读排查 → 出报告。

子命令：
  parse-alert  从告警原文里抠 env / traceId / 服务 / 时间 / 异常 / 关键词，输出 JSON
  create       建一个问题目录（<root>/OL-YYYYMMDD-01），自动解析并落 issue.json
  list         列问题
  investigate  查日志 → 准备只读代码 → 截堆栈代码 → AI 排查 → 写日志摘要 / 堆栈代码 / 排查报告
  show         看一个问题（issue.json + 报告正文）

日志查询是可插拔的：配 logs.query_cmd（命令模板，占位符 {query} / {query_quoted} / {minutes} / {env}），
没配就用 logs.files 里的本地日志文件做 grep 兜底。不绑定任何云厂商 SDK。

只读：不改代码、不提交、不连数据库；落盘的日志和告警原文一律先打码。
"""
import argparse
import glob
import json
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / '_lib'))

from wbkit import agent, config as cfg, git  # noqa: E402

DEFAULT_ROOT = './online-issues'
STATUSES = ('待排查', '排查中', '待处理', '已解决')
ENVS = ('prod', 'uat', 'test')
ENGINE_ORDER = ('claude', 'codex', 'deepseek', 'kimi')
REPORT_FIELDS = ('summary', 'confirmed', 'cause', 'evidence', 'impact', 'need_code_change',
                 'fix_plan', 'files', 'workaround', 'data_fix_sql', 'confidence')
ISSUE_RE = re.compile(r'^OL-\d{8}-\d{2,3}$')

# 环境关键词：按顺序匹配，先 prod 再 uat 再 test，避免“测试环境”抢了“生产”。
ENV_WORDS = {
    'prod': ('prod', 'production', 'prd', '生产', '线上'),
    'uat': ('uat', 'staging', 'stage', '预发', '预发布'),
    'test': ('test', 'testing', '测试', '联调'),
}

TRACE_RE = re.compile(r'\b[0-9a-f]{32}\b')
TRACE_KV_RE = re.compile(r'(?i)\btrace[_-]?id\b\s*[=:]\s*["\']?([0-9A-Za-z_-]{8,})')
EXC_RE = re.compile(r'\b((?:[a-z_]\w*\.)+[A-Z]\w*(?:Exception|Error))\b')
TIME_RE = re.compile(r'(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})')

# 日志行头：时间 + 一串方括号 + “- 内容”。各服务格式不一样，不按位置取值。
HEAD_RE = re.compile(r'^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)\s+((?:\[[^\]]*\]\s*)+?)-\s?(.*)', re.S)
LEVEL_RE = re.compile(r'^(?:[\w-]*\s*-\s*)?(TRACE|DEBUG|INFO|WARN|WARNING|ERROR|FATAL)$')
LOGGER_RE = re.compile(r'^((?:[a-z_]\w*\.)+[A-Z][\w$]*)(?::\s*\d*)?$')
# 堆栈帧：(OrderService.java:88) → 类名 + 行号
FRAME = re.compile(r'\(([A-Za-z_$][\w$]*)\.(?:java|kt|scala):(\d+)\)')
SRC_EXTS = ('.java', '.kt', '.scala')

# ---------- 打码 ----------

MASKS = [
    (re.compile(r'(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)'), r'\1****\2'),                         # 手机号
    (re.compile(r'(?<![\dXx])(\d{6})\d{8}(\d{3}[\dXx])(?![\dXx])'), r'\1********\2'),          # 身份证
    (re.compile(r'(?<!\d)(\d{4})\d{8,11}(\d{4})(?!\d)'), r'\1****\2'),                          # 银行卡
    (re.compile(r'([\w.+-]{1,2})[\w.+-]*@([\w-]+(?:\.[\w-]+)+)'), r'\1***@\2'),                  # 邮箱
    (re.compile(r'(?i)\b(bearer)\s+[A-Za-z0-9._\-]{8,}'), r'\1 ***'),                           # Bearer token
    (re.compile(r'(?i)\b(token|secret|password|passwd|api[_-]?key|authorization)'
                r'(["\']?\s*[:=]\s*["\']?)[A-Za-z0-9._\-/+]{8,}'), r'\1\2***'),                  # 各种 token
]


def mask(text):
    """脱敏：手机号、身份证、银行卡、邮箱、token。写文件、进提示词前都先过一遍。"""
    for pat, rep in MASKS:
        text = pat.sub(rep, text or '')
    return text


# ---------- 小工具 ----------

def now():
    return time.strftime('%Y-%m-%dT%H:%M:%S')


def emit(data):
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _split_list(value):
    """逗号 / 空格分隔的字符串或列表 → 去重后的列表。"""
    if not value:
        return []
    items = value if isinstance(value, (list, tuple)) else re.split(r'[,，\s]+', str(value))
    out = []
    for x in items:
        x = str(x).strip()
        if x and x not in out:
            out.append(x)
    return out


def _env_words(conf):
    words = {k: tuple(v) for k, v in ENV_WORDS.items()}
    for env, extra in (((conf or {}).get('logs') or {}).get('env_words') or {}).items():
        if extra:
            words[env] = tuple(extra)
    return words


def _read_text(path='', allow_stdin=True):
    if path:
        return Path(path).expanduser().read_text(encoding='utf-8')
    if allow_stdin and not sys.stdin.isatty():
        return sys.stdin.read()
    raise ValueError('没有内容：用 --alert 指定文件，或从 stdin 传入')


# ---------- 解析告警原文 ----------

def parse_alert(text, services=(), words=None):
    """从告警原文抠线索：环境、traceId、服务名、时间、异常类名、关键词。

    服务名只在候选列表（--services / 配置 services）里挑，避免把线程名、包名当成服务。
    抠不到的留空，由调用方补。
    """
    text = text or ''
    low = text.lower()
    words = words or ENV_WORDS

    env = ''
    for name in ('prod', 'uat', 'test'):
        for w in words.get(name) or ():
            w = str(w)
            if w.isascii():
                hit = re.search(r'(?<![a-z0-9])%s(?![a-z0-9])' % re.escape(w.lower()), low)
            else:
                hit = w in text
            if hit:
                env = name
                break
        if env:
            break

    traces = []
    for m in TRACE_RE.finditer(text):
        if m.group(0) not in traces:
            traces.append(m.group(0))
    for m in TRACE_KV_RE.finditer(text):
        val = m.group(1)
        if val.lower() not in ('null', 'none') and val not in traces:
            traces.append(val)

    svcs = []
    for s in _split_list(services):
        # 允许后面接 -pod 后缀，但前后不能是普通单词字符
        if re.search(r'(?<![\w-])%s(?![\w])' % re.escape(s), text, re.I) and s not in svcs:
            svcs.append(s)

    tstr, end = '', None
    times = TIME_RE.findall(text)
    if times:
        latest = max(times)
        tstr = '%s %s' % latest
        try:
            end = int(time.mktime(time.strptime(tstr, '%Y-%m-%d %H:%M:%S'))) + 300  # 最后一条往后放 5 分钟
        except ValueError:
            end = None

    excs = []
    for m in EXC_RE.finditer(text):
        name = m.group(1).rsplit('.', 1)[-1]
        if name not in excs:
            excs.append(name)

    keywords = excs[:2]
    if not keywords:
        m = re.search(r'(?:ERROR|Exception|异常|失败|timeout|超时)[^\n]{0,40}', text, re.I)
        if m:
            skip = {'ERROR', 'EXCEPTION', 'FAILED', 'FAIL', 'WARN', 'WARNING'}
            keywords = [w for w in re.findall(r'[一-龥]{2,}|[A-Za-z_][A-Za-z0-9_]{3,}', m.group(0))
                        if w.upper() not in skip][:2]

    return {'env': env, 'trace': traces[0] if traces else '', 'traces': traces[:5],
            'services': svcs[:5], 'time': tstr, 'end': end,
            'exceptions': excs[:5], 'keywords': keywords}


# ---------- 问题目录 / issue.json ----------

def root_of(root=None, conf=None):
    """问题根目录：--root 优先，其次配置 issues_root，最后 ./online-issues。"""
    value = root or (conf or {}).get('issues_root') or DEFAULT_ROOT
    return Path(str(value)).expanduser()


def issue_file(root, oid):
    if not ISSUE_RE.match(oid or ''):
        raise ValueError('不认识的问题编号：%s（应该像 OL-20260101-01）' % oid)
    return Path(root) / oid / 'issue.json'


def load_issue(root, oid):
    try:
        return json.loads(issue_file(root, oid).read_text(encoding='utf-8'))
    except FileNotFoundError:
        raise ValueError('没有这个问题：%s' % oid)
    except (OSError, ValueError) as e:
        raise ValueError('issue.json 读不了：%s（%s）' % (issue_file(root, oid), e))


def save_issue(root, issue):
    f = issue_file(root, issue.get('id'))
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix('.tmp')
    tmp.write_text(json.dumps(issue, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    tmp.replace(f)
    return issue


def _event(issue, status, note):
    issue['status'] = status
    issue.setdefault('log', []).append({'at': now(), 'status': status, 'note': note or ''})
    return issue


def list_issues(root):
    out = []
    for f in sorted(Path(root).glob('OL-*/issue.json'), reverse=True):
        try:
            issue = json.loads(f.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        report = issue.get('report') or {}
        out.append({'id': issue.get('id'), 'title': issue.get('title'), 'env': issue.get('env'),
                    'status': issue.get('status'), 'created_at': issue.get('created_at'),
                    'services': issue.get('services') or [], 'trace': issue.get('trace') or '',
                    'summary': report.get('summary') or '', 'error': issue.get('error') or ''})
    return out


def create_issue(root, title='', alert_text='', env='', services=(), minutes=0, detail='',
                 trace='', keywords=(), conf=None):
    """建一个问题目录，解析告警并落 issue.json（告警原文打码后一起存）。"""
    conf = conf or {}
    candidates = _split_list(services) or _split_list(conf.get('services'))
    parsed = parse_alert(alert_text, candidates, _env_words(conf)) if alert_text.strip() else {}

    env = (env or parsed.get('env') or 'prod').strip()
    if env not in ENVS:
        raise ValueError('环境只能是：' + ' / '.join(ENVS))
    svcs = _split_list(services) or parsed.get('services') or []
    kws = _split_list(keywords) or parsed.get('keywords') or []
    title = (title or '').strip() or (parsed.get('exceptions') or ['线上问题'])[0]
    minutes = int(minutes or (conf.get('logs') or {}).get('default_minutes') or 60)

    base = Path(root).expanduser()
    base.mkdir(parents=True, exist_ok=True)
    day = time.strftime('%Y%m%d')
    nums = []
    for p in base.glob('OL-%s-*' % day):
        tail = p.name.rsplit('-', 1)[-1]
        if tail.isdigit():
            nums.append(int(tail))
    oid = 'OL-%s-%02d' % (day, 1 + max(nums or [0]))
    d = base / oid
    d.mkdir(parents=True, exist_ok=True)

    alert_file = ''
    if alert_text.strip():
        (d / '告警原文.txt').write_text(mask(alert_text)[:200000], encoding='utf-8')
        alert_file = '告警原文.txt'

    issue = {
        'id': oid, 'title': title[:120], 'env': env, 'status': '待排查', 'created_at': now(),
        'minutes': minutes, 'trace': (trace or parsed.get('trace') or '').strip(),
        'services': svcs[:8], 'keywords': kws[:8], 'detail': (detail or '').strip()[:4000],
        'alert_file': alert_file, 'end': parsed.get('end'), 'parsed': parsed,
        'report': {}, 'log': [], 'error': '',
    }
    _event(issue, '待排查', '创建')
    save_issue(base, issue)
    return issue


# ---------- 日志查询（可插拔） ----------

def _fill(template, term, minutes, env):
    """填充查询模板。{query} 原样替换，{query_quoted} 做 shell 转义，模板自己决定用哪个。"""
    return (template.replace('{query_quoted}', shlex.quote(str(term)))
            .replace('{query}', str(term))
            .replace('{minutes}', str(int(minutes)))
            .replace('{env}', str(env)))


def _iter_log_files(paths):
    """展开日志路径：支持 ~、通配符、目录（递归找 *.log）。"""
    for raw in paths:
        p = Path(str(raw)).expanduser()
        text = str(p)
        if any(ch in text for ch in '*?['):
            for hit in sorted(glob.glob(text, recursive=True)):
                fp = Path(hit)
                if fp.is_file():
                    yield fp
        elif p.is_dir():
            for fp in sorted(p.rglob('*.log')):
                if fp.is_file():
                    yield fp
        elif p.is_file():
            yield p


def _grep_files(logs_conf, env, terms, limit, notes):
    """本地日志兜底：按关键词（不区分大小写）逐行匹配，命中行原样返回。"""
    paths = (logs_conf.get('files') or {}).get(env) or []
    files = list(_iter_log_files(paths))
    if not files:
        notes.append('%s 环境没配本地日志文件（logs.files.%s），日志部分为空' % (env, env))
        return
    lows = [t.lower() for t in terms if t]
    for fp in files:
        try:
            with open(fp, 'r', encoding='utf-8', errors='replace') as f:
                data = f.read(8 * 1024 * 1024)
        except OSError as e:
            notes.append('读不了 %s：%s' % (fp, e))
            continue
        for line in data.splitlines():
            if any(t in line.lower() for t in lows):
                yield line.rstrip('\r'), fp.name
                limit -= 1
                if limit <= 0:
                    return


def parse_entry(line, known=(), file=''):
    """一行日志 → {time, level, trace, thread, service, logger, msg, ts, file, text}。

    格式不统一，所以不按位置取值：行首方括号拆出来，按长相认级别、traceId、类名、服务。
    """
    out = {'file': file, 'text': line, 'time': '', 'level': '', 'trace': '', 'thread': '',
           'service': '', 'logger': '', 'msg': line, 'ts': 0}
    m = HEAD_RE.match(line)
    if not m:
        tm = TRACE_RE.search(line)
        out['trace'] = tm.group(0) if tm else ''
        return out
    t, brackets, msg = m.groups()
    out['time'], out['msg'] = t, msg
    try:
        out['ts'] = int(time.mktime(time.strptime(t[:19].replace('T', ' '), '%Y-%m-%d %H:%M:%S')))
    except ValueError:
        out['ts'] = 0
    known = set(known or ())
    for f in (x.strip() for x in re.findall(r'\[([^\]]*)\]', brackets)):
        if not f:
            continue
        lm, gm = LEVEL_RE.match(f), LOGGER_RE.match(f)
        if lm and not out['level']:
            out['level'] = lm.group(1).replace('WARNING', 'WARN')
        elif TRACE_RE.fullmatch(f) and not out['trace']:
            out['trace'] = f
        elif gm and not out['logger']:
            out['logger'] = gm.group(1)
        elif f in known and not out['service']:
            out['service'] = f
        elif not out['thread']:
            out['thread'] = f
    return out


def query_logs(conf, env, terms, minutes, run=subprocess.run):
    """按关键词查日志。配了 logs.query_cmd 就走模板，否则 grep 本地 logs.files。

    返回 {env, minutes, terms, entries, queries, notes, source}。同一行只留一次。
    """
    conf = conf or {}
    lc = conf.get('logs') or {}
    max_lines = int(lc.get('max_lines') or 300)
    known = _split_list(conf.get('services'))
    res = {'env': env, 'minutes': int(minutes), 'terms': list(terms), 'entries': [],
           'queries': [], 'notes': [], 'source': ''}
    seen = set()
    template = (lc.get('query_cmd') or '').strip()

    if template:
        res['source'] = 'query_cmd'
        for term in terms:
            if len(res['entries']) >= max_lines:
                res['notes'].append('命中超过 %d 条，后面截断' % max_lines)
                break
            cmd = _fill(template, term, minutes, env)
            lines, err = [], ''
            try:
                p = run(cmd, shell=True, capture_output=True, text=True,
                        timeout=int(lc.get('timeout') or 180))
                lines = [x for x in (p.stdout or '').splitlines() if x.strip()]
                if p.returncode != 0:
                    err = ((p.stderr or '').strip() or '退出码 %d' % p.returncode)[:300]
            except (OSError, subprocess.SubprocessError) as e:
                err = str(e)[:300]
            res['queries'].append({'term': term, 'cmd': mask(cmd)[:500], 'count': len(lines),
                                   'error': err})
            if err:
                res['notes'].append('查询 %r 出错：%s' % (term, err))
            for line in lines:
                if len(res['entries']) >= max_lines:
                    break
                key = line.strip()
                if key in seen:
                    continue
                seen.add(key)
                res['entries'].append(parse_entry(line[:4000], known))
    else:
        res['source'] = 'files'
        limit = max_lines
        for line, fname in _grep_files(lc, env, terms, limit, res['notes']):
            key = line.strip()
            if key in seen:
                continue
            seen.add(key)
            res['entries'].append(parse_entry(line[:4000], known, fname))
        files = [str(x) for x in ((lc.get('files') or {}).get(env) or [])]
        res['queries'].append({'term': '、'.join(terms), 'cmd': 'grep -i（本地文件：%s）'
                               % ('、'.join(files) or '未配置'), 'count': len(res['entries']), 'error': ''})
    return res


# ---------- 日志摘要 ----------

def signature(entry):
    """归类特征：异常类 @ 类名；没有异常就用类名 + 消息开头（数字抹掉）。"""
    m = EXC_RE.search(entry.get('msg') or '')
    logger = (entry.get('logger') or '').rsplit('.', 1)[-1] or entry.get('service') or '?'
    if m:
        return '%s @ %s' % (m.group(1).rsplit('.', 1)[-1], logger)
    head = re.sub(r'\d+', '#', (entry.get('msg') or '').split('\n')[0])[:80]
    return '%s: %s' % (logger, head)


def group_entries(entries, now_ts=None):
    """按 signature 归类，按次数倒序。"""
    now_ts = now_ts or time.time()
    groups = {}
    for e in entries:
        sig = signature(e)
        g = groups.setdefault(sig, {'sig': sig, 'count': 0, 'first': None, 'last': None,
                                    'services': set(), 'traces': [], 'sample': e})
        g['count'] += 1
        g['services'].add(e.get('service') or '')
        if e.get('trace') and e['trace'] not in g['traces'] and len(g['traces']) < 5:
            g['traces'].append(e['trace'])
        if e.get('ts'):
            g['first'] = min(g['first'] or e['ts'], e['ts'])
            g['last'] = max(g['last'] or e['ts'], e['ts'])
    out = sorted(groups.values(), key=lambda g: (-g['count'], -(g['last'] or 0)))
    for g in out:
        g['services'] = sorted(x for x in g['services'] if x)
        g['ongoing'] = bool(g['last'] and now_ts - g['last'] < 600)   # 最近 10 分钟还在报
    return out


def _fmt_ts(ts):
    return time.strftime('%m-%d %H:%M:%S', time.localtime(ts)) if ts else '?'


def _trim(msg, lines=25):
    ls = mask(msg or '').split('\n')
    return '\n'.join(ls[:lines]) + ('\n…（还有 %d 行）' % (len(ls) - lines) if len(ls) > lines else '')


def summary_md(res):
    """日志摘要（已打码）：按报错归类 + 样本 + 用到的查询，给人看也给 AI 读。"""
    groups = group_entries(res.get('entries') or [])
    lines = ['# 日志摘要（%s，最近 %s 分钟）' % (res.get('env') or '?', res.get('minutes') or '?'), '']
    lines.append('- 查询方式：%s' % ('命令模板 logs.query_cmd' if res.get('source') == 'query_cmd'
                                    else '本地日志文件 logs.files（grep 兜底）'))
    for n in res.get('notes') or []:
        lines.append('- ' + n)
    if res.get('queries'):
        lines += ['', '## 用到的查询', '', '| 查询词 | 命令 | 命中 |', '|---|---|---|']
        for q in res['queries']:
            lines.append('| %s | `%s` | %s |' % (str(q.get('term') or '').replace('|', '/'),
                                               str(q.get('cmd') or '').replace('|', '/').replace('`', "'"),
                                               q.get('count', 0)))
    total = len(res.get('entries') or [])
    if groups:
        lines += ['', '## 报错归类（共 %d 条，按次数排）' % total, '',
                  '| 特征 | 次数 | 首次 | 最近 | 服务 | 还在报 |', '|---|---|---|---|---|---|']
        for g in groups[:15]:
            lines.append('| %s | %d | %s | %s | %s | %s |' % (
                g['sig'].replace('|', '/'), g['count'], _fmt_ts(g['first']), _fmt_ts(g['last']),
                '、'.join(g['services']) or '?', '是' if g['ongoing'] else '否'))
        for g in groups[:5]:
            s = g['sample']
            lines += ['', '### %s（%d 次）' % (g['sig'], g['count']),
                      'traceId：%s' % ('、'.join(g['traces']) or '无'), '',
                      '```', '%s [%s] [%s] [%s] %s' % (s.get('time'), s.get('level'), s.get('trace'),
                                                       s.get('service'), s.get('logger') or ''),
                      _trim(s.get('msg')), '```']
    if res.get('entries'):
        lines += ['', '## 日志样本（原样，已打码）', '', '```']
        for e in (res.get('entries') or [])[:20]:
            lines.append(mask((e.get('text') or '')[:500]))
        lines += ['```']
    if not res.get('entries'):
        lines += ['', '这个时间窗里没查到相关日志。可以换关键词、放宽时间，或确认环境和日志路径配对了。']
    return '\n'.join(lines)


# ---------- 线上代码 ----------

def prepare_projects(repos, code_dirs, base, workdir, progress):
    """准备只读代码：--repo + --base 用 detached worktree 检出 origin/<base>；--code 直接用现成目录。

    worktree 放在 <问题根目录>/_code/<仓库名>，可跨问题复用；不动用户当前工作区。
    """
    projects = []
    for raw in repos:
        repo = Path(str(raw)).expanduser()
        name = repo.name
        if not (repo / '.git').exists():
            projects.append({'name': name, 'path': str(repo), 'base': '', 'head': '',
                             'note': '不是 git 仓库，按现成目录用'})
            continue
        if not base:
            projects.append({'name': name, 'path': str(repo), 'base': '', 'head': _head(repo)})
            continue
        try:
            git.fetch(repo, [base])
        except git.GitError as e:
            progress('拉取 %s 的 %s 失败，用本地已有的 origin/%s（%s）' % (name, base, base, str(e)[:120]))
        ref = 'origin/' + base
        target = Path(workdir) / name
        try:
            if git.rev(repo, ref) is None:
                raise git.GitError('没有 %s' % ref)
            target.parent.mkdir(parents=True, exist_ok=True)
            if (target / '.git').exists():
                git.git(target, 'checkout', '-q', '--detach', ref)
            else:
                git.git(repo, 'worktree', 'add', '-q', '--detach', str(target), ref)
            projects.append({'name': name, 'path': str(target), 'base': base,
                             'head': _head(target)})
        except git.GitError as e:
            progress('准备 %s 的只读检出失败（%s），退回当前工作区' % (name, str(e)[:120]))
            projects.append({'name': name, 'path': str(repo), 'base': '', 'head': _head(repo),
                             'note': str(e)[:200]})
    for raw in code_dirs:
        p = Path(str(raw)).expanduser()
        projects.append({'name': p.name, 'path': str(p), 'base': '', 'head': _head(p)})
    # 同名去重（先出现的优先：--repo 的 worktree 比 --code 更可信）
    out, seen = [], set()
    for p in projects:
        if p['name'] not in seen:
            seen.add(p['name'])
            out.append(p)
    return out


def _head(path):
    try:
        return git.git(path, 'rev-parse', '--short', 'HEAD', check=False) or ''
    except (git.GitError, OSError):
        return ''


def _source_index(project_path):
    """类名 → 相对路径（只看主代码，跳过测试和构建产物）。"""
    out = {}
    try:
        files = git.git(project_path, 'ls-files', '*.java', '*.kt', '*.scala', check=False) or ''
    except (git.GitError, OSError):
        files = ''
    if not files:
        files = '\n'.join(str(p.relative_to(project_path)) for p in Path(project_path).rglob('*')
                          if p.suffix in SRC_EXTS)
    for f in files.splitlines():
        f = f.strip()
        if f and '/src/test/' not in f and not f.startswith(('target/', 'build/')):
            out.setdefault(Path(f).stem, f)
    return out


def stack_frames(entries, max_frames=8):
    """从日志里抽堆栈帧：(Class.java:88) → (服务, 类名, 行号)，按出现顺序去重。"""
    frames, seen = [], set()
    for e in entries:
        for m in FRAME.finditer(e.get('msg') or ''):
            key = (e.get('service') or '', m.group(1), int(m.group(2)))
            if key not in seen:
                seen.add(key)
                frames.append(key)
            if len(frames) >= max_frames:
                return frames
    return frames


def stack_context(projects, entries, out_file, around=8, max_frames=8):
    """把堆栈对应的代码提前截好写成一页，放进提示词；只截本次代码里找得到的类。"""
    frames = stack_frames(entries, max_frames)
    if not frames or not projects:
        return None
    index = {p['name']: _source_index(p['path']) for p in projects}
    by_name = {p['name']: p for p in projects}
    blocks, used = [], set()
    for svc, cls, line_no in frames:
        if len(blocks) >= max_frames:
            break
        for name in [svc] + [n for n in index if n != svc]:
            rel = (index.get(name) or {}).get(cls)
            if not rel or (name, rel) in used:
                continue
            try:
                src = (Path(by_name[name]['path']) / rel).read_text(
                    encoding='utf-8', errors='replace').splitlines()
            except OSError:
                continue
            if not 1 <= line_no <= len(src):
                continue
            lo, hi = max(1, line_no - around), min(len(src), line_no + around)
            used.add((name, rel))
            blocks.append('\n'.join(
                ['## %s / %s:%d（提交 %s）' % (name, rel, line_no, by_name[name].get('head') or '?'),
                 '```java'] + ['%5d  %s' % (n, src[n - 1]) for n in range(lo, hi + 1)] + ['```']))
            break
    if not blocks:
        return None
    text = '\n\n'.join(['# 堆栈对应的代码（从线上代码里截的，行号就是日志里的行号）'] + blocks)
    Path(out_file).write_text(mask(text), encoding='utf-8')
    return str(out_file)


# ---------- 提示词 / 输出解析 / 报告 ----------

def build_prompt(issue, issue_dir, log_md, stack_md, projects):
    d = Path(issue_dir)
    alert = d / (issue.get('alert_file') or '告警原文.txt')
    code_text = '；'.join('%s（%s，提交 %s）' % (p['name'], p['path'], p.get('head') or '?')
                         for p in projects if p.get('path')) or '本机没有相关代码，只依据日志判断'
    lines = [
        '你在排查一个**线上问题**（环境 %s）。**只读**：只看日志和代码，不要改任何文件、不要提交、'
        '不要连数据库、不要执行会改数据的命令。' % issue.get('env'),
        '- 问题：' + str(issue.get('title') or ''),
        ('- 补充说明：\n' + str(issue.get('detail'))) if issue.get('detail') else '',
        '- 线索：traceId=%s；服务=%s；关键词=%s' % (
            issue.get('trace') or '无', '、'.join(issue.get('services') or []) or '无',
            '、'.join(issue.get('keywords') or []) or '无'),
        ('- 告警原文（已打码）：%s' % alert) if alert.exists() else '',
        '- 日志摘要（已打码，先读它）：%s' % log_md,
        ('- 堆栈对应的代码（已按日志里的“类名:行号”截好，直接读，不要重复找；确实要看调用方，最多再读 3 个文件）：%s'
         % stack_md) if stack_md else '',
        '- 线上代码（只读）：' + code_text,
        '判断：日志里看到的是不是告警说的问题；根因在哪个服务、哪个类哪个方法、为什么；影响范围'
        '（多少次、从什么时候开始、现在还在不在发生，按日志摘要里的统计说）；要不要改代码，改的话改哪里、'
        '怎么改、风险；不改代码能先怎么止血（配置、开关、数据修复）。需要修数据只写 SQL 给人看，不执行。'
        '证据要带 traceId 或日志时间、代码位置（文件:行）。',
        '写给后端开发看：大白话，结论先说，不写排查过程。',
        '最后只输出 JSON：{"summary": "一句话结论", "confirmed": "日志里看到了什么，是不是同一个问题", '
        '"cause": "根因", "evidence": ["证据：日志 / 代码位置"], "impact": "影响范围", '
        '"need_code_change": true, "fix_plan": "怎么改（不用改就写为什么）", "files": ["服务/路径#方法"], '
        '"workaround": "止血建议，没有就空", "data_fix_sql": "要修数据时的 SQL，没有就空", '
        '"confidence": "高|中|低"}',
    ]
    return '\n'.join(x for x in lines if x)


def parse_json(text):
    """从模型输出里抠第一个 JSON 对象；允许被 ```json 代码块包着。解析失败给明确错误。"""
    text = (text or '').strip()
    text = re.sub(r'^```(?:json)?\s*', '', text)
    text = re.sub(r'\s*```$', '', text)
    start = text.find('{')
    if start < 0:
        raise ValueError('AI 的输出里没有 JSON 对象：%s' % (text[:200] or '（空）'))
    try:
        obj, _ = json.JSONDecoder().raw_decode(text[start:])
    except ValueError as e:
        raise ValueError('AI 的输出不是合法 JSON（%s）：%s' % (e, text[start:start + 200]))
    if not isinstance(obj, dict):
        raise ValueError('AI 输出的 JSON 不是对象：%s' % text[start:start + 200])
    return obj


def _truthy(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ('true', 'yes', 'y', '1', '是', '要')


def normalize_report(out, engine=''):
    """把模型输出收成固定字段，缺 summary / cause 视为失败。"""
    if not str(out.get('summary') or '').strip():
        raise ValueError('AI 输出的 JSON 里没有 summary（一句话结论）')
    if not str(out.get('cause') or '').strip():
        raise ValueError('AI 输出的 JSON 里没有 cause（根因）')
    report = {k: out.get(k) for k in REPORT_FIELDS}
    for k in ('evidence', 'files'):
        value = report.get(k) or []
        report[k] = [str(x) for x in value] if isinstance(value, (list, tuple)) else [str(value)]
    report['need_code_change'] = _truthy(report.get('need_code_change'))
    report['summary'] = str(report['summary']).strip()
    report['cause'] = str(report['cause']).strip()
    report['confidence'] = str(report.get('confidence') or '').strip() or '?'
    report['engine'] = engine
    return report


def report_md(issue, report):
    """排查报告 markdown：现象 / 根因 / 证据 / 影响范围 / 修复建议 / 止血建议。"""
    st = issue.get('stats') or {}
    evidence = '\n'.join('- ' + str(x) for x in report.get('evidence') or []) or '- 无'
    files = '\n'.join('- ' + str(x) for x in report.get('files') or []) or '- 无'
    head = '环境 %s · 日志 %s 条 · 可信度 %s · %s 排查 · %s' % (
        issue.get('env') or '?', st.get('errors', 0), report.get('confidence') or '?',
        report.get('engine') or '?', report.get('at') or issue.get('created_at') or '')
    lines = [
        '# %s %s' % (issue.get('id') or '', issue.get('title') or ''), '',
        head, '',
        '## 结论', str(report.get('summary') or ''), '',
        '## 现象确认', str(report.get('confirmed') or ''), '',
        '## 根因', str(report.get('cause') or ''), '',
        '## 证据', evidence, '',
        '## 影响范围', str(report.get('impact') or ''), '',
        '## 修复意见（%s）' % ('要改代码' if report.get('need_code_change') else '不用改代码'),
        str(report.get('fix_plan') or ''), '',
        '涉及：', files, '',
        '## 止血建议', str(report.get('workaround') or '无'), '',
    ]
    if report.get('data_fix_sql'):
        lines += ['## 数据修复 SQL（只给你看，没执行）', '```sql', str(report['data_fix_sql']), '```', '']
    return '\n'.join(lines)


# ---------- 排查 ----------

def pick_engine(conf=None):
    """按 claude → codex → deepseek → kimi 的顺序取第一个可用的。"""
    conf = conf or {}
    reasons = []
    for e in ENGINE_ORDER:
        try:
            agent.find_cli(e, conf)
        except agent.AgentError as ex:
            reasons.append('%s：%s' % (e, ex))
            continue
        if e in ('deepseek', 'kimi') and not cfg.api_key(e):
            reasons.append('%s：没有 key' % e)
            continue
        return e
    raise ValueError('没有可用的 AI 引擎（claude / codex / deepseek / kimi）：' + '；'.join(reasons))


def build_queries(issue):
    """查询词：traceId 优先，其次关键词；都没有就用服务名兜底。"""
    out = []
    for value in [issue.get('trace')] + list(issue.get('keywords') or []) + \
                 list(issue.get('services') or []):
        value = str(value or '').strip()
        if value and value not in out:
            out.append(value)
    return out[:6]


def investigate(oid, root=None, conf=None, engine='', runner=None, repos=(), code=(),
                base='release', timeout=2400, minutes=0, progress=None):
    """同步排查一个问题：查日志 → 准备代码 → 截堆栈 → AI 只读排查 → 写报告。

    runner 可注入（默认 wbkit.agent.run），方便单测；失败不抛异常，写进 issue['error'] 后返回。
    """
    conf = conf if conf is not None else cfg.load()
    base_dir = root_of(root, conf)
    progress = progress or (lambda msg: print('[investigate] ' + str(msg), file=sys.stderr, flush=True))

    issue = load_issue(base_dir, oid)
    issue_dir = base_dir / oid
    issue_dir.mkdir(parents=True, exist_ok=True)
    _event(issue, '排查中', '开始排查')
    issue['error'] = ''
    save_issue(base_dir, issue)

    try:
        minutes = int(minutes or issue.get('minutes') or
                      (conf.get('logs') or {}).get('default_minutes') or 60)
        terms = build_queries(issue)
        if not terms:
            raise ValueError('这条问题没有 traceId、关键词或服务，先在 create 时补上线索')

        progress('查日志：%s' % '、'.join(terms))
        res = query_logs(conf, issue.get('env') or 'prod', terms, minutes)
        (issue_dir / '日志摘要.md').write_text(summary_md(res), encoding='utf-8')

        found = []
        for s in list(issue.get('services') or []) + [(e.get('service') or '') for e in res['entries']]:
            if s and s not in found:
                found.append(s)
        issue['services'] = found[:8]

        progress('准备线上代码（%d 个仓库 / 目录）' % (len(repos) + len(code)))
        projects = prepare_projects(repos, code, base, base_dir / '_code', progress)
        stack_md = stack_context(projects, res['entries'], issue_dir / '堆栈代码.md')

        log_md = str(issue_dir / '日志摘要.md')
        prompt = build_prompt(issue, issue_dir, log_md, stack_md, projects)
        engine = engine or pick_engine(conf)
        runner = runner or agent.run

        log_path = issue_dir / 'logs' / ('ai-%s-%s.log' % (time.strftime('%Y%m%d-%H%M%S'), engine))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        progress('AI 排查中（%s，超时 %ss）' % (engine, timeout))
        result = runner(engine, prompt, cwd=str(issue_dir), profile='read', timeout=int(timeout),
                        dirs=[str(issue_dir)] + [p['path'] for p in projects if p.get('path')],
                        log=str(log_path))
        if not result.get('ok'):
            raise RuntimeError('AI 排查失败：%s' % (result.get('error') or '模型没有回应'))

        try:
            out = normalize_report(parse_json(result.get('text')), engine)
        except ValueError as e:
            raise RuntimeError('%s；原始输出见 %s' % (e, log_path.name))

        groups = group_entries(res['entries'])
        issue['stats'] = {'errors': len(res['entries']), 'groups': len(groups),
                          'queries': len(res['queries'])}
        out.update(at=now(), log=str(log_path.relative_to(issue_dir)))
        issue['projects'] = [{'name': p['name'], 'path': p['path'], 'base': p.get('base'),
                              'head': p.get('head')} for p in projects]
        issue['report'] = out
        (issue_dir / '排查报告.md').write_text(report_md(issue, out), encoding='utf-8')
        _event(issue, '待处理', '排查完了：%s' % out['summary'][:120])
        issue['error'] = ''
        save_issue(base_dir, issue)
        progress('排查完了：%s' % out['summary'][:120])
        return issue
    except Exception as e:  # noqa: BLE001 排查失败要落到 issue.json，命令行再决定退出码
        issue['status'] = '待排查'
        issue['error'] = str(e)[:800]
        issue.setdefault('log', []).append({'at': now(), 'status': '待排查', 'note': '排查失败：%s' % str(e)[:200]})
        save_issue(base_dir, issue)
        progress('排查失败：%s' % e)
        return issue


# ---------- 命令行 ----------

def cmd_parse_alert(a):
    text = _read_text(a.alert)
    conf = cfg.load()
    words = _env_words(conf)
    services = _split_list(a.services) or _split_list(conf.get('services'))
    emit(parse_alert(text, services, words))
    return 0


def cmd_create(a):
    conf = cfg.load()
    text = _read_text(a.alert)
    issue = create_issue(root_of(a.root, conf), title=a.title, alert_text=text, env=a.env,
                         services=_split_list(a.services), minutes=a.minutes, detail=a.detail,
                         trace=a.trace, conf=conf)
    emit({'issue': issue, 'summary': '已建 %s，跑 investigate %s 开始排查' % (issue['id'], issue['id'])})
    return 0


def cmd_list(a):
    conf = cfg.load()
    root = root_of(a.root, conf)
    items = list_issues(root)
    emit({'root': str(root), 'count': len(items), 'issues': items})
    return 0


def cmd_show(a):
    conf = cfg.load()
    root = root_of(a.root, conf)
    issue = load_issue(root, a.oid)
    report = issue.get('report') or {}
    md_file = root / a.oid / '排查报告.md'
    if md_file.exists():
        md = md_file.read_text(encoding='utf-8')
    elif report:
        md = report_md(issue, report)
    else:
        md = ''
    emit({'root': str(root), 'issue': issue, 'report_md': md})
    return 0


def cmd_investigate(a):
    conf = cfg.load()
    repos = []
    for r in a.repo:
        p = Path(r).expanduser()
        if p.exists():
            repos.append(str(p))
        else:
            repos.append(str(cfg.project(r)['path']))
    issue = investigate(a.oid, root=a.root, conf=conf, engine=a.engine, repos=repos,
                        code=a.code, base=a.base, timeout=a.timeout, minutes=a.minutes)
    emit(issue)
    return 0 if not issue.get('error') else 1


def main(argv=None):
    p = argparse.ArgumentParser(description='线上告警只读排查：解析、落盘、查日志、对代码、出报告（输出 JSON）')
    sub = p.add_subparsers(dest='cmd', required=True)

    s = sub.add_parser('parse-alert', help='从告警原文抠 env / traceId / 服务 / 时间 / 异常 / 关键词')
    s.add_argument('--alert', default='', help='告警原文文件（也可从 stdin 传）')
    s.add_argument('--services', default='', help='服务名候选，逗号分隔；不传就用配置 services')
    s.set_defaults(fn=cmd_parse_alert)

    s = sub.add_parser('create', help='建一个问题目录，自动解析告警并落 issue.json')
    s.add_argument('--title', default='', help='一句话标题；不传就用异常类名')
    s.add_argument('--alert', default='', help='告警原文文件（也可从 stdin 传）')
    s.add_argument('--env', default='', choices=[''] + list(ENVS), help='不传就从告警里猜，猜不到按 prod')
    s.add_argument('--services', default='', help='服务名，逗号分隔')
    s.add_argument('--minutes', type=int, default=0, help='查日志的时间窗（分钟），默认取配置或 60')
    s.add_argument('--detail', default='', help='补充说明')
    s.add_argument('--trace', default='', help='手工指定 traceId（覆盖解析结果）')
    s.add_argument('--root', default='', help='问题根目录，默认配置 issues_root 或 ./online-issues')
    s.set_defaults(fn=cmd_create)

    s = sub.add_parser('list', help='列问题')
    s.add_argument('--root', default='')
    s.set_defaults(fn=cmd_list)

    s = sub.add_parser('show', help='看一个问题：issue.json + 报告正文')
    s.add_argument('oid', help='问题编号，如 OL-20260101-01')
    s.add_argument('--root', default='')
    s.set_defaults(fn=cmd_show)

    s = sub.add_parser('investigate', help='查日志 → 准备只读代码 → AI 排查 → 写报告')
    s.add_argument('oid', help='问题编号，如 OL-20260101-01')
    s.add_argument('--root', default='')
    s.add_argument('--engine', default='', choices=[''] + list(ENGINE_ORDER),
                   help='不传就按 claude→codex→deepseek→kimi 取第一个可用的')
    s.add_argument('--repo', action='append', default=[],
                   help='本地仓库路径（可重复），也可写配置 projects 里的项目名')
    s.add_argument('--base', default='release',
                   help='用 origin/<base> 的只读检出，默认 release；传空字符串则直接用仓库当前工作区')
    s.add_argument('--code', action='append', default=[], help='现成代码目录（可重复），不再拉 git')
    s.add_argument('--timeout', type=int, default=2400, help='AI 排查超时（秒），默认 2400')
    s.add_argument('--minutes', type=int, default=0, help='覆盖问题里的日志时间窗（分钟）')
    s.set_defaults(fn=cmd_investigate)

    a = p.parse_args(argv)
    try:
        return a.fn(a)
    except (ValueError, KeyError, OSError, git.GitError, agent.AgentError) as e:
        print(json.dumps({'ok': False, 'error': str(e)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
