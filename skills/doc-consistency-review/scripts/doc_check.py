#!/usr/bin/env python3
"""文档一致性检查、单篇评审、文档问答。

check 输出 JSON 数组；review/ask 走 wbkit.agent.run。只依赖标准库，
单测可注入模块级 _runner。
"""
import argparse
import json
import os
import re
import sys
import tempfile
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / '_lib'))

ENGINES = ('claude', 'codex', 'deepseek', 'kimi')
MD_EXT = {'.md', '.markdown'}
SKIP_DIRS = {'.git', 'node_modules', '__pycache__', '.venv', 'venv', 'target', 'dist',
             'build', '.idea', '.cache'}
MAX_CODE_BYTES = 2 * 1024 * 1024

HEADING_RE = re.compile(r'^(#{1,6})\s+(.+?)\s*#*\s*$')
LINK_RE = re.compile(r'!?\[([^\]]*)\]\(([^)\s]+)\)')
API_RE = re.compile(r'/api/[A-Za-z0-9_\-./{}:]+')
TODO_RE = re.compile(r'\b(?:TODO|FIXME|TBD)\b|待补充|待完善|待确认|待定', re.I)
CLASS_RE = re.compile(r'\b[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+\b')
WORD_RE = re.compile(r'[A-Za-z][A-Za-z0-9_]*')
SCHEME_RE = re.compile(r'^[a-z][a-z0-9+.\-]*:', re.I)

# 单测注入点：fn(engine, prompt, cwd, profile='read') -> dict
_runner = None


class DocError(Exception):
    pass


def _finding(kind, severity, file, line, message, evidence=''):
    return {'kind': kind, 'severity': severity, 'file': str(file), 'line': int(line),
            'message': message, 'evidence': evidence}


def _docs(root):
    root = Path(root)
    if not root.is_dir():
        raise DocError('目录不存在：%s' % root)
    files = [p for p in sorted(root.rglob('*'))
             if p.is_file() and p.suffix.lower() in MD_EXT
             and not any(x in SKIP_DIRS for x in p.relative_to(root).parts[:-1])]
    return files, root


def _fenced(lines):
    """代码块内的行号，规则扫描时跳过，避免把示例当成结论。"""
    mask, inside = set(), False
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith(('```', '~~~')):
            mask.add(i)
            inside = not inside
        elif inside:
            mask.add(i)
    return mask


def _norm_words(token):
    """拆词并小写：order_no / orderNo / OrderNo -> ['order', 'no']。"""
    parts = [p for p in re.split(r'[_\W]+', token) if p]
    return [w.lower() for p in parts
            for w in re.findall(r'[A-Z]+(?![a-z])|[A-Z][a-z]*|[a-z]+|\d+', p)]


def _clean_line(line):
    """去掉链接地址和接口路径，只留可分析的正文，减少命名误报。"""
    clean = LINK_RE.sub(lambda m: m.group(1), line)
    clean = API_RE.sub(' ', clean)
    return re.sub(r'https?://\S+', ' ', clean)


def _rule_api(docs_data):
    """规则 (a)：接口路径只在部分文档出现。"""
    paths, docs_with_api = {}, set()
    for doc in docs_data:
        for i, line in enumerate(doc['lines']):
            if i in doc['mask']:
                continue
            for m in API_RE.finditer(line):
                p = m.group(0).rstrip('.,;:。，、)）')
                if not p or p == '/api/':
                    continue
                docs_with_api.add(doc['rel'])
                item = paths.setdefault(p, {'file': doc['rel'], 'line': i + 1, 'docs': set()})
                item['docs'].add(doc['rel'])
    if len(docs_with_api) < 2:
        return []
    return [_finding('api-path-partial', 'low', item['file'], item['line'],
                     '接口路径 %s 只在 %d/%d 篇文档出现，缺失：%s'
                     % (p, len(item['docs']), len(docs_with_api), '、'.join(missing)), p)
            for p, item in paths.items()
            for missing in [sorted(docs_with_api - item['docs'])] if missing]


