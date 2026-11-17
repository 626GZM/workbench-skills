#!/usr/bin/env python3
"""扫描代码仓库里的 TODO / 遗留问题，输出可跟踪的清单。

纯静态扫描：只读文件、只读 git，不调用模型、不改任何文件。
四个子命令：scan（全量扫描）、report（把扫描结果渲染成 markdown）、
diff（只报本次改动新增的条目）、stats（按类型 / 作者 / 年龄分桶）。
除 report 输出 markdown 外，其余都输出 JSON。
"""
import argparse
import json
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / '_lib'))

from wbkit import git  # noqa: E402

# 默认扫描的扩展名；--ext 传了就整套替换
DEFAULT_EXTS = ['.java', '.kt', '.py', '.js', '.ts', '.tsx', '.jsx', '.vue',
                '.go', '.rs', '.rb', '.php', '.cs', '.scala', '.sql', '.xml',
                '.yml', '.yaml', '.properties', '.sh']
# 默认整棵跳过的目录；--exclude 传了就整套替换
DEFAULT_EXCLUDE = ['.git', 'node_modules', 'target', 'dist', 'build', 'out',
                   '.idea', '.gradle', '.venv', 'venv', '__pycache__']
# 只在这些扩展名上做“结构类”检查（空 catch、注释掉的代码等）
CODE_EXTS = {'.java', '.kt', '.py', '.js', '.ts', '.tsx', '.jsx', '.vue',
             '.cs', '.scala', '.go', '.rs', '.php', '.rb', '.m', '.swift'}
MAX_FILE_BYTES = 2 * 1024 * 1024
TEXT_LIMIT = 200

# 严重程度：fixme/hack/debug/empty_catch/swallowed_exception 高，todo 中，注释掉的代码低
SEVERITY = {
    'fixme': 'high',
    'hack': 'high',
    'debug': 'high',
    'empty_catch': 'high',
    'swallowed_exception': 'high',
    'todo': 'medium',
    'commented_code': 'low',
}
SEV_ORDER = {'high': 0, 'medium': 1, 'low': 2}

# 标记词 → kind。XXX 按 todo 算，tag 里保留原词
MARKER_KIND = {
    'TODO': 'todo', '待办': 'todo', '待改': 'todo',
    'XXX': 'todo',
    'FIXME': 'fixme',
    'HACK': 'hack',
}

MARK_RE = re.compile(r'\b(TODO|FIXME|XXX|HACK)\b|(待办|待改)')
# 调试残留：只认这三类最典型的
DEBUG_RES = [
    ('CONSOLE_LOG', re.compile(r'\bconsole\s*\.\s*log\s*\(')),
    ('PRINT_STACK_TRACE', re.compile(r'\.\s*printStackTrace\s*\(')),
    ('SYSTEM_OUT', re.compile(r'\bSystem\s*\.\s*out\s*\.\s*println\s*\(')),
]

CATCH_RE = re.compile(r'\bcatch\s*(?:\([^)]*\))?\s*\{')
CATCH_HEAD = re.compile(r'\bcatch\b')
EXCEPT_RE = re.compile(r'^(\s*)except\b[^:]*:\s*(.*)$')
COMMENT_LINE_RE = re.compile(r'^\s*(//|/\*|\*|#|<!--)')

# catch 体里出现这些，说明异常被处理过，不算吞掉
HANDLED_RE = re.compile(r'\b(throw|raise|return)\b|\.\s*(error|fatal)\s*\(|logger\s*\.', re.I)
# 只有“打印一下”的 catch / except 才算异常被吞，其余（清理、重试、continue）宁可漏报
PRINT_STMT_RE = re.compile(
    r'^(?:[\w$.]*\s*\.\s*)?(?:printStackTrace|print|println|'
    r'console\s*\.\s*log|System\s*\.\s*(?:out|err)\s*\.\s*println)\s*\(.*\)$')
# 注释块里出现这些词多半是版权头，不当成注释掉的代码
LICENSE_HINT = re.compile(r'copyright|license|licensed|apache|gpl|spdx', re.I)
DOC_TAG = re.compile(r'@\w+')
CODE_KEYWORDS = re.compile(
    r'\b(if|else|for|while|return|function|def|class|public|private|protected|static|final|'
    r'void|int|long|double|float|boolean|char|String|var|let|const|import|export|new|this|'
    r'self|try|catch|switch|case|break|continue|throw|raise|await|async|lambda|print|printf)\b')
