#!/usr/bin/env python3
"""需求阶段看板：建看板、查状态、按顺序推进、跳过阶段、登记变更、看时间线、导出 markdown。

状态只存在目标目录的 workflow.json 里，不依赖任何外部系统。所有子命令输出 JSON。
"""
import argparse
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / '_lib'))

from wbkit import config as cfg  # noqa: E402

FILE = 'workflow.json'
DEFAULT_STAGES = ['需求分析', '方案设计', '开发', '自测', '联调', '测试', '上线']
STATUSES = ['未开始', '进行中', '已确认', '已跳过']
DONE = ('已确认', '已跳过')


class WorkflowError(Exception):
    """看板数据或操作不合法。命令行会把消息输出成 {"error": ...} 并返回 2。"""


def now():
    return datetime.now().astimezone().isoformat(timespec='seconds')


def emit(data):
    print(json.dumps(data, ensure_ascii=False, indent=2))


def path_of(directory):
    return Path(directory) / FILE


def parse_stages(text):
    """把逗号分隔的阶段名拆成列表，中英文逗号都认。"""
    names = [x.strip() for x in (text or '').replace('，', ',').split(',') if x.strip()]
    if not names:
        raise WorkflowError('阶段列表不能为空')
    dup = sorted({x for x in names if names.count(x) > 1})
    if dup:
        raise WorkflowError('阶段名重复：%s' % '、'.join(dup))
    return names


def default_stages():
    """优先用配置里的 stages，其次用内置七阶段。"""
    configured = cfg.get('stages')
    if not configured:
        return list(DEFAULT_STAGES)
    if isinstance(configured, (list, tuple)):
        return parse_stages(','.join(str(x) for x in configured))
    return parse_stages(str(configured))


def validate(data):
    """检查 workflow.json 的结构和状态枚举，返回问题列表。"""
    if not isinstance(data, dict):
        return ['workflow.json 顶层必须是对象']
    problems = []
    stages = data.get('stages')
    if not isinstance(stages, dict) or not stages:
        return ['workflow.json 缺少非空的 stages 对象']
    for name, status in stages.items():
        if status not in STATUSES:
            problems.append('阶段 %s 的状态 %r 不在 %s 中' % (name, status, '/'.join(STATUSES)))
    for key in ('history', 'changes'):
        if not isinstance(data.get(key, []), list):
            problems.append('%s 必须是数组' % key)
    if not isinstance(data.get('title', ''), str):
        problems.append('title 必须是字符串')
    return problems


def load(directory):
    """读 workflow.json；文件不存在、JSON 坏了、结构不对都抛 WorkflowError。"""
    p = path_of(directory)
    if not p.exists():
        raise WorkflowError('%s 不存在，先跑 init' % p)
    try:
        data = json.loads(p.read_text(encoding='utf-8'))
    except ValueError as e:
        raise WorkflowError('%s 不是合法 JSON：%s' % (p, e))
    except OSError as e:
        raise WorkflowError('%s 读不了：%s' % (p, e))
    problems = validate(data)
    if problems:
        raise WorkflowError('；'.join(problems))
    return data


