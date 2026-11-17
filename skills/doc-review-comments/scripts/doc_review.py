#!/usr/bin/env python3
"""方案文档逐段审阅：把意见锚定到原文某一段，逐条交给模型改，留前后对比。

子命令：
  add     在文档的一段原文上挂一条意见（锚点找不到时给最接近的 3 个候选）
  list    列出意见（可按 open / resolved 过滤）
  apply   逐条让模型按意见改文档，改完写回说明并输出 unified diff
  accept  标记这条意见已接受
  reject  标记这条意见不采纳，并记下原因
  diff    输出当前文档与上一版备份的 unified diff
  select  解析文档里的勾选表，列出供人点选的选项

数据在 <dir>/.review/comments.json，备份在 <dir>/.review/backup/。
除 diff 外全部输出 JSON。
"""
import argparse
import datetime
import difflib
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / '_lib'))

from wbkit import agent  # noqa: E402

REVIEW_DIR = '.review'
COMMENTS_FILE = '.review/comments.json'
BACKUP_DIR = '.review/backup'
LOG_DIR = '.review/logs'
STATUSES = ('open', 'resolved')
ANCHOR_CHARS = 40                   # 锚点只存原文前 40 字，够定位
OUTSIDE_WARN_LINES = 5              # 目标段之外改动超过这么多行就给 warning
MAX_DOC_CHARS = 40000               # 提示词里最多带这么多字文档
SEP_RE = re.compile(r'^\|?[\s:\-|]+\|?$')


class ReviewError(ValueError):
    """可以直接展示给用户的错误；extra 会一起并进 JSON 输出。"""

    def __init__(self, message, **extra):
        super().__init__(message)
        self.extra = extra


def emit(data):
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _now():
    return datetime.datetime.now().astimezone().isoformat(timespec='seconds')


def _root(directory):
    root = Path(directory).expanduser().resolve()
    if not root.is_dir():
        raise ReviewError('目录不存在：%s' % directory)
    return root


def _doc_path(root, doc):
    """只允许改目录里的 md 文档，且不能碰 .review 下的备份。"""
    p = (root / doc).resolve()
    try:
        rel = p.relative_to(root)
    except ValueError:
        raise ReviewError('只能审阅目录里的文档：%s' % doc)
    if rel.parts and rel.parts[0] == REVIEW_DIR:
        raise ReviewError('不能审阅 .review 里的文件：%s' % doc)
    if p.suffix.lower() != '.md' or not p.is_file():
        raise ReviewError('只能审阅存在的 .md 文档：%s' % doc)
    return p


# ---------- 意见读写 ----------

def _load(root):
    path = root / COMMENTS_FILE
    if not path.is_file():
        return {'doc': '', 'comments': []}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as e:
        raise ReviewError('意见文件读不了：%s（%s）' % (path, e))
    if not isinstance(data, dict):
        raise ReviewError('意见文件格式不对：%s' % path)
    data['doc'] = str(data.get('doc') or '')
    data['comments'] = [c for c in data.get('comments') or [] if isinstance(c, dict)]
    return data


def _save(root, data):
    path = root / COMMENTS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix='.comments-')
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _next_id(comments):
    used = set()
    for c in comments:
        m = re.fullmatch(r'C-(\d+)', str(c.get('id') or ''))
        if m:
            used.add(int(m.group(1)))
    n = 1
    while n in used:
        n += 1
    return 'C-%02d' % n


def _find(data, cid):
    c = next((x for x in data['comments'] if x.get('id') == cid), None)
    if not c:
        raise ReviewError('没有这条意见：%s' % cid)
    return c


def _short(text, limit=60):
    text = ' '.join(str(text or '').split())
    return text[:limit] + ('…' if len(text) > limit else '')


def candidates(content, anchor, limit=3):
    """锚点没命中时，从原文里挑最像的几段，方便用户改正锚点。"""
    anchor = _short(anchor, 200)
    blocks = [b.strip() for b in re.split(r'\n\s*\n', content or '') if b.strip()]
    lines = [x.strip() for x in (content or '').splitlines() if x.strip()]
    seen, scored = set(), []
    for cand in blocks + lines:
        if cand in seen:
            continue
        seen.add(cand)
        m = difflib.SequenceMatcher(None, anchor, cand).find_longest_match(
            0, len(anchor), 0, len(cand))
        score = m.size / max(1, len(anchor))
        scored.append((score, cand, m.b))
    scored.sort(key=lambda x: x[0], reverse=True)
    out = []
    for _, cand, at in scored[:limit]:
        snippet = cand if len(cand) <= 100 else cand[max(0, at - 20):max(0, at - 20) + 100]
        out.append(_short(snippet, 100))
    return out


