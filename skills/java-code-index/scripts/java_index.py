#!/usr/bin/env python3
"""Java 代码索引：给一个 Java 仓库建静态代码地图，支持查找、调用关系、HTTP 接口和影响面。

纯静态解析，不调用模型、不改源码。解析器是自带的轻量实现：
1. 先把注释、字符串、字符、文本块替换成等长空格（保留换行和偏移）；
2. 再做括号配平的结构化扫描，递归解析 package / import / 类型 / 方法 / 注解；
3. 注解参数从原始文本里取回，因此 Spring 路径这类字符串不会丢。

不依赖 javaparser 或任何第三方库。
"""
import argparse
import bisect
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / '_lib'))

DEFAULT_SRC = 'src/main/java'
DEFAULT_OUT = '.wbkit-out/java-index'
MAX_FILE_BYTES = 2 * 1024 * 1024

# 构建索引时整棵跳过的目录
SKIP_DIRS = {'.git', 'target', 'build', 'out', 'node_modules', '.gradle', '.idea',
             '__pycache__', 'dist'}

TYPE_KEYWORDS = ('class', 'interface', 'enum', 'record')
MODIFIERS = {'public', 'protected', 'private', 'static', 'final', 'abstract',
             'default', 'synchronized', 'native', 'transient', 'volatile',
             'strictfp', 'sealed', 'non-sealed'}

# 这些名字后面跟括号时是语句关键字，不是方法调用/声明
CTRL_NAMES = {'if', 'for', 'while', 'switch', 'catch', 'try', 'return', 'new',
              'throw', 'assert', 'case', 'else', 'do', 'synchronized',
              'instanceof', 'yield'}

MAPPING_ANNOTATIONS = {
    'GetMapping': 'GET', 'PostMapping': 'POST', 'PutMapping': 'PUT',
    'DeleteMapping': 'DELETE', 'PatchMapping': 'PATCH',
}

PACKAGE_RE = re.compile(r'(?m)^\s*package\s+([A-Za-z_$][\w$.]*)\s*;')
IMPORT_RE = re.compile(r'(?m)^\s*import\s+(static\s+)?([A-Za-z_$][\w$.*]+)\s*;')
TYPE_DECL_RE = re.compile(r'\b(class|interface|enum|record)\s+([A-Za-z_$][\w$]*)')
IDENT_RE = re.compile(r'[A-Za-z_$][\w$-]*')
CALL_RE = re.compile(r'(?<![\w$])([A-Za-z_$][\w$]*)\s*\(')

EMPTY = ''


class JIError(Exception):
    """命令行可以预期的错误：目录不存在、索引坏了、文件没找到。"""


class ParseError(Exception):
    """单个文件解析失败。调用方会把它记进 parse_errors，不中断整体构建。"""


