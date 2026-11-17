#!/usr/bin/env python3
"""PRD 文档导入：把 md / html / txt / docx / pdf 整理进一个干净的需求目录，并抽出可执行要素。

子命令：
  ingest   复制正文和图片，图片统一落到目录里，引用改成相对路径
  list     列出正文文件和图片数，并给出 # / ## 大纲
  extract  抽目标、范围（含/不含）、验收条件、接口约定、待确认问题
  check    检查残缺：缺图、空章节、验收为空、标题跳级、外链图片

只依赖标准库；所有输出都是 JSON。
"""
import argparse
import json
import re
import shlex
import shutil
import subprocess
import sys
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote
from xml.etree import ElementTree

# 构建时 tools/sync_lib.py 会把底座放到 scripts/_lib。这个 skill 只用标准库，
# 保留这行是为了跟其它 skill 的入口写法一致，目录单独拷走也能跑。
sys.path.insert(0, str(Path(__file__).resolve().parent / '_lib'))

ASSETS = 'assets'
PDF_CMD = 'pdftotext -layout'

MARKDOWN_SUFFIX = {'.md', '.markdown'}
TEXT_SUFFIX = {'.txt', '.text'}
HTML_SUFFIX = {'.html', '.htm'}
DOC_SUFFIX = MARKDOWN_SUFFIX | TEXT_SUFFIX | HTML_SUFFIX | {'.docx', '.pdf'}

IMG_MD = re.compile(r'!\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+"[^"]*")?\s*\)')
IMG_HTML = re.compile(r'<img\b[^>]*\bsrc\s*=\s*["\']([^"\']+)["\']', re.I)
REMOTE = re.compile(r'^(?:https?:|data:|mailto:|ftp:|#)', re.I)

HEADING = re.compile(r'^(#{1,6})\s+(.+?)\s*#*\s*$')
ITEM = re.compile(r'^\s*(?:[-*+•·]\s+|\d+(?:\.\d+)*[.、)）]\s*|[（(]\d+[)）]\s*|[①-⑳]\s*)')

OUT_HINT = re.compile(r'不包含|不含|不包括|不做|范围外|暂不|不考虑')
SCOPE_PREFIX = re.compile(r'^(?:范围)?(?:包含|包括|涉及|不包含|不含|不包括|不做|范围外|暂不|不考虑)\s*[：:]?\s*')
ACCEPT_HINT = re.compile(r'验收|必须|不得|禁止|至少|不超过|需支持|应支持')
QUESTION_HINT = re.compile(r'待确认|待定|待补充|未确认|未明确|TBD', re.I)

GOAL_KEYS = ('目标', '需求目标', '项目目标', '业务目标', '价值', '收益')
SCOPE_KEYS = ('范围', '需求范围', '业务范围', '范围说明', '交付范围')
ACCEPT_KEYS = ('验收', '验收条件', '验收标准', '验收要点', '测试要点', '验收口径')
IFACE_KEYS = ('接口', '接口约定', '接口清单', '接口设计', '接口影响', '依赖')

METHOD_PATH = re.compile(
    r'\b(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\b\s+(/[^\s|，。；,;)\]）]*)', re.I)
METHOD_HEADERS = ('方法', 'method', '请求方法', 'http方法')
PATH_HEADERS = ('路径', 'path', 'url', '接口', '接口地址', '地址', '端点', 'endpoint')

W_NS = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
R_NS = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
A_NS = 'http://schemas.openxmlformats.org/drawingml/2006/main'
W = '{%s}' % W_NS
R = '{%s}' % R_NS
A = '{%s}' % A_NS


# ---------- 通用 ----------

def emit(data):
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _write_out(data, out):
    """把同一份 JSON 落到文件，并把 out 路径带回输出。"""
    p = Path(out).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    data['out'] = str(p.resolve())
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return data


def _read_text(path):
    raw = Path(path).read_bytes()
    for enc in ('utf-8-sig', 'utf-8', 'gb18030'):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode('utf-8', 'replace')


def _code_mask(lines):
    """逐行标记是否在代码块里；代码块里的标题和图片引用不参与扫描。"""
    mask, in_code = [], False
    for line in lines:
        if line.strip().startswith('```'):
            mask.append(True)
            in_code = not in_code
            continue
        mask.append(in_code)
    return mask


def _title_of(text, fallback):
    m = re.search(r'^#\s+(.+)$', text, re.M)
    return m.group(1).strip() if m else fallback