def _rule_naming(docs_data):
    """规则 (b)：同一实体多种写法（去大小写/下划线/驼峰后相同）。"""
    groups = {}
    for doc in docs_data:
        for i, line in enumerate(doc['lines']):
            if i in doc['mask']:
                continue
            for m in WORD_RE.finditer(_clean_line(line)):
                tok = m.group(0)
                if len(tok) < 4 or ('_' not in tok and not any(c.isupper() for c in tok[1:])):
                    continue
                words = _norm_words(tok)
                if len(words) < 2:
                    continue
                g = groups.setdefault('/'.join(words), {})
                item = g.setdefault(tok, {'count': 0, 'file': doc['rel'], 'line': i + 1})
                item['count'] += 1
    findings = []
    for g in groups.values():
        if len(g) < 2:
            continue
        order = list(g)
        canonical = max(order, key=lambda s: (g[s]['count'], -order.index(s)))
        variants = '、'.join('%s x%d' % (s, g[s]['count']) for s in order)
        for spelling in order:
            if spelling != canonical:
                findings.append(_finding(
                    'naming-inconsistent', 'medium', g[spelling]['file'], g[spelling]['line'],
                    '同一实体有多种写法：%s，建议统一为 %s' % (variants, canonical),
                    '%s vs %s' % (spelling, canonical)))
    return findings


def _rule_links(docs_data):
    """规则 (c)：相对链接指向的文件不存在。"""
    findings = []
    for doc in docs_data:
        for i, line in enumerate(doc['lines']):
            if i in doc['mask']:
                continue
            for m in LINK_RE.finditer(line):
                target = m.group(2).strip().strip('<>')
                if (not target or target.startswith(('#', '/', '~'))
                        or SCHEME_RE.match(target) or '*' in target or '{' in target):
                    continue
                rel = urllib.parse.unquote(target.split('#', 1)[0].split('?', 1)[0])
                if rel and not (doc['path'].parent / rel).exists():
                    findings.append(_finding(
                        'broken-link', 'medium', doc['rel'], i + 1,
                        '链接指向的文件不存在：%s' % target, target))
    return findings


def _rule_empty(docs_data):
    """规则 (d)：标题下没有任何内容（也没有下级标题）。"""
    findings = []
    for doc in docs_data:
        lines = doc['lines']
        for i, line in enumerate(lines):
            m = None if i in doc['mask'] else HEADING_RE.match(line)
            if not m:
                continue
            level, title = len(m.group(1)), m.group(2).strip()
            has_content, j = False, i + 1
            while j < len(lines):
                if not lines[j].strip():
                    j += 1
                    continue
                nm = None if j in doc['mask'] else HEADING_RE.match(lines[j])
                has_content = not (nm and len(nm.group(1)) <= level)
                break
            if not has_content:
                findings.append(_finding(
                    'empty-section', 'low', doc['rel'], i + 1,
                    '章节“%s”下没有内容' % title, line.strip()))
    return findings


def _rule_todos(docs_data):
    """规则 (e)：残留 TODO / 待补充。"""
    findings = []
    for doc in docs_data:
        for i, line in enumerate(doc['lines']):
            m = None if i in doc['mask'] else TODO_RE.search(line)
            if m:
                findings.append(_finding(
                    'todo-residue', 'low', doc['rel'], i + 1,
                    '文档里还有未完成标记：%s' % m.group(0), line.strip()[:120]))
    return findings


def _scan_code(code_dirs, needles):
    """在代码目录里全文找这些字符串，返回找到的集合。"""
    found, pending = set(), set(needles)
    for d in code_dirs:
        d = Path(d)
        if not d.is_dir():
            continue
        for root, dirs, files in os.walk(d):
            dirs[:] = [x for x in dirs if x not in SKIP_DIRS]
            for name in files:
                if not pending:
                    return found
                p = Path(root) / name
                try:
                    if p.stat().st_size > MAX_CODE_BYTES:
                        continue
                    data = p.read_bytes()
                except OSError:
                    continue
                if b'\x00' in data[:4096]:
                    continue
                text = data.decode('utf-8', errors='replace')
                for needle in list(pending):
                    if needle in text:
                        found.add(needle)
                        pending.discard(needle)
    return found