CALL_RE = re.compile(r'[A-Za-z_]\w*\s*\(')
ASSIGN_RE = re.compile(r'[A-Za-z_]\w*\s*=\s*[^=]')
HUNK_RE = re.compile(r'^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@')


class ScanError(Exception):
    pass


# ---------------------------------------------------------------- 通用小工具

def emit(data):
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _parse_list(value, default):
    if not value:
        return list(default)
    return [x.strip() for x in value.split(',') if x.strip()]


def _norm_exts(items):
    out = set()
    for e in items:
        e = e.lower()
        out.add(e if e.startswith('.') else '.' + e)
    return out


def _clean_text(text, limit=TEXT_LIMIT):
    """把一段文本压成单行，去掉开头多余的标点。"""
    s = ' '.join(str(text or '').split())
    s = re.sub(r'^[:：\-–—]+\s*', '', s)
    return s[:limit]


def _comment_pos(line):
    """行内第一个注释起始位置；没有注释返回 -1。"""
    best = -1
    for pat in (r'//', r'<!--', r'/\*', r'#'):
        m = re.search(pat, line)
        if m and (best < 0 or m.start() < best):
            best = m.start()
    if best < 0 and re.match(r'^\s*\*', line):
        return len(line) - len(line.lstrip())
    return best


def _in_string(line, pos):
    """粗略判断 pos 是否落在字符串字面量里：数引号的奇偶。"""
    head = re.sub(r'\\["\'`]', '', line[:pos])
    return any(head.count(q) % 2 == 1 for q in ('"', "'", '`'))


def _strip_comments(text):
    """去掉块注释、行注释和 # 注释，用于判断 catch / except 体里还剩什么。"""
    text = re.sub(r'/\*.*?\*/', ' ', text, flags=re.S)
    text = re.sub(r'//[^\n]*', ' ', text)
    text = re.sub(r'#[^\n]*', ' ', text)
    return ' '.join(text.split())


def _is_comment_line(line):
    s = line.strip()
    if not s or s.startswith('#!'):
        return False
    if s in ('*/', '/*', '//', '*'):
        return True
    return bool(COMMENT_LINE_RE.match(s))


def _iter_files(root, exts, exclude):
    """代码文件；排除目录整棵不往里走，顺序固定方便复现。"""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames
                             if d not in exclude and not d.startswith('.'))
        for name in sorted(filenames):
            p = Path(dirpath) / name
            if name.startswith('.') or p.suffix.lower() not in exts:
                continue
            try:
                if p.stat().st_size > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            yield p


def _read_text(path):
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if b'\x00' in raw[:8192]:
        return None
    return raw.decode('utf-8', 'replace')


# ---------------------------------------------------------------- 结构类检查

def _locate_open_brace(lines, idx, col):
    """找 catch 后面那个 { 的位置；支持 { 写在下一行。"""
    pos = lines[idx].find('{', col)
    if pos >= 0:
        return idx, pos
    for nxt in range(idx + 1, min(len(lines), idx + 3)):
        if lines[nxt].strip().startswith('{'):
            return nxt, lines[nxt].index('{')
    return None, None


def _brace_block(lines, start, col):
    """从 start 行的 col 处的 { 开始，配平到对应的 }，返回 (结束行号, 块内文本)。"""
    depth = 0
    started = False
    body = []
    for idx in range(start, min(len(lines), start + 400)):
        line = lines[idx]
        pos = col if idx == start else 0
        for ch in line[pos:]:
            if ch == '{':
                depth += 1
                if depth == 1:
                    started = True
                    continue
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    return idx, ''.join(body)
            if started:
                body.append(ch)
        if started:
            body.append('\n')
    return None, None


def _only_prints(body):
    """整个块里只剩打印语句（不抛、不打错误日志）。"""
    stmts = [s.strip() for s in body.split(';') if s.strip()]
    return bool(stmts) and all(PRINT_STMT_RE.match(s) for s in stmts)