def _tidy(lines):
    """合并连续空行、去掉首尾空行。"""
    out = []
    for line in lines:
        if not line.strip():
            if out and not out[-1].strip():
                continue
            out.append('')
        else:
            out.append(line.rstrip())
    while out and not out[0].strip():
        out.pop(0)
    while out and not out[-1].strip():
        out.pop()
    return '\n'.join(out)


def _unique(target_dir, name):
    """同名文件加序号，避免不同目录的同名图片互相覆盖。"""
    target = Path(target_dir) / name
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    i = 1
    while True:
        cand = Path(target_dir) / ('%s-%d%s' % (stem, i, suffix))
        if not cand.exists():
            return cand
        i += 1


def _md_table(rows):
    """二维列表 -> markdown 表；不足两行就只输出一行。"""
    cleaned = []
    for row in rows:
        cells = [re.sub(r'\s+', ' ', str(c or '')).replace('|', '\\|').strip() for c in row]
        if any(cells):
            cleaned.append(cells)
    if not cleaned:
        return []
    width = max(len(r) for r in cleaned)
    cleaned = [r + [''] * (width - len(r)) for r in cleaned]
    if len(cleaned) < 2:
        return ['| ' + ' | '.join(cleaned[0]) + ' |']
    out = ['| ' + ' | '.join(cleaned[0]) + ' |',
           '| ' + ' | '.join(['---'] * width) + ' |']
    out += ['| ' + ' | '.join(r) + ' |' for r in cleaned[1:]]
    return out


# ---------- 图片引用 ----------

def _image_refs(text):
    """md 和 html 里的图片引用，按出现顺序去重。"""
    refs = [m.group(1) for m in IMG_MD.finditer(text)]
    refs += [m.group(1) for m in IMG_HTML.finditer(text)]
    return list(dict.fromkeys(refs))


def _is_remote(ref):
    return bool(REMOTE.match(ref.strip()))


def _local_refs(text):
    return [r for r in _image_refs(text) if not _is_remote(r)]


def _resolve_ref(ref, base):
    """把图片引用变成实际路径：相对路径基于文档所在目录。"""
    raw = unquote(ref.split('#')[0].split('?')[0])
    p = Path(raw)
    return p if p.is_absolute() else (Path(base) / p)


# ---------- docx ----------

def _docx_heading_level(p):
    ppr = p.find(W + 'pPr')
    if ppr is None:
        return 0
    style = ppr.find(W + 'pStyle')
    if style is not None:
        val = (style.get(W + 'val') or '').strip()
        m = re.search(r'(?:heading|标题)\s*([1-6])', val, re.I) or re.fullmatch(r'([1-6])', val)
        if m:
            return int(m.group(1))
    outline = ppr.find(W + 'outlineLvl')
    if outline is not None:
        val = (outline.get(W + 'val') or '').strip()
        if val.isdigit():
            return min(int(val) + 1, 6)
    return 0


def _docx_text(p):
    parts = []
    for node in p.iter():
        if node.tag == W + 't':
            parts.append(node.text or '')
        elif node.tag in (W + 'tab', W + 'br'):
            parts.append(' ')
    return re.sub(r'[ \t]+', ' ', ''.join(parts)).strip()


def _docx_image_ids(p):
    ids = []
    for blip in p.iter(A + 'blip'):
        rid = blip.get(R + 'embed')
        if rid:
            ids.append(rid)
    return ids


def _docx_is_list(p):
    ppr = p.find(W + 'pPr')
    return bool(ppr is not None and ppr.find(W + 'numPr') is not None)


def _docx_rels(z):
    """rId -> media 相对路径，只保留图片关系。"""
    try:
        xml = z.read('word/_rels/document.xml.rels')
    except KeyError:
        return {}
    root = ElementTree.fromstring(xml)
    out = {}
    for rel in root:
        if 'image' in (rel.get('Type') or ''):
            out[rel.get('Id')] = rel.get('Target') or ''
    return out


def _docx_copy_image(z, target, dest, warnings):
    """把 word/media 里的图片落到 assets/，返回相对路径。"""
    name = Path(target).name
    data = None
    for cand in (target.lstrip('/'), 'word/' + target.lstrip('/')):
        try:
            data = z.read(cand)
            break
        except KeyError:
            continue
    if data is None:
        warnings.append('docx 图片找不到：%s' % target)
        return ''
    assets = Path(dest) / ASSETS
    assets.mkdir(parents=True, exist_ok=True)
    out = _unique(assets, name)
    out.write_bytes(data)
    return out.relative_to(dest).as_posix()


