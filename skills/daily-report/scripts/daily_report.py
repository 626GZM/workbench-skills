#!/usr/bin/env python3
"""日报生成：把某一天的工作记录汇总成可以直接复制给领导的日报。

输入是一个“工作日历目录”（结构见 SKILL.md）：
    workspace/<需求目录>/meta.json     需求标题、当前阶段、功能模块、阶段进度
    workspace/<需求目录>/timeline.jsonl 每行一条工作记录（at / note / ok / role / engine）

子命令：
    build  明细（detail）或简版（brief）
    revise 按一句话改已有日报
    rules  固定要求（“以后都这样”）的增删查

所有子命令输出 JSON；参数 / 输入错误退出码 2。
"""
import argparse
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / '_lib'))

from wbkit import agent, config as cfg, git  # noqa: E402

ENGINES = ('claude', 'codex', 'deepseek', 'kimi')
DEF_RULES = str(cfg.CONFIG_HOME / 'daily-rules.json')

# 对外口径：工程词换成人话（日报是给公司看的，不出现 e2e / dev / test / 联调环境 / 冒烟）
E2E = re.compile(r'(本地|dev|test|uat|预发|联调环境|测试环境)?\s*e2e\s*(（核心）)?', re.I)
E2E_PLAIN = {'本地': '本地自测', 'dev': '与前端联调', '联调环境': '与前端联调',
             'test': '验证通过', '测试环境': '验证通过', 'uat': '验证通过', '预发': '验证通过'}
# 计数类记账（“讨论 3 个问题”“评审 2 轮”）不写进日报
COUNTING = re.compile(r'(讨论|评审|沟通|确认)[^。；\n]{0,8}\d+|\d+\s*轮')
# 等待、卡住、失败重试、延期、返工不写
NEGATIVE = re.compile(r'等待|卡住|失败|重试|延期|返工|阻塞')
# 不提 AI、模型、工具、自动化
AI_WORD = re.compile(r'\bAI\b|大模型|模型|自动化|工作台')

SAMPLE = '''1. [REQ-101 订单导出优化] 导出任务：完成导出任务列表、导出任务详情两个接口，进度：已完成
2. [REQ-101 订单导出优化] 模板配置：完成模板字段校验和保存逻辑，进度：进行中 80%'''


# ---------- 文本口径 ----------

def _plain(text):
    """内部记录 → 对外说法：工程词换人话，去掉 AI / 模型这类字样。"""
    s = E2E.sub(lambda m: E2E_PLAIN.get((m.group(1) or '').lower(), '验证通过'), str(text or ''))
    s = re.sub(r'联调环境', '与前端联调', s)
    s = re.sub(r'测试环境|预发环境', '验证通过', s)
    s = re.sub(r'\bdev\b', '与前端联调', s, flags=re.I)
    s = re.sub(r'\b(?:test|uat)\b', '验证通过', s, flags=re.I)
    s = re.sub(r'冒烟', '验证通过', s)
    s = AI_WORD.sub('', s)
    s = re.sub(r'\s{2,}', ' ', s)
    return s.strip(' ，,；;。')


def _usable(note):
    """一条记录能不能写进日报：非空、不是计数、不涉及等待 / 失败 / 返工。"""
    text = _plain(note)
    if not text or COUNTING.search(text) or NEGATIVE.search(text):
        return ''
    return text


def _pct(value):
    """对外进度只出 已完成 / 70 / 80 / 90 三档。"""
    try:
        pct = int(float(value))
    except (TypeError, ValueError):
        pct = 70
    if pct >= 100:
        return '已完成'
    return '进行中 %d%%' % (90 if pct >= 90 else 80 if pct >= 70 else 70)


def _day(at):
    try:
        return datetime.fromisoformat(str(at)).astimezone().strftime('%Y-%m-%d')
    except (TypeError, ValueError):
        return str(at or '')[:10]


def _tag(meta, name):
    """「【REQ-101】订单导出优化」→「REQ-101 订单导出优化」。"""
    title = str(meta.get('title') or name)
    m = re.match(r'\s*[【\[]([^】\]]+)[】\]]\s*(.*)', title)
    if m:
        return ('%s %s' % (m.group(1), m.group(2))).strip()
    code = str(meta.get('code') or '').strip()
    return ('%s %s' % (code, title)).strip() if code else title