def _atomic_write(path, text):
    """先写同目录临时文件再改名，避免写一半留下坏文件。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix='.%s-' % path.stem)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def save(directory, data):
    _atomic_write(path_of(directory), json.dumps(data, ensure_ascii=False, indent=2) + '\n')


def current_stage(data):
    """第一个还没结束的阶段；全都结束了返回 None。"""
    for name, status in data['stages'].items():
        if status not in DONE:
            return name
    return None


def progress(data):
    """进度百分比：已确认 / 已跳过算 1，进行中算 0.5，未开始算 0。"""
    stages = list(data['stages'].values())
    if not stages:
        return 0
    score = sum(1 if s in DONE else 0.5 if s == '进行中' else 0 for s in stages)
    return int(round(score / len(stages) * 100))


def counts(data):
    out = {'total': len(data['stages']), 'done': 0, 'skipped': 0, 'in_progress': 0, 'pending': 0}
    for status in data['stages'].values():
        if status == '已确认':
            out['done'] += 1
        elif status == '已跳过':
            out['skipped'] += 1
        elif status == '进行中':
            out['in_progress'] += 1
        else:
            out['pending'] += 1
    return out


def next_step(data, directory):
    """下一步该干什么，顺带给一条可以直接复制的命令。"""
    name = current_stage(data)
    if name is None:
        return {'stage': None, 'action': '全部阶段已结束', 'command': None}
    if data['stages'][name] == '进行中':
        return {'stage': name, 'action': '确认完成',
                'command': 'python3 scripts/stage_workflow.py advance --dir %s --stage %s --to 已确认'
                           % (directory, name)}
    return {'stage': name, 'action': '开始',
            'command': 'python3 scripts/stage_workflow.py advance --dir %s --stage %s'
                       % (directory, name)}


def describe(data, directory):
    """status 类命令的统一输出。"""
    changes = data.get('changes') or []
    return {
        'file': str(path_of(directory).resolve()),
        'title': data.get('title', ''),
        'current': current_stage(data),
        'progress': progress(data),
        'counts': counts(data),
        'stages': [{'name': k, 'status': v} for k, v in data['stages'].items()],
        'next': next_step(data, directory),
        'changes': {'total': len(changes),
                    'open': sum(1 for c in changes if c.get('status') != '已处理')},
    }


def append_history(data, entry):
    data.setdefault('history', []).append(dict({'at': now()}, **entry))


def _start_next(data, stage):
    """当前阶段结束后，把后面第一个未开始的阶段置为进行中；返回阶段名。"""
    names = list(data['stages'])
    for name in names[names.index(stage) + 1:]:
        if data['stages'][name] == '未开始':
            data['stages'][name] = '进行中'
            return name
    return None


def cmd_init(a):
    p = path_of(a.dir)
    if p.exists():
        raise WorkflowError('%s 已存在，不覆盖' % p)
    names = parse_stages(a.stages) if a.stages else default_stages()
    data = {
        'title': a.title,
        'stages': {name: '未开始' for name in names},
        'history': [],
        'changes': [],
    }
    data['history'].append({'at': now(), 'action': 'init', 'stage': None, 'from': None, 'to': None,
                            'note': '建立阶段看板：%s' % '、'.join(names)})
    save(a.dir, data)
    emit({'file': str(p.resolve()), 'title': a.title, 'stages': names,
          'current': names[0], 'progress': 0})


def cmd_status(a):
    emit(describe(load(a.dir), a.dir))


def cmd_advance(a):
    data = load(a.dir)
    names = list(data['stages'])
    stage = a.stage or current_stage(data)
    if stage is None:
        raise WorkflowError('所有阶段都已结束，没有可推进的阶段')
    if stage not in data['stages']:
        raise WorkflowError('未知阶段 %s（可用：%s）' % (stage, '、'.join(names)))
    if a.to and a.to not in ('进行中', '已确认'):
        raise WorkflowError('advance 只能改成 进行中 或 已确认；跳过阶段用 skip')
    old = data['stages'][stage]
    if old in DONE:
        raise WorkflowError('%s 已经结束（%s），没有可推进的' % (stage, old))
    to = a.to or ('进行中' if old == '未开始' else '已确认')
    if to == old:
        raise WorkflowError('%s 已经是“%s”，没有变化' % (stage, old))

    active = current_stage(data)
    if stage != active and not a.force:
        raise WorkflowError('只能推进当前阶段“%s”，%s 还在后面；要跳过去加 --force' % (active, stage))
    if to == '已确认' and not a.force:
        blocked = [(n, data['stages'][n]) for n in names[:names.index(stage)]
                   if data['stages'][n] not in DONE]
        if blocked:
            raise WorkflowError('不能把 %s 推进到已确认：前面还有 %s 没结束；确认要跳过加 --force' % (
                stage, '、'.join('%s【%s】' % (n, s) for n, s in blocked)))

    data['stages'][stage] = to
    # 只有前面的阶段都结束时，确认后才能安全地把下一阶段置为进行中（--force 跳级时不自动开始）
    ready = all(data['stages'][n] in DONE for n in names[:names.index(stage)])
    started = _start_next(data, stage) if (to == '已确认' and ready) else None
    entry = {'at': now(), 'action': 'stage', 'stage': stage,
             'from': old, 'to': to, 'note': a.note or ''}
    append_history(data, entry)
    save(a.dir, data)
    out = describe(data, a.dir)
    out.update({'stage': stage, 'from': old, 'to': to, 'started': started, 'history_entry': entry})
    emit(out)


def cmd_skip(a):
    data = load(a.dir)
    names = list(data['stages'])
    stage = a.stage
    if stage not in data['stages']:
        raise WorkflowError('未知阶段 %s（可用：%s）' % (stage, '、'.join(names)))
    reason = (a.reason or '').strip()
    if not reason:
        raise WorkflowError('跳过阶段要写 --reason')
    old = data['stages'][stage]
    if old in DONE:
        raise WorkflowError('%s 已经结束（%s）' % (stage, old))
    active = current_stage(data)
    if stage != active:
        raise WorkflowError('只能跳过当前阶段“%s”；%s 还没轮到' % (active, stage))

    data['stages'][stage] = '已跳过'
    started = _start_next(data, stage)
    entry = {'at': now(), 'action': 'skip', 'stage': stage,
             'from': old, 'to': '已跳过', 'note': reason}
    append_history(data, entry)
    save(a.dir, data)
    out = describe(data, a.dir)
    out.update({'stage': stage, 'from': old, 'to': '已跳过', 'started': started, 'history_entry': entry})
    emit(out)


def _next_change_id(changes):
    nums = [int(str(c.get('id'))[3:]) for c in changes
            if str(c.get('id') or '').startswith('CH-') and str(c.get('id'))[3:].isdigit()]
    return 'CH-%02d' % (max(nums) + 1 if nums else 1)


def cmd_change_add(a):
    data = load(a.dir)
    title = (a.title or '').strip()
    if not title:
        raise WorkflowError('变更标题不能为空')
    changes = data.setdefault('changes', [])
    item = {'id': _next_change_id(changes), 'title': title, 'status': '待分析', 'at': now()}
    changes.append(item)
    append_history(data, {'at': item['at'], 'action': 'change', 'stage': None, 'from': None,
                          'to': '待分析', 'note': '%s 登记变更：%s' % (item['id'], title)})
    save(a.dir, data)
    emit({'file': str(path_of(a.dir).resolve()), 'change_id': item['id'], 'change': item})


def cmd_change_done(a):
    data = load(a.dir)
    cid = (a.id or '').strip()
    item = next((c for c in data.get('changes') or [] if c.get('id') == cid), None)
    if item is None:
        raise WorkflowError('没有编号为 %s 的变更' % cid)
    if item.get('status') == '已处理':
        raise WorkflowError('%s 已经处理过了' % cid)
    summary = (a.summary or '').strip()
    item.update(status='已处理', summary=summary, done_at=now())
    append_history(data, {'at': item['done_at'], 'action': 'change', 'stage': None, 'from': '待分析',
                          'to': '已处理',
                          'note': '%s 处理完成：%s' % (cid, summary or item.get('title', ''))})
    save(a.dir, data)
    emit({'file': str(path_of(a.dir).resolve()), 'change_id': cid, 'change': item})


def cmd_history(a):
    data = load(a.dir)
    items = list(data.get('history') or [])
    limit = max(0, a.limit)
    tail = items[-limit:] if limit else []
    emit({'file': str(path_of(a.dir).resolve()), 'total': len(items),
          'count': len(tail), 'history': tail})


def _history_line(entry):
    at = str(entry.get('at') or '')
    note = (entry.get('note') or '').strip()
    if entry.get('action') == 'change':
        return '- %s %s' % (at, note)
    stage = entry.get('stage')
    if stage:
        text = '阶段 %s：%s → %s' % (stage, entry.get('from') or '未开始', entry.get('to') or '')
        if note:
            text += '（%s）' % note
        return '- %s %s' % (at, text)
    return '- %s %s' % (at, note)


def render_markdown(data):
    """把看板渲染成 markdown。"""
    name = current_stage(data)
    c = counts(data)
    lines = [
        '# %s' % (data.get('title') or '阶段看板'),
        '',
        '- 当前阶段：%s' % (name or '全部结束'),
        '- 进度：%d%%（已确认 %d / 已跳过 %d / 进行中 %d / 未开始 %d，共 %d）'
        % (progress(data), c['done'], c['skipped'], c['in_progress'], c['pending'], c['total']),
        '',
        '## 阶段',
        '',
        '| 阶段 | 状态 |',
        '|---|---|',
    ]
    for stage, status in data['stages'].items():
        lines.append('| %s | %s |' % (stage, status))

    changes = data.get('changes') or []
    lines += ['', '## 需求变更', '']
    if changes:
        lines += ['| 编号 | 变更 | 状态 |', '|---|---|---|']
        for item in changes:
            lines.append('| %s | %s | %s |' % (item.get('id'), item.get('title'), item.get('status')))
    else:
        lines.append('暂无变更。')

    history = (data.get('history') or [])[-10:]
    lines += ['', '## 最近动态', '']
    lines += [_history_line(h) for h in history] if history else ['暂无记录。']
    return '\n'.join(lines) + '\n'


def cmd_export(a):
    data = load(a.dir)
    text = render_markdown(data)
    out = Path(a.out).expanduser()
    _atomic_write(out, text)
    emit({'out': str(out.resolve()), 'bytes': out.stat().st_size, 'markdown': text})


def main(argv=None):
    p = argparse.ArgumentParser(description='需求阶段看板：建、查、推进、跳过、登记变更、时间线、导出（JSON 输出）')
    sub = p.add_subparsers(dest='cmd', required=True)

    s = sub.add_parser('init', help='在目录里建 workflow.json')
    s.add_argument('--dir', required=True)
    s.add_argument('--title', required=True)
    s.add_argument('--stages', default='', help='逗号分隔的阶段顺序；不填用配置 stages 或内置七阶段')
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser('status', help='当前阶段、各阶段状态、进度、下一步')
    s.add_argument('--dir', required=True)
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser('advance', help='按顺序推进一格（--force 可跳）')
    s.add_argument('--dir', required=True)
    s.add_argument('--stage', default='', help='要推进的阶段，默认取当前阶段')
    s.add_argument('--to', default='', help='进行中 / 已确认，默认按当前状态往下一格')
    s.add_argument('--note', default='')
    s.add_argument('--force', action='store_true', help='允许跳级或越过未结束的阶段')
    s.set_defaults(fn=cmd_advance)

    s = sub.add_parser('skip', help='跳过当前阶段')
    s.add_argument('--dir', required=True)
    s.add_argument('--stage', required=True)
    s.add_argument('--reason', required=True)
    s.set_defaults(fn=cmd_skip)

    s = sub.add_parser('change', help='需求变更登记')
    ch = s.add_subparsers(dest='change_cmd', required=True)
    add = ch.add_parser('add', help='登记一条变更')
    add.add_argument('--dir', required=True)
    add.add_argument('--title', required=True)
    add.set_defaults(fn=cmd_change_add)
    done = ch.add_parser('done', help='标记一条变更已处理')
    done.add_argument('--dir', required=True)
    done.add_argument('--id', required=True)
    done.add_argument('--summary', default='')
    done.set_defaults(fn=cmd_change_done)

    s = sub.add_parser('history', help='时间线（默认最近 50 条）')
    s.add_argument('--dir', required=True)
    s.add_argument('--limit', type=int, default=50)
    s.set_defaults(fn=cmd_history)

    s = sub.add_parser('export', help='导出 markdown 看板')
    s.add_argument('--dir', required=True)
    s.add_argument('--out', required=True)
    s.set_defaults(fn=cmd_export)

    a = p.parse_args(argv)
    try:
        a.fn(a)
    except (WorkflowError, OSError) as e:
        print(json.dumps({'error': str(e)}, ensure_ascii=False), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