def _docx_table(tbl):
    rows = []
    for tr in tbl.findall(W + 'tr'):
        cells = []
        for tc in tr.findall(W + 'tc'):
            cells.append(re.sub(r'\s+', ' ',
                                ' '.join(_docx_text(p) for p in tc.findall(W + 'p'))).strip())
        if any(cells):
            rows.append(cells)
    return _md_table(rows)


def _import_docx(src, dest, title, warnings):
    """只读 word/document.xml：保留标题层级、列表、表格，图片按出现顺序插入。"""
    with zipfile.ZipFile(src) as z:
        try:
            xml = z.read('word/document.xml')
        except KeyError:
            raise ValueError('docx 里没有 word/document.xml：%s' % src)
        rels = _docx_rels(z)
        root = ElementTree.fromstring(xml)
        body = root.find(W + 'body')
        lines, images = [], 0
        for el in list(body if body is not None else []):
            if el.tag == W + 'p':
                text = _docx_text(el)
                level = _docx_heading_level(el)
                if text:
                    if level:
                        lines.append('#' * level + ' ' + text)
                    elif _docx_is_list(el):
                        lines.append('- ' + text)
                    else:
                        lines.append(text)
                for rid in _docx_image_ids(el):
                    target = rels.get(rid)
                    if not target:
                        warnings.append('docx 图片关系缺失：%s' % rid)
                        continue
                    rel_path = _docx_copy_image(z, target, dest, warnings)
                    if rel_path:
                        lines += ['', '![](%s)' % rel_path, '']
                        images += 1
            elif el.tag == W + 'tbl':
                block = _docx_table(el)
                if block:
                    lines += [''] + block + ['']
    if not any(line.strip() for line in lines):
        raise ValueError('docx 里没有可提取的文字：%s' % src)
    md = _tidy(lines)
    if not md.startswith('# '):
        md = '# %s\n\n%s' % (title, md)
    out = Path(dest) / (Path(src).stem + '.md')
    out.write_text(md + '\n', encoding='utf-8')
    return out, images


# ---------- 各格式导入 ----------

def _copy_images(text, base_dir, dest, warnings):
    """复制正文引用的本地图片：同目录/子目录保持原路径，../ 和绝对路径落到 assets/ 并改写引用。"""
    mapping, copied = {}, 0
    for ref in _local_refs(text):
        rel = Path(unquote(ref.split('#')[0].split('?')[0]))
        real = rel if rel.is_absolute() else (Path(base_dir) / rel).resolve()
        if not real.is_file():
            warnings.append('图片不存在：%s' % ref)
            continue
        inside = not rel.is_absolute() and '..' not in rel.parts
        if inside:
            target = Path(dest) / rel
        else:
            assets = Path(dest) / ASSETS
            assets.mkdir(parents=True, exist_ok=True)
            target = _unique(assets, real.name)
        if target.resolve() == real.resolve():
            copied += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(real, target)
        copied += 1
        if not inside:
            mapping[ref] = target.relative_to(dest).as_posix()
    for old, new in mapping.items():
        text = text.replace(old, new)
    return text, copied


def _import_md(src, dest, warnings):
    """复制 md 正文和本地图片，跨目录的图片落到 assets/ 并改写引用。"""
    text, copied = _copy_images(_read_text(src), Path(src).parent, dest, warnings)
    out = Path(dest) / (Path(src).stem + '.md')
    out.write_text(text, encoding='utf-8')
    return out, copied