# ---------- 读工作区 ----------

def _read_json(path):
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _timeline(path):
    try:
        text = Path(path).read_text(encoding='utf-8', errors='replace')
    except OSError:
        return []
    rows = []
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def load_tasks(workspace):
    """读工作区里的需求目录。没有 meta.json 也没有 timeline.jsonl 的目录跳过。"""
    root = Path(workspace).expanduser()
    tasks = []
    for d in sorted(root.iterdir()):
        if not d.is_dir() or d.name.startswith('.'):
            continue
        meta = _read_json(d / 'meta.json')
        if not meta and not (d / 'timeline.jsonl').is_file():
            continue
        tasks.append({'dir': d, 'meta': meta, 'name': d.name,
                      'tag': _tag(meta, d.name), 'rows': _timeline(d / 'timeline.jsonl')})
    return tasks


def today_notes(task, day):
    """这一天的记录（去掉失败 / 重试的、计数类的），按出现顺序去重。"""
    out, seen = [], set()
    for row in task['rows']:
        if _day(row.get('at')) != day:
            continue
        if row.get('ok') is False:            # 失败、重试的记录不写进日报
            continue
        text = _usable(row.get('note'))
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _progress(meta):
    """(当前阶段, 对外进度文案)。没填 progress 时进度文案为空。"""
    stage = str(meta.get('stage') or '').strip()
    prog = meta.get('progress') if isinstance(meta.get('progress'), dict) else {}
    if not stage and prog:
        stage = next(iter(prog))
    if stage in prog:
        return stage, _pct(prog[stage])
    if prog:
        return stage, _pct(next(iter(prog.values())))
    return stage, ''


def _plan(meta):
    """明日计划：有 plan 字段就照抄；否则按 progress 里没到 100% 的阶段推。"""
    plan = meta.get('plan')
    if isinstance(plan, list) and plan:
        return [str(x).strip() for x in plan if str(x).strip()]
    prog = meta.get('progress') if isinstance(meta.get('progress'), dict) else {}
    for stage, value in prog.items():
        try:
            pct = int(float(value))
        except (TypeError, ValueError):
            pct = 0
        if pct < 100:
            return ['继续%s' % stage]
    if prog:
        return ['需求收尾']
    stage = str(meta.get('stage') or '').strip()
    return ['继续%s' % stage] if stage else []


def day_items(tasks, day):
    """今天有记录的需求（没记录的不进日报，不编“按计划推进”）。"""
    items = []
    for task in tasks:
        notes = today_notes(task, day)
        if not notes:
            continue
        meta = task['meta']
        stage, ptext = _progress(meta)
        modules = [str(m).strip() for m in meta.get('modules') or [] if str(m).strip()]
        items.append({'tag': task['tag'], 'notes': notes, 'modules': modules,
                      'stage': stage, 'pct': ptext, 'plan': _plan(meta)})
    return items


def git_subjects(repo, day, branch=''):
    """仓库这一天的提交标题（分支没建、仓库不在、git 不可用都返回空）。"""
    try:
        start = datetime.strptime(day, '%Y-%m-%d')
        ref = 'HEAD' if not branch else 'refs/heads/' + git.ref(branch)
    except (TypeError, ValueError, git.GitError):
        return []
    since = start.strftime('%Y-%m-%d 00:00:00')
    until = (start + timedelta(days=1)).strftime('%Y-%m-%d 00:00:00')
    try:
        out = git.git(str(Path(str(repo)).expanduser()), 'log', ref, '--no-merges',
                      '--since', since, '--until', until, '--format=%s', check=False)
    except (git.GitError, OSError):
        return []
    return [l.strip() for l in out.splitlines() if l.strip()]


# ---------- 明细日报 ----------

