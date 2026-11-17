#!/usr/bin/env python3
"""多模型代码审查：取 git 改动 → 按目录/文件分组并截断 → 一个或多个模型审查 → 合并去重。

子命令：
  review  取一次 diff，交给一个模型审查，输出结构化问题
  batch   同一份 diff 交给多个模型并跑，按 (文件, 行号, 标题) 去重合并，标注 agreement
  report  把 review / batch 的结果渲染成 markdown

只读：不改文件、不提交、不推送。除 report 外全部输出 JSON。
"""
import argparse
import json
import os
import re
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / '_lib'))

from wbkit import agent, git  # noqa: E402

SEVERITIES = ('blocker', 'major', 'minor', 'nit')
RANK = {s: i for i, s in enumerate(SEVERITIES)}
SEVERITY_LABEL = {'blocker': '阻断', 'major': '重要', 'minor': '次要', 'nit': '细节'}
SEVERITY_ALIAS = {
    'blocker': 'blocker', 'critical': 'blocker', 'severe': 'blocker', 'fatal': 'blocker',
    '严重': 'blocker', '阻断': 'blocker', '致命': 'blocker',
    'major': 'major', 'high': 'major', '重要': 'major', '较重': 'major',
    'minor': 'minor', 'medium': 'minor', '一般': 'minor', '次要': 'minor',
    'nit': 'nit', 'low': 'nit', 'info': 'nit', 'trivial': 'nit', '建议': 'nit', '细节': 'nit',
}
MAX_DIFF_LINES = 500           # 单个文件 diff 最多送给模型的行数
MAX_DIFF_CHARS = 24000         # 单个文件 diff 最多送给模型的字符数
MAX_UNTRACKED_BYTES = 1000000  # 未跟踪文件最多读 1MB，避免把大文件塞进提示词

REVIEW_RULES = '''你是代码审查员，**只读**：不改任何文件、不提交、不推送。

## 审查口径
- 只审查下面 diff 里出现的改动；diff 之外、没看到的代码不要臆测。
- 只报能证实的问题：必须贴出证据行（改动里的代码原文），并指出文件和新文件行号；拿不准就不报。
- 不评论风格偏好（命名喜好、缩进、注释多少、个人写法）。
- 优先级从高到低：安全（注入、越权、密钥泄漏）、正确性、并发、事务、空指针、边界（0 / 空 / 负数 / 超长 / 时区 / 精度）、资源泄漏、错误处理。
- 每条给出可执行的改法（改哪个函数、加什么判断、换成什么调用），不要只说“建议优化”。
- 不编造没看到的代码；没有问题就输出空数组。
- 只输出 JSON 数组，不要任何解释文字，也不要用 Markdown 代码块包裹。

## 输出格式
按严重程度从高到低排列，只输出下面这个 JSON 数组：
[{"file":"相对路径","line":123,"severity":"blocker|major|minor|nit","title":"一句话问题","detail":"证据行 + 触发场景 → 实际结果 → 期望结果","suggestion":"可执行的改法"}]

字段说明：
- line：新文件里的行号（diff 的 + 侧）；无法确定时用 null。
- severity：blocker=会导致数据错误 / 安全漏洞 / 线上故障；major=明确的逻辑或契约缺陷；minor=边界或健壮性不足；nit=可改可不改的小问题。'''


def emit(data):
    print(json.dumps(data, ensure_ascii=False, indent=2))


# ---------- diff 解析 ----------

def _clean_path(raw):
    """把 diff 头里的 a/、b/ 前缀和引号去掉。"""
    path = str(raw or '').strip().split('\t')[0]
    if len(path) >= 2 and path.startswith('"') and path.endswith('"'):
        path = path[1:-1]
    if path != '/dev/null' and path[:2] in ('a/', 'b/'):
        path = path[2:]
    return path


def _dir_of(path):
    return path.rsplit('/', 1)[0] if '/' in path else '.'


