#!/usr/bin/env python3
"""测试 / 联调问题分诊：拆一段反馈、落盘、只读排查定归属、按后端问题改代码。

子命令：
  split      一整段反馈拆成一条条问题（规则拆；--ai 交给模型拆，失败回退规则）
  add        新记一条问题（编号按来源前缀递增）
  list       列问题（可按状态过滤）
  diagnose   只读排查一条：判归属、找根因、给回复草稿
  fix        按排查结论改后端代码（--commit 才提交，从不推送）
  status     改问题状态

输出一律 JSON，方便 AI 直接消费。只用标准库。
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / '_lib'))

from wbkit import agent, config as cfg, git  # noqa: E402

# 来源 -> 编号前缀。可以在配置 issues.sources 里覆盖或新增。
DEFAULT_SOURCES = {'测试': 'ISSUE', '联调': 'FE'}
# 归属枚举。可以在配置 issues.verdicts 里改，但要和提示词里的口径对得上。
DEFAULT_VERDICTS = ('backend', 'not_bug', 'frontend', 'need_info')
STATUSES = ('待处理', '处理中', '待验证', '已修复', '已关闭')

VERDICT_TEXT = {
    'backend': '后端问题',
    'not_bug': '不是 bug，符合需求',
    'frontend': '前端问题',
    'need_info': '信息不够',
}

STORE_NAME = 'issues.json'
API_RE = re.compile(r'\b(GET|POST|PUT|DELETE|PATCH)\s+(/[\w/{}.\-]+)', re.I)
ITEM_RE = re.compile(r'^\s*(?:\d+\s*[.、．)）]|[（(]\d+[)）]|[-*•·]\s|问题\s*\d+\s*[：:.、]?)\s*')


def emit(data):
    print(json.dumps(data, ensure_ascii=False, indent=2))


def now():
    return time.strftime('%Y-%m-%d %H:%M:%S')


# ---------- 配置 ----------

def sources():
    """来源到编号前缀的映射：默认 测试→ISSUE、联调→FE，配置可覆盖。"""
    conf = cfg.get('issues.sources') or {}
    return dict(DEFAULT_SOURCES, **{str(k): str(v) for k, v in conf.items()})


def verdicts():
    conf = cfg.get('issues.verdicts') or DEFAULT_VERDICTS
    return [str(x) for x in conf] or list(DEFAULT_VERDICTS)


def default_store():
    return Path(cfg.get('issues.store') or STORE_NAME).expanduser()


def default_engine():
    return cfg.get('issues.engine') or 'claude'


def store_of(arg=''):
    return Path(arg).expanduser() if arg else default_store()


# ---------- issues.json ----------

def load_store(path):
    p = Path(path)
    try:
        data = json.loads(p.read_text(encoding='utf-8'))
    except FileNotFoundError:
        data = {}
    except (OSError, ValueError) as e:
        raise ValueError('问题文件读不了：%s（%s）' % (p, e))
    if not isinstance(data, dict):
        raise ValueError('问题文件格式不对，顶层要是对象：%s' % p)
    data.setdefault('issues', [])
    return data


def save_store(path, data):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def find_issue(data, iid):
    for i in data.get('issues') or []:
        if i.get('id') == iid:
            return i
    raise ValueError('没有这个问题：%s' % iid)


def add_issue(path, title, detail='', api='', source='测试'):
    """记一条问题：编号按来源前缀递增（ISSUE-01 / FE-01），状态待处理。"""
    kind = sources()
    if source not in kind:
        raise ValueError('问题来源只能是：' + '、'.join(kind))
    title = (title or '').strip()
    if not title:
        raise ValueError('写一句话说清是什么问题')
    prefix = kind[source]
    data = load_store(path)
    issues = data['issues']
    pat = re.compile(r'%s-(\d+)' % re.escape(prefix))
    nums = []
    for i in issues:
        m = pat.fullmatch(str(i.get('id') or ''))
        if m:
            nums.append(int(m.group(1)))
    item = {
        'id': '%s-%02d' % (prefix, 1 + max(nums or [0])),
        'source': source,
        'title': title[:200],
        'detail': (detail or '').strip()[:8000],
        'api': (api or '').strip()[:300],
        'status': '待处理',
        'verdict': '', 'cause': '', 'plan': '', 'reply': '',
        'log': [{'at': now(), 'action': 'add', 'note': '记入'}],
    }
    issues.append(item)
    save_store(path, data)
    return item


def set_status(path, iid, status, note=''):
    if status not in STATUSES:
        raise ValueError('状态只能是：' + '、'.join(STATUSES))
    data = load_store(path)
    issue = find_issue(data, iid)
    issue['status'] = status
    issue.setdefault('log', []).append({'at': now(), 'action': 'status', 'status': status,
                                       'note': (note or '').strip()[:1000]})
    save_store(path, data)
    return issue


# ---------- 拆分 ----------

def _norm(text):
    return re.sub(r'\s+', ' ', text or '').strip()


def dedupe(items):
    """按标题 + 描述去重，空标题丢掉。"""
    seen, out = set(), []
    for it in items or []:
        title = str(it.get('title') or '').strip()
        if not title:
            continue
        key = (_norm(title), _norm(it.get('detail')))
        if key in seen:
            continue
        seen.add(key)
        out.append({'title': title[:200], 'detail': str(it.get('detail') or '').strip(),
                    'api': str(it.get('api') or '').strip()[:300]})
    return out


def split_text(text):
    """规则拆：有两条以上编号 / 分点就按标记拆，否则按空行拆。

    第一个编号之前的开场白（“测试反馈：”）丢掉；第一行太长就截一句当标题，整段放描述；
    自动抽「方法 + 路径」写到 api。不调模型。
    """
    lines = (text or '').replace('\r\n', '\n').split('\n')
    marked = sum(bool(ITEM_RE.match(l)) for l in lines if l.strip())
    blocks = []
    if marked >= 2:
        cur, started = [], False
        for line in lines:
            if ITEM_RE.match(line):
                if cur:
                    blocks.append(cur)
                cur = [ITEM_RE.sub('', line, count=1)]
                started = True
            elif started:
                cur.append(line)
        if cur:
            blocks.append(cur)
    else:
        cur = []
        for line in lines:
            if not line.strip():
                if cur:
                    blocks.append(cur)
                    cur = []
            else:
                cur.append(line)
        if cur:
            blocks.append(cur)

    out = []
    for block in blocks:
        block = [l.rstrip() for l in block]
        while block and not block[-1].strip():
            block.pop()
        while block and not block[0].strip():
            block.pop(0)
        if not block:
            continue
        title = block[0].strip()
        detail = '\n'.join(block[1:]).strip()
        if len(title) > 60:                     # 第一行太长：截第一句当标题，整段放描述
            head = re.split(r'[，。；,;]', title)[0].strip()
            title, detail = (head or title)[:60], '\n'.join(block).strip()
        m = API_RE.search('\n'.join(block))
        out.append({'title': title, 'detail': detail,
                    'api': ('%s %s' % (m.group(1).upper(), m.group(2))) if m else ''})
    return dedupe(out)


def split_with_ai(text, source, engine, runner=None):
    """内容很乱（聊天记录、口语化）时交给模型拆。runner 供测试注入，默认 wbkit.agent.run。"""
    who = '前端联调时前端' if source == '联调' else '测试同学'
    prompt = '\n'.join([
        '下面是%s反馈的问题（可能是聊天记录、文档片段）。' % who,
        '把它拆成一条条独立的问题，去掉寒暄、称呼和重复，不要编造原文里没有的信息。',
        '每条：title 一句话说清问题（不超过 40 字）；detail 保留原文里的请求参数、返回、期望、操作步骤；',
        'api 写涉及的接口（方法 + 路径，没有就留空）。',
        '只输出 JSON 数组，不要别的文字：[{"title": "", "detail": "", "api": ""}]',
        '',
        (text or '')[:12000],
    ])
    result = (runner or agent.run)(engine, prompt, str(Path.cwd()), profile='read', timeout=600)
    if not result.get('ok'):
        raise ValueError(result.get('error') or '没有回应')
    m = re.search(r'\[.*\]', result.get('text') or '', re.S)
    if not m:
        raise ValueError('AI 的输出里没有 JSON 数组')
    arr = json.loads(m.group(0))
    items = dedupe([{'title': i.get('title'), 'detail': i.get('detail'), 'api': i.get('api')}
                    for i in arr if isinstance(i, dict)])
    if not items:
        raise ValueError('AI 没拆出条目')
    return items


# ---------- 提示词 ----------

VERDICT_RULES = [
    '- backend：后端代码的问题（返回不对、校验不对、缺字段、报错）→ 在仓库里定位到文件和函数，写清怎么改、回归要测什么。',
    '- not_bug：按需求和方案本来就是这样 → 不改代码，引用需求或验收条件原文解释。',
    '- frontend：后端返回没问题，是前端展示或交互的问题 → 不改代码，写清前端哪里不对。',
    '- need_info：信息不够 → 不改代码，写清还要对方补什么（复现步骤、测试数据、环境、截图、请求和返回）。',
]


def _docs_files(docs_dirs):
    out = []
    for d in docs_dirs:
        p = Path(d)
        if p.is_dir():
            out += [str(f.resolve()) for f in sorted(p.rglob('*.md'))]
    return list(dict.fromkeys(out))[:30]


def _repo_list(repos):
    return [(Path(r).expanduser().resolve().name, str(Path(r).expanduser().resolve())) for r in repos]


def build_diagnose_prompt(issue, docs_dirs, repos):
    docs = _docs_files(docs_dirs)
    kinds = verdicts()
    lines = [
        '你在排查%s反馈的问题 **%s**。**只读**：只看代码和文档，不要改任何文件、不要提交、不要切分支。'
        % (issue.get('source') or '测试', issue.get('id')),
        '- 问题：' + str(issue.get('title') or ''),
        ('- 描述：\n' + str(issue.get('detail'))) if issue.get('detail') else '',
        ('- 涉及接口：' + str(issue.get('api'))) if issue.get('api') else '',
        '依据文档（用 Read 打开；目录里没有就读代码）：',
        *(['- ' + x for x in docs] or ['- （没有文档，跳过）']),
        '代码仓库（用 Grep / Read 看）：',
        *(['- %s（%s）' % (n, p) for n, p in _repo_list(repos)] or ['- 没给仓库路径，只能在当前目录里找']),
        '判定口径：',
        *VERDICT_RULES,
        '再写清根因和打算怎么改：改哪个文件哪个方法、要不要动接口或数据表、回归要测什么。',
        'reply 是给对方（测试 / 前端）的回复草稿：像当面说话，几句大白话，不写排查过程、不堆术语。',
        '最后只输出一个 JSON 对象：{"verdict": "%s", "cause": "根因（一两句）", "plan": "打算怎么改（不改就写为什么）", '
        '"files": ["仓库/路径/文件#方法"], "reply": "给对方的话"}' % '|'.join(kinds),
    ]
    return '\n'.join(x for x in lines if x)


def build_fix_prompt(issue, docs_dirs, repos):
    docs = _docs_files(docs_dirs)
    kinds = verdicts()
    diag = []
    if issue.get('verdict') or issue.get('cause') or issue.get('plan'):
        diag = [
            '之前已经只读排查过（有新发现和它冲突时以代码事实为准，并在 reply 里说明）：',
            '- 归属：%s' % (issue.get('verdict') or ''),
            '- 根因：' + str(issue.get('cause') or ''),
            '- 改法：' + str(issue.get('plan') or ''),
            ('- 涉及：' + '、'.join(issue.get('files') or [])) if issue.get('files') else '',
        ]
    lines = [
        '你在处理%s反馈的问题 **%s**。' % (issue.get('source') or '测试', issue.get('id')),
        '- 问题：' + str(issue.get('title') or ''),
        ('- 描述：\n' + str(issue.get('detail'))) if issue.get('detail') else '',
        ('- 涉及接口：' + str(issue.get('api'))) if issue.get('api') else '',
        '依据文档（用 Read 打开）：',
        *(['- ' + x for x in docs] or ['- （没有文档，跳过）']),
        '工作区（只在这里改，不要动别的目录）：',
        *(['- %s（%s）' % (n, p) for n, p in _repo_list(repos)] or []),
        *diag,
        '判定口径：',
        *VERDICT_RULES,
        '按归属动手：',
        '- backend：直接在工作区改代码，补一个能覆盖该问题的回归测试；不要 git push，提交与否由调用方决定。',
        '- not_bug / frontend / need_info：一个文件都不要改。',
        'reply 给对方：像当面说话，几句大白话说清情况、要他怎么做；不写过程、不堆术语。',
        '最后只输出一个 JSON 对象：{"verdict": "%s", "reply": "给对方的话", "summary": "后端改了什么（没改就空）", '
        '"commits": ["提交号"]}' % '|'.join(kinds),
    ]
    return '\n'.join(x for x in lines if x)


def parse_json(text):
    m = re.search(r'\{.*\}', text or '', re.S)
    if not m:
        raise ValueError('AI 的输出里没有 JSON')
    return json.loads(m.group(0))


def _docs_of(store, docs):
    dirs = [Path(d).expanduser() for d in docs or [] if str(d).strip()]
    if not dirs:
        dirs = [Path(store).expanduser().parent]       # 默认：问题文件所在目录里的 md
    return [d.resolve() for d in dirs if d.is_dir()]


# ---------- 只读排查 ----------

def diagnose(store, iid, repos=(), docs=(), engine='', runner=None, timeout=1800):
    """只读排查一条问题，把 verdict / cause / plan / reply 写回 issues.json。"""
    store = Path(store)
    data = load_store(store)
    issue = find_issue(data, iid)
    docs_dirs = _docs_of(store, docs)
    repo_paths = [str(Path(r).expanduser().resolve()) for r in repos]
    prompt = build_diagnose_prompt(issue, docs_dirs, repo_paths)
    cwd = repo_paths[0] if repo_paths else (str(docs_dirs[0]) if docs_dirs else str(store.parent))
    engine = engine or default_engine()
    log_path = store.parent / 'logs' / ('%s-%s-diagnose-%s.log'
                                        % (time.strftime('%Y%m%d-%H%M%S'), iid, engine))
    log_path.parent.mkdir(parents=True, exist_ok=True)
    result = (runner or agent.run)(engine, prompt, cwd, profile='read', timeout=timeout,
                                   dirs=repo_paths + [str(d) for d in docs_dirs], log=str(log_path))
    if not result.get('ok'):
        raise ValueError(result.get('error') or '没有回应')
    out = parse_json(result.get('text'))
    kinds = verdicts()
    verdict = out.get('verdict') if out.get('verdict') in kinds else 'need_info'

    data = load_store(store)
    issue = find_issue(data, iid)
    issue.update(verdict=verdict,
                 cause=str(out.get('cause') or '')[:1000],
                 plan=str(out.get('plan') or '')[:3000],
                 reply=str(out.get('reply') or '')[:2000])
    files = [str(x)[:300] for x in out.get('files') or [] if x][:20]
    if files:
        issue['files'] = files
    issue.setdefault('log', []).append({'at': now(), 'action': 'diagnose', 'verdict': verdict,
                                        'note': VERDICT_TEXT.get(verdict, verdict), 'log': str(log_path)})
    save_store(store, data)
    return issue


# ---------- 改代码 ----------

def _commit(repos, title):
    """把工作区里已有的改动提交（不推送）。返回提交号列表。"""
    made = []
    for repo in repos:
        try:
            tracked, untracked = git.dirty_files(repo)
        except git.GitError:
            continue
        if not tracked and not untracked:
            continue
        try:
            git.git(repo, 'add', '-A')
            git.git(repo, 'commit', '-q', '-m', 'fix: %s' % (title or 'issue')[:60])
        except git.GitError:
            continue
        made.append(git.rev(repo, 'HEAD'))
    return [x for x in made if x]


def fix(store, iid, repos, docs=(), engine='', commit=False, dry_run=False, runner=None, timeout=3600):
    """按排查结论处理一条问题：backend 在工作区改代码；其它归属不改。从不推送。"""
    store = Path(store)
    data = load_store(store)
    issue = find_issue(data, iid)
    if not repos:
        raise ValueError('fix 要指定改哪个仓库：--repo DIR')
    docs_dirs = _docs_of(store, docs)
    repo_paths = [str(Path(r).expanduser().resolve()) for r in repos]
    prompt = build_fix_prompt(issue, docs_dirs, repo_paths)
    cwd = repo_paths[0]
    engine = engine or default_engine()
    dirs = repo_paths + [str(d) for d in docs_dirs]

    if dry_run:
        argv, error = None, ''
        try:
            argv, _ = agent.command(engine, prompt, cwd, profile='coder', dirs=dirs)
        except agent.AgentError as e:
            error = str(e)
        return {'dry_run': True, 'issue_id': iid, 'engine': engine, 'profile': 'coder',
                'cwd': cwd, 'repos': repo_paths, 'prompt': prompt, 'argv': argv, 'error': error}

    log_path = store.parent / 'logs' / ('%s-%s-fix-%s.log'
                                        % (time.strftime('%Y%m%d-%H%M%S'), iid, engine))
    log_path.parent.mkdir(parents=True, exist_ok=True)
    result = (runner or agent.run)(engine, prompt, cwd, profile='coder', timeout=timeout,
                                   dirs=dirs, log=str(log_path))
    if not result.get('ok'):
        raise ValueError(result.get('error') or '没有回应')
    out = parse_json(result.get('text'))
    kinds = verdicts()
    verdict = out.get('verdict') if out.get('verdict') in kinds else 'need_info'
    commits = [str(x)[:100] for x in out.get('commits') or [] if x]
    if commit and verdict == 'backend':
        commits += [c for c in _commit(repo_paths, issue.get('title')) if c not in commits]

    data = load_store(store)
    issue = find_issue(data, iid)
    issue.update(verdict=verdict,
                 reply=str(out.get('reply') or '')[:2000],
                 summary=str(out.get('summary') or '')[:2000],
                 commits=commits)
    if verdict == 'backend':
        issue['status'] = '待验证'
    issue.setdefault('log', []).append({'at': now(), 'action': 'fix', 'verdict': verdict,
                                        'status': issue.get('status'),
                                        'note': '改了代码' if verdict == 'backend' else '没改代码',
                                        'commits': commits, 'log': str(log_path)})
    save_store(store, data)
    return issue


# ---------- 命令行 ----------

def _read_text(a):
    if a.stdin or a.text == '-':
        return sys.stdin.read()
    if a.text:
        return Path(a.text).expanduser().read_text(encoding='utf-8')
    if not sys.stdin.isatty():
        return sys.stdin.read()
    raise ValueError('没有内容：用 --text FILE 或 --stdin')


def _detail_of(value):
    if not value:
        return ''
    p = Path(value).expanduser()
    return p.read_text(encoding='utf-8') if p.is_file() else value


def cmd_split(a):
    text = _read_text(a)
    items = None
    if a.ai:
        try:
            items = split_with_ai(text, a.source, a.engine)
        except (ValueError, agent.AgentError):
            items = None                       # 没 key、网络失败、输出看不懂：回退规则拆
    if items is None:
        items = split_text(text)
    emit(items)
    return 0


def cmd_add(a):
    issue = add_issue(store_of(a.store), a.title, _detail_of(a.detail), a.api, a.source)
    emit(issue)
    return 0


def cmd_list(a):
    data = load_store(store_of(a.store))
    issues = [i for i in data['issues'] if not a.status or i.get('status') == a.status]
    emit({'count': len(issues), 'store': str(store_of(a.store).resolve()), 'issues': issues})
    return 0


def cmd_diagnose(a):
    emit(diagnose(store_of(a.store), a.id, repos=a.repo, docs=a.docs, engine=a.engine))
    return 0


def cmd_fix(a):
    emit(fix(store_of(a.store), a.id, a.repo, docs=a.docs, engine=a.engine,
             commit=a.commit, dry_run=a.dry_run))
    return 0


def cmd_status(a):
    emit(set_status(store_of(a.store), a.id, a.new_status))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description='测试 / 联调问题分诊：拆分、落盘、只读排查、按后端问题改代码，输出 JSON')
    sub = p.add_subparsers(dest='cmd', required=True)

    s = sub.add_parser('split', help='一整段反馈拆成一条条问题，输出 JSON 数组')
    s.add_argument('--text', default='', help='反馈原文所在文件；- 表示从 stdin 读')
    s.add_argument('--stdin', action='store_true', help='从 stdin 读反馈原文')
    s.add_argument('--ai', action='store_true', help='让模型拆（没 key / 失败自动回退规则拆）')
    s.add_argument('--engine', default=default_engine(), choices=list(agent.ENGINES),
                   help='--ai 用的引擎，默认读配置 issues.engine')
    s.add_argument('--source', default='测试', choices=list(sources()), help='问题来源，默认 测试')
    s.set_defaults(fn=cmd_split)

    s = sub.add_parser('add', help='新记一条问题')
    s.add_argument('--store', default='', help='issues.json 路径，默认读配置 issues.store')
    s.add_argument('--title', required=True, help='一句话说清问题')
    s.add_argument('--detail', default='', help='详细描述或描述所在文件')
    s.add_argument('--api', default='', help='涉及接口，如 POST /orders/export')
    s.add_argument('--source', default='测试', choices=list(sources()))
    s.set_defaults(fn=cmd_add)

    s = sub.add_parser('list', help='列问题')
    s.add_argument('--store', default='')
    s.add_argument('--status', default='', choices=[''] + list(STATUSES))
    s.set_defaults(fn=cmd_list)

    s = sub.add_parser('diagnose', help='只读排查一条：判归属、找根因、给回复草稿')
    s.add_argument('--store', default='')
    s.add_argument('--id', required=True)
    s.add_argument('--repo', action='append', default=[], help='代码仓库目录，可重复')
    s.add_argument('--docs', action='append', default=[], help='依据文档目录（读里面的 md），可重复')
    s.add_argument('--engine', default=default_engine(), choices=list(agent.ENGINES))
    s.set_defaults(fn=cmd_diagnose)

    s = sub.add_parser('fix', help='按排查结论改后端代码（--commit 才提交，从不推送）')
    s.add_argument('--store', default='')
    s.add_argument('--id', required=True)
    s.add_argument('--repo', action='append', default=[], help='代码仓库目录，可重复')
    s.add_argument('--docs', action='append', default=[])
    s.add_argument('--engine', default=default_engine(), choices=list(agent.ENGINES))
    s.add_argument('--commit', action='store_true', help='改完提交到当前分支（不推送）')
    s.add_argument('--dry-run', action='store_true', help='只打印将要执行的代理参数，不真跑')
    s.set_defaults(fn=cmd_fix)

    s = sub.add_parser('status', help='改问题状态')
    s.add_argument('--store', default='')
    s.add_argument('--id', required=True)
    s.add_argument('--set', dest='new_status', required=True, choices=list(STATUSES))
    s.set_defaults(fn=cmd_status)

    a = p.parse_args(argv)
    try:
        return a.fn(a)
    except (ValueError, KeyError, OSError, agent.AgentError) as e:
        print(json.dumps({'ok': False, 'error': str(e)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
