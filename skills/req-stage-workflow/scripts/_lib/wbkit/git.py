"""只读的 git 查询。唯一的网络操作是 fetch（只拉取，不推送）。

一次调用里会反复查同一个仓库的同一件事（分支、HEAD、是否祖先），用 with memo(): 包住
一段计算，同样的查询只跑一次；出了这个范围立刻失效，不跨进程缓存。
"""
import contextlib
import re
import subprocess
import threading
import time
from pathlib import Path

_LOCAL = threading.local()


class GitError(Exception):
    pass


@contextlib.contextmanager
def memo():
    """范围缓存：这段计算里重复的 git 查询只跑一次。可嵌套，退出时恢复上一层。"""
    prev = getattr(_LOCAL, 'store', None)
    _LOCAL.store = {}
    try:
        yield
    finally:
        _LOCAL.store = prev


def _cached(key, make):
    store = getattr(_LOCAL, 'store', None)
    if store is None:
        return make()
    if key in store:
        return store[key]
    val = make()
    store[key] = val
    return val


def diff_bytes(repo, *args):
    """git diff 的原样输出（bytes）。存补丁、之后 git apply 用；文本模式会把 CRLF 的 \\r 吃掉。"""
    proc = subprocess.run(['git', '-C', str(repo), 'diff', '--binary'] + list(args),
                          capture_output=True, timeout=60)
    if proc.returncode != 0:
        raise GitError(proc.stderr.decode('utf-8', 'replace').strip() or 'git diff 失败')
    return proc.stdout


def git(repo, *args, check=True):
    return _cached((str(repo), args, check), lambda: _git(repo, *args, check=check))


def _git(repo, *args, check=True):
    proc = None
    for wait in (0.5, 1, 2, 3, 0):
        proc = subprocess.run(['git', '-C', str(repo)] + list(args),
                              capture_output=True, text=True, timeout=60)
        # 别的 git 进程正占着锁：等一下再试，别一下就失败
        if proc.returncode == 0 or '.lock' not in (proc.stderr or '') or not wait:
            break
        time.sleep(wait)
    if check and proc.returncode != 0:
        raise GitError((proc.stderr or proc.stdout).strip() or 'git %s 失败' % ' '.join(args))
    return proc.stdout.strip()


def rev(repo, ref_name):
    """ref 存在时返回完整提交号，否则 None。"""
    out = git(repo, 'rev-parse', '--verify', '--quiet', str(ref_name) + '^{commit}', check=False)
    return out or None


def branch(repo):
    return git(repo, 'rev-parse', '--abbrev-ref', 'HEAD')


def dirty_files(repo):
    """已跟踪文件中未提交的改动（含暂存区）；未跟踪文件单独返回。返回 (tracked, untracked)。"""
    return _cached(('dirty', str(repo)), lambda: _dirty_files(repo))


def _dirty_files(repo):
    tracked, untracked = [], []
    out = subprocess.run(['git', '-C', str(repo), 'status', '--porcelain'],
                         capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise GitError(out.stderr.strip())
    for line in out.stdout.splitlines():
        if not line.strip():
            continue
        (untracked if line.startswith('??') else tracked).append(line[3:])
    return tracked, untracked


def is_ancestor(repo, older, newer):
    return _cached(('anc', str(repo), older, newer), lambda: subprocess.run(
        ['git', '-C', str(repo), 'merge-base', '--is-ancestor', older, newer],
        capture_output=True, timeout=60).returncode == 0)


def merge_base(repo, a, b):
    out = git(repo, 'merge-base', a, b, check=False)
    return out or None


def commits(repo, rng, first_parent=False):
    args = ['rev-list'] + (['--first-parent'] if first_parent else []) + [rng]
    return git(repo, *args).split()


def merged_in(repo, base_ref, head='HEAD'):
    """开发分支上通过合并带进来、且不在 base 分支上的提交。"""
    rng = '%s..%s' % (base_ref, head)
    return sorted(set(commits(repo, rng)) - set(commits(repo, rng, first_parent=True)))


def commit_time(repo, ref_name):
    """提交时间（committer date，unix 秒）。"""
    return int(git(repo, 'log', '-1', '--format=%ct', ref_name))


def last_fetch_time(repo):
    return _cached(('fetch', str(repo)), lambda: _last_fetch_time(repo))


def _last_fetch_time(repo):
    times = []
    for flag in ('--git-dir', '--git-common-dir'):
        d = Path(git(repo, 'rev-parse', flag))
        if not d.is_absolute():
            d = Path(repo) / d
        if (d / 'FETCH_HEAD').exists():
            times.append(int((d / 'FETCH_HEAD').stat().st_mtime))
    return max(times) if times else None


REF = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_./-]*')


def ref(name):
    """分支名进 git 命令前先过这一关：不能以 - 开头、不能有 .. 和空白。"""
    name = str(name or '')
    if not REF.fullmatch(name) or '..' in name or name.endswith(('/', '.lock')):
        raise GitError('分支名不合法：%r' % name)
    return name


def fetch(repo, branches):
    """从 origin 拉取指定分支（只读远端，不推送）。不存在的远端分支记为 False。"""
    results = {}
    for b in branches:
        proc = subprocess.run(
            ['git', '-C', str(repo), 'fetch', '--quiet', 'origin',
             'refs/heads/%s:refs/remotes/origin/%s' % (ref(b), ref(b))],
            capture_output=True, text=True, timeout=120)
        if proc.returncode == 0:
            results[b] = True
        elif "couldn't find remote ref" in proc.stderr:
            results[b] = False
        else:
            raise GitError(proc.stderr.strip())
    return results


def repo_root(path='.'):
    """目录所属仓库的根目录；不是 git 仓库返回 None。"""
    try:
        out = subprocess.run(['git', '-C', str(path), 'rev-parse', '--show-toplevel'],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None