def build_detail(workspace, day, git_repo='', git_branch=''):
    """按需求分组：当前进度 + 今天做了什么 + 明日计划。纯规则拼接，不调模型。"""
    sections, plans = [], []
    for item in day_items(load_tasks(workspace), day):
        work = []
        if item['stage'] and item['pct']:
            work.append('当前进度：%s %s' % (item['stage'], item['pct']))
        work += item['notes']
        sections.append((item['tag'], work))
        if item['plan']:
            plans.append((item['tag'], item['plan']))
    if git_repo:
        subjects = git_subjects(git_repo, day, git_branch)
        if subjects:
            sections.append(('代码提交', ['代码提交：' + '；'.join(subjects[:5])]))

    lines = ['# 日报 %s' % day, '', '## 今日工作']
    if not sections:
        lines.append('- （这一天没有记录到进展）')
    for title, work in sections:
        lines += ['', '**%s**' % title] + ['- ' + w for w in work]
    lines += ['', '## 明日计划']
    if not plans:
        lines.append('- （暂无）')
    for title, todo in plans:
        lines += ['', '**%s**' % title] + ['- ' + t for t in todo]
    return {'date': day, 'source': 'rule' if sections else 'none',
            'markdown': '\n'.join(lines) + '\n', 'out': None}


# ---------- 简版日报 ----------

def brief_candidates(items):
    """规则版每条：[需求] 模块：做成什么，进度：x。按需求分组，条数轮流分。"""
    groups = []
    for item in items:
        lines = []
        for note in item['notes']:
            if item['modules']:
                module = item['modules'][len(lines) % len(item['modules'])]
            else:
                module = item['stage'] or '进展'
            desc = note
            for sep in ('：', ':'):
                if desc.startswith(module + sep):
                    desc = desc[len(module) + 1:].strip()
                    break
            line = '[%s] %s：%s，进度：%s' % (item['tag'], module, desc,
                                            item['pct'] or '进行中 80%')
            if line not in lines:
                lines.append(line)
        groups.append(lines)
    return groups


def _take(groups, n):
    """在需求之间轮流取，最多 n 条；不够就按实际条数，绝不编造。"""
    lines = []
    for i in range(max([len(g) for g in groups] or [0])):
        for group in groups:
            if len(lines) >= n:
                return lines
            if i < len(group):
                lines.append(group[i])
    return lines


def brief_rule(items, n):
    lines = _take(brief_candidates(items), n)
    return ['%d. %s' % (i, line) for i, line in enumerate(lines, 1)]


def _facts(items):
    blocks = []
    for item in items:
        cur = ('%s %s' % (item['stage'], item['pct'])).strip() or '（没有填）'
        blocks.append('需求：[%s]\n功能模块：%s\n当前进度：%s\n今天的记录：\n%s\n接下来：%s' % (
            item['tag'],
            '、'.join(item['modules']) or '（没有功能模块清单，按工作环节写）',
            cur,
            '\n'.join('- ' + x for x in item['notes']),
            '；'.join(item['plan']) or '无'))
    return '\n\n'.join(blocks)


def brief_prompt(items, n, fixed=()):
    """简版日报的提示词：格式、口径、禁止事项、我的固定要求。"""
    return '\n'.join([
        '帮一名开发写今天的工作日报，**正好 %d 条**，仿照下面的样例：' % n, SAMPLE, '',
        '要求：',
        '- 格式：`序号. [需求编号 需求名] 模块名：做成了什么，进度：已完成` 或 `…，进度：进行中 70% / 80% / 90%`。',
        '- 每条都写“做成了什么”，带范围或数量（完成哪几个接口、覆盖哪几条链路），不要只写“开发中”“联调中”“需求澄清”这种环节名。',
        '- 不写“讨论 N 个问题”“评审 N 轮”“沟通 N 次”“确认了几件事”这类计数。',
        '- 不出现 e2e、dev、test、联调环境、冒烟这类工程词：跟前端对接口说“与前端联调”，本地跑通说“本地自测”，环境里跑通说“验证通过”。',
        '- 不写等待、卡住、失败重试、延期、返工；还在做的只写推进方向和进展。',
        '- 进度只写 已完成 / 进行中 70% / 80% / 90%，不写别的数字。',
        '- 当天做了什么只能来自“今天的记录”，不编造；多个需求时按工作量分配条数。',
        '- 每条控制在 60 字以内，不提 AI、模型、工具、自动化、工作台。',
        *('- 我的固定要求：' + r for r in fixed),
        '', '今天的记录：', _facts(items), '', '只输出这 %d 行，不要解释，不要代码块。' % n,
    ])