def emit(data):
    print(json.dumps(data, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# 文本预处理：去注释、去字符串，保持长度
# ---------------------------------------------------------------------------

def sanitize(text):
    """把注释和字符串内容替换成空格，换行保留，长度与原文本一致。

    这样后续可以直接按偏移定位，也能放心地做括号配平：字符串里的
    "class Fake { void ghost() {" 不会再干扰解析。
    """
    out = list(text)
    n = len(text)
    i = 0
    while i < n:
        c = text[i]
        if c == '/' and i + 1 < n and text[i + 1] == '/':
            out[i] = out[i + 1] = ' '
            i += 2
            while i < n and text[i] != '\n':
                out[i] = ' '
                i += 1
            continue
        if c == '/' and i + 1 < n and text[i + 1] == '*':
            out[i] = out[i + 1] = ' '
            i += 2
            while i < n:
                if text[i] == '*' and i + 1 < n and text[i + 1] == '/':
                    out[i] = out[i + 1] = ' '
                    i += 2
                    break
                if text[i] != '\n':
                    out[i] = ' '
                i += 1
            continue
        if text.startswith('"""', i):
            out[i] = out[i + 1] = out[i + 2] = ' '
            i += 3
            while i < n:
                if text.startswith('"""', i):
                    out[i] = out[i + 1] = out[i + 2] = ' '
                    i += 3
                    break
                if text[i] != '\n':
                    out[i] = ' '
                i += 1
            continue
        if c in ('"', "'"):
            quote = c
            out[i] = ' '
            i += 1
            while i < n:
                ch = text[i]
                if ch == '\\':
                    out[i] = ' '
                    if i + 1 < n:
                        out[i + 1] = ' '
                        i += 1
                    i += 1
                    continue
                if ch == quote:
                    out[i] = ' '
                    i += 1
                    break
                if ch != '\n':
                    out[i] = ' '
                i += 1
            continue
        i += 1
    return ''.join(out)


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------

def match_pair(text, open_off, open_ch='{', close_ch='}'):
    """从 open_off 开始找配对的右括号，返回偏移；不配对返回 -1。"""
    depth = 0
    for i in range(open_off, len(text)):
        ch = text[i]
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return i
    return -1


def split_top(text, sep=','):
    """按顶层分隔符切分：泛型尖括号、圆括号、方括号里的逗号不算。"""
    parts, buf = [], []
    angle = paren = bracket = 0
    for ch in text:
        if ch == '<':
            angle += 1
        elif ch == '>':
            angle = max(0, angle - 1)
        elif ch == '(':
            paren += 1
        elif ch == ')':
            paren = max(0, paren - 1)
        elif ch == '[':
            bracket += 1
        elif ch == ']':
            bracket = max(0, bracket - 1)
        if ch == sep and angle == 0 and paren == 0 and bracket == 0:
            parts.append(''.join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append(''.join(buf))
    return [p.strip() for p in parts if p.strip()]


def strip_generics(text):
    """去掉类型上的泛型参数：Map<String, List<T>> -> Map。"""
    out, depth = [], 0
    for ch in text:
        if ch == '<':
            depth += 1
        elif ch == '>':
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(ch)
    return ''.join(out)


def strip_leading_type_params(text):
    """去掉方法签名开头的泛型声明：<T extends A & B> Foo bar -> Foo bar。"""
    s = text.strip()
    if not s.startswith('<'):
        return s
    close = match_pair(s, 0, '<', '>')
    return s[close + 1:].strip() if close != -1 else s


def simple_name(name):
    """取类型/注解的简单名：com.example.Order -> Order，@Foo -> Foo。"""
    return (name or '').replace('@', '').rsplit('.', 1)[-1]


def _top_keywords(text, words):
    """找出顶层（不在泛型/括号里）出现的关键字位置。"""
    hits = []
    angle = paren = bracket = 0
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == '<':
            angle += 1
        elif ch == '>':
            angle = max(0, angle - 1)
        elif ch == '(':
            paren += 1
        elif ch == ')':
            paren = max(0, paren - 1)
        elif ch == '[':
            bracket += 1
        elif ch == ']':
            bracket = max(0, bracket - 1)
        elif ch.isalpha() and angle == 0 and paren == 0 and bracket == 0:
            m = IDENT_RE.match(text, i)
            if m:
                if m.group(0) in words:
                    hits.append((i, m.group(0)))
                i = m.end()
                continue
        i += 1
    return hits


def _first_paren(text):
    """第一个顶层左圆括号的位置（泛型不算）。"""
    angle = 0
    for i, ch in enumerate(text):
        if ch == '<':
            angle += 1
        elif ch == '>':
            angle = max(0, angle - 1)
        elif ch == '(' and angle == 0:
            return i
    return -1


def _annotation_spans(text):
    """找出注解的 [start, end, name]；嵌在其它注解参数里的嵌套注解会跳过。"""
    spans = []
    last_end = 0
    for m in re.finditer(r'@\s*([A-Za-z_$][\w$.]*)', text):
        if m.start() < last_end:
            continue
        name = m.group(1)
        if name == 'interface':        # @interface 是声明，不是注解
            continue
        start, end = m.start(), m.end()
        j = end
        while j < len(text) and text[j].isspace():
            j += 1
        if j < len(text) and text[j] == '(':
            close = match_pair(text, j, '(', ')')
            if close != -1:
                end = close + 1
        spans.append((start, end, name))
        last_end = end
    return spans


def mask_annotations(text):
    """把注解（含参数）替换成空格，方便定位方法签名。"""
    chars = list(text)
    for start, end, _ in _annotation_spans(text):
        for k in range(start, min(end, len(chars))):
            if chars[k] != '\n':
                chars[k] = ' '
    return ''.join(chars)


def parse_annotations(clean_header, raw_header):
    """从成员头部提取注解：[{name, args}]，args 从原始文本取，保留字符串。"""
    out = []
    for start, end, name in _annotation_spans(clean_header):
        args = ''
        k = clean_header.find('(', start, end)
        if k != -1:
            close = match_pair(clean_header, k, '(', ')')
            if close != -1:
                args = raw_header[k + 1:close].strip()
        out.append({'name': name, 'args': args})
    return out


def annotation_names(annotations):
    return [simple_name(a['name']) for a in annotations or []]


def _modifiers(text):
    return sorted({w for w in IDENT_RE.findall(text) if w in MODIFIERS})


# ---------------------------------------------------------------------------
# Java 解析器
# ---------------------------------------------------------------------------

class JavaParser:
    """单个 Java 文件的轻量解析。解析结果放在 self.classes。"""

    def __init__(self, raw_text, rel_path):
        self.raw = raw_text
        self.clean = sanitize(raw_text)
        self.rel = rel_path
        starts = [0]
        for m in re.finditer('\n', raw_text):
            starts.append(m.end())
        self._line_starts = starts
        self.package = ''
        self.imports = []
        self.classes = []

    # ---- 位置与行号 ----

    def line(self, offset):
        return bisect.bisect_right(self._line_starts, offset)

    def raw_line(self, offset):
        idx = self.line(offset) - 1
        lines = self.raw.splitlines()
        return lines[idx].strip() if 0 <= idx < len(lines) else ''

    # ---- 入口 ----

    def parse(self):
        m = PACKAGE_RE.search(self.clean)
        if m:
            self.package = m.group(1)
        for im in IMPORT_RE.finditer(self.clean):
            self.imports.append(('static ' if im.group(1) else '') + im.group(2))
        self._walk(0, len(self.clean), None)
        return self.classes

    # ---- 结构扫描 ----

    def _walk(self, start, end, outer):
        """扫描一个容器体（顶层或类体）的成员。outer 为 None 表示文件顶层。"""
        i = start
        member_start = start
        paren = 0
        while i < end:
            ch = self.clean[i]
            if ch == '(':
                paren += 1
            elif ch == ')':
                paren = max(0, paren - 1)
            elif paren == 0 and ch == ';':
                if outer is not None:
                    self._abstract_method(self.clean[member_start:i],
                                          self.raw[member_start:i],
                                          member_start, i, outer)
                i += 1
                member_start = i
                continue
            elif paren == 0 and ch == '{':
                header = self.clean[member_start:i]
                raw_header = self.raw[member_start:i]
                close = match_pair(self.clean, i)
                if close < 0:
                    raise ParseError('第 %d 行：花括号不配平' % self.line(i))
                cls = self._class_from_header(header, raw_header,
                                              member_start, outer)
                if cls is not None:
                    self.classes.append(cls)
                    self._walk(i + 1, close, cls)
                else:
                    self._concrete_method(header, raw_header, member_start,
                                          i + 1, close, outer)
                i = close + 1
                member_start = i
                continue
            i += 1

    # ---- 类 ----

    def _class_from_header(self, header, raw_header, base, outer):
        masked = mask_annotations(header)
        matches = list(TYPE_DECL_RE.finditer(masked))
        if not matches:
            return None
        m = matches[-1]                       # 取最后一个，避免注解参数里的干扰
        kind = m.group(1)
        name = m.group(2)

        after = masked[m.end(2):]
        hits = _top_keywords(after, {'extends', 'implements', 'permits'})
        clauses = {}
        for idx, (pos, kw) in enumerate(hits):
            stop = hits[idx + 1][0] if idx + 1 < len(hits) else len(after)
            clauses[kw] = [strip_generics(t).strip()
                           for t in split_top(after[pos + len(kw):stop])]

        fqn = (outer['fqn'] + '.' + name) if outer else (
            self.package + '.' + name if self.package else name)
        annotations = parse_annotations(header, raw_header)
        return {
            'fqn': fqn,
            'file': self.rel,
            'kind': kind,
            'line': self.line(base + m.start(2)),
            'package': self.package,
            'extends': clauses.get('extends', []),
            'implements': clauses.get('implements', []),
            'annotations': annotations,
            'modifiers': _modifiers(masked[:m.start(1)]),
            'imports': list(self.imports),
            'methods': [],
        }

    # ---- 方法 ----

    def _abstract_method(self, header, raw_header, base, semi_off, outer):
        method = self._method_from_header(header, raw_header, base, outer)
        if method is not None:
            method['end'] = self.line(semi_off)
            method['_body'] = None
            outer['methods'].append(method)

    def _concrete_method(self, header, raw_header, base, body_start, body_end, outer):
        if outer is None:
            return
        method = self._method_from_header(header, raw_header, base, outer)
        if method is not None:
            method['end'] = self.line(body_end)
            method['_body'] = (body_start, body_end)
            outer['methods'].append(method)

    def _method_from_header(self, header, raw_header, base, outer):
        if outer is None:
            return None
        masked = mask_annotations(header)
        open_idx = _first_paren(masked)
        if open_idx < 0:
            return None

        before = masked[:open_idx]
        m = re.search(r'([A-Za-z_$][\w$]*)\s*$', before)
        if not m:
            return None
        name = m.group(1)
        if name in CTRL_NAMES or name == simple_name(outer['fqn']):
            return None                       # 关键字或构造器
        prefix = before[:m.start(1)]
        if prefix.rstrip().endswith('.'):
            return None                       # obj.foo( 之类的调用
        if '=' in prefix:
            return None                       # 字段初始化 / lambda 赋值

        close_idx = match_pair(masked, open_idx, '(', ')')
        if close_idx < 0:
            return None
        tail = masked[close_idx + 1:].strip()
        if tail and not tail.startswith(('throws', 'default')):
            return None

        type_tokens = [t for t in prefix.split() if t not in MODIFIERS]
        returns = strip_generics(strip_leading_type_params(' '.join(type_tokens)))
        returns = re.sub(r'\s+', ' ', returns).strip()
        if not returns:
            return None                       # 没有返回类型，多半不是方法

        params = []
        for raw in split_top(masked[open_idx + 1:close_idx]):
            param = self._parse_param(raw)
            if param:
                params.append(param)

        method = {
            'name': name,
            'line': self.line(base + m.start(1)),
            'returns': returns,
            'params': params,
            'annotations': parse_annotations(header, raw_header),
            'modifiers': _modifiers(prefix),
        }
        method['signature'] = '%s %s(%s)' % (
            returns, name,
            ', '.join((p['type'] + (' ' + p['name'] if p['name'] else '')).strip()
                      for p in params))
        return method

    @staticmethod
    def _parse_param(raw):
        s = ' '.join(re.sub(r'\bfinal\b', ' ', raw).split())
        if not s:
            return None
        m = re.search(r'([A-Za-z_$][\w$]*)\s*$', s)
        if not m:
            return None
        name = m.group(1)
        ptype = s[:m.start(1)].strip()
        if not ptype:
            return {'type': name, 'name': ''}
        return {'type': ptype, 'name': name}


def parse_java(raw_text, rel_path):
    parser = JavaParser(raw_text, rel_path)
    parser.parse()
    return parser.classes


# ---------------------------------------------------------------------------
# 索引构建与加载
# ---------------------------------------------------------------------------

def _rel(repo, path):
    try:
        return Path(path).resolve().relative_to(repo).as_posix()
    except ValueError:
        return Path(path).as_posix()


def _public_view(obj):
    return {k: v for k, v in obj.items() if not k.startswith('_')}


def build_index(repo, src_dir):
    """遍历源码目录，返回索引字典。单个文件失败只记 parse_errors。"""
    classes = []
    errors = []
    files = 0
    for path in sorted(Path(src_dir).rglob('*.java')):
        parts = path.relative_to(src_dir).parts
        if any(p in SKIP_DIRS for p in parts[:-1]):
            continue
        files += 1
        rel = _rel(repo, path)
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                raise ParseError('文件超过 2 MB，已跳过')
            raw = path.read_text(encoding='utf-8', errors='replace')
            if '\x00' in raw:
                raise ParseError('疑似二进制文件')
            classes.extend(parse_java(raw, rel))
        except ParseError as e:
            errors.append({'file': rel, 'error': str(e)})
        except Exception as e:                # 解析器自身异常也不该中断整次构建
            errors.append({'file': rel, 'error': '解析失败：%s' % e})

    for cls in classes:
        cls['methods'] = [_public_view(m) for m in cls['methods']]
    classes.sort(key=lambda c: (c['file'], c['line'], c['fqn']))

    return {
        'repo': str(repo),
        'src': _rel(repo, src_dir),
        'generated_at': datetime.now().isoformat(timespec='seconds'),
        'classes': classes,
        'parse_errors': errors,
        'stats': {
            'files': files,
            'classes': len(classes),
            'methods': sum(len(c['methods']) for c in classes),
            'parse_errors': len(errors),
        },
    }


def _repo(value):
    p = Path(value).expanduser()
    if not p.is_dir():
        raise JIError('仓库目录不存在：%s' % value)
    return p.resolve()


def _out_path(repo, value):
    p = Path(value).expanduser()
    return p if p.is_absolute() else Path(repo) / p


def _index_for(args):
    """读取已有索引；没有就临时在内存里建一份，不写盘。"""
    repo = _repo(args.repo)
    index_path = _out_path(repo, args.out) / 'index.json'
    if index_path.is_file():
        try:
            return repo, json.loads(index_path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as e:
            raise JIError('索引文件读不了：%s（%s）' % (index_path, e))
    src = Path(repo) / DEFAULT_SRC
    if not src.is_dir():
        raise JIError('没有索引：%s；请先运行 build' % index_path)
    return repo, build_index(repo, src)


def _parse_rel(repo, rel):
    """查询时按当前源码重新解析一个文件；失败返回 None。"""
    try:
        raw = (Path(repo) / rel).read_text(encoding='utf-8', errors='replace')
        parser = JavaParser(raw, rel)
        parser.parse()
        return parser
    except (OSError, ParseError):
        return None


# ---------------------------------------------------------------------------
# 匹配与调用关系
# ---------------------------------------------------------------------------

def _matcher(query):
    """关键词匹配：带 * 的按通配符全名匹配，否则按子串（都不区分大小写）。"""
    q = (query or '').strip()
    if not q:
        raise JIError('查询关键词不能为空')
    if '*' in q:
        pattern = '^' + '.*'.join(re.escape(p) for p in q.split('*')) + '$'
        rx = re.compile(pattern, re.I | re.S)
        return lambda s: bool(rx.match(s or ''))
    ql = q.lower()
    return lambda s: ql in (s or '').lower()


def _method_ref(query):
    """把 foo / Class.foo / pkg.Class#foo 拆成 (类简单名或空, 方法名)。"""
    q = (query or '').strip()
    if not q:
        raise JIError('--method 不能为空')
    if '#' in q:
        cls, name = q.rsplit('#', 1)
    elif '.' in q:
        cls, name = q.rsplit('.', 1)
    else:
        cls, name = '', q
    cls = simple_name(cls) if cls else ''
    name = name.strip()
    if not name:
        raise JIError('--method 不合法：%s' % query)
    return cls, name


def _is_call(text, match):
    """判断正则命中是不是一个方法调用（排除 new/关键字）。"""
    name = match.group(1)
    if name in CTRL_NAMES:
        return False
    j = match.start(1) - 1
    while j >= 0 and text[j].isspace():
        j -= 1
    k = j
    while k >= 0 and (text[k].isalnum() or text[k] in '_$'):
        k -= 1
    return text[k + 1:j + 1] != 'new'


def _call_sites(parser, cls):
    """产出某个类里所有方法体内的调用点。"""
    for method in cls['methods']:
        body = method.get('_body')
        if not body:
            continue
        start, end = body
        for m in CALL_RE.finditer(parser.clean, start, end):
            if not _is_call(parser.clean, m):
                continue
            yield method, m.group(1), parser.line(m.start(1)), parser.raw_line(m.start(1))[:160]


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------

def cmd_build(a):
    repo = _repo(a.repo)
    src = Path(a.src).expanduser()
    if not src.is_absolute():
        src = repo / src
    if not src.is_dir():
        raise JIError('源码目录不存在：%s' % a.src)

    index = build_index(repo, src)
    out_dir = _out_path(repo, a.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    index_path = out_dir / 'index.json'
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + '\n',
                          encoding='utf-8')

    emit({
        'repo': str(repo),
        'src': index['src'],
        'out': str(out_dir),
        'index': str(index_path),
        'stats': index['stats'],
        'parse_errors': index['parse_errors'],
    })


def cmd_find(a):
    _, index = _index_for(a)
    match = _matcher(a.query.lstrip('@'))
    hits = []

    for cls in index.get('classes', []):
        names = [simple_name(cls['fqn']), cls['fqn']] + \
            annotation_names(cls.get('annotations'))
        if any(match(n) for n in names):
            hits.append({
                'kind': 'class',
                'name': simple_name(cls['fqn']),
                'fqn': cls['fqn'],
                'class_kind': cls['kind'],
                'file': cls['file'],
                'line': cls['line'],
                'location': '%s:%d' % (cls['file'], cls['line']),
            })
            continue
        for method in cls.get('methods', []):
            names = [method['name'],
                     '%s.%s' % (simple_name(cls['fqn']), method['name']),
                     '%s#%s' % (simple_name(cls['fqn']), method['name']),
                     '%s.%s' % (cls['fqn'], method['name'])]
            names += annotation_names(method.get('annotations'))
            if any(match(n) for n in names):
                hits.append({
                    'kind': 'method',
                    'name': method['name'],
                    'class': cls['fqn'],
                    'signature': method.get('signature', ''),
                    'file': cls['file'],
                    'line': method['line'],
                    'location': '%s:%d' % (cls['file'], method['line']),
                })

    hits.sort(key=lambda h: (h['file'], h['line'], h['kind']))
    emit({'repo': index.get('repo'), 'query': a.query, 'count': len(hits),
          'hits': hits})


def cmd_callers(a):
    repo, index = _index_for(a)
    cls_filter, name = _method_ref(a.method)

    targets = []
    for cls in index.get('classes', []):
        if cls_filter and simple_name(cls['fqn']) != cls_filter:
            continue
        for method in cls.get('methods', []):
            if method['name'] == name:
                targets.append({
                    'class': cls['fqn'],
                    'method': method['name'],
                    'signature': method.get('signature', ''),
                    'file': cls['file'],
                    'line': method['line'],
                    'location': '%s:%d' % (cls['file'], method['line']),
                })

    callers = []
    seen = set()
    for rel in sorted({c['file'] for c in index.get('classes', [])}):
        parser = _parse_rel(repo, rel)
        if parser is None:
            continue
        for cls in parser.classes:
            for method, call, line, snippet in _call_sites(parser, cls):
                if call != name:
                    continue
                key = (rel, line, cls['fqn'], method['name'])
                if key in seen:
                    continue
                seen.add(key)
                callers.append({
                    'class': cls['fqn'],
                    'method': method['name'],
                    'file': rel,
                    'line': line,
                    'location': '%s:%d' % (rel, line),
                    'text': snippet,
                })

    callers.sort(key=lambda c: (c['file'], c['line']))
    emit({
        'repo': index.get('repo'),
        'method': a.method,
        'targets': targets,
        'count': len(callers),
        'callers': callers,
        'note': '基于方法名文本匹配的启发式结果：不解析变量类型，可能漏报或误报',
    })


def cmd_callees(a):
    repo, index = _index_for(a)
    cls_filter, name = _method_ref(a.method)

    name_map = {}
    for cls in index.get('classes', []):
        for method in cls.get('methods', []):
            name_map.setdefault(method['name'], []).append(cls['fqn'])

    targets = []
    for cls in index.get('classes', []):
        if cls_filter and simple_name(cls['fqn']) != cls_filter:
            continue
        for method in cls.get('methods', []):
            if method['name'] == name:
                targets.append({'class': cls['fqn'], 'method': method['name'],
                                'file': cls['file'], 'line': method['line']})

    rows = []
    seen = set()
    for target in targets:
        parser = _parse_rel(repo, target['file'])
        if parser is None:
            continue
        for cls in parser.classes:
            if cls['fqn'] != target['class']:
                continue
            for method in cls['methods']:
                if method['name'] != name or method['line'] != target['line']:
                    continue
                body = method.get('_body')
                if not body:
                    continue
                for cm in CALL_RE.finditer(parser.clean, body[0], body[1]):
                    if not _is_call(parser.clean, cm):
                        continue
                    line = parser.line(cm.start(1))
                    key = (target['file'], line, cm.group(1))
                    if key in seen:
                        continue
                    seen.add(key)
                    rows.append({
                        'name': cm.group(1),
                        'line': line,
                        'file': target['file'],
                        'resolved': name_map.get(cm.group(1), [])[:10],
                        'text': parser.raw_line(cm.start(1))[:160],
                    })

    rows.sort(key=lambda r: (r['file'], r['line']))
    emit({
        'repo': index.get('repo'),
        'method': a.method,
        'targets': targets,
        'count': len(rows),
        'callees': rows,
        'note': '基于方法体文本匹配的启发式结果：resolved 只是同名方法的声明位置，不代表真实分派',
    })


# ---- Spring 接口 ----

STRING_RE = re.compile(r'"((?:\\.|[^"\\])*)"')


def _annotation_paths(args):
    """从 @GetMapping("/x") / @RequestMapping(value = {"/a", "/b"}) 里取路径。"""
    if not args:
        return []
    text = args.strip()
    if text.startswith('"'):
        m = STRING_RE.search(text)
        return [m.group(1)] if m else []
    m = re.search(r'\b(?:value|path)\s*=\s*', text)
    if not m:
        return []
    rest = text[m.end():].strip()
    if rest.startswith('{'):
        close = match_pair(rest, 0, '{', '}')
        body = rest[1:close] if close != -1 else rest[1:]
        return [s.group(1) for s in STRING_RE.finditer(body)]
    sm = STRING_RE.search(rest)
    return [sm.group(1)] if sm else []


def _annotation_methods(args):
    if not args:
        return []
    return re.findall(r'RequestMethod\.([A-Z]+)', args)


def _normalize_path(path):
    if not path:
        return ''
    p = re.sub(r'/+', '/', '/' + str(path).strip().lstrip('/'))
    return p.rstrip('/') or '/'      # 根路径保留一个斜杠


def _join_path(base, path):
    if not base:
        return _normalize_path(path) or '/'
    if not path:
        return _normalize_path(base)
    return _normalize_path(base.rstrip('/') + '/' + str(path).lstrip('/'))


def cmd_api(a):
    _, index = _index_for(a)
    controllers = []
    total = 0
    prefix = _normalize_path(a.path) if a.path else ''

    for cls in index.get('classes', []):
        annotations = cls.get('annotations', [])
        names = annotation_names(annotations)
        if not ({'RestController', 'Controller'} & set(names)):
            continue

        base_paths = ['']
        for ann in annotations:
            if simple_name(ann['name']) == 'RequestMapping':
                paths = _annotation_paths(ann.get('args'))
                if paths:
                    base_paths = paths
                break

        endpoints = []
        for method in cls.get('methods', []):
            for ann in method.get('annotations', []):
                short = simple_name(ann['name'])
                if short == 'RequestMapping':
                    http_methods = _annotation_methods(ann.get('args')) or ['ANY']
                    paths = _annotation_paths(ann.get('args')) or ['']
                elif short in MAPPING_ANNOTATIONS:
                    http_methods = [MAPPING_ANNOTATIONS[short]]
                    paths = _annotation_paths(ann.get('args')) or ['']
                else:
                    continue
                for base in base_paths:
                    for path in paths:
                        full = _join_path(base, path)
                        if prefix and not (full == prefix or full.startswith(prefix.rstrip('/') + '/')):
                            continue
                        endpoints.append({
                            'http': http_methods[0] if len(http_methods) == 1 else http_methods,
                            'path': full,
                            'method': method['name'],
                            'signature': method.get('signature', ''),
                            'line': method['line'],
                            'location': '%s:%d' % (cls['file'], method['line']),
                            'annotations': [simple_name(x['name']) for x in method['annotations']],
                        })

        if endpoints:
            endpoints.sort(key=lambda e: (str(e['path']), e['line']))
            total += len(endpoints)
            controllers.append({
                'class': cls['fqn'],
                'file': cls['file'],
                'line': cls['line'],
                'base_path': _join_path(base_paths[0], '') or '/',
                'endpoints': endpoints,
            })

    controllers.sort(key=lambda c: (c['file'], c['line']))
    emit({'repo': index.get('repo'), 'path': a.path or None, 'count': total,
          'controllers': controllers})


# ---- 影响面 ----

def _target_rel(repo, value, src):
    raw = Path(value)
    candidates = [raw] if raw.is_absolute() else [Path(repo) / raw,
                                                 Path(repo) / (src or '') / raw]
    for cand in candidates:
        if cand.is_file():
            return _rel(repo, cand)
    raise JIError('找不到文件：%s' % value)


def cmd_impact(a):
    repo, index = _index_for(a)
    rel = _target_rel(repo, a.file, index.get('src'))

    targets = [c for c in index.get('classes', []) if c['file'] == rel]
    if not targets:
        raise JIError('这个文件里没有可识别的类：%s' % a.file)

    target_fqns = {c['fqn'] for c in targets}
    target_simple = {simple_name(c['fqn']) for c in targets}
    target_pkgs = {c['package'] for c in targets if c.get('package')}
    target_methods = {m['name'] for c in targets for m in c.get('methods', [])}
    target_interfaces = {simple_name(t) for c in targets
                         for t in c.get('implements', [])}
    repo_interfaces = {simple_name(c['fqn']) for c in index.get('classes', [])
                       if c['kind'] == 'interface'}

    rows = []
    seen = set()

    def add(cls, reason, detail, method=None, line=None):
        key = (cls['fqn'], reason, detail)
        if key in seen:
            return
        seen.add(key)
        row = {'class': cls['fqn'], 'file': cls['file'], 'reason': reason,
               'detail': detail}
        if method:
            row['method'] = method
            row['line'] = line
            row['location'] = '%s:%d' % (cls['file'], line)
        rows.append(row)

    parsers = {}

    def parser_for(rel_path):
        if rel_path not in parsers:
            parsers[rel_path] = _parse_rel(repo, rel_path)
        return parsers[rel_path]

    for cls in index.get('classes', []):
        if cls['fqn'] in target_fqns:
            continue

        for imp in cls.get('imports') or []:
            imp = imp.replace('static ', '').strip()
            if imp.endswith('.*'):
                if imp[:-2] in target_pkgs:
                    add(cls, 'import', imp)
            elif imp in target_fqns:
                add(cls, 'import', imp)

        for parent in cls.get('extends') or []:
            if simple_name(parent) in target_simple:
                add(cls, 'extends', parent)

        for iface in cls.get('implements') or []:
            short = simple_name(iface)
            if short in target_simple:
                add(cls, 'implements', iface)
            elif short in target_interfaces and short in repo_interfaces:
                add(cls, 'sibling_impl', iface)

        parser = parser_for(cls['file'])
        if parser is None:
            continue
        for parsed_cls in parser.classes:
            if parsed_cls['fqn'] != cls['fqn']:
                continue
            own = {m['name'] for m in parsed_cls['methods']}
            for method, call, line, _ in _call_sites(parser, parsed_cls):
                if call in target_methods and call not in own:
                    add(cls, 'call', call, method['name'], line)

    order = {'implements': 0, 'extends': 1, 'sibling_impl': 2,
             'import': 3, 'call': 4}
    rows.sort(key=lambda r: (order.get(r['reason'], 9), r['file'],
                             r.get('line') or 0))

    emit({
        'repo': index.get('repo'),
        'file': rel,
        'classes': [{'fqn': c['fqn'], 'kind': c['kind'], 'line': c['line']}
                    for c in targets],
        'count': len(rows),
        'impact': rows,
        'note': '只覆盖显式 import、extends/implements、同接口实现和同名方法调用；'
                '同包未 import、反射/框架注入、动态代理等场景可能漏报',
    })


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main(argv=None):
    p = argparse.ArgumentParser(
        description='Java 代码索引：建代码地图、查找、调用关系、HTTP 接口、影响面（全部输出 JSON）')
    sub = p.add_subparsers(dest='cmd', required=True)

    def common(sp):
        sp.add_argument('--repo', default='.', help='仓库目录，默认当前目录')
        sp.add_argument('--out', default=DEFAULT_OUT,
                        help='索引目录，默认 %s（相对仓库）' % DEFAULT_OUT)

    s = sub.add_parser('build', help='遍历 Java 文件，生成 index.json')
    common(s)
    s.add_argument('--src', default=DEFAULT_SRC, help='源码目录，默认 %s' % DEFAULT_SRC)
    s.set_defaults(fn=cmd_build)

    s = sub.add_parser('find', help='按类名 / 方法名 / 注解模糊查（支持 * 通配）')
    common(s)
    s.add_argument('--query', required=True)
    s.set_defaults(fn=cmd_find)

    s = sub.add_parser('callers', help='谁调用了这个方法（启发式）')
    common(s)
    s.add_argument('--method', required=True, help='foo / Class.foo / pkg.Class#foo')
    s.set_defaults(fn=cmd_callers)

    s = sub.add_parser('callees', help='这个方法调用了谁（启发式）')
    common(s)
    s.add_argument('--method', required=True)
    s.set_defaults(fn=cmd_callees)

    s = sub.add_parser('api', help='列出 Spring controller 的 HTTP 接口')
    common(s)
    s.add_argument('--path', default='', help='只保留这个路径前缀，如 /orders')
    s.set_defaults(fn=cmd_api)

    s = sub.add_parser('impact', help='改这个文件可能影响谁')
    common(s)
    s.add_argument('--file', required=True, help='相对仓库或相对源码目录的 Java 文件')
    s.set_defaults(fn=cmd_impact)

    a = p.parse_args(argv)
    try:
        a.fn(a)
    except JIError as e:
        print(json.dumps({'error': str(e)}, ensure_ascii=False), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
