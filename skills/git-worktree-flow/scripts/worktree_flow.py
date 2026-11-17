#!/usr/bin/env python3
"""git worktree 并行开发工作区：创建、列出、删除、清理、同步基线。

所有子命令输出 JSON。只调用 git，不拼业务分支命名，不搬运未提交改动。
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / '_lib'))

from wbkit import git  # noqa: E402


def emit(data):
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _root(repo):
    """确认目录属于某个 git 仓库，返回仓库根。"""
    root = git.repo_root(repo)
    if not root:
        raise git.GitError('不是 git 仓库：%s' % repo)
    return root


def _worktrees(root):
    """解析 git worktree list --porcelain，返回原始记录。"""
    out = git.git(root, 'worktree', 'list', '--porcelain')
    rows, cur = [], None
    for line in out.splitlines():
        if line.startswith('worktree '):
            if cur:
                rows.append(cur)
            cur = {'path': line[len('worktree '):], 'head': None, 'branch': None,
                   'detached': False, 'bare': False, 'locked': None, 'prunable': None}
            continue
        if cur is None:
            continue
        if line.startswith('HEAD '):
            cur['head'] = line[len('HEAD '):]
        elif line.startswith('branch '):
            refname = line[len('branch '):]
            cur['branch'] = refname[len('refs/heads/'):] if refname.startswith('refs/heads/') else refname
        elif line == 'detached':
            cur['detached'] = True
        elif line == 'bare':
            cur['bare'] = True
        elif line.startswith('locked'):
            cur['locked'] = line[len('locked'):].strip() or True
        elif line.startswith('prunable'):
            cur['prunable'] = line[len('prunable'):].strip() or True
    if cur:
        rows.append(cur)
    return rows


def _same(a, b):
    return Path(a).resolve() == Path(b).resolve()


def _find(root, target):
    """在 worktree 列表里按目录找一条记录。"""
    for row in _worktrees(root):
        if _same(row['path'], target):
            return row
    return None


def list_worktrees(repo):
    """列出仓库的所有 worktree：路径、分支、HEAD、是否脏。"""
    root = _root(repo)
    rows = _worktrees(root)
    for row in rows:
        path = Path(row['path'])
        row['exists'] = path.is_dir()
        if row['exists'] and not row['bare']:
            try:
                tracked, untracked = git.dirty_files(str(path))
            except git.GitError:
                tracked, untracked = [], []
            row['tracked'] = len(tracked)
            row['untracked'] = len(untracked)
            row['dirty'] = bool(tracked or untracked)
        else:
            row['tracked'], row['untracked'], row['dirty'] = 0, 0, False
    return {'repo': root, 'count': len(rows), 'worktrees': rows}


def create(repo, branch, path, base=None, detach=False):
    """在一个新目录里检出分支，建 git worktree。

    分支已存在就检出它；不存在就从 --base（缺省当前 HEAD）拉出来。
    --detach 时新建一个分离 HEAD 的目录，检出的提交优先取分支现有提交，其次 --base，最后 HEAD。
    """
    root = _root(repo)
    branch = git.ref(branch)                      # ref 合法性校验，非法名在这里抛错
    target = Path(path).expanduser().resolve()
    if _find(root, target):
        raise git.GitError('目标目录已经是 worktree：%s' % target)
    base = git.ref(base) if base else None
    if base and not git.rev(root, base):
        raise git.GitError('基线不存在：%s' % base)
    branch_sha = git.rev(root, 'refs/heads/' + branch)
    target.parent.mkdir(parents=True, exist_ok=True)
    if detach:
        ref = branch_sha and branch or (base or 'HEAD')
        git.git(root, 'worktree', 'add', '--detach', str(target), ref)
        created = None
    elif branch_sha:
        git.git(root, 'worktree', 'add', str(target), branch)
        created = branch
    elif base:
        git.git(root, 'worktree', 'add', '--no-track', '-b', branch, str(target), base)
        created = branch
    else:
        git.git(root, 'worktree', 'add', '--no-track', '-b', branch, str(target), 'HEAD')
        created = branch
    return {'repo': root, 'path': str(target), 'branch': created,
            'detached': bool(detach), 'base': base,
            'head': git.rev(str(target), 'HEAD')}


def remove(repo, path, force=False):
    """删除一个 worktree。默认拒绝有未提交改动（含未跟踪文件）的目录。"""
    root = _root(repo)
    target = Path(path).expanduser().resolve()
    row = _find(root, target)
    if not row:
        raise git.GitError('不是该仓库的 worktree：%s' % target)
    if _same(row['path'], root):
        raise git.GitError('主工作区不能用这个命令删除：%s' % target)
    if not target.is_dir():
        raise git.GitError('目录已不存在，用 prune 清理记录：%s' % target)
    if not force:
        tracked, untracked = git.dirty_files(str(target))
        if tracked or untracked:
            files = tracked + untracked
            raise git.GitError('worktree 有未提交改动，已拒绝删除（确认要删加 --force）：%s'
                               % '、'.join(files[:20]))
    git.git(root, 'worktree', 'remove', *(['--force'] if force else []), str(target))
    return {'repo': root, 'removed': str(target), 'branch': row['branch'], 'forced': bool(force)}


def prune(repo):
    """清理目录已经不存在、但还留在 worktree 记录里的条目。"""
    root = _root(repo)
    out = git.git(root, 'worktree', 'prune', '--verbose')
    pruned = [line.strip() for line in out.splitlines() if line.strip()]
    rows = _worktrees(root)
    return {'repo': root, 'pruned': pruned, 'count': len(rows),
            'worktrees': [row['path'] for row in rows]}


def _conflicts(path, ours, theirs):
    """不动工作区，预演一次合并：有冲突返回冲突文件，没有就返回空。"""
    proc = subprocess.run(['git', '-C', str(path), 'merge-tree', '--write-tree',
                           '--name-only', '--no-messages', ours, theirs],
                          capture_output=True, text=True, timeout=60)
    lines = proc.stdout.strip().splitlines()
    if proc.returncode == 0:
        return []
    if proc.returncode == 1:
        return [line for line in lines[1:] if line.strip()]
    raise git.GitError(proc.stderr.strip() or 'git merge-tree 失败')


def sync(repo, path, base):
    """把基线合进 worktree 当前分支。冲突或工作区脏时只返回说明，不动工作区。"""
    root = _root(repo)
    base = git.ref(base)
    target = Path(path).expanduser().resolve()
    if not target.is_dir():
        raise git.GitError('worktree 目录不存在：%s' % target)
    row = _find(root, target)
    if not row:
        raise git.GitError('不是该仓库的 worktree：%s' % target)
    if row['detached'] or not row['branch']:
        raise git.GitError('worktree 处于 detached HEAD，没法同步分支：%s' % target)
    base_sha = git.rev(root, base)
    if not base_sha:
        raise git.GitError('基线不存在：%s' % base)
    head = git.rev(str(target), 'HEAD')
    out = {'repo': root, 'path': str(target), 'branch': row['branch'], 'base': base,
           'before': head, 'after': head, 'fast_forward': False, 'files': []}
    if git.is_ancestor(str(target), base_sha, head):
        out['status'] = 'up-to-date'
        return out
    tracked, untracked = git.dirty_files(str(target))
    if tracked or untracked:
        out['status'] = 'dirty'
        out['files'] = tracked + untracked
        return out
    files = _conflicts(str(target), head, base_sha)
    if files:
        out['status'] = 'conflict'
        out['files'] = files
        return out
    try:
        git.git(str(target), 'merge', '--no-edit', '-q', base)
    except git.GitError as e:
        gitdir = Path(git.git(str(target), 'rev-parse', '--absolute-git-dir'))
        if (gitdir / 'MERGE_HEAD').exists():
            git.git(str(target), 'merge', '--abort', check=False)
        raise git.GitError('合入基线失败（已撤销）：%s' % str(e)[:300])
    after = git.rev(str(target), 'HEAD')
    out.update(status='merged', after=after, fast_forward=after == base_sha)
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description='git worktree 并行开发工作区：创建、列出、删除、清理、同步基线，输出 JSON')
    sub = p.add_subparsers(dest='cmd', required=True)

    s = sub.add_parser('create', help='建 worktree（分支不存在则从基线拉出来）')
    s.add_argument('--repo', required=True, help='主仓库目录')
    s.add_argument('--branch', required=True, help='worktree 检出的分支名')
    s.add_argument('--path', required=True, help='worktree 目录')
    s.add_argument('--base', default='', help='新分支的起点，如 origin/main')
    s.add_argument('--detach', action='store_true', help='分离 HEAD，不占用分支')
    s.set_defaults(fn=lambda a: emit(create(a.repo, a.branch, a.path, a.base or None, a.detach)))

    s = sub.add_parser('list', help='列出该仓库所有 worktree')
    s.add_argument('--repo', required=True)
    s.set_defaults(fn=lambda a: emit(list_worktrees(a.repo)))

    s = sub.add_parser('remove', help='删除 worktree（有未提交改动默认拒绝）')
    s.add_argument('--repo', required=True)
    s.add_argument('--path', required=True)
    s.add_argument('--force', action='store_true', help='丢弃未提交改动并强制删除')
    s.set_defaults(fn=lambda a: emit(remove(a.repo, a.path, a.force)))

    s = sub.add_parser('prune', help='清理已失效的 worktree 记录')
    s.add_argument('--repo', required=True)
    s.set_defaults(fn=lambda a: emit(prune(a.repo)))

    s = sub.add_parser('sync', help='把基线合进 worktree 分支；冲突时只报文件')
    s.add_argument('--repo', required=True)
    s.add_argument('--path', required=True)
    s.add_argument('--base', required=True, help='基线 ref，如 origin/main')
    s.set_defaults(fn=lambda a: emit(sync(a.repo, a.path, a.base)))

    a = p.parse_args(argv)
    try:
        a.fn(a)
    except git.GitError as e:
        print(json.dumps({'error': str(e)}, ensure_ascii=False), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