def brief_ai(items, n, fixed, engine, cwd):
    """让模型按样例仿写；条数不对、调用失败都抛异常，由调用方回退规则版。"""
    result = agent.run(engine, brief_prompt(items, n, fixed), str(cwd), profile='docs')
    if not result.get('ok'):
        raise RuntimeError(result.get('error') or '模型调用失败')
    lines = []
    for raw in str(result.get('text') or '').splitlines():
        if re.match(r'\s*\d+[.、]', raw):
            line = _plain(re.sub(r'^\s*\d+\s*[.、]\s*', '', raw).strip())
            if line:
                lines.append(line)
    if len(lines) != n:
        raise RuntimeError('模型返回了 %d 条，不是 %d 条' % (len(lines), n))
    return ['%d. %s' % (i, line) for i, line in enumerate(lines, 1)]


def build_brief(workspace, day, n=6, engine='kimi', rules_file='', git_repo='', git_branch=''):
    """简版：正好 n 条；模型可用走 AI，失败回退规则版；没记录就不编。"""
    items = day_items(load_tasks(workspace), day)
    if git_repo:
        subjects = git_subjects(git_repo, day, git_branch)
        if subjects:
            items.append({'tag': '代码提交', 'notes': ['代码提交：' + '；'.join(subjects[:5])],
                          'modules': [], 'stage': '', 'pct': '已完成', 'plan': []})
    if not items:
        return {'date': day, 'source': 'none', 'lines': [], 'out': None,
                'message': '这一天没有记录到进展'}

    fixed = read_rules(rules_file)
    try:
        lines = brief_ai(items, n, fixed, engine, workspace)
        source = 'ai'
    except Exception:                    # 没装 CLI、没 key、限流、条数不对，一律回退规则版
        lines = brief_rule(items, n)
        source = 'rule'
    return {'date': day, 'source': source, 'lines': lines, 'out': None}


# ---------- 一句话改稿 ----------

def revise_text(text, ask, fixed, engine, cwd):
    """按一句话改已有日报，只改说到的地方。"""
    if not str(text or '').strip():
        raise ValueError('日报是空的，先生成一版再改')
    if not str(ask or '').strip():
        raise ValueError('写一句要怎么改')
    prompt = '\n'.join([
        '下面是我（一名开发）今天的日报草稿，按我的要求改。',
        '- 只改要求说到的地方，其余内容、条数、序号、格式（包括 # 和 ** 这些符号）都保持原样。',
        '- 不编造草稿里没有的工作；不写计数、等待、卡住、失败重试、延期、返工。',
        '- 不出现 e2e、dev、test、联调环境、冒烟这些工程词；进度只写 已完成 / 进行中 70% / 80% / 90%。',
        '- 不提 AI、模型、工具、自动化、工作台。',
        *('- 也要遵守我的固定要求：' + r for r in fixed if r != str(ask).strip()),
        '', '我的要求：' + str(ask).strip(), '', '草稿：', '<<<', str(text).strip(), '>>>', '',
        '只输出改好的日报全文，不要解释，不要加 <<< >>> 或代码块。',
    ])
    result = agent.run(engine, prompt, str(cwd), profile='docs')
    if not result.get('ok'):
        raise RuntimeError(result.get('error') or '模型调用失败')
    out = re.sub(r'^```\w*\n|\n```$', '', str(result.get('text') or '').strip()).strip()
    out = out.strip('<>').strip()
    if not out:
        raise RuntimeError('模型没返回内容，再试一次')
    return out


# ---------- 固定要求 ----------

def rules_path(value=''):
    return Path(str(value or DEF_RULES)).expanduser()


def read_rules(path=''):
    try:
        data = json.loads(rules_path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return []
    return [str(x) for x in data if str(x).strip()] if isinstance(data, list) else []


def save_rules(items, path=''):
    p = rules_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding='utf-8')
    return items


def add_rule(text, path=''):
    text = re.sub(r'\s+', ' ', str(text or '')).strip()[:300]
    if not text:
        raise ValueError('rules add 要 --text "固定要求"')
    items = read_rules(path)
    if text not in items:
        items.append(text)
        save_rules(items, path)
    return items