class _HtmlToMd(HTMLParser):
    """极简 HTML → md：标题、段落、列表、表格、图片；脚本和样式直接跳过。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.lines = []
        self.buf = []
        self.skip = 0
        self.heading = 0
        self.rows = None
        self.row = None
        self.cell = None

    def _flush(self, prefix=''):
        text = re.sub(r'\s+', ' ', ''.join(self.buf)).strip()
        self.buf = []
        if not text:
            return
        if self.cell is not None:      # 表格单元格里嵌套了 p / br 时，文字并入当前单元格
            self.cell.append((prefix.strip() + ' ' + text).strip())
        else:
            self.lines.append(prefix + text)

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        attr = {k.lower(): (v or '') for k, v in attrs}
        if tag in ('script', 'style'):
            self.skip += 1
            return
        if self.skip:
            return
        if tag in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6'):
            self._flush()
            if self.lines and self.lines[-1]:
                self.lines.append('')
            self.heading = int(tag[1])
        elif tag in ('p', 'br', 'div', 'section', 'article'):
            self._flush()
        elif tag == 'li':
            self._flush()
            self.buf.append('- ')
        elif tag == 'img':
            self._flush()
            src = attr.get('src', '').strip()
            if src:
                self.lines += ['', '![%s](%s)' % (attr.get('alt', ''), src), '']
        elif tag == 'table':
            self._flush()
            self.rows = []
        elif tag == 'tr' and self.rows is not None:
            self.row = []
        elif tag in ('td', 'th') and self.row is not None:
            self.cell = []

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in ('script', 'style'):
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6') and self.heading:
            self._flush('#' * self.heading + ' ')
            self.lines.append('')
            self.heading = 0
        elif tag in ('p', 'div', 'section', 'article', 'li'):
            self._flush()
        elif tag in ('td', 'th') and self.cell is not None:
            self.row.append(re.sub(r'\s+', ' ', ''.join(self.cell)).strip())
            self.cell = None
        elif tag == 'tr' and self.row is not None:
            self.rows.append(self.row)
            self.row = None
        elif tag == 'table' and self.rows is not None:
            self.lines += [''] + _md_table(self.rows) + ['']
            self.rows = None

    def handle_data(self, data):
        if self.skip:
            return
        if self.cell is not None:
            self.cell.append(data)
        else:
            self.buf.append(data)

    def finish(self):
        self._flush()
        while self.buf and not ''.join(self.buf).strip():
            self.buf.pop()
        if self.cell is not None and self.row is not None:
            self.row.append(re.sub(r'\s+', ' ', ''.join(self.cell)).strip())
            self.cell = None
        if self.row is not None and self.rows is not None:
            self.rows.append(self.row)
            self.row = None
        if self.rows is not None:
            self.lines += [''] + _md_table(self.rows) + ['']
            self.rows = None
        return self.lines


def _import_html(src, dest, title, warnings):
    """HTML 转 md（去标签，保留标题层级、列表、表格和图片 src）。"""
    parser = _HtmlToMd()
    parser.feed(_read_text(src))
    parser.close()
    md = _tidy(parser.finish())
    if not md.startswith('# '):
        md = _tidy(('# %s\n\n%s' % (title, md)).splitlines())
    text, copied = _copy_images(md, Path(src).parent, dest, warnings)
    out = Path(dest) / (Path(src).stem + '.md')
    out.write_text(text + '\n', encoding='utf-8')
    return out, copied


def _import_txt(src, dest, title, warnings):
    text = _read_text(src).strip()
    if not text:
        warnings.append('文本文件是空的：%s' % Path(src).name)
    out = Path(dest) / (Path(src).stem + '.md')
    out.write_text('# %s\n\n%s\n' % (title, text) if text else '# %s\n' % title, encoding='utf-8')
    return out, 0


def _import_pdf(src, dest, title, pdf_cmd, warnings):
    """PDF 不做二进制解析，交给外部命令转文本；失败就提示先转 md。"""
    tokens = shlex.split(pdf_cmd or PDF_CMD)
    has_src = any('{src}' in t for t in tokens)
    has_out = any('{out}' in t for t in tokens)
    out_tmp = Path(dest) / ('.%s.pdf.txt' % Path(src).stem)
    cmd = []
    for token in tokens:
        if token == '{out}':
            cmd.append(str(out_tmp))
        else:
            cmd.append(token.replace('{src}', str(src)))
    if not has_src:
        cmd.append(str(src))
    if not has_out and Path(cmd[0]).name == 'pdftotext':
        cmd.append('-')  # pdftotext 输出到标准输出的写法

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding='utf-8', errors='replace', timeout=180)
    except (OSError, subprocess.SubprocessError) as e:
        warnings.append('PDF 转换失败（%s），请先把 PDF 转成 md 再导入' % e)
        return None, 0
    if proc.returncode != 0:
        detail = (proc.stderr or '').strip()[:200]
        warnings.append('PDF 转换失败（退出码 %s）%s，请先把 PDF 转成 md 再导入'
                        % (proc.returncode, ('：' + detail) if detail else ''))
        if out_tmp.exists():
            out_tmp.unlink()
        return None, 0

    text = _read_text(out_tmp) if has_out else (proc.stdout or '')
    if out_tmp.exists():
        out_tmp.unlink()
    text = text.replace('\f', '\n\n').strip()
    if not text:
        warnings.append('PDF 里没有提取到文字：%s，请先把 PDF 转成 md' % Path(src).name)
        return None, 0
    out = Path(dest) / (Path(src).stem + '.md')
    out.write_text('# %s\n\n%s\n' % (title, text), encoding='utf-8')
    return out, 0


def _doc_title(path, explicit):
    if explicit:
        return explicit
    if Path(path).suffix.lower() in MARKDOWN_SUFFIX:
        return _title_of(_read_text(path), Path(path).stem)
    return Path(path).stem


# ---------- ingest ----------

def import_cmd(src_path, dest_dir, title='', force=False, pdf_cmd=PDF_CMD):
    src, dest = Path(src_path).expanduser(), Path(dest_dir).expanduser()
    if src.is_dir():
        sources = sorted(p for p in src.iterdir()
                         if p.is_file() and p.suffix.lower() in DOC_SUFFIX)
        if not sources:
            raise ValueError('目录里没有可导入的文档：%s' % src)
    elif src.is_file():
        if src.suffix.lower() not in DOC_SUFFIX:
            raise ValueError('不支持的文件类型：%s（支持 md / html / txt / docx / pdf）'
                             % (src.suffix or '无扩展名'))
        sources = [src]
    else:
        raise ValueError('找不到文件或目录：%s' % src)

    if dest.exists() and not dest.is_dir():
        raise ValueError('目标不是目录：%s' % dest)
    if dest.exists() and any(dest.iterdir()) and not force:
        raise ValueError('目标目录不是空的：%s（覆盖请加 --force）' % dest)
    dest.mkdir(parents=True, exist_ok=True)

    warnings, files, images, titles = [], [], 0, []
    for path in sources:
        doc_title = _doc_title(path, title if len(sources) == 1 else '')
        titles.append(doc_title)
        suffix = path.suffix.lower()
        if suffix in MARKDOWN_SUFFIX:
            out, n = _import_md(path, dest, warnings)
        elif suffix in HTML_SUFFIX:
            out, n = _import_html(path, dest, doc_title, warnings)
        elif suffix in TEXT_SUFFIX:
            out, n = _import_txt(path, dest, doc_title, warnings)
        elif suffix == '.docx':
            out, n = _import_docx(path, dest, doc_title, warnings)
        else:
            out, n = _import_pdf(path, dest, doc_title, pdf_cmd, warnings)
        images += n
        if out is not None:
            files.append(out.relative_to(dest).as_posix())

    overall = title or (titles[0] if len(sources) == 1 and titles else dest.name)
    return {'dest': str(dest.resolve()), 'title': overall, 'files': files,
            'images': images, 'warnings': warnings}


# ---------- 大纲 ----------

def _sections(lines):
    """按 markdown 标题切节：每节带自己的正文（含子标题），代码块里的假标题忽略。"""
    mask = _code_mask(lines)
    heads = []
    for i, line in enumerate(lines):
        m = HEADING.match(line.strip())
        if m and not mask[i]:
            heads.append({'idx': i, 'level': len(m.group(1)), 'title': m.group(2).strip()})
    for k, sec in enumerate(heads):
        end = len(lines)
        for nxt in heads[k + 1:]:
            if nxt['level'] <= sec['level']:
                end = nxt['idx']
                break
        sec['body'] = [(j + 1, lines[j]) for j in range(sec['idx'] + 1, end)]
    return heads


def _outline_sections(text):
    lines = text.splitlines()
    mask = _code_mask(lines)
    out = []
    for i, line in enumerate(lines):
        m = HEADING.match(line.strip())
        if not m or mask[i]:
            continue
        level = len(m.group(1))
        if level > 2:
            continue
        name = m.group(2).strip()
        tags = [k for k in ('验收', '范围', '接口', '异常') if k in name]
        out.append({'line': i + 1, 'level': level, 'title': name, 'tags': tags})
    return out


# ---------- 目录清单 ----------

def list_cmd(directory, out=''):
    """列出需求目录里的正文文件：大小、行数、图片数，并附 # / ## 大纲。"""
    d = Path(directory).expanduser()
    if not d.is_dir():
        raise ValueError('目录不存在：%s' % d)
    mds = sorted(p for p in d.glob('*.md') if p.is_file())
    if not mds:
        raise ValueError('目录里没有 md：%s' % d)
    files, total_size, total_images = [], 0, 0
    for p in mds:
        text = _read_text(p)
        size = p.stat().st_size
        n_images = len(_image_refs(text))
        files.append({'file': p.name, 'size': size, 'lines': len(text.splitlines()),
                      'images': n_images, 'title': _title_of(text, p.stem),
                      'sections': _outline_sections(text)})
        total_size += size
        total_images += n_images
    assets = d / ASSETS
    data = {'dir': str(d.resolve()), 'files': files,
            'total_size': total_size, 'total_images': total_images,
            'assets': len([p for p in assets.rglob('*') if p.is_file()]) if assets.is_dir() else 0}
    return _write_out(data, out) if out else data


