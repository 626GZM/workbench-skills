#!/usr/bin/env python3
"""任务交接 / 简报：在一个任务目录里维护 brief.json，按需生成简报或交接单。

子命令：
  init     建 brief.json（已存在时只补缺失字段，不覆盖已有内容）
  add      追加一条 decision / todo / pitfall / fact
  build    生成简报：项目背景、目标、验收条件、当前状态（git）、待办、关键决定、踩坑、下一步
  show     输出 brief.json 和简报正文
  handoff  生成给下一个人的交接单：已完成、未完成、卡在哪、风险、需要谁配合

build / handoff 可以用 --engine 让模型整理成通顺文档；调用失败自动回退规则版。
git 只读：不提交、不推送、不切换分支。
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / '_lib'))

from wbkit import agent, git  # noqa: E402

BRIEF_FILE = 'brief.json'
VERSION = 1
KINDS = {'decision': 'decisions', 'todo': 'todos', 'pitfall': 'pitfalls', 'fact': 'facts'}
ENGINES = ('claude', 'codex', 'deepseek', 'kimi')
RULE_ENGINES = ('', 'none', 'rule')

BUILD_SECTIONS = ('项目背景', '目标', '验收条件', '当前状态', '待办', '关键决定', '踩坑记录', '下一步')
HANDOFF_SECTIONS = ('已完成', '未完成', '卡在哪', '风险', '需要谁配合')


def now():
    return time.strftime('%Y-%m-%dT%H:%M:%S')


def emit(data):
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _norm(text):
    return re.sub(r'\s+', ' ', str(text or '')).strip()


def empty_brief():
    """一份空的简报。列表项统一用 {text, at}，待办额外有 done。"""
    return {
        'version': VERSION,
        'title': '',
        'background': '',
        'goal': '',
        'acceptance': [],
        'constraints': [],
        'repos': [],
        'status': '',
        'support': [],
        'todos': [],
        'decisions': [],
        'pitfalls': [],
        'facts': [],
        'created_at': '',
        'updated_at': '',
    }


def brief_path(task_dir):
    return Path(task_dir).expanduser() / BRIEF_FILE


def load(task_dir):
    p = brief_path(task_dir)
    if not p.is_file():
        raise ValueError('没有找到 %s，先运行 init --dir %s' % (p, task_dir))
    try:
        data = json.loads(p.read_text(encoding='utf-8'))
    except ValueError as e:
        raise ValueError('%s 不是合法 JSON：%s' % (p, e))
    if not isinstance(data, dict):
        raise ValueError('%s 内容应是对象' % p)
    base = empty_brief()
    for key, value in base.items():
        data.setdefault(key, value)
    data['version'] = data.get('version') or VERSION
    return data


def save(task_dir, data):
    p = brief_path(task_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    ts = now()
    if not data.get('created_at'):
        data['created_at'] = ts
    data['updated_at'] = ts
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return p


def _append(items, text, done=None):
    """按文本去重追加。返回 (是否新增, 是否有变化)。"""
    text = _norm(text)
    for item in items:
        if _norm(item.get('text')) == text:
            if done and not item.get('done'):
                item['done'] = True
                return False, True
            return False, False
    row = {'text': text, 'at': now()}
    if done is not None:
        row['done'] = bool(done)
    items.append(row)
    return True, True


def init_brief(task_dir, title='', background='', goal='', acceptance=(), constraints=(),
               repos=(), support=(), status=''):
    """建 brief.json。已存在时只补缺失字段和列表项；同值重复调用不产生变化。"""
    task_dir = Path(task_dir).expanduser()
    task_dir.mkdir(parents=True, exist_ok=True)
    p = task_dir / BRIEF_FILE
    created = not p.is_file()
    data = empty_brief() if created else load(task_dir)
    changed = created

    if title and data.get('title') != title:
        data['title'] = title
        changed = True
    if background and data.get('background') != background:
        data['background'] = background
        changed = True
    if goal and data.get('goal') != goal:
        data['goal'] = goal
        changed = True
    if status and data.get('status') != status:
        data['status'] = status
        changed = True

    for key, values in (('acceptance', acceptance), ('constraints', constraints),
                        ('support', support)):
        for value in values or ():
            added, modified = _append(data[key], value)
            if added or modified:
                changed = True
    for value in repos or ():
        value = str(value).strip()
        if value and value not in data['repos']:
            data['repos'].append(value)
            changed = True

    if changed:
        save(task_dir, data)
    return {'dir': str(task_dir.resolve()), 'path': str(p.resolve()),
            'created': created, 'changed': changed, 'brief': data}


def add_entry(task_dir, kind, text, done=False):
    """追加一条记录；同 kind 下同样文本只保留一条。--done 只对待办有效。"""
    if kind not in KINDS:
        raise ValueError('--kind 只能是 %s' % ' / '.join(KINDS))
    text = _norm(text)
    if not text:
        raise ValueError('--text 不能为空')
    task_dir = Path(task_dir).expanduser()
    data = load(task_dir)
    key = KINDS[kind]
    added, changed = _append(data[key], text, done=(bool(done) if kind == 'todo' else None))
    if changed:
        save(task_dir, data)
    return {'dir': str(task_dir.resolve()), 'kind': kind, 'added': added,
            'changed': changed, 'count': len(data[key]), 'brief': data}


# ---------- git 现状 ----------

def git_state(repo):
    """读一个仓库的分支、未提交改动、最近 5 条提交。仓库不存在或不是仓库时返回 ok=False。"""
    p = Path(str(repo)).expanduser()
    state = {'path': str(p), 'root': '', 'ok': False, 'branch': '', 'dirty': [],
             'untracked': [], 'commits': [], 'reason': ''}
    if not p.exists():
        state['reason'] = '目录不存在'
        return state
    root = git.repo_root(str(p))
    if not root:
        state['reason'] = '不是 git 仓库'
        return state
    state['root'] = root
    try:
        state['branch'] = git.branch(root)
        tracked, untracked = git.dirty_files(root)
        state['dirty'] = tracked
        state['untracked'] = untracked
        out = git.git(root, 'log', '-5', '--format=%h%x09%ci%x09%s')
        for line in out.splitlines():
            parts = line.split('\t', 2)
            if len(parts) == 3:
                state['commits'].append({'hash': parts[0], 'time': parts[1],
                                         'subject': parts[2]})
        state['ok'] = True
    except (git.GitError, OSError) as e:
        state['reason'] = '读取 git 失败：%s' % e
    return state


def _bullet(items, empty_text='未记录', with_time=False):
    if not items:
        return ['- ' + empty_text]
    lines = []
    for item in items:
        text = _norm(item.get('text') if isinstance(item, dict) else item)
        if not text:
            continue
        if with_time and isinstance(item, dict) and item.get('at'):
            lines.append('- %s（%s）' % (text, item['at']))
        else:
            lines.append('- ' + text)
    return lines or ['- ' + empty_text]


def _repo_lines(state):
    if not state.get('ok'):
        return ['- `%s`：跳过（%s）' % (state['path'], state.get('reason') or '无法读取')]
    dirty = state.get('dirty') or []
    untracked = state.get('untracked') or []
    total = len(dirty) + len(untracked)
    lines = ['- `%s`：分支 `%s`，未提交改动 %d 个（已跟踪 %d / 未跟踪 %d）' % (
        state['path'], state.get('branch') or '未知', total, len(dirty), len(untracked))]
    files = dirty + untracked
    if files:
        shown = '、'.join('`%s`' % f for f in files[:5])
        more = '（共 %d 个，只列前 5 个）' % len(files) if len(files) > 5 else ''
        lines.append('  - 改动文件：%s%s' % (shown, more))
    else:
        lines.append('  - 工作区干净')
    commits = state.get('commits') or []
    if commits:
        lines.append('  - 最近 %d 条提交：' % len(commits))
        for c in commits:
            lines.append('    - `%s` %s %s' % (c['hash'], c['time'][:19], c['subject']))
    else:
        lines.append('  - 最近提交：无')
    return lines


# ---------- 规则版 Markdown ----------

def render_build(brief, states):
    """规则版简报。所有缺失信息写“未记录”，不推测。"""
    title = _norm(brief.get('title')) or '未命名任务'
    lines = ['# 交接简报：%s' % title, '',
             '> 由 brief.json 和 git 现状生成。简报里没写的标为“未记录”，不做推测。', '']

    lines += ['## 项目背景', '']
    lines.append('- 标题：%s' % title)
    lines.append('- 背景：%s' % (_norm(brief.get('background')) or '未记录'))
    repos = brief.get('repos') or []
    lines.append('- 相关仓库：%s' % ('、'.join('`%s`' % r for r in repos) if repos else '未记录'))
    facts = brief.get('facts') or []
    if facts:
        lines.append('- 已确认的事实：')
        lines += ['  - %s' % _norm(f.get('text')) for f in facts if _norm(f.get('text'))]
    else:
        lines.append('- 已确认的事实：未记录')
    lines.append('')

    lines += ['## 目标', '']
    lines.append('- 目标：%s' % (_norm(brief.get('goal')) or '未记录'))
    constraints = brief.get('constraints') or []
    if constraints:
        lines.append('- 约束：')
        lines += ['  - %s' % c for c in constraints if _norm(c)]
    else:
        lines.append('- 约束：未记录')
    lines.append('')

    lines += ['## 验收条件', ''] + _bullet(brief.get('acceptance') or []) + ['']

    lines += ['## 当前状态', '']
    lines.append('- 简报记录：%s' % (_norm(brief.get('status')) or '未记录'))
    if not states:
        lines.append('- 未登记相关仓库，跳过 git 检查')
    else:
        lines.append('- 代码现状（以 git 为准）：')
        for state in states:
            lines += ['  ' + x for x in _repo_lines(state)]
    lines.append('')

    todos = brief.get('todos') or []
    if todos:
        lines += ['## 待办', '']
        for t in todos:
            mark = 'x' if t.get('done') else ' '
            lines.append('- [%s] %s' % (mark, _norm(t.get('text'))))
        lines.append('')
    else:
        lines += ['## 待办', '', '- 未记录', '']

    lines += ['## 关键决定', ''] + _bullet(brief.get('decisions') or []) + ['']
    lines += ['## 踩坑记录', ''] + _bullet(brief.get('pitfalls') or []) + ['']

    lines += ['## 下一步', '']
    opened = [t for t in todos if not t.get('done')]
    if opened:
        lines.append('- 继续：%s' % _norm(opened[0].get('text')))
    elif todos:
        lines.append('- 待办已全部勾选，等待验收')
    else:
        lines.append('- 未记录')
    return '\n'.join(lines) + '\n'


def render_handoff(brief):
    """交接单：只写 brief.json 里记录过的事实。已完成 / 未完成按 todo.done 严格区分。"""
    title = _norm(brief.get('title')) or '未命名任务'
    todos = brief.get('todos') or []
    done = [t for t in todos if t.get('done')]
    opened = [t for t in todos if not t.get('done')]

    lines = ['# 交接单：%s' % title, '',
             '> 只写 brief.json 里记录过的内容；没写的一律“未记录”，不推测。', '']
    lines += ['## 已完成', '']
    lines += ['- [x] %s' % _norm(t.get('text')) for t in done] or ['- 未记录']
    lines += ['', '## 未完成', '']
    lines += ['- [ ] %s' % _norm(t.get('text')) for t in opened] or ['- 未记录']
    lines += ['', '## 卡在哪', '']
    lines.append('- %s' % (_norm(brief.get('status')) or '未记录'))
    lines += ['', '## 风险', ''] + _bullet(brief.get('pitfalls') or []) + ['']
    lines += ['## 需要谁配合', ''] + _bullet(brief.get('support') or []) + ['']
    return '\n'.join(lines) + '\n'


# ---------- 模型整理 ----------

def _ai_enabled(engine):
    return bool(engine) and str(engine) not in RULE_ENGINES


def _strip_fence(text):
    text = str(text or '').strip()
    return re.sub(r'^```\w*\n|\n```$', '', text).strip()


def _check_sections(text, sections):
    missing = [s for s in sections if not re.search(r'^##\s+%s\s*$' % re.escape(s), text, re.M)]
    if missing:
        raise RuntimeError('模型输出缺少小节：%s' % '、'.join(missing))


def _polish(engine, task_dir, brief, states, draft, sections, kind):
    """让模型整理规则版草稿；只允许改写已有事实，缺的写“未记录”。"""
    prompt = '\n'.join([
        '你在整理一份任务%s，读者是接下来接手的人。' % ('简报' if kind == 'build' else '交接单'),
        '要求：',
        '- 只能使用下面 <简报数据>、<git 现状>、<规则版草稿> 里出现过的事实。',
        '- 不要补充、推测、编造任何内容；简报里没写的信息一律写“未记录”。',
        '- 禁止写“按计划推进”“进展顺利”“一切正常”这类没有事实依据的话。',
        '- 保留 Markdown，必须包含这些二级标题，顺序不变：%s。' % '、'.join(sections),
        '- 不要输出代码块围栏，不要解释，直接输出文档正文。',
        '',
        '<简报数据>', json.dumps(brief, ensure_ascii=False, indent=2), '</简报数据>',
        '<git 现状>', json.dumps(states, ensure_ascii=False, indent=2), '</git 现状>',
        '<规则版草稿>', draft.strip(), '</规则版草稿>',
    ])
    result = agent.run(engine, prompt, str(Path(task_dir).expanduser()), profile='docs')
    if not result.get('ok'):
        raise RuntimeError(result.get('error') or '模型调用失败')
    text = _strip_fence(result.get('text'))
    if not text:
        raise RuntimeError('模型没有返回内容')
    _check_sections(text, sections)
    return text + '\n'


def _prepare(task_dir, git_repos):
    data = load(task_dir)
    repos = list(git_repos or ()) or list(data.get('repos') or [])
    states = [git_state(r) for r in repos]
    return data, states


def build(task_dir, git_repos=(), out='', engine=''):
    """生成简报。模型失败自动回退规则版，不抛异常。"""
    data, states = _prepare(task_dir, git_repos)
    markdown = render_build(data, states)
    source, error = 'rule', ''
    if _ai_enabled(engine):
        try:
            markdown = _polish(engine, task_dir, data, states, markdown, BUILD_SECTIONS, 'build')
            source = 'ai'
        except Exception as e:  # 没装 CLI、没 key、限流、输出缺小节，一律回退
            error = str(e)
    out_path = ''
    if out:
        p = Path(out).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(markdown, encoding='utf-8')
        out_path = str(p.resolve())
    return {'dir': str(Path(task_dir).expanduser().resolve()), 'markdown': markdown,
            'source': source, 'engine': str(engine or ''), 'error': error,
            'out': out_path, 'brief': data, 'repos': states}


def handoff(task_dir, out, engine=''):
    """生成交接单并写文件。模型失败自动回退规则版。"""
    if not str(out or '').strip():
        raise ValueError('handoff 需要 --out 指定输出文件')
    data, states = _prepare(task_dir, ())
    markdown = render_handoff(data)
    source, error = 'rule', ''
    if _ai_enabled(engine):
        try:
            markdown = _polish(engine, task_dir, data, states, markdown, HANDOFF_SECTIONS, 'handoff')
            source = 'ai'
        except Exception as e:
            error = str(e)
    p = Path(out).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(markdown, encoding='utf-8')
    return {'dir': str(Path(task_dir).expanduser().resolve()), 'markdown': markdown,
            'source': source, 'engine': str(engine or ''), 'error': error,
            'out': str(p.resolve()), 'brief': data, 'repos': states}


def show(task_dir):
    """输出 brief.json 和规则版简报正文。"""
    data, states = _prepare(task_dir, ())
    return {'dir': str(Path(task_dir).expanduser().resolve()), 'brief': data,
            'markdown': render_build(data, states)}


# ---------- CLI ----------

def _engine(value):
    value = str(value or '').strip()
    if value and value not in ENGINES and value not in RULE_ENGINES:
        raise ValueError('--engine 只能是 %s / none' % ' / '.join(ENGINES))
    return value


def cmd_init(a):
    emit(init_brief(a.dir, title=a.title, background=a.background, goal=a.goal,
                    acceptance=a.acceptance or (), constraints=a.constraint or (),
                    repos=a.repo or (), support=a.support or (), status=a.status))


def cmd_add(a):
    emit(add_entry(a.dir, a.kind, a.text, done=a.done))


def cmd_build(a):
    result = build(a.dir, a.git_repo or (), a.out, _engine(a.engine))
    if a.json:
        emit(result)
    else:
        print(result['markdown'], end='')


def cmd_show(a):
    emit(show(a.dir))


def cmd_handoff(a):
    result = handoff(a.dir, a.out, _engine(a.engine))
    if a.json:
        emit(result)
    else:
        print(result['markdown'], end='')


def main(argv=None):
    p = argparse.ArgumentParser(
        description='任务简报 / 交接单：维护 brief.json，读取 git 现状生成 Markdown')
    sub = p.add_subparsers(dest='cmd', required=True)

    s = sub.add_parser('init', help='建 brief.json（已存在时只补缺失字段）')
    s.add_argument('--dir', required=True, help='任务目录')
    s.add_argument('--title', default='', help='任务标题')
    s.add_argument('--background', default='', help='项目背景')
    s.add_argument('--goal', default='', help='目标')
    s.add_argument('--status', default='', help='当前状态（自由文本）')
    s.add_argument('--acceptance', action='append', default=[], help='验收条件，可重复')
    s.add_argument('--constraint', action='append', default=[], help='约束，可重复')
    s.add_argument('--repo', action='append', default=[], help='相关仓库路径，可重复')
    s.add_argument('--support', action='append', default=[], help='需要谁配合，可重复')
    s.add_argument('--json', action='store_true', help='输出 JSON（默认就是 JSON）')
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser('add', help='追加一条记录')
    s.add_argument('--dir', required=True, help='任务目录')
    s.add_argument('--kind', required=True, choices=list(KINDS), help='记录类型')
    s.add_argument('--text', required=True, help='内容')
    s.add_argument('--done', action='store_true', help='todo 用：标记为已完成')
    s.add_argument('--json', action='store_true', help='输出 JSON（默认就是 JSON）')
    s.set_defaults(fn=cmd_add)

    s = sub.add_parser('build', help='生成 Markdown 简报')
    s.add_argument('--dir', required=True, help='任务目录')
    s.add_argument('--git-repo', action='append', default=[],
                   help='读哪个仓库的 git 现状，可重复；省略时用 brief.json 里的 repos')
    s.add_argument('--out', default='', help='把 Markdown 写到这个文件')
    s.add_argument('--engine', default='',
                   help='整理用的模型（claude/codex/deepseek/kimi）；不给就只出规则版')
    s.add_argument('--json', action='store_true', help='输出 JSON（含 markdown 和 git 状态）')
    s.set_defaults(fn=cmd_build)

    s = sub.add_parser('show', help='看 brief.json 和简报正文')
    s.add_argument('--dir', required=True, help='任务目录')
    s.add_argument('--json', action='store_true', help='输出 JSON（默认就是 JSON）')
    s.set_defaults(fn=cmd_show)

    s = sub.add_parser('handoff', help='生成交接单')
    s.add_argument('--dir', required=True, help='任务目录')
    s.add_argument('--out', required=True, help='交接单写到这个文件')
    s.add_argument('--engine', default='',
                   help='整理用的模型（claude/codex/deepseek/kimi）；不给就只出规则版')
    s.add_argument('--json', action='store_true', help='输出 JSON（含 markdown）')
    s.set_defaults(fn=cmd_handoff)

    a = p.parse_args(argv)
    try:
        a.fn(a)
    except (agent.AgentError, ValueError, OSError, RuntimeError) as e:
        print(json.dumps({'error': str(e)}, ensure_ascii=False), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
