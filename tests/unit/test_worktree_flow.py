"""git-worktree-flow：建/列/删工作区、拒绝脏工作区、清理失效记录、非法分支名被拒、基线同步。"""
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'lib'))
sys.path.insert(0, str(ROOT / 'skills' / 'git-worktree-flow' / 'scripts'))

import worktree_flow as wf  # noqa: E402


class WorktreeFlowTest(unittest.TestCase):
    """每个用例在临时目录里造一个真 git 仓库。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.repo = self.tmp / 'repo'
        self.repo.mkdir()
        self._git(self.repo, 'init', '-q', '-b', 'main')
        self._git(self.repo, 'config', 'user.email', 'test@example.com')
        self._git(self.repo, 'config', 'user.name', 'Test User')
        (self.repo / 'README.md').write_text('v1\n', encoding='utf-8')
        self._git(self.repo, 'add', 'README.md')
        self._git(self.repo, 'commit', '-q', '-m', 'init')
        self.wt = self.tmp / 'wt-x'

    def tearDown(self):
        self._tmp.cleanup()

    def _git(self, cwd, *args):
        proc = subprocess.run(['git', '-C', str(cwd)] + list(args), capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    def _item(self, path=None):
        path = str((path or self.wt).resolve())
        rows = wf.list_worktrees(str(self.repo))['worktrees']
        return next(row for row in rows if row['path'] == path)

    # ---------- create / list ----------

    def test_create_从基线建分支并列出(self):
        out = wf.create(str(self.repo), 'feature/x', str(self.wt), base='main')
        self.assertEqual(out['branch'], 'feature/x')
        self.assertFalse(out['detached'])
        self.assertTrue((self.wt / 'README.md').is_file())
        self.assertEqual(self._git(self.repo, 'rev-parse', '--verify', 'refs/heads/feature/x'), out['head'])

        data = wf.list_worktrees(str(self.repo))
        self.assertEqual(data['count'], 2)
        item = self._item()
        self.assertEqual(item['branch'], 'feature/x')
        self.assertEqual(item['head'], out['head'])
        self.assertTrue(item['exists'])
        self.assertFalse(item['dirty'])

    def test_create_分支已存在直接检出(self):
        self._git(self.repo, 'branch', 'feature/y')
        out = wf.create(str(self.repo), 'feature/y', str(self.tmp / 'wt-y'))
        self.assertEqual(out['branch'], 'feature/y')
        self.assertEqual(out['head'], self._git(self.repo, 'rev-parse', 'refs/heads/main'))

    def test_create_detach_不占分支(self):
        out = wf.create(str(self.repo), 'spike', str(self.wt), base='main', detach=True)
        self.assertTrue(out['detached'])
        self.assertIsNone(out['branch'])
        item = self._item()
        self.assertTrue(item['detached'])
        self.assertIsNone(item['branch'])
        self.assertEqual(item['head'], out['head'])

    def test_create_拒绝已占用的目录(self):
        wf.create(str(self.repo), 'feature/x', str(self.wt), base='main')
        with self.assertRaises(wf.git.GitError) as ctx:
            wf.create(str(self.repo), 'feature/z', str(self.wt), base='main')
        self.assertIn('已经是 worktree', str(ctx.exception))

    def test_非法分支名被拒(self):
        for bad in ('bad..name', '-bad', 'bad name', 'bad/', 'x.lock'):
            with self.assertRaises(wf.git.GitError, msg=bad):
                wf.create(str(self.repo), bad, str(self.tmp / 'wt-bad'), base='main')
        self.assertFalse((self.tmp / 'wt-bad').exists())

    # ---------- remove ----------

    def test_remove_干净的目录直接删(self):
        wf.create(str(self.repo), 'feature/x', str(self.wt), base='main')
        out = wf.remove(str(self.repo), str(self.wt))
        self.assertFalse(out['forced'])
        self.assertFalse(self.wt.exists())
        self.assertEqual(wf.list_worktrees(str(self.repo))['count'], 1)

    def test_remove_拒绝脏工作区(self):
        wf.create(str(self.repo), 'feature/x', str(self.wt), base='main')
        (self.wt / 'README.md').write_text('changed\n', encoding='utf-8')
        with self.assertRaises(wf.git.GitError) as ctx:
            wf.remove(str(self.repo), str(self.wt))
        self.assertIn('未提交改动', str(ctx.exception))
        self.assertTrue(self.wt.exists())

        # 未跟踪文件同样算未提交改动
        (self.wt / 'README.md').write_text('v1\n', encoding='utf-8')
        (self.wt / 'new.txt').write_text('n\n', encoding='utf-8')
        with self.assertRaises(wf.git.GitError):
            wf.remove(str(self.repo), str(self.wt))

        # --force 才丢弃
        out = wf.remove(str(self.repo), str(self.wt), force=True)
        self.assertTrue(out['forced'])
        self.assertFalse(self.wt.exists())

    def test_remove_主工作区被拒(self):
        with self.assertRaises(wf.git.GitError) as ctx:
            wf.remove(str(self.repo), str(self.repo))
        self.assertIn('主工作区', str(ctx.exception))

    # ---------- prune ----------

    def test_prune_清理失效记录(self):
        wf.create(str(self.repo), 'feature/x', str(self.wt), base='main')
        self.assertEqual(wf.list_worktrees(str(self.repo))['count'], 2)
        shutil.rmtree(self.wt)
        out = wf.prune(str(self.repo))
        self.assertEqual(out['count'], 1)
        self.assertNotIn(str(self.wt.resolve()), out['worktrees'])
        self.assertEqual(len(wf.list_worktrees(str(self.repo))['worktrees']), 1)

    # ---------- sync ----------

    def _commit_base(self, name='base.txt'):
        (self.repo / name).write_text('base\n', encoding='utf-8')
        self._git(self.repo, 'add', name)
        self._git(self.repo, 'commit', '-q', '-m', 'main 新提交')

    def test_sync_合入基线并快进(self):
        wf.create(str(self.repo), 'feature/x', str(self.wt), base='main')
        self._commit_base()
        out = wf.sync(str(self.repo), str(self.wt), 'main')
        self.assertEqual(out['status'], 'merged')
        self.assertTrue(out['fast_forward'])
        self.assertTrue((self.wt / 'base.txt').is_file())
        self.assertEqual(out['after'], self._git(self.repo, 'rev-parse', 'refs/heads/feature/x'))
        self.assertEqual(wf.sync(str(self.repo), str(self.wt), 'main')['status'], 'up-to-date')

    def test_sync_冲突只报文件不动工作区(self):
        wf.create(str(self.repo), 'feature/x', str(self.wt), base='main')
        (self.wt / 'README.md').write_text('from-wt\n', encoding='utf-8')
        self._git(self.wt, 'commit', '-qam', 'wt 改 README')
        (self.repo / 'README.md').write_text('from-main\n', encoding='utf-8')
        self._git(self.repo, 'commit', '-qam', 'main 改 README')
        head_before = self._git(self.wt, 'rev-parse', 'HEAD')

        out = wf.sync(str(self.repo), str(self.wt), 'main')
        self.assertEqual(out['status'], 'conflict')
        self.assertIn('README.md', out['files'])
        self.assertEqual(self._git(self.wt, 'rev-parse', 'HEAD'), head_before)
        self.assertEqual((self.wt / 'README.md').read_text(encoding='utf-8'), 'from-wt\n')
        self.assertEqual(self._git(self.wt, 'status', '--porcelain'), '')
        merging = subprocess.run(['git', '-C', str(self.wt), 'rev-parse', '-q', '--verify', 'MERGE_HEAD'],
                                 capture_output=True)
        self.assertNotEqual(merging.returncode, 0)

    def test_sync_工作区脏时只报告(self):
        wf.create(str(self.repo), 'feature/x', str(self.wt), base='main')
        self._commit_base()
        (self.wt / 'README.md').write_text('wip\n', encoding='utf-8')
        out = wf.sync(str(self.repo), str(self.wt), 'main')
        self.assertEqual(out['status'], 'dirty')
        self.assertIn('README.md', out['files'])
        self.assertEqual(out['before'], out['after'])

    def test_sync_基线不存在报错(self):
        wf.create(str(self.repo), 'feature/x', str(self.wt), base='main')
        with self.assertRaises(wf.git.GitError) as ctx:
            wf.sync(str(self.repo), str(self.wt), 'origin/nope')
        self.assertIn('基线不存在', str(ctx.exception))


if __name__ == '__main__':
    unittest.main()