# ---------- 要素抽取 ----------

def _strip_item(text):
    """去掉列表编号、引用符号和加粗标记。"""
    s = re.sub(r'^>\s*', '', (text or '').strip())
    s = ITEM.sub('', s, count=1)
    s = re.sub(r'^\*\*(.+?)\*\*$', r'\1', s)
    return s.strip()


def _items(raw):
    """一行拆成条目：列表项去掉编号，普通段落整行算一条。"""
    s = (raw or '').strip()
    if not s or s.startswith(('|', '>', '```', '#')):
        return []
    m = ITEM.match(s)
    if m:
        t = s[m.end():].strip()
        return [t] if t else []
    return [s]


def _strip_accept(text):
    s = re.sub(r'^\*\*(.+?)\*\*$', r'\1', (text or '').strip())
    s = re.sub(r'^(?:AC|验收条件|验收点)\s*[-－—:：]?\s*\d+\s*[：:.、)）]?\s*', '', s, flags=re.I)
    return s.strip(' 。；;')


def _match_sections(sections, keys):
    return [s for s in sections if any(k in s['title'] for k in keys)]


def _dedupe(items, key):
    out, seen = [], set()
    for it in items:
        norm = re.sub(r'\s+', '', key(it)).lower()
        if norm and norm not in seen:
            seen.add(norm)
            out.append(it)
    return out