def _scan_catches(lines):
    """空 catch / 异常被吞。只看看得懂的 { } 块。"""
    out = []
    for idx, line in enumerate(lines):
        if _is_comment_line(line):
            continue
        m = CATCH_RE.search(line)
        if m:
            bidx, col = idx, m.end() - 1
        else:
            head = CATCH_HEAD.search(line)
            if not head:
                continue
            bidx, col = _locate_open_brace(lines, idx, head.end())
            if bidx is None:
                continue
        end, body = _brace_block(lines, bidx, col)
        if end is None:
            continue
        clean = _strip_comments(body)
        lineno = idx + 1
        if not clean.strip('; '):
            out.append((lineno, 'empty_catch', 'EMPTY_CATCH', 'catch 块为空'))
        elif not HANDLED_RE.search(clean) and _only_prints(clean):
            out.append((lineno, 'swallowed_exception', 'SWALLOWED_EXCEPTION',
                        _clean_text(body) or 'catch 块没有处理异常'))
    return out


def _scan_excepts(lines):
    """Python 的 except: pass、以及 except 里只打印不抛的写法。"""
    out = []
    for idx, line in enumerate(lines):
        m = EXCEPT_RE.match(line)
        if not m:
            continue
        indent = len(m.group(1))
        rest = m.group(2).strip()
        if rest and not rest.startswith('#'):
            body = rest
        else:
            body_lines = []
            for j in range(idx + 1, min(len(lines), idx + 50)):
                cur = lines[j]
                if not cur.strip():
                    body_lines.append(cur)
                    continue
                if len(cur) - len(cur.lstrip()) <= indent:
                    break
                body_lines.append(cur)
            body = '\n'.join(body_lines)
        clean = _strip_comments(body).strip()
        lineno = idx + 1
        if clean in ('pass', '...'):
            out.append((lineno, 'swallowed_exception', 'SWALLOWED_EXCEPTION', 'except 里只有 pass'))
        elif clean and not HANDLED_RE.search(clean) and _only_prints(clean):
            out.append((lineno, 'swallowed_exception', 'SWALLOWED_EXCEPTION',
                        _clean_text(body) or 'except 没有处理异常'))
    return out


def _code_score(line):
    """一行注释“像代码”的程度。"""
    body = re.sub(r'^\s*(//|/\*|\*|#|<!--)', '', line).strip()
    if not body:
        return 0
    score = 0
    if body.rstrip().endswith((';', '{', '}', ');')):
        score += 1
    if CODE_KEYWORDS.search(body):
        score += 1
    if CALL_RE.search(body):
        score += 1
    if ASSIGN_RE.search(body):
        score += 1
    if re.search(r'[{}]', body):
        score += 1
    return score


def _looks_license(run):
    if sum(len(LICENSE_HINT.findall(x)) for x in run) >= 2:
        return True
    return sum(1 for x in run if DOC_TAG.search(x)) >= 2


def _scan_commented_blocks(lines):
    """连续 5 行以上、且整体看起来像代码的注释块，报一行。"""
    out = []
    i, n = 0, len(lines)
    while i < n:
        if not _is_comment_line(lines[i]):
            i += 1
            continue
        start = i
        while i < n and _is_comment_line(lines[i]):
            i += 1
        run = lines[start:i]
        if len(run) < 5 or _looks_license(run):
            continue
        score = sum(_code_score(x) for x in run)
        code_lines = sum(1 for x in run if _code_score(x) > 0)
        if score >= 3 and code_lines >= 3:
            out.append((start + 1, 'commented_code', 'COMMENTED_CODE',
                        _clean_text(run[0]) or '连续注释掉的代码'))
    return out


# ---------------------------------------------------------------- 单文件扫描