def drop_rule(index, path=''):
    if index is None:
        raise ValueError('rules drop 要 --index 序号（从 0 开始）')
    items = read_rules(path)
    if not 0 <= index < len(items):
        raise ValueError('没有第 %d 条固定要求（共 %d 条）' % (index, len(items)))
    items.pop(index)
    save_rules(items, path)
    return items


# ---------- CLI ----------

def emit(data):
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _check_date(day):
    try:
        datetime.strptime(day, '%Y-%m-%d')
    except (TypeError, ValueError):
        raise ValueError('--date 要 YYYY-MM-DD：%r' % day)


def cmd_build(a):
    ws = Path(a.workspace).expanduser()
    if not ws.is_dir():
        raise ValueError('工作区目录不存在：%s' % a.workspace)
    _check_date(a.date)
    if a.n < 1:
        raise ValueError('--n 至少 1')
    if a.style == 'brief':
        out = build_brief(ws, a.date, a.n, a.engine, a.rules, a.git_repo, a.git_branch)
        text = '\n'.join(out['lines']) + ('\n' if out['lines'] else '')
    else:
        out = build_detail(ws, a.date, a.git_repo, a.git_branch)
        text = out['markdown']
    if a.out:
        p = Path(a.out).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding='utf-8')
        out['out'] = str(p.resolve())
    emit(out)


def cmd_revise(a):
    p = Path(a.text).expanduser()
    if not str(a.text or '').strip() or not p.is_file():
        raise ValueError('日报文件不存在：%s' % a.text)
    text = p.read_text(encoding='utf-8')
    out = revise_text(text, a.ask, read_rules(a.rules), a.engine, p.resolve().parent)
    emit({'source': 'ai', 'text': out})


def cmd_rules(a):
    if a.action == 'add':
        items = add_rule(a.text, a.rules)
    elif a.action == 'drop':
        items = drop_rule(a.index, a.rules)
    else:
        items = read_rules(a.rules)
    emit({'path': str(rules_path(a.rules)), 'rules': items})


def main(argv=None):
    p = argparse.ArgumentParser(description='日报生成：明细 / 简版 / 一句话改稿 / 固定要求，输出 JSON')
    sub = p.add_subparsers(dest='cmd', required=True)

    s = sub.add_parser('build', help='生成日报：明细（detail）或简版（brief）')
    s.add_argument('--workspace', required=True, help='工作日历目录（每个需求一个子目录）')
    s.add_argument('--date', required=True, help='哪一天，YYYY-MM-DD')
    s.add_argument('--out', default='', help='把结果另存到这个文件')
    s.add_argument('--style', choices=['detail', 'brief'], default='detail',
                   help='detail 明细（默认）/ brief 简版')
    s.add_argument('--n', type=int, default=6, help='简版条数，默认 6')
    s.add_argument('--engine', choices=ENGINES, default='kimi', help='简版仿写用的模型，默认 kimi')
    s.add_argument('--rules', default='', help='固定要求文件，默认 ~/.config/workbench-skills/daily-rules.json')
    s.add_argument('--git-repo', default='', help='可选：统计当天提交的仓库')
    s.add_argument('--git-branch', default='', help='可选：看哪个分支的提交，默认 HEAD')
    s.set_defaults(fn=cmd_build)

    s = sub.add_parser('revise', help='按一句话改已有日报，只改说到的地方')
    s.add_argument('--text', required=True, help='日报文本文件')
    s.add_argument('--ask', required=True, help='一句话要求，如“把第 2 条写得更具体”')
    s.add_argument('--engine', choices=ENGINES, default='kimi')
    s.add_argument('--rules', default='')
    s.set_defaults(fn=cmd_revise)

    s = sub.add_parser('rules', help='固定要求管理（“以后都这样”）')
    s.add_argument('action', choices=['add', 'list', 'drop'])
    s.add_argument('--text', default='', help='add 时：固定要求内容')
    s.add_argument('--index', type=int, default=None, help='drop 时：序号（从 0 开始）')
    s.add_argument('--rules', default='', help='固定要求文件，默认 ~/.config/workbench-skills/daily-rules.json')
    s.set_defaults(fn=cmd_rules)

    a = p.parse_args(argv)
    try:
        a.fn(a)
    except (agent.AgentError, ValueError, OSError, RuntimeError) as e:
        print(json.dumps({'error': str(e)}, ensure_ascii=False), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