def _extract_goals(sections, mask, file):
    out = []
    for sec in _match_sections(sections, GOAL_KEYS):
        for no, raw in sec['body']:
            if mask[no - 1] or HEADING.match(raw.strip()):
                continue
            for item in _items(raw):
                t = item.strip(' 。；;')
                if 2 <= len(t) <= 200:
                    out.append({'text': t, 'file': file, 'line': no})
    return out


def _extract_scope(sections, mask, file):
    inside, outside = [], []
    for sec in _match_sections(sections, SCOPE_KEYS):
        mode = 'in'
        for no, raw in sec['body']:
            h = HEADING.match(raw.strip())
            if h:
                mode = 'out' if OUT_HINT.search(h.group(2)) else 'in'
                continue
            if mask[no - 1]:
                continue
            for item in _items(raw):
                text = _strip_item(item)
                if not text:
                    continue
                if OUT_HINT.search(text):
                    mode = 'out'
                text = SCOPE_PREFIX.sub('', text).strip(' 。；;')
                if len(text) < 2:
                    continue
                bucket = outside if mode == 'out' else inside
                bucket.append({'text': text, 'file': file, 'line': no})
    return inside, outside


def _extract_acceptance(sections, lines, mask, file):
    out, secs = [], _match_sections(sections, ACCEPT_KEYS)
    for sec in secs:
        for no, raw in sec['body']:
            if mask[no - 1] or HEADING.match(raw.strip()):
                continue
            for item in _items(raw):
                t = _strip_accept(item)
                if 2 <= len(t) <= 300:
                    out.append({'text': t, 'file': file, 'line': no})
    if not secs:  # 没有专门小节时，全文找带验收口吻的句子兜底
        for no, raw in enumerate(lines, 1):
            if mask[no - 1] or HEADING.match(raw.strip()) or raw.strip().startswith('|'):
                continue
            if ACCEPT_HINT.search(raw):
                for item in _items(raw):
                    t = _strip_accept(item)
                    if 2 <= len(t) <= 300:
                        out.append({'text': t, 'file': file, 'line': no})
    return out


def _cells(raw):
    s = raw.strip()
    if s.startswith('|'):
        s = s[1:]
    if s.endswith('|'):
        s = s[:-1]
    return [c.strip() for c in re.split(r'(?<!\\)\|', s)]


def _is_sep(cells):
    return bool(cells) and all(re.fullmatch(r':?-{2,}:?', c or '') for c in cells)