def add(root, doc, anchor, text, section=''):
    """在文档的一段原文上挂一条意见。锚点在原文里找不到时报错并给候选。"""
    root = _root(root)
    path = _doc_path(root, doc)
    anchor = (anchor or '').strip()[:ANCHOR_CHARS]
    text = (text or '').strip()
    if not anchor:
        raise ReviewError('原文片段不能为空，先选一段原文')
    if not text:
        raise ReviewError('意见不能为空')
    content = path.read_text(encoding='utf-8')
    if anchor not in content:
        raise ReviewError('原文里找不到这段：「%s」；最接近的片段见 candidates' % _short(anchor),
                          candidates=candidates(content, anchor))
    data = _load(root)
    rec = {'id': _next_id(data['comments']), 'doc': doc, 'anchor': anchor,
           'section': (section or '').strip(), 'text': text,
           'status': 'open', 'resolution': ''}
    data['comments'].append(rec)
    data['doc'] = doc
    _save(root, data)
    return rec


def list_comments(root, status=None):
    """列出意见；status 只能是 open / resolved。"""
    root = _root(root)
    if status and status not in STATUSES:
        raise ReviewError('status 只能是 %s' % ' / '.join(STATUSES))
    data = _load(root)
    items = [c for c in data['comments'] if not status or c.get('status') == status]
    counts = {s: sum(1 for c in data['comments'] if c.get('status') == s) for s in STATUSES}
    return {'ok': True, 'dir': str(root), 'doc': data.get('doc') or '',
            'count': len(items), 'counts': counts, 'comments': items}


# ---------- 找锚点所在段落、算 diff ----------

def unified_diff(before, after, before_label='改之前', after_label='改之后', n=3):
    return '\n'.join(difflib.unified_diff(before.splitlines(), after.splitlines(),
                                          before_label, after_label, lineterm='', n=n))


def _anchor_region(text, anchor):
    """锚点所在段落（按空行分隔）的行范围 [lo, hi)，行号从 0 起。"""
    idx = text.find(anchor or '')
    if idx < 0:
        return None
    start = text.count('\n', 0, idx)
    end = text.count('\n', 0, idx + len(anchor))
    lines = text.splitlines()
    lo = start
    while lo > 0 and lines[lo - 1].strip():
        lo -= 1
    hi = min(len(lines), end + 1)
    while hi < len(lines) and lines[hi].strip():
        hi += 1
    return lo, hi


def _outside_changes(before, after, region):
    """统计“目标段之外”改了多少行（按行做序列比较，纯插入按位置判断）。"""
    ops = difflib.SequenceMatcher(None, before.splitlines(), after.splitlines()).get_opcodes()
    if region is None:
        return sum(max(i2 - i1, j2 - j1) for tag, i1, i2, j1, j2 in ops if tag != 'equal')
    lo, hi = region
    total = 0
    for tag, i1, i2, j1, j2 in ops:
        if tag == 'equal':
            continue
        inside = (lo <= i1 <= hi) if i1 == i2 else (i1 < hi and lo < i2)
        if not inside:
            total += max(i2 - i1, j2 - j1)
    return total


# ---------- 提示词与模型返回 ----------

RULES = '''你是这份方案文档的作者，现在按用户的一条审阅意见改文档。

## 硬约束
- 只改意见挂的这一个段落（下面给出的小节和原文片段）；其他段落一个字都不要动。
- 只改这一个文件，不改别的文件，不跑命令，不提交。
- 改动尽量小，不要重写整篇，也不要顺手优化没被提到的部分。
- 做不到、或者认为不该这么改，就不要改文件，在 how 里说明原因。
- 改完必须说明改了哪一段、怎么改的。

## 输出
先改文件，最后只输出一行 JSON：
{"id":"意见编号","section":"改动所在的小节","how":"改了哪一段、怎么改的，一句话"}'''


