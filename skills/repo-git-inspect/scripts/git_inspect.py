#!/usr/bin/env python3
"""git 只读查询：分支、差异、提交、merge-base、拉取。不改工作区、不推送。

输出 JSON，方便 AI 直接消费。
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / '_lib'))

from wbkit import git  # noqa: E402


def emit(data):
    print(json.dumps(data, ensure_ascii=False, indent=2))


def cmd_info(a):
    repo = a.repo
    tracked, untracked = git.dirty_files(repo)
    out = {
        'repo': str(Path(repo).resolve()),
        'root': git.repo_root(repo),
        'branch': git.branch(repo),
        'head': git.rev(repo, 'HEAD'),
        'dirty': len(tracked),
        'untracked': len(untracked),
        'dirty_files': tracked[:a.limit],
        'last_commit': {
            'hash': git.git(repo, 'log', '-1', '--format=%H'),
            'subject': git.git(repo, 'log', '-1', '--format=%s'),
            'time': git.commit_time(repo, 'HEAD'),
        },
        'last_fetch': git.last_fetch_time(repo),
    }
    emit(out)


def cmd_branch(a):
    emit({'repo': a.repo, 'branch': git.branch(a.repo)})


def cmd_dirty(a):
    tracked, untracked = git.dirty_files(a.repo)
    emit({'repo': a.repo, 'tracked': tracked, 'untracked': untracked,
          'tracked_count': len(tracked), 'untracked_count': len(untracked)})


def cmd_log(a):
    args = ['log', '--format=%H%x09%an%x09%ct%x09%s', '--max-count=%d' % a.limit]
    if a.range:
        args.append(git.ref(a.range.split('..')[0]) + '..' + git.ref(a.range.split('..')[-1])
                    if '..' in a.range else git.ref(a.range))
    if a.first_parent:
        args.append('--first-parent')
    rows = []
    for line in git.git(a.repo, *args).splitlines():
        parts = line.split('\t', 3)
        if len(parts) == 4:
            rows.append({'hash': parts[0], 'author': parts[1], 'time': int(parts[2]), 'subject': parts[3]})
    emit({'repo': a.repo, 'range': a.range, 'commits': rows})


def cmd_diff(a):
    base = git.ref(a.base)
    head = git.ref(a.head) if a.head else None
    args = [base] + ([head] if head else []) + (['--stat'] if a.stat else [])
    if a.output and not a.stat:
        Path(a.output).write_bytes(git.diff_bytes(a.repo, *args))
        emit({'repo': a.repo, 'base': base, 'head': head, 'output': str(Path(a.output).resolve()),
              'bytes': Path(a.output).stat().st_size})
        return
    text = git.git(a.repo, 'diff', *args)
    emit({'repo': a.repo, 'base': base, 'head': head, 'diff': text[:a.limit_bytes],
          'truncated': len(text) > a.limit_bytes})


def cmd_merge_base(a):
    emit({'repo': a.repo, 'a': a.a, 'b': a.b, 'merge_base': git.merge_base(a.repo, a.a, a.b)})


def cmd_commits(a):
    rng = a.range
    emit({'repo': a.repo, 'range': rng, 'commits': git.commits(a.repo, rng, first_parent=a.first_parent)})


def cmd_fetch(a):
    branches = [b.strip() for b in a.branches.split(',') if b.strip()]
    emit({'repo': a.repo, 'results': git.fetch(a.repo, branches)})


def cmd_root(a):
    emit({'path': a.path, 'root': git.repo_root(a.path)})


def main(argv=None):
    p = argparse.ArgumentParser(description='git 只读查询，输出 JSON')
    sub = p.add_subparsers(dest='cmd', required=True)

    s = sub.add_parser('info', help='仓库概况：分支、HEAD、脏文件、最近提交')
    s.add_argument('--repo', default='.')
    s.add_argument('--limit', type=int, default=20)
    s.set_defaults(fn=cmd_info)

    s = sub.add_parser('branch', help='当前分支')
    s.add_argument('--repo', default='.')
    s.set_defaults(fn=cmd_branch)

    s = sub.add_parser('dirty', help='未提交改动')
    s.add_argument('--repo', default='.')
    s.set_defaults(fn=cmd_dirty)

    s = sub.add_parser('log', help='提交列表')
    s.add_argument('--repo', default='.')
    s.add_argument('--range', default='')
    s.add_argument('--limit', type=int, default=20)
    s.add_argument('--first-parent', action='store_true')
    s.set_defaults(fn=cmd_log)

    s = sub.add_parser('diff', help='差异（默认文本，--stat 出统计，--output 存补丁）')
    s.add_argument('--repo', default='.')
    s.add_argument('--base', required=True)
    s.add_argument('--head', default='')
    s.add_argument('--stat', action='store_true')
    s.add_argument('--output', default='')
    s.add_argument('--limit-bytes', type=int, default=200000)
    s.set_defaults(fn=cmd_diff)

    s = sub.add_parser('merge-base', help='两个 ref 的 merge base')
    s.add_argument('--repo', default='.')
    s.add_argument('--a', required=True)
    s.add_argument('--b', required=True)
    s.set_defaults(fn=cmd_merge_base)

    s = sub.add_parser('commits', help='区间内的提交号')
    s.add_argument('--repo', default='.')
    s.add_argument('--range', required=True)
    s.add_argument('--first-parent', action='store_true')
    s.set_defaults(fn=cmd_commits)

    s = sub.add_parser('fetch', help='从 origin 拉取分支（不推送）')
    s.add_argument('--repo', default='.')
    s.add_argument('--branches', required=True, help='逗号分隔')
    s.set_defaults(fn=cmd_fetch)

    s = sub.add_parser('root', help='找目录所属仓库根')
    s.add_argument('--path', default='.')
    s.set_defaults(fn=cmd_root)

    a = p.parse_args(argv)
    try:
        a.fn(a)
    except git.GitError as e:
        print(json.dumps({'error': str(e)}, ensure_ascii=False), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