def _table_interfaces(block, file):
    """从 markdown 表格里抽 方法 + 路径；表头认不出来就整行找 METHOD /path。"""
    out = []
    rows = [(no, _cells(raw)) for no, raw in block]
    header = None
    if len(rows) >= 2 and _is_sep(rows[1][1]):
        header, data = rows[0][1], rows[2:]
    else:
        data = rows
    method_col = path_col = None
    if header:
        for idx, cell in enumerate(header):
            low = cell.lower()
            if method_col is None and low in METHOD_HEADERS:
                method_col = idx
            if path_col is None and low in PATH_HEADERS:
                path_col = idx

    if method_col is not None and path_col is not None:
        for no, row in data:
            method = row[method_col].strip().upper() if method_col < len(row) else ''
            path = row[path_col].strip() if path_col < len(row) else ''
            if not re.fullmatch(r'[A-Z]+', method) or not path:
                found = METHOD_PATH.search(' '.join(row))
                if found:
                    method, path = found.group(1).upper(), found.group(2)
            if not re.fullmatch(r'[A-Z]+', method or '') or not path:
                continue
            note = ' / '.join(c for k, c in enumerate(row) if k not in (method_col, path_col) and c)
            out.append({'method': method, 'path': path, 'note': note, 'file': file, 'line': no})
        return out

    for no, row in data:
        found = METHOD_PATH.search(' '.join(row))
        if found:
            out.append({'method': found.group(1).upper(), 'path': found.group(2),
                        'note': '', 'file': file, 'line': no})
    return out


def _extract_interfaces(lines, mask, file):
    out, i = [], 0
    while i < len(lines):
        if mask[i] or not lines[i].strip().startswith('|'):
            i += 1
            continue
        block = []
        while i < len(lines) and lines[i].strip().startswith('|') and not mask[i]:
            block.append((i + 1, lines[i]))
            i += 1
        out += _table_interfaces(block, file)

    for no, line in enumerate(lines, 1):
        if mask[no - 1] or line.strip().startswith('|'):
            continue
        for m in METHOD_PATH.finditer(line):
            out.append({'method': m.group(1).upper(), 'path': m.group(2),
                        'note': '', 'file': file, 'line': no})
    return _dedupe(out, lambda x: x['method'] + x['path'])


def _extract_questions(lines, mask, file):
    out = []
    for no, raw in enumerate(lines, 1):
        if mask[no - 1]:
            continue
        s = _strip_item(raw)
        if not s or HEADING.match(raw.strip()):
            continue
        marked = bool(QUESTION_HINT.search(s)) or s.rstrip().endswith(('？', '?'))
        if marked and len(s) >= 3:
            out.append({'text': s, 'file': file, 'line': no})
    return out


def extract_cmd(directory, out=''):
    d = Path(directory).expanduser()
    if not d.is_dir():
        raise ValueError('目录不存在：%s' % d)
    mds = sorted(p for p in d.glob('*.md') if p.is_file())
    if not mds:
        raise ValueError('目录里没有 md：%s' % d)

    goals, scope_in, scope_out = [], [], []
    acceptance, interfaces, questions = [], [], []
    for p in mds:
        text = _read_text(p)
        lines = text.splitlines()
        mask = _code_mask(lines)
        sections = _sections(lines)
        goals += _extract_goals(sections, mask, p.name)
        a, b = _extract_scope(sections, mask, p.name)
        scope_in += a
        scope_out += b
        acceptance += _extract_acceptance(sections, lines, mask, p.name)
        interfaces += _extract_interfaces(lines, mask, p.name)
        questions += _extract_questions(lines, mask, p.name)

    goals = _dedupe(goals, lambda x: x['text'])
    scope_in = _dedupe(scope_in, lambda x: x['text'])
    scope_out = _dedupe(scope_out, lambda x: x['text'])
    acceptance = _dedupe(acceptance, lambda x: x['text'])
    acceptance = [dict(ac, id='AC-%02d' % i) for i, ac in enumerate(acceptance, 1)]
    questions = _dedupe(questions, lambda x: x['text'])

    warnings = []
    if not goals:
        warnings.append('没有找到目标小节')
    if not acceptance:
        warnings.append('没有找到验收条件')
    if not interfaces:
        warnings.append('没有找到接口约定')

    data = {
        'dir': str(d.resolve()),
        'files': [p.name for p in mds],
        'goals': goals,
        'scope': {'in': scope_in, 'out': scope_out},
        'acceptance': acceptance,
        'interfaces': interfaces,
        'questions': questions,
        'warnings': warnings,
    }
    return _write_out(data, out) if out else data


# ---------- 残缺检查 ----------