def build_prompt(doc, content, comment):
    """单条意见的提示词：带上小节、锚点原文和当前全文。"""
    body = content
    if len(body) > MAX_DOC_CHARS:
        body = body[:MAX_DOC_CHARS] + '\n\n（文档过长，只带了前 %d 字）' % MAX_DOC_CHARS
    lines = [RULES, '',
             '## 这条意见',
             '- 编号：%s' % comment.get('id'),
             '- 小节：%s' % (comment.get('section') or '（未标注）'),
             '- 原文片段：「%s」' % (comment.get('anchor') or ''),
             '- 意见：%s' % comment.get('text'),
             '',
             '## 文档：%s' % doc,
             '```markdown', body.rstrip('\n'), '```']
    return '\n'.join(lines) + '\n'


def parse_change(text):
    """从模型输出里取 {section, how}。允许被说明文字或代码块包着。"""
    raw = (text or '').strip()
    dec = json.JSONDecoder()
    for i, ch in enumerate(raw):
        if ch != '{':
            continue
        try:
            obj, _ = dec.raw_decode(raw[i:])
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        changes = obj.get('changes')
        if isinstance(changes, list):
            for item in changes:
                if isinstance(item, dict) and str(item.get('how') or '').strip():
                    return {'section': str(item.get('section') or '').strip(),
                            'how': str(item['how']).strip()}
        how = str(obj.get('how') or obj.get('resolution') or '').strip()
        if how:
            return {'section': str(obj.get('section') or '').strip(), 'how': how}
    return {}


# ---------- apply ----------

def _backup_dir(root):
    d = root / BACKUP_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _snapshot(root, path):
    """把改之前的原文存一份，文件名是时间戳。"""
    d = _backup_dir(root)
    ts = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    target = d / ('%s.md' % ts)
    n = 2
    while target.exists():
        target = d / ('%s-%d.md' % (ts, n))
        n += 1
    shutil.copy2(path, target)
    return target


def _pick_targets(data, doc, cid):
    pool = [c for c in data['comments'] if c.get('doc') == doc]
    if cid:
        hit = [c for c in pool if c.get('id') == cid]
        if not hit:
            raise ReviewError('这个文档下没有意见 %s' % cid)
        if hit[0].get('status') != 'open':
            raise ReviewError('意见 %s 已经是 %s，不能再改' % (cid, hit[0].get('status')))
        return hit
    # 默认只改还没改过的（resolution 为空）；改过的用 --id 重跑
    return [c for c in pool if c.get('status') == 'open' and not c.get('resolution')]


def apply(root, doc, cid=None, engine='kimi', dry_run=False, runner=None, timeout=1800,
          conf=None):
    """逐条让模型改文档。一条失败只影响这一条，其余照旧。"""
    root = _root(root)
    path = _doc_path(root, doc)
    data = _load(root)
    targets = _pick_targets(data, doc, cid)
    if not targets:
        return {'ok': True, 'doc': doc, 'engine': engine, 'dry_run': bool(dry_run),
                'backup': None, 'applied': [], 'diff': '',
                'note': '没有待改的意见；改过的意见用 --id 重跑'}

    if dry_run:
        content = path.read_text(encoding='utf-8')
        calls = []
        for c in targets:
            prompt = build_prompt(doc, content, c)
            calls.append({'id': c['id'], 'profile': 'docs',
                          'prompt_chars': len(prompt), 'prompt': prompt})
        return {'ok': True, 'doc': doc, 'engine': engine, 'dry_run': True,
                'backup': None, 'applied': [], 'diff': '', 'calls': calls,
                'note': '只打印提示词，没有改文件；去掉 --dry-run 才会真改'}

    backup = _snapshot(root, path)
    original = path.read_text(encoding='utf-8')
    runner = runner or agent.run
    applied = []
    data['doc'] = doc
    for c in targets:
        before = path.read_text(encoding='utf-8')
        prompt = build_prompt(doc, before, c)
        item = {'id': c['id'], 'ok': False, 'changed': False,
                'resolution': c.get('resolution') or '', 'diff': '',
                'warnings': [], 'error': None}
        log = root / LOG_DIR / ('%s-%s.log' % (datetime.datetime.now().strftime('%Y%m%d-%H%M%S'), c['id']))
        try:
            result = runner(engine, prompt, cwd=str(root), profile='docs', timeout=timeout,
                            log=str(log), conf=conf)
        except Exception as e:  # 模型进程的任何异常都只算这一条失败
            result = {'ok': False, 'error': str(e)}

        if not result.get('ok'):
            item['error'] = str(result.get('error') or '模型调用失败')[:500]
            c['error'] = item['error']
            _save(root, data)
            applied.append(item)
            continue

        after = path.read_text(encoding='utf-8')
        change = parse_change(result.get('text'))
        item['diff'] = unified_diff(before, after)
        item['changed'] = before != after
        if item['changed'] and not change.get('how'):
            path.write_text(before, encoding='utf-8')       # 没有说明就回滚这一条
            item.update(changed=False, diff='',
                        error='模型没有说明改了哪一段、怎么改的，已回滚这一条')
            c['error'] = item['error']
            _save(root, data)
            applied.append(item)
            continue

        resolution = change.get('how') or '模型没有改动'
        section = change.get('section') or ''
        if section and section not in resolution:
            resolution = '%s：%s' % (section, resolution)
        c['resolution'] = resolution
        c.pop('error', None)
        item.update(ok=True, resolution=resolution)

        outside = _outside_changes(before, after, _anchor_region(before, c.get('anchor') or ''))
        if item['changed'] and outside > OUTSIDE_WARN_LINES:
            item['warnings'].append('目标段之外改了 %d 行，确认一下是不是改多了' % outside)
        _save(root, data)
        applied.append(item)

    final = path.read_text(encoding='utf-8')
    return {'ok': all(i['ok'] for i in applied), 'doc': doc, 'engine': engine,
            'dry_run': False, 'backup': str(backup), 'applied': applied,
            'applied_count': sum(1 for i in applied if i['ok']),
            'failed_count': sum(1 for i in applied if not i['ok']),
            'diff': unified_diff(original, final)}