def _path_from_header(line):
    """从 `diff --git a/x b/x` 里取一个兜底路径（后面会被 ---/+++ 覆盖）。"""
    rest = line[len('diff --git '):].strip()
    if ' b/' in rest:
        return _clean_path(rest.rsplit(' b/', 1)[-1])
    return _clean_path(rest.split()[-1] if rest.split() else '')


def split_diff(text):
    """按 `diff --git` 切成一个文件一段。返回 [{path, old_path, diff, add, del, binary, new_file}]。"""
    files, cur = [], None
    for raw in (text or '').splitlines():
        line = raw + '\n'
        if raw.startswith('diff --git '):
            if cur:
                files.append(cur)
            cur = {'path': _path_from_header(raw), 'old_path': None, 'diff': line,
                   'add': 0, 'del': 0, 'binary': False, 'new_file': False}
            continue
        if cur is None:
            continue
        cur['diff'] += line
        if raw.startswith('--- '):
            path = _clean_path(raw[4:])
            if path == '/dev/null':
                cur['new_file'] = True
            else:
                cur['old_path'] = path
        elif raw.startswith('+++ '):
            path = _clean_path(raw[4:])
            if path == '/dev/null':
                cur['path'] = cur['old_path'] or cur['path']
            else:
                cur['path'] = path
        elif raw.startswith('rename to '):
            cur['path'] = _clean_path(raw[len('rename to '):])
        elif raw.startswith('+') and not raw.startswith('+++'):
            cur['add'] += 1
        elif raw.startswith('-') and not raw.startswith('---'):
            cur['del'] += 1
        elif raw.startswith('Binary files '):
            cur['binary'] = True
    if cur:
        files.append(cur)
    return files


def limit_diff(file, max_lines=MAX_DIFF_LINES, max_chars=MAX_DIFF_CHARS):
    """单个文件的 diff 超长时截断，保留标志和原始行数。"""
    lines = file['diff'].splitlines()
    kept, chars = [], 0
    for line in lines:
        if len(kept) >= max_lines or chars + len(line) > max_chars:
            break
        kept.append(line)
        chars += len(line) + 1
    out = dict(file, orig_lines=len(lines), truncated=len(kept) < len(lines), diff='\n'.join(kept))
    return out


def _untracked_diff(repo, rel):
    """未跟踪的文本文件合成一段“全新增”的 diff；二进制或读不了返回 None。"""
    path = Path(repo) / rel
    if not path.is_file():
        return None
    try:
        raw = path.read_bytes()[:MAX_UNTRACKED_BYTES]
    except OSError:
        return None
    if b'\0' in raw[:8000]:
        return None
    lines = raw.decode('utf-8', 'replace').splitlines()
    header = 'diff --git a/%s b/%s\nnew file mode 100644\n--- /dev/null\n+++ b/%s\n' % (rel, rel, rel)
    if lines:
        header += '@@ -0,0 +1,%d @@\n' % len(lines)
    body = ''.join('+%s\n' % line for line in lines)
    return {'path': rel, 'old_path': None, 'diff': header + body, 'add': len(lines), 'del': 0,
            'binary': False, 'new_file': True}