def check_cmd(directory):
    d = Path(directory).expanduser()
    if not d.is_dir():
        raise ValueError('目录不存在：%s' % d)
    mds = sorted(p for p in d.glob('*.md') if p.is_file())
    if not mds:
        raise ValueError('目录里没有 md：%s' % d)

    issues = []
    for p in mds:
        text = _read_text(p)
        lines = text.splitlines()
        mask = _code_mask(lines)

        for no, line in enumerate(lines, 1):
            if mask[no - 1]:
                continue
            for ref in _image_refs(line):
                if _is_remote(ref):
                    if re.match(r'^https?:', ref, re.I):
                        issues.append({'type': 'remote_image', 'file': p.name, 'line': no,
                                       'ref': ref, 'message': '外链图片可能失效：%s' % ref})
                    continue
                if not _resolve_ref(ref, p.parent).is_file():
                    issues.append({'type': 'missing_image', 'file': p.name, 'line': no,
                                   'ref': ref, 'message': '图片引用不存在：%s' % ref})

        heads = []
        for i, line in enumerate(lines):
            m = HEADING.match(line.strip())
            if m and not mask[i]:
                heads.append((i, len(m.group(1)), m.group(2).strip()))

        prev = None
        for i, level, name in heads:
            if prev is not None and level > prev + 1:
                issues.append({'type': 'heading_jump', 'file': p.name, 'line': i + 1,
                               'title': name,
                               'message': '标题层级从 h%d 跳到 h%d：%s' % (prev, level, name)})
            prev = level

        for k, (i, level, name) in enumerate(heads):
            end = len(lines)
            for j, lvl, _ in heads[k + 1:]:
                if lvl <= level:
                    end = j
                    break
            if not any(x.strip() for x in lines[i + 1:end]):
                issues.append({'type': 'empty_section', 'file': p.name, 'line': i + 1,
                               'title': name, 'message': '章节是空的：%s' % name})

        sections = _sections(lines)
        acc = _match_sections(sections, ACCEPT_KEYS)
        if not acc:
            issues.append({'type': 'missing_acceptance', 'file': p.name, 'line': None,
                           'message': '没有验收条件小节'})
        else:
            has_text = False
            for sec in acc:
                for no, raw in sec['body']:
                    if mask[no - 1] or HEADING.match(raw.strip()):
                        continue
                    if raw.strip():
                        has_text = True
                        break
                if has_text:
                    break
            if not has_text:
                issues.append({'type': 'acceptance_empty', 'file': p.name, 'line': None,
                               'message': '验收条件小节是空的'})

    by_type = {}
    for issue in issues:
        by_type[issue['type']] = by_type.get(issue['type'], 0) + 1
    return {
        'dir': str(d.resolve()),
        'files': [p.name for p in mds],
        'ok': not issues,
        'summary': {'files': len(mds), 'issues': len(issues), 'by_type': by_type},
        'issues': issues,
    }


# ---------- CLI ----------

def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='prd_ingest.py',
        description='PRD 文档导入：整理正文和图片，列目录/抽要素，检查残缺（输出 JSON）')
    sub = parser.add_subparsers(dest='cmd', required=True)

    s = sub.add_parser('ingest', help='导入 md / html / txt / docx / pdf 到需求目录')
    s.add_argument('--src', required=True, help='PRD 文件或目录')
    s.add_argument('--dir', '--dest', dest='dest', required=True, help='需求目录')
    s.add_argument('--title', default='', help='标题，不给就从正文一级标题或文件名取')
    s.add_argument('--force', action='store_true', help='目标目录非空时覆盖')
    s.add_argument('--pdf-cmd', default=PDF_CMD,
                   help='PDF 转文本命令，默认 pdftotext -layout；支持 {src} / {out} 占位')
    s.set_defaults(fn=lambda a: import_cmd(a.src, a.dest, a.title, a.force, a.pdf_cmd))

    s = sub.add_parser('list', help='列出正文文件的大小、图片数和大纲')
    s.add_argument('--dir', required=True, help='需求目录')
    s.add_argument('--out', default='', help='把 JSON 同时写到文件')
    s.set_defaults(fn=lambda a: list_cmd(a.dir, a.out))

    s = sub.add_parser('extract', help='抽目标、范围、验收条件、接口约定、待确认问题')
    s.add_argument('--dir', required=True, help='需求目录')
    s.add_argument('--out', default='', help='把 JSON 同时写到文件')
    s.set_defaults(fn=lambda a: extract_cmd(a.dir, a.out))

    s = sub.add_parser('check', help='检查缺图、外链图片、空章节、验收为空、标题跳级')
    s.add_argument('--dir', required=True, help='需求目录')
    s.set_defaults(fn=lambda a: check_cmd(a.dir))

    a = parser.parse_args(argv)
    try:
        result = a.fn(a)
    except (ValueError, OSError) as e:
        print(json.dumps({'error': str(e)}, ensure_ascii=False), file=sys.stderr)
        return 2
    emit(result)
    return 0


if __name__ == '__main__':
    sys.exit(main())