# ---------- 接受 / 拒绝 ----------

def _decide(root, cid, line):
    root = _root(root)
    data = _load(root)
    c = _find(data, cid)
    if c.get('status') != 'open':
        raise ReviewError('意见 %s 已经是 %s，不能再改状态' % (cid, c.get('status')))
    c['status'] = 'resolved'
    c['resolution'] = (c.get('resolution') + '\n' if c.get('resolution') else '') + line
    c.pop('error', None)
    _save(root, data)
    return c


def accept(root, cid):
    """这条意见按模型改的结果收下。"""
    return _decide(root, cid, '[已接受]')


def reject(root, cid, reason):
    """这条意见不采纳，reason 写进 resolution。"""
    reason = (reason or '').strip()
    if not reason:
        raise ReviewError('写一下为什么不采纳')
    return _decide(root, cid, '[不采纳] 原因：%s' % reason)


# ---------- diff / select ----------

def diff_text(root, doc, backup=''):
    """当前文档对某个备份（默认最近一版）的 unified diff。"""
    root = _root(root)
    path = _doc_path(root, doc)
    bdir = root / BACKUP_DIR
    if backup:
        target = Path(backup).expanduser()
        if not target.is_file():
            target = bdir / backup
        if not target.is_file() and not backup.endswith('.md'):
            target = bdir / (backup + '.md')
        if not target.is_file():
            raise ReviewError('找不到备份：%s（备份目录 %s）' % (backup, bdir))
    else:
        files = sorted(bdir.glob('*.md')) if bdir.is_dir() else []
        if not files:
            raise ReviewError('还没有备份，先跑一次 apply')
        target = files[-1]
    return unified_diff(target.read_text(encoding='utf-8'), path.read_text(encoding='utf-8'),
                        '备份 %s' % target.name, '现在 %s' % path.name)


def _cells(line):
    return [c.strip() for c in line.strip().strip('|').split('|')]


def select(root, doc, table):
    """解析文档里的勾选表：按表头定位，逐行拆出 ID、问题、可选项。"""
    root = _root(root)
    path = _doc_path(root, doc)
    lines = path.read_text(encoding='utf-8').splitlines()
    wanted = [re.sub(r'\s+', '', c) for c in _cells(table)]
    head, tables = None, []
    for i, line in enumerate(lines):
        if not line.strip().startswith('|'):
            continue
        cells = _cells(line)
        tables.append(cells)
        if [re.sub(r'\s+', '', c) for c in cells] == wanted:
            head = i
            break
    if head is None:
        raise ReviewError('文档里找不到表头：%s' % ' | '.join(_cells(table)),
                          tables=[' | '.join(t) for t in tables[:5]])

    cols = _cells(lines[head])
    opt_col = next((i for i, c in enumerate(cols) if '可选' in c or '选项' in c), len(cols) - 1)
    question_col = next((i for i, c in enumerate(cols) if '问题' in c or '说明' in c),
                        1 if len(cols) > 1 else 0)
    rows = []
    for line in lines[head + 1:]:
        s = line.strip()
        if not s:
            if rows:
                break
            continue
        if not s.startswith('|'):
            if rows:
                break
            continue
        if SEP_RE.fullmatch(s):
            continue
        cells = _cells(line)
        if not cells or not cells[0]:
            continue
        raw = cells[opt_col] if opt_col < len(cells) else ''
        rows.append({'id': cells[0],
                     'question': cells[question_col] if question_col < len(cells) else '',
                     'options': [o.strip() for o in re.split(r'\s*/\s*|／', raw) if o.strip()]})
    return {'ok': True, 'doc': doc, 'header': cols, 'count': len(rows), 'rows': rows}