def _rule_code(docs_data, code_dirs):
    """规则 (f)：文档提到的类名或 /api 路径在代码目录里找不到。"""
    if not code_dirs:
        return []
    mentions = []
    for doc in docs_data:
        for i, line in enumerate(doc['lines']):
            if i in doc['mask']:
                continue
            clean = _clean_line(line)
            for m in CLASS_RE.finditer(clean):
                mentions.append((doc, i + 1, m.group(0)))
            for m in API_RE.finditer(line):
                p = m.group(0).rstrip('.,;:。，、)）')
                if p and p != '/api/':
                    mentions.append((doc, i + 1, p))
    found = _scan_code(code_dirs, {needle for _, _, needle in mentions})
    findings, seen = [], set()
    for doc, line, needle in mentions:
        key = (doc['rel'], needle)
        if needle in found or key in seen:
            continue
        seen.add(key)
        findings.append(_finding(
            'code-missing', 'medium', doc['rel'], line,
            '文档提到的 %s 在代码目录里找不到' % needle, needle))
    return findings


def check(directory, code_dirs=()):
    """跨文档一致性检查，返回问题列表（JSON 数组可直接序列化）。"""
    files, root = _docs(directory)
    docs_data = []
    for p in files:
        lines = Path(p).read_text(encoding='utf-8', errors='replace').splitlines()
        docs_data.append({'path': p, 'rel': str(p.relative_to(root)),
                          'lines': lines, 'mask': _fenced(lines)})
    findings = (
        _rule_api(docs_data) + _rule_naming(docs_data) + _rule_links(docs_data)
        + _rule_empty(docs_data) + _rule_todos(docs_data)
        + _rule_code(docs_data, list(code_dirs or [])))
    seen, out = set(), []
    for f in findings:
        key = (f['kind'], f['file'], f['line'], f['message'])
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
    return sorted(out, key=lambda f: (f['file'], f['line'], f['kind']))


REVIEW_PROMPT = """你是严格的文档评审专家。请找出这份文档里的问题：表述不清、前后矛盾、
缺少关键信息、无法落地、与既有约定不符。

只输出 JSON 数组，不要输出解释，也不要用 Markdown 代码块包裹。每个元素包含 5 个字段：
{"severity": "high 或 medium 或 low", "section": "问题所在章节标题",
 "quote": "原文摘录，不超过 80 字", "problem": "问题说明", "suggestion": "修改建议"}
没有问题时输出 []。

文件名：<<NAME>>

===== 文档开始 =====
<<TEXT>>
===== 文档结束 =====
"""

ASK_PROMPT = """请只根据下面的文档回答问题。回答用纯文本，不要复述整篇文档，
也不要编造文档里没有的信息。

问题：<<QUESTION>>
文件名：<<NAME>>

===== 文档开始 =====
<<TEXT>>
===== 文档结束 =====
"""


def _prompt(tpl, **kw):
    for k, v in kw.items():
        tpl = tpl.replace('<<%s>>' % k.upper(), str(v))
    return tpl


def _default_runner(engine, prompt, cwd, profile='read'):
    from wbkit import agent
    return agent.run(engine, prompt, cwd, profile=profile)


def _call(engine, prompt, cwd):
    fn = _runner or _default_runner
    return fn(engine, prompt, cwd, profile='read')


def _read_doc(path):
    p = Path(path)
    if not p.is_file():
        raise DocError('文件不存在：%s' % p)
    return p, p.read_text(encoding='utf-8', errors='replace')


def _extract_json_array(text):
    """从模型输出里找第一个能解析的 JSON 数组（容忍代码块和前后解释）。"""
    dec = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch == '[':
            try:
                value, _ = dec.raw_decode(text[i:])
                if isinstance(value, list):
                    return value
            except ValueError:
                pass
    return None