def collect_diff(repo, base='origin/main', head='HEAD', uncommitted=False,
                 max_files=40, max_lines=MAX_DIFF_LINES):
    """取 diff 并按目录 / 文件整理。

    uncommitted=True 时取 HEAD 对工作区（含暂存区），未跟踪的文本文件补成“全新增”的 diff；
    否则取 base...head（三点：从分叉点算起）。文件数超过 max_files 时优先保留改动行数多的。
    """
    repo = str(Path(repo).expanduser())
    root = git.repo_root(repo)
    if not root:
        raise git.GitError('%s 不是 git 仓库' % repo)
    repo = root
    skipped = []

    if uncommitted:
        text = git.git(repo, '-c', 'core.quotePath=false', 'diff', 'HEAD', '--') if git.rev(repo, 'HEAD') else ''
        mode, base_name, head_name = 'uncommitted', 'HEAD', None
        # 用 ls-files 逐个列出未跟踪文件（status 默认会把整个未跟踪目录折叠成一条）
        untracked = git.git(repo, '-c', 'core.quotePath=false',
                            'ls-files', '--others', '--exclude-standard').splitlines()
        for rel in (x.strip() for x in untracked):
            if not rel:
                continue
            one = _untracked_diff(repo, _clean_path(rel))
            if one is None:
                skipped.append({'path': _clean_path(rel), 'reason': '二进制或读不了'})
            else:
                text += ('' if not text or text.endswith('\n') else '\n') + one['diff']
    else:
        base = git.ref(base)
        head = git.ref(head or 'HEAD')
        if not git.rev(repo, base):
            raise git.GitError('找不到基线 %s（先 git fetch，或换 --base）' % base)
        if not git.rev(repo, head):
            raise git.GitError('找不到目标 %s' % head)
        text = git.git(repo, '-c', 'core.quotePath=false', 'diff', '%s...%s' % (base, head), '--')
        mode, base_name, head_name = 'branch', base, head

    all_files = split_diff(text)
    files = []
    for one in all_files:
        if one['binary']:
            skipped.append({'path': one['path'], 'reason': '二进制文件'})
        else:
            files.append(one)

    if max_files and len(files) > max_files:
        picked = {f['path'] for f in sorted(
            files, key=lambda f: (f['add'] + f['del'], f['path']), reverse=True)[:max_files]}
        skipped += [{'path': f['path'], 'reason': '超出 --max-files'} for f in files if f['path'] not in picked]
        files = [f for f in files if f['path'] in picked]

    files = [limit_diff(f, max_lines=max_lines) for f in files]
    files.sort(key=lambda f: f['path'])

    groups, cur_dir = [], None
    for f in files:
        f['dir'] = _dir_of(f['path'])
        if f['dir'] != cur_dir:
            groups.append({'dir': f['dir'], 'files': []})
            cur_dir = f['dir']
        groups[-1]['files'].append(f['path'])

    return {'repo': repo, 'mode': mode, 'base': base_name, 'head': head_name,
            'files': files, 'groups': groups, 'skipped': skipped, 'total_files': len(all_files)}


def public_files(files):
    """输出给调用方的文件信息：不带 diff 正文，避免把整份改动塞进结果 JSON。"""
    keys = ('path', 'dir', 'add', 'del', 'orig_lines', 'truncated', 'new_file')
    return [{k: f[k] for k in keys if k in f} for f in files]


# ---------- 提示词 ----------

def build_prompt(diff):
    """把分组后的 diff 拼成提示词，超长文件在标题和正文前都标注已截断。"""
    scope = ['## 本次审查范围',
             '- 仓库：`%s`' % diff['repo']]
    if diff['mode'] == 'uncommitted':
        scope.append('- 模式：未提交改动（HEAD 对工作区，含暂存区与未跟踪文本文件）')
    else:
        scope.append('- 模式：分支 / 提交改动（`%s...%s`，三点表示从分叉点算起）' % (diff['base'], diff['head']))
    scope.append('- 变更文件：%d 个；实际送入审查：%d 个；跳过：%d 个'
                 % (diff['total_files'], len(diff['files']), len(diff['skipped'])))

    body = [REVIEW_RULES, '', '\n'.join(scope), '', '## 改动内容（按目录分组）']
    for group in diff['groups']:
        title = '仓库根目录' if group['dir'] == '.' else group['dir'] + '/'
        body += ['', '### %s' % title]
        for f in diff['files']:
            if f['path'] not in group['files']:
                continue
            cut = ' · 已截断' if f.get('truncated') else ''
            body += ['', '#### `%s`（+%d -%d）%s' % (f['path'], f['add'], f['del'], cut)]
            if f.get('truncated'):
                body.append('> 说明：该文件 diff 过长（原 %d 行），已截断为前 %d 行。'
                            % (f['orig_lines'], len(f['diff'].splitlines())))
            body += ['```diff', f['diff'].rstrip('\n'), '```']
    if diff['skipped']:
        body += ['', '## 本次没有审查的文件']
        body += ['- `%s`（%s）' % (s['path'], s['reason']) for s in diff['skipped']]
    return '\n'.join(body) + '\n'