def _scan_text(text, ext):
    """返回 [(行号, kind, tag, text)]，按行号排好。"""
    lines = text.splitlines()
    hits = []

    # 1) 行内标记（只认注释里的）和调试残留
    for i, line in enumerate(lines, 1):
        cpos = _comment_pos(line)
        m = MARK_RE.search(line)
        if m and cpos >= 0 and m.start() > cpos:
            token = m.group(0)
            kind = MARKER_KIND.get(token.upper(), MARKER_KIND.get(token, 'todo'))
            hits.append((i, kind, token.upper(), _clean_text(line[m.end():])))
        for tag, rx in DEBUG_RES:
            dm = rx.search(line)
            if dm and (cpos < 0 or dm.start() < cpos) and not _in_string(line, dm.start()):
                hits.append((i, 'debug', tag, _clean_text(line[dm.start():])))
                break

    # 2) 结构类检查只在代码文件上做（XML/YAML 里的注释不代表代码）
    if ext in CODE_EXTS:
        hits += _scan_catches(lines)
        hits += _scan_excepts(lines)
        hits += _scan_commented_blocks(lines)

    hits.sort(key=lambda x: (x[0], x[1]))
    return hits


# ---------------------------------------------------------------- blame

def _blame(repo, rel, line, is_git):
    """取某一行的作者和提交；不在 git 仓库或文件没入库时返回空。"""
    if not is_git:
        return '', '', None
    out = git.git(repo, 'blame', '-L', '%d,%d' % (line, line),
                  '--line-porcelain', '--', rel, check=False)
    if not out:
        return '', '', None
    sha, author, ts = '', '', None
    for row in out.splitlines():
        if row.startswith('author '):
            author = row[7:].strip()
        elif row.startswith('author-time '):
            try:
                ts = int(row.split()[1])
            except (IndexError, ValueError):
                ts = None
    first = out.splitlines()[0].split()
    if first:
        sha = first[0]
    return author, sha, ts


# ---------------------------------------------------------------- 汇总

def _collect(repo, ext_text, exclude_text, keep=None):
    """走一遍文件，拿到候选条目（还没取 blame）。"""
    root = Path(repo).expanduser().resolve()
    if not root.is_dir():
        raise ScanError('目录不存在：%s' % repo)
    exts = _norm_exts(_parse_list(ext_text, DEFAULT_EXTS))
    exclude = set(_parse_list(exclude_text, DEFAULT_EXCLUDE))
    candidates, scanned = [], 0
    for path in _iter_files(root, exts, exclude):
        text = _read_text(path)
        if text is None:
            continue
        scanned += 1
        rel = str(path.relative_to(root))
        for line_no, kind, tag, content in _scan_text(text, path.suffix.lower()):
            if keep is None or keep(rel, line_no):
                candidates.append((rel, line_no, kind, tag, content))
    return str(root), candidates, scanned