def _normalize_reviews(items):
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        sev = str(it.get('severity') or 'medium').strip().lower()
        out.append({'severity': sev if sev in ('high', 'medium', 'low') else 'medium',
                    'section': str(it.get('section') or '').strip(),
                    'quote': str(it.get('quote') or '').strip(),
                    'problem': str(it.get('problem') or '').strip(),
                    'suggestion': str(it.get('suggestion') or '').strip()})
    return out


def _write_raw(text, out=None):
    """解析失败时把模型原始输出落盘，返回路径。"""
    if out:
        raw = Path(out).with_name(Path(out).name + '.raw.txt')
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_text(text, encoding='utf-8')
        return str(raw)
    tmp = tempfile.NamedTemporaryFile('w', suffix='.raw.txt', prefix='doc-review-',
                                      delete=False, encoding='utf-8')
    tmp.write(text)
    tmp.close()
    return tmp.name


def review(path, engine='kimi', out=None):
    """评审单篇文档。成功返回 {ok, file, engine, count, reviews}；失败带 error / raw_output。"""
    p, text = _read_doc(path)
    res = _call(engine, _prompt(REVIEW_PROMPT, name=p.name, text=text), str(p.parent))
    if not res.get('ok'):
        return {'ok': False, 'file': str(p.resolve()), 'engine': engine,
                'error': res.get('error') or '模型调用失败'}
    raw_text = str(res.get('text') or '')
    items = _extract_json_array(raw_text)
    if items is None:
        return {'ok': False, 'file': str(p.resolve()), 'engine': engine,
                'error': '模型输出里没有合法的 JSON 数组',
                'raw_output': _write_raw(raw_text, out or None)}
    reviews = _normalize_reviews(items)
    data = {'ok': True, 'file': str(p.resolve()), 'engine': engine,
            'count': len(reviews), 'reviews': reviews}
    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n',
                             encoding='utf-8')
    return data


def ask(path, question, engine='kimi'):
    """就文档问答，返回 {ok, file, engine, answer} 或 {ok: False, error}。"""
    p, text = _read_doc(path)
    if not str(question).strip():
        raise DocError('问题不能为空')
    res = _call(engine, _prompt(ASK_PROMPT, question=question, name=p.name, text=text),
                str(p.parent))
    if not res.get('ok'):
        return {'ok': False, 'file': str(p.resolve()), 'engine': engine,
                'error': res.get('error') or '模型调用失败'}
    return {'ok': True, 'file': str(p.resolve()), 'engine': engine,
            'answer': str(res.get('text') or '').strip()}


def _build_parser():
    p = argparse.ArgumentParser(prog='doc_check.py',
                                description='文档一致性检查 / 单篇评审 / 文档问答')
    sub = p.add_subparsers(dest='cmd', required=True)

    s = sub.add_parser('check', help='跨文档一致性检查，输出 JSON 数组')
    s.add_argument('--dir', required=True, help='文档目录')
    s.add_argument('--code', action='append', default=[], help='代码目录，可多次传入')

    s = sub.add_parser('review', help='单篇文档评审，输出 JSON')
    s.add_argument('--file', required=True)
    s.add_argument('--engine', default='kimi', choices=ENGINES)
    s.add_argument('--out', default='', help='把结果写到文件')

    s = sub.add_parser('ask', help='就文档提问，输出纯文本')
    s.add_argument('--file', required=True)
    s.add_argument('--question', required=True)
    s.add_argument('--engine', default='kimi', choices=ENGINES)
    return p


def main(argv=None):
    a = _build_parser().parse_args(argv)
    try:
        if a.cmd == 'check':
            print(json.dumps(check(a.dir, a.code), ensure_ascii=False, indent=2))
            return 0
        if a.cmd == 'review':
            data = review(a.file, a.engine, a.out or None)
            print(json.dumps(data, ensure_ascii=False, indent=2))
            return 0 if data['ok'] else 1
        data = ask(a.file, a.question, a.engine)
        if not data['ok']:
            print(data['error'], file=sys.stderr)
            return 1
        print(data['answer'])
        return 0
    except DocError as e:
        print(json.dumps({'error': str(e)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