# ---------- 结果解析与合并 ----------

def normalize_severity(value):
    text = str(value or '').strip().lower()
    return SEVERITY_ALIAS.get(text, 'minor')


def _excerpt(text, limit=300):
    text = str(text or '').strip().replace('\n', ' ')
    return text[:limit] + ('…' if len(text) > limit else '')


def parse_issues(text):
    """从模型输出里取问题数组。允许前后有说明、被 ```json 包着，也允许外层是 {issues|findings|items}。

    解析失败抛 ValueError；调用方负责把原始输出落盘并告诉用户位置。
    """
    raw = (text or '').strip()
    if not raw:
        raise ValueError('模型输出为空')
    start = min([i for i in (raw.find('['), raw.find('{')) if i >= 0], default=-1)
    if start < 0:
        raise ValueError('输出里没有 JSON 数组：%s' % _excerpt(raw))
    try:
        obj, _ = json.JSONDecoder().raw_decode(raw[start:])
    except ValueError as e:
        raise ValueError('JSON 解析失败（%s）：%s' % (e, _excerpt(raw[start:])))
    if isinstance(obj, dict):
        for key in ('issues', 'findings', 'items', 'comments'):
            if isinstance(obj.get(key), list):
                obj = obj[key]
                break
        else:
            obj = [obj] if (obj.get('file') or obj.get('path')) and obj.get('title') else None
    if not isinstance(obj, list):
        raise ValueError('JSON 不是问题数组：%s' % _excerpt(raw[start:]))

    issues = []
    for item in obj:
        if not isinstance(item, dict):
            continue
        path = str(item.get('file') or item.get('path') or '').strip()
        title = str(item.get('title') or item.get('message') or '').strip()
        if not path or not title:
            continue
        try:
            line = int(item['line']) if item.get('line') not in (None, '') else None
        except (TypeError, ValueError):
            line = None
        issues.append({
            'file': path,
            'line': line if line and line > 0 else None,
            'severity': normalize_severity(item.get('severity')),
            'title': title,
            'detail': str(item.get('detail') or item.get('description') or '').strip(),
            'suggestion': str(item.get('suggestion') or item.get('fix') or '').strip(),
        })
    return issues


def _norm_key(issue):
    path = str(issue.get('file') or '').strip().replace('\\', '/')
    while path.startswith('./'):
        path = path[2:]
    title = re.sub(r'[\s\W_]+', '', str(issue.get('title') or '').lower())
    return path, int(issue.get('line') or 0), title


def merge_issues(per_engine):
    """按 (文件, 行号, 标题归一化) 去重合并，记下哪些模型都提到了（agreement）。

    严重程度取最高；详情 / 建议取偏长的那份（信息更全）。
    """
    buckets, order = {}, []
    for engine, issues in per_engine:
        for issue in issues or []:
            key = _norm_key(issue)
            if key not in buckets:
                merged = dict(issue)
                merged.pop('engine', None)
                merged['severity'] = normalize_severity(issue.get('severity'))
                merged['agreement'] = [engine]
                buckets[key] = merged
                order.append(key)
                continue
            merged = buckets[key]
            if engine not in merged['agreement']:
                merged['agreement'].append(engine)
            if RANK.get(normalize_severity(issue.get('severity')), 2) < RANK.get(merged['severity'], 2):
                merged['severity'] = normalize_severity(issue.get('severity'))
            if len(str(issue.get('detail') or '')) > len(str(merged.get('detail') or '')):
                merged['detail'] = issue['detail']
            if len(str(issue.get('suggestion') or '')) > len(str(merged.get('suggestion') or '')):
                merged['suggestion'] = issue['suggestion']

    out = []
    for key in order:
        merged = buckets[key]
        merged['agreement'] = sorted(merged['agreement'])
        merged['agreement_count'] = len(merged['agreement'])
        out.append(merged)
    out.sort(key=lambda i: (RANK.get(i.get('severity'), 2), str(i.get('file') or ''),
                            int(i.get('line') or 0)))
    return out