def _finalize(repo, candidates, limit):
    """去重、排序、截断后再逐条取 blame，避免对超大仓库打一堆 git。"""
    structural = {(c[0], c[1]) for c in candidates
                  if c[2] in ('empty_catch', 'swallowed_exception')}
    seen, uniq = set(), []
    for rel, line_no, kind, tag, content in candidates:
        key = (rel, line_no, kind)
        if key in seen:
            continue
        # 同一行既报了 catch 结构又报了 printStackTrace，只留更有信息量的结构类
        if kind == 'debug' and (rel, line_no) in structural:
            continue
        seen.add(key)
        uniq.append((rel, line_no, kind, tag, content))
    uniq.sort(key=lambda c: (c[0], c[1], c[2]))
    matched = len(uniq)
    truncated = matched > limit
    uniq = uniq[:limit]

    is_git = git.repo_root(repo) is not None
    cache = {}

    def blame_of(rel, line_no):
        key = (rel, line_no)
        if key not in cache:
            cache[key] = _blame(repo, rel, line_no, is_git)
        return cache[key]

    entries = []
    now = time.time()
    for rel, line_no, kind, tag, content in uniq:
        author, commit, ts = blame_of(rel, line_no)
        age = None if ts is None else max(0, int((now - ts) // 86400))
        entries.append({
            'file': rel,
            'line': line_no,
            'kind': kind,
            'tag': tag,
            'text': content,
            'author': author,
            'commit': commit,
            'age_days': age,
            'severity': SEVERITY[kind],
        })
    return entries, matched, truncated


def scan_repo(repo, ext_text='', exclude_text='', limit=500, keep=None):
    root, candidates, scanned = _collect(repo, ext_text, exclude_text, keep=keep)
    entries, matched, truncated = _finalize(root, candidates, limit)
    return {'repo': root, 'files': scanned, 'entries': entries,
            'total': len(entries), 'matched': matched, 'truncated': truncated}


# ---------------------------------------------------------------- diff

def _added_lines(repo, base):
    """相对 merge-base 的新增行；未跟踪的新文件整篇算新增。"""
    mb = git.merge_base(repo, base, 'HEAD')
    if not mb:
        return None, None
    text = git.git(repo, '-c', 'core.quotePath=false', 'diff', '--no-color',
                   '--relative', '-U0', mb, check=False)
    ranges, cur = {}, None
    for line in text.splitlines():
        if line.startswith('+++ '):
            path = line[4:].split('\t')[0].strip()
            if path == '/dev/null' or path.startswith('"'):
                cur = None
            else:
                cur = path[2:] if path.startswith('b/') else path
        elif line.startswith('@@') and cur:
            m = HUNK_RE.match(line)
            if not m:
                continue
            start = int(m.group(1))
            count = int(m.group(2) or 1)
            if count > 0:
                ranges.setdefault(cur, []).append((start, start + count - 1))
    untracked = set()
    out = git.git(repo, 'ls-files', '--others', '--exclude-standard', check=False)
    for row in out.splitlines():
        if row.strip():
            untracked.add(row.strip())
    return ranges, untracked


def diff_repo(repo, base, ext_text='', exclude_text='', limit=500):
    root = Path(repo).expanduser().resolve()
    ranges, untracked = _added_lines(str(root), base)
    if ranges is None:
        raise ScanError('找不到 %s 和 HEAD 的共同祖先，无法对比' % base)

    def keep(rel, line_no):
        if rel in untracked:
            return True
        return any(a <= line_no <= b for a, b in ranges.get(rel, ()))

    data = scan_repo(str(root), ext_text, exclude_text, limit, keep=keep)
    data['base'] = base
    data['merge_base'] = git.merge_base(str(root), base, 'HEAD')
    return data


# ---------------------------------------------------------------- stats

def _age_bucket(days):
    if days is None:
        return 'unknown'
    if days <= 7:
        return '0-7'
    if days <= 30:
        return '8-30'
    if days <= 90:
        return '31-90'
    if days <= 180:
        return '91-180'
    return '180+'


def stats_repo(repo, ext_text='', exclude_text='', limit=500):
    data = scan_repo(repo, ext_text, exclude_text, limit)
    entries = data['entries']
    by_kind = Counter(e['kind'] for e in entries)
    by_author = Counter(e['author'] or '未归属' for e in entries)
    by_age = Counter(_age_bucket(e['age_days']) for e in entries)
    by_severity = Counter(e['severity'] for e in entries)
    return {
        'repo': data['repo'],
        'files': data['files'],
        'total': data['total'],
        'matched': data['matched'],
        'truncated': data['truncated'],
        'by_kind': dict(sorted(by_kind.items(), key=lambda kv: (-kv[1], kv[0]))),
        'by_author': dict(sorted(by_author.items(), key=lambda kv: (-kv[1], kv[0]))),
        'by_age': {k: by_age.get(k, 0) for k in ('0-7', '8-30', '31-90', '91-180', '180+', 'unknown')},
        'by_severity': {k: by_severity.get(k, 0) for k in ('high', 'medium', 'low')},
    }


# ---------------------------------------------------------------- report

def _load_entries(path):
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
    except OSError as e:
        raise ScanError('读不了输入文件：%s（%s）' % (path, e))
    except ValueError as e:
        raise ScanError('输入不是合法 JSON：%s（%s）' % (path, e))
    if isinstance(data, list):
        return data, ''
    if isinstance(data, dict):
        return data.get('entries') or [], data.get('repo') or ''
    raise ScanError('输入 JSON 既不是数组也没有 entries 字段')


def _esc(text):
    return str(text or '').replace('|', '\\|').replace('\n', ' ')


def _group_value(entry, by):
    if by == 'author':
        return entry.get('author') or '未归属'
    return entry.get(by) or ''


def _groups(entries, by):
    counts = Counter(_group_value(e, by) for e in entries)
    if by == 'severity':
        keys = sorted(counts, key=lambda k: (SEV_ORDER.get(k, 9), k))
    elif by == 'kind':
        keys = sorted(counts, key=lambda k: (-counts[k], k))
    else:
        keys = sorted(counts)
    return [(k, [e for e in entries if _group_value(e, by) == k]) for k in keys]


def render_report(entries, repo, group_by='file'):
    lines = ['# TODO / 遗留问题清单', '']
    if repo:
        lines.append('- 仓库：`%s`' % repo)
    lines.append('- 共 %d 条' % len(entries))
    sev = Counter(e.get('severity') for e in entries)
    parts = ['%s %d' % (k, sev[k]) for k in ('high', 'medium', 'low') if sev.get(k)]
    if parts:
        lines.append('- 严重度：' + ' / '.join(parts))
    lines.append('')
    if not entries:
        lines.append('（没有发现需要跟踪的条目）')
        return '\n'.join(lines)

    show_file = group_by != 'file'
    for key, items in _groups(entries, group_by):
        lines.append('## %s（%d 条）' % (key, len(items)))
        lines.append('')
        head = ['行'] + (['文件'] if show_file else []) + ['类型', '严重度', '责任人', '提交', '年龄', '内容']
        lines.append('| ' + ' | '.join(head) + ' |')
        lines.append('|' + '|'.join(['---'] * len(head)) + '|')
        for e in sorted(items, key=lambda x: (x.get('file') or '', x.get('line') or 0)):
            row = [str(e.get('line') or '')]
            if show_file:
                row.append(_esc(e.get('file')))
            row += [
                _esc(e.get('kind')),
                _esc(e.get('severity')),
                _esc(e.get('author') or '未归属'),
                _esc((e.get('commit') or '')[:8] or '-'),
                '-' if e.get('age_days') is None else '%d 天' % e['age_days'],
                _esc(e.get('text')),
            ]
            lines.append('| ' + ' | '.join(row) + ' |')
        lines.append('')
    return '\n'.join(lines).rstrip() + '\n'


# ---------------------------------------------------------------- CLI

def add_scan_args(s):
    s.add_argument('--repo', default='.', help='仓库目录')
    s.add_argument('--ext', default='', help='逗号分隔，默认常见代码扩展名')
    s.add_argument('--exclude', default='', help='逗号分隔的跳过目录')
    s.add_argument('--limit', type=int, default=500, help='最多返回多少条')


def cmd_scan(a):
    emit(scan_repo(a.repo, a.ext, a.exclude, a.limit))
    return 0


def cmd_diff(a):
    emit(diff_repo(a.repo, a.base, a.ext, a.exclude, a.limit))
    return 0


def cmd_stats(a):
    emit(stats_repo(a.repo, a.ext, a.exclude, a.limit))
    return 0


def cmd_report(a):
    entries, repo = _load_entries(a.input)
    md = render_report(entries, repo, a.group_by)
    if a.out:
        Path(a.out).expanduser().write_text(md, encoding='utf-8')
    print(md, end='')
    return 0


def build_parser():
    p = argparse.ArgumentParser(description='扫描 TODO / 遗留问题，输出可跟踪的清单（JSON）')
    sub = p.add_subparsers(dest='cmd', required=True)

    s = sub.add_parser('scan', help='全量扫描仓库')
    add_scan_args(s)
    s.set_defaults(fn=cmd_scan)

    s = sub.add_parser('report', help='把扫描结果渲染成 markdown 清单')
    s.add_argument('--input', required=True, help='scan 输出的 JSON 文件')
    s.add_argument('--out', default='', help='同时写到这个 markdown 文件')
    s.add_argument('--group-by', default='file', choices=['file', 'author', 'kind', 'severity'],
                   help='分组方式，默认按文件')
    s.set_defaults(fn=cmd_report)

    s = sub.add_parser('diff', help='只报相对 base 新增的 TODO（对比 merge-base）')
    add_scan_args(s)
    s.add_argument('--base', default='origin/main', help='基线分支，默认 origin/main')
    s.set_defaults(fn=cmd_diff)

    s = sub.add_parser('stats', help='按类型 / 作者 / 年龄分桶统计')
    add_scan_args(s)
    s.set_defaults(fn=cmd_stats)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except (ScanError, git.GitError) as e:
        print(json.dumps({'error': str(e)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