# ---------- CLI ----------

def cmd_add(a):
    emit(add(a.dir, a.doc, a.anchor, a.text, a.section))
    return 0


def cmd_list(a):
    emit(list_comments(a.dir, a.status))
    return 0


def cmd_apply(a):
    data = apply(a.dir, a.doc, cid=a.id, engine=a.engine, dry_run=a.dry_run, timeout=a.timeout)
    emit(data)
    return 0 if data.get('ok') else 1


def cmd_accept(a):
    emit(accept(a.dir, a.id))
    return 0


def cmd_reject(a):
    emit(reject(a.dir, a.id, a.reason))
    return 0


def cmd_diff(a):
    text = diff_text(a.dir, a.doc, a.backup)
    print(text if text else '（没有变化）')
    return 0


def cmd_select(a):
    emit(select(a.dir, a.doc, a.table))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description='方案文档逐段审阅：意见挂在原文上，逐条交给模型改，留前后 diff')
    sub = p.add_subparsers(dest='cmd', required=True)

    s = sub.add_parser('add', help='在原文一段上挂一条意见')
    s.add_argument('--dir', required=True, help='需求 / 文档所在目录')
    s.add_argument('--doc', required=True, help='目录里的 md 文件名')
    s.add_argument('--anchor', required=True, help='原文片段（存前 40 字做锚点）')
    s.add_argument('--text', required=True, help='意见内容')
    s.add_argument('--section', default='', help='小节标题，如“3.2 接口约定”')
    s.set_defaults(fn=cmd_add)

    s = sub.add_parser('list', help='列出意见')
    s.add_argument('--dir', required=True)
    s.add_argument('--status', choices=STATUSES, help='只看某一个状态')
    s.set_defaults(fn=cmd_list)

    s = sub.add_parser('apply', help='逐条让模型按意见改文档')
    s.add_argument('--dir', required=True)
    s.add_argument('--doc', required=True)
    s.add_argument('--id', default=None, help='只改这一条（已改过的也能重跑）')
    s.add_argument('--engine', default='kimi', choices=list(agent.ENGINES))
    s.add_argument('--timeout', type=int, default=1800, help='单次模型调用超时秒数')
    s.add_argument('--dry-run', action='store_true', help='只打印提示词，不改文件')
    s.set_defaults(fn=cmd_apply)

    s = sub.add_parser('accept', help='标记这条意见已接受')
    s.add_argument('--dir', required=True)
    s.add_argument('--id', required=True)
    s.set_defaults(fn=cmd_accept)

    s = sub.add_parser('reject', help='标记这条意见不采纳')
    s.add_argument('--dir', required=True)
    s.add_argument('--id', required=True)
    s.add_argument('--reason', required=True, help='不采纳的原因')
    s.set_defaults(fn=cmd_reject)

    s = sub.add_parser('diff', help='当前文档对上一版备份的 unified diff')
    s.add_argument('--dir', required=True)
    s.add_argument('--doc', required=True)
    s.add_argument('--backup', default='', help='备份时间戳；不给就用最近一版')
    s.set_defaults(fn=cmd_diff)

    s = sub.add_parser('select', help='解析文档里的勾选表，列出可选项')
    s.add_argument('--dir', required=True)
    s.add_argument('--doc', required=True)
    s.add_argument('--table', required=True, help='表头，如 "| 编号 | 问题 | 可选方向 |"')
    s.set_defaults(fn=cmd_select)

    a = p.parse_args(argv)
    try:
        return a.fn(a)
    except (ReviewError, agent.AgentError) as e:
        data = {'ok': False, 'error': str(e)}
        data.update(getattr(e, 'extra', {}) or {})
        print(json.dumps(data, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