# ---------- 调模型 ----------

def describe_call(engine, prompt, repo, profile, conf=None):
    """--dry-run 用：只算将执行的 argv / cwd / profile，不启动进程，也不打印 key。"""
    call = {'engine': engine, 'cwd': str(Path(repo).resolve()), 'profile': profile,
            'prompt_chars': len(prompt)}
    try:
        argv, env = agent.command(engine, '', str(repo), profile=profile, conf=conf)
        call.update(ok=True, argv=argv,
                    env_keys=sorted(k for k in env if k.endswith(('KEY', 'TOKEN', 'MODEL'))))
    except agent.AgentError as e:
        call.update(ok=False, argv=None, error=str(e))
    return call


def _write_raw(text, out, engine):
    """解析失败时把模型原始输出落盘，返回路径。给了 --out 就放在它旁边。"""
    if out:
        target = Path(out).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        path = target.with_name('%s.raw-%s.txt' % (target.stem, engine))
        path.write_text(text or '', encoding='utf-8')
        return str(path)
    fd, name = tempfile.mkstemp(prefix='review-raw-', suffix='.txt')
    os.close(fd)
    path = Path(name)
    path.write_text(text or '', encoding='utf-8')
    return str(path)


def _execute(engine, prompt, repo, profile, timeout, runner, out):
    """跑一家模型并解析。runner 可注入，方便单测。"""
    runner = runner or agent.run
    try:
        result = runner(engine, prompt, cwd=str(repo), profile=profile, timeout=timeout)
    except agent.AgentError as e:
        return {'engine': engine, 'ok': False, 'seconds': 0, 'models': [],
                'issues': [], 'error': str(e)}
    base = {'engine': engine, 'ok': bool(result.get('ok')), 'seconds': result.get('seconds', 0),
            'models': result.get('models') or [], 'log': result.get('log')}
    if not result.get('ok'):
        return dict(base, issues=[], error=str(result.get('error') or '模型调用失败')[:500])
    try:
        issues = parse_issues(result.get('text'))
    except ValueError as e:
        raw = _write_raw(result.get('text') or '', out, engine)
        return dict(base, ok=False, issues=[], raw_output=raw,
                    error='输出不是合法的 JSON 问题数组：%s；原始输出见 %s' % (e, raw))
    for issue in issues:
        issue['engine'] = engine
    issues.sort(key=lambda i: (RANK.get(i.get('severity'), 2), str(i.get('file') or ''),
                               int(i.get('line') or 0)))
    return dict(base, issues=issues, issue_count=len(issues), error=None)


def _meta(diff, prompt):
    return {'repo': diff['repo'],
            'range': {'mode': diff['mode'], 'base': diff['base'], 'head': diff['head']},
            'files': public_files(diff['files']), 'groups': diff['groups'],
            'skipped': diff['skipped'], 'total_files': diff['total_files'],
            'prompt_chars': len(prompt)}


# ---------- 子命令实现 ----------

def review(repo, base='origin/main', head='HEAD', uncommitted=False, engine='kimi',
           profile='read', max_files=40, timeout=1200, out='', dry_run=False, runner=None,
           conf=None):
    """取一次 diff，交给一个模型审查。返回结果 dict（不写文件，由 CLI 决定是否落盘）。"""
    diff = collect_diff(repo, base=base, head=head, uncommitted=uncommitted, max_files=max_files)
    prompt = build_prompt(diff)
    meta = _meta(diff, prompt)
    if not diff['files']:
        return dict(meta, ok=True, engine=engine, issues=[], issue_count=0, note='没有可审查的改动')
    if dry_run:
        return dict(meta, ok=True, dry_run=True, engine=engine,
                    call=describe_call(engine, prompt, diff['repo'], profile, conf))
    run = _execute(engine, prompt, diff['repo'], profile, timeout, runner, out)
    result = dict(meta, engine=engine, ok=run['ok'], seconds=run.get('seconds'),
                  models=run.get('models'), issues=run.get('issues') or [],
                  issue_count=len(run.get('issues') or []))
    for key in ('error', 'log', 'raw_output'):
        if run.get(key):
            result[key] = run[key]
    return result


def batch(repo, engines, base='origin/main', head='HEAD', uncommitted=False, profile='read',
          max_files=40, timeout=1200, out='', dry_run=False, runner=None, conf=None):
    """同一份 diff 交给多个模型并跑，再按 (文件, 行号, 标题) 合并去重。"""
    diff = collect_diff(repo, base=base, head=head, uncommitted=uncommitted, max_files=max_files)
    prompt = build_prompt(diff)
    meta = _meta(diff, prompt)
    if dry_run:
        return dict(meta, ok=True, dry_run=True, engines=list(engines),
                    calls=[describe_call(e, prompt, diff['repo'], profile, conf) for e in engines])
    if not diff['files']:
        return dict(meta, ok=True, engines=list(engines), runs=[], issues=[], issue_count=0,
                    note='没有可审查的改动')

    with ThreadPoolExecutor(max_workers=max(1, len(engines))) as pool:
        futures = {e: pool.submit(_execute, e, prompt, diff['repo'], profile, timeout, runner, out)
                   for e in engines}
        runs = [futures[e].result() for e in engines]

    merged = merge_issues([(r['engine'], r.get('issues') or []) for r in runs if r['ok']])
    public_runs = [{k: r[k] for k in ('engine', 'ok', 'seconds', 'models', 'issue_count',
                                      'error', 'log', 'raw_output') if k in r} for r in runs]
    return dict(meta, ok=bool(merged) or any(r['ok'] for r in runs), engines=list(engines),
                runs=public_runs, issues=merged, issue_count=len(merged))


def render_markdown(data):
    """把 review / batch 的结果渲染成 markdown：按严重程度分组，带 文件:行。"""
    if isinstance(data, list):
        data = {'issues': data}
    issues = data.get('issues') or []
    range_ = data.get('range') or {}
    engines = data.get('engines') or ([data['engine']] if data.get('engine') else [])
    if range_.get('mode') == 'uncommitted':
        scope = '%s · 未提交改动' % (data.get('repo') or '?')
    else:
        scope = '%s · %s...%s' % (data.get('repo') or '?', range_.get('base') or '?',
                                  range_.get('head') or '?')
    counts = {s: sum(1 for i in issues if i.get('severity') == s) for s in SEVERITIES}
    summary = '，'.join('%s %d' % (SEVERITY_LABEL[s], counts[s]) for s in SEVERITIES if counts[s]) or '无'
    lines = ['# 代码审查报告', '',
             '- 审查范围：%s' % scope,
             '- 模型：%s' % ('、'.join(engines) if engines else '?'),
             '- 问题：%d 条（%s）' % (len(issues), summary), '']

    failed = [r for r in data.get('runs') or [] if not r.get('ok')]
    if failed:
        lines += ['> 未跑成：%s' % '；'.join('%s（%s）' % (r.get('engine'), (r.get('error') or '')[:80])
                                          for r in failed), '']
    if data.get('skipped'):
        lines += ['> 未审查的文件：%s' % '、'.join('`%s`（%s）' % (s.get('path'), s.get('reason'))
                                              for s in data['skipped']), '']
    if not issues:
        lines += ['未发现问题。', '']
        return '\n'.join(lines).rstrip() + '\n'

    for severity in SEVERITIES:
        group = sorted([i for i in issues if i.get('severity') == severity],
                       key=lambda i: (str(i.get('file') or ''), int(i.get('line') or 0)))
        if not group:
            continue
        lines += ['## %s（%s，%d 条）' % (SEVERITY_LABEL[severity], severity, len(group)), '']
        for issue in group:
            loc = ('`%s:%s`' % (issue.get('file'), issue.get('line'))
                   if issue.get('line') else '`%s`' % issue.get('file'))
            lines += ['### %s %s' % (loc, issue.get('title') or ''), '']
            reported = issue.get('agreement') or ([issue['engine']] if issue.get('engine') else [])
            if reported:
                lines.append('- 提及模型：%s%s' % ('、'.join(reported),
                             '（%d 家一致）' % len(reported) if len(reported) > 1 else ''))
            if issue.get('detail'):
                lines.append('- 详情：' + str(issue['detail']).replace('\n', '\n  '))
            if issue.get('suggestion'):
                lines.append('- 建议：' + str(issue['suggestion']).replace('\n', '\n  '))
            lines.append('')
    return '\n'.join(lines).rstrip() + '\n'


# ---------- CLI ----------

def _write_json(path, data):
    if not path:
        return
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def cmd_review(a):
    data = review(a.repo, base=a.base, head=a.head, uncommitted=a.uncommitted, engine=a.engine,
                  profile=a.profile, max_files=a.max_files, timeout=a.timeout, out=a.out,
                  dry_run=a.dry_run)
    _write_json(a.out, data)
    emit(data)
    return 0 if data.get('ok') else 1


def cmd_batch(a):
    engines = [e.strip() for e in re.split(r'[,，\s]+', a.engines) if e.strip()]
    bad = [e for e in engines if e not in agent.ENGINES]
    if bad:
        print(json.dumps({'ok': False, 'error': '不认识的引擎：%s（可选 %s）'
                          % ('、'.join(bad), '、'.join(agent.ENGINES))}, ensure_ascii=False),
              file=sys.stderr)
        return 2
    data = batch(a.repo, engines, base=a.base, head=a.head, uncommitted=a.uncommitted,
                 profile=a.profile, max_files=a.max_files, timeout=a.timeout, out=a.out,
                 dry_run=a.dry_run)
    _write_json(a.out, data)
    emit(data)
    return 0 if data.get('ok') else 1


def cmd_report(a):
    try:
        data = json.loads(Path(a.input).expanduser().read_text(encoding='utf-8'))
    except (OSError, ValueError) as e:
        print(json.dumps({'ok': False, 'error': '读不了结果文件：%s' % e}, ensure_ascii=False),
              file=sys.stderr)
        return 2
    text = render_markdown(data)
    if a.out:
        target = Path(a.out).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding='utf-8')
    print(text, end='')
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description='多模型代码审查：取 diff、分组截断、一个或多个模型审查并合并，输出 JSON')
    sub = p.add_subparsers(dest='cmd', required=True)

    def common(s):
        s.add_argument('--repo', default='.', help='仓库目录')
        s.add_argument('--base', default='origin/main', help='基线 ref（默认 origin/main）')
        s.add_argument('--head', default='HEAD', help='目标 ref（默认 HEAD）')
        s.add_argument('--uncommitted', action='store_true', help='审查未提交改动（含暂存区和未跟踪文本文件）')
        s.add_argument('--profile', default='read', choices=sorted(agent.PROFILES), help='模型权限档位')
        s.add_argument('--max-files', type=int, default=40, help='最多送审文件数（默认 40）')
        s.add_argument('--timeout', type=int, default=1200, help='单次模型调用超时秒数')
        s.add_argument('--out', default='', help='结果 JSON 写到这里')
        s.add_argument('--dry-run', action='store_true', help='只打印将执行的代理参数，不真跑')

    s = sub.add_parser('review', help='一个模型审查一次')
    common(s)
    s.add_argument('--engine', default='kimi', choices=list(agent.ENGINES))
    s.set_defaults(fn=cmd_review)

    s = sub.add_parser('batch', help='多个模型并跑并合并')
    common(s)
    s.add_argument('--engines', default='kimi,codex', help='逗号分隔，如 kimi,codex')
    s.set_defaults(fn=cmd_batch)

    s = sub.add_parser('report', help='把结果 JSON 渲染成 markdown')
    s.add_argument('--input', required=True)
    s.add_argument('--out', default='', help='markdown 写到这里（不给就只打印）')
    s.set_defaults(fn=cmd_report)

    a = p.parse_args(argv)
    try:
        return a.fn(a)
    except git.GitError as e:
        print(json.dumps({'ok': False, 'error': str(e)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
