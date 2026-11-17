"""multi-model-code-review：取 diff、分块截断、JSON 解析容错、多模型合并去重、markdown 渲染、--dry-run。"""
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'lib'))
sys.path.insert(0, str(ROOT / 'skills' / 'multi-model-code-review' / 'scripts'))

import code_review as cr  # noqa: E402


class ReviewTestBase(unittest.TestCase):
    """每个用例在临时目录里造一个真 git 仓库：main 上有基线，feature/x 上有改动。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.repo = self.tmp / 'repo'
        self.repo.mkdir()
        self._git(self.repo, 'init', '-q', '-b', 'main')
        self._git(self.repo, 'config', 'user.email', 'test@example.com')
        self._git(self.repo, 'config', 'user.name', 'Test User')
        (self.repo / 'src').mkdir()
        self.base_source = '\n'.join('value = %d' % i for i in range(1, 21)) + '\n'
        (self.repo / 'src' / 'app.py').write_text(self.base_source, encoding='utf-8')
        self._git(self.repo, 'add', 'src/app.py')
        self._git(self.repo, 'commit', '-q', '-m', 'init')

        self._git(self.repo, 'checkout', '-q', '-b', 'feature/x')
        (self.repo / 'src' / 'app.py').write_text(
            self.base_source.replace('value = 3', 'value = 30') + 'value = 21\n', encoding='utf-8')
        (self.repo / 'src' / 'util.py').write_text(
            '\n'.join('def util_%d():\n    return %d' % (i, i) for i in range(1, 5)) + '\n',
            encoding='utf-8')
        self._git(self.repo, 'add', '-A')
        self._git(self.repo, 'commit', '-q', '-m', 'feature')

    def tearDown(self):
        self._tmp.cleanup()

    def _git(self, cwd, *args):
        proc = subprocess.run(['git', '-C', str(cwd)] + list(args), capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    def _diff(self, **kwargs):
        kwargs.setdefault('base', 'main')
        kwargs.setdefault('head', 'feature/x')
        return cr.collect_diff(str(self.repo), **kwargs)


class CollectDiffTest(ReviewTestBase):
    def test_取分支改动并按目录分组(self):
        diff = self._diff()
        self.assertEqual(diff['mode'], 'branch')
        self.assertEqual((diff['base'], diff['head']), ('main', 'feature/x'))
        self.assertEqual(diff['skipped'], [])
        paths = [f['path'] for f in diff['files']]
        self.assertEqual(paths, ['src/app.py', 'src/util.py'])
        self.assertTrue(all(f['add'] > 0 for f in diff['files']))
        self.assertEqual([g['dir'] for g in diff['groups']], ['src'])
        self.assertEqual(diff['groups'][0]['files'], paths)

        prompt = cr.build_prompt(diff)
        self.assertIn('只审查下面 diff 里出现的改动', prompt)
        self.assertIn('```diff', prompt)
        self.assertIn('src/util.py', prompt)
        self.assertIn('### src/', prompt)

    def test_取单个提交的改动(self):
        parent = self._git(self.repo, 'rev-parse', 'HEAD^')
        diff = cr.collect_diff(str(self.repo), base=parent, head='HEAD')
        self.assertIn('src/util.py', [f['path'] for f in diff['files']])

    def test_未提交改动包含未跟踪文本文件(self):
        (self.repo / 'src' / 'app.py').write_text(self.base_source + 'value = 99\n', encoding='utf-8')
        notes = self.repo / 'notes'
        notes.mkdir()
        (notes / 'new.txt').write_text('第一行\n第二行\n', encoding='utf-8')

        diff = cr.collect_diff(str(self.repo), uncommitted=True)
        self.assertEqual(diff['mode'], 'uncommitted')
        paths = [f['path'] for f in diff['files']]
        self.assertIn('src/app.py', paths)
        self.assertIn('notes/new.txt', paths)
        new = next(f for f in diff['files'] if f['path'] == 'notes/new.txt')
        self.assertTrue(new['new_file'])
        self.assertIn('new file mode', new['diff'])
        self.assertIn('+第一行', new['diff'])

    def test_分块截断和文件数上限(self):
        diff = self._diff(max_lines=3)
        self.assertTrue(any(f['truncated'] for f in diff['files']))
        prompt = cr.build_prompt(diff)
        self.assertIn('已截断', prompt)
        self.assertIn('原 %d 行' % next(f['orig_lines'] for f in diff['files'] if f['truncated']), prompt)

        limited = self._diff(max_files=1)
        self.assertEqual(len(limited['files']), 1)
        self.assertEqual(len(limited['skipped']), 1)
        self.assertEqual(limited['skipped'][0]['reason'], '超出 --max-files')


class ParseIssuesTest(unittest.TestCase):
    def test_代码块和说明文字都能解析(self):
        text = ('好的，审查结果如下：\n```json\n'
                '[{"file":"a.py","line":"7","severity":"严重","title":"空指针","detail":"d","suggestion":"s"},'
                '{"file":"b.py","title":"边界","severity":"建议"}]\n```')
        issues = cr.parse_issues(text)
        self.assertEqual(len(issues), 2)
        self.assertEqual(issues[0]['severity'], 'blocker')
        self.assertEqual(issues[0]['line'], 7)
        self.assertEqual(issues[1]['severity'], 'nit')
        self.assertIsNone(issues[1]['line'])

    def test_外层对象和字段别名(self):
        issues = cr.parse_issues('{"findings":[{"path":"c.py","line":3,"severity":"high",'
                                 '"message":"并发写","fix":"加锁"}]}')
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]['file'], 'c.py')
        self.assertEqual(issues[0]['severity'], 'major')
        self.assertEqual(issues[0]['title'], '并发写')
        self.assertEqual(issues[0]['suggestion'], '加锁')

    def test_缺字段的条目被丢掉(self):
        issues = cr.parse_issues('[{"file":"a.py"},{"file":"b.py","title":"ok"},3]')
        self.assertEqual([i['file'] for i in issues], ['b.py'])

    def test_非法JSON报错(self):
        with self.assertRaises(ValueError) as ctx:
            cr.parse_issues('这不是 JSON')
        self.assertIn('JSON', str(ctx.exception))
        with self.assertRaises(ValueError):
            cr.parse_issues('[{"file": "a.py",')


class RawOutputTest(ReviewTestBase):
    def test_解析失败给出原始输出位置(self):
        def bad_runner(engine, prompt, **kwargs):
            return {'ok': True, 'engine': engine, 'text': '这不是 JSON'}

        out = self.tmp / 'review.json'
        data = cr.review(str(self.repo), base='main', head='feature/x',
                         runner=bad_runner, out=str(out))
        self.assertFalse(data['ok'])
        self.assertIn('原始输出见', data['error'])
        raw = Path(data['raw_output'])
        self.assertTrue(raw.is_file())
        self.assertEqual(raw.read_text(encoding='utf-8'), '这不是 JSON')


class BatchTest(ReviewTestBase):
    def _fake_runner(self, engine, prompt, **kwargs):
        self.assertIn('审查口径', prompt)
        if engine == 'kimi':
            items = [
                {'file': 'src/order.py', 'line': 88, 'severity': 'major', 'title': '空指针风险',
                 'detail': 'kimi 的详情', 'suggestion': '入口判空'},
                {'file': 'src/order.py', 'line': 10, 'severity': 'nit', 'title': '命名偏好',
                 'detail': '', 'suggestion': ''},
            ]
        else:
            items = [
                {'file': 'src/order.py', 'line': 88, 'severity': 'blocker', 'title': '空指针 风险！',
                 'detail': 'codex 的详情更长一些，带证据行', 'suggestion': '入口判空并补单测'},
            ]
        return {'ok': True, 'engine': engine, 'seconds': 1, 'models': [engine],
                'text': json.dumps(items, ensure_ascii=False)}

    def test_多模型合并去重和agreement(self):
        data = cr.batch(str(self.repo), ['kimi', 'codex'], base='main', head='feature/x',
                        runner=self._fake_runner)
        self.assertTrue(data['ok'])
        self.assertEqual(len(data['issues']), 2)
        merged = data['issues'][0]
        self.assertEqual(merged['severity'], 'blocker')
        self.assertEqual(merged['file'], 'src/order.py')
        self.assertEqual(merged['line'], 88)
        self.assertEqual(merged['agreement'], ['codex', 'kimi'])
        self.assertEqual(merged['agreement_count'], 2)
        self.assertIn('codex 的详情更长一些', merged['detail'])
        self.assertEqual([r['engine'] for r in data['runs']], ['kimi', 'codex'])
        self.assertTrue(all(r['ok'] for r in data['runs']))

    def test_一家失败不影响另一家(self):
        def flaky(engine, prompt, **kwargs):
            if engine == 'codex':
                return {'ok': False, 'engine': engine, 'error': '超时（1200 秒）'}
            return {'ok': True, 'engine': engine, 'text': '[]'}

        data = cr.batch(str(self.repo), ['kimi', 'codex'], base='main', head='feature/x',
                        runner=flaky)
        self.assertTrue(data['ok'])
        self.assertEqual(len(data['runs']), 2)
        self.assertEqual(data['runs'][1]['error'], '超时（1200 秒）')
        self.assertEqual(data['issues'], [])

    def test_dry_run不调模型(self):
        called = []

        def boom(engine, prompt, **kwargs):
            called.append(engine)
            raise AssertionError('--dry-run 不应该启动模型')

        one = cr.review(str(self.repo), base='main', head='feature/x', engine='kimi',
                        dry_run=True, runner=boom)
        self.assertTrue(one['dry_run'])
        self.assertEqual(called, [])
        self.assertEqual(one['call']['profile'], 'read')
        self.assertGreater(one['call']['prompt_chars'], 0)
        self.assertIn('argv', one['call'])

        many = cr.batch(str(self.repo), ['kimi', 'codex'], base='main', head='feature/x',
                        dry_run=True, runner=boom)
        self.assertEqual(len(many['calls']), 2)
        self.assertEqual(called, [])


class ReportTest(unittest.TestCase):
    DATA = {
        'repo': '~/repos/project-a',
        'range': {'mode': 'branch', 'base': 'origin/main', 'head': 'HEAD'},
        'engines': ['kimi', 'codex'],
        'issues': [
            {'file': 'src/order.py', 'line': 88, 'severity': 'blocker', 'title': '空指针风险',
             'detail': '第 88 行没有判空', 'suggestion': '入口判空',
             'agreement': ['codex', 'kimi'], 'agreement_count': 2},
            {'file': 'src/order.py', 'line': 10, 'severity': 'nit', 'title': '命名偏好'},
        ],
    }

    def test_按严重程度分组并带文件行号(self):
        md = cr.render_markdown(self.DATA)
        self.assertIn('# 代码审查报告', md)
        self.assertIn('## 阻断（blocker，1 条）', md)
        self.assertIn('## 细节（nit，1 条）', md)
        self.assertIn('`src/order.py:88`', md)
        self.assertIn('- 提及模型：codex、kimi（2 家一致）', md)
        self.assertIn('- 建议：入口判空', md)
        self.assertNotIn('## 重要', md)

    def test_没有问题时明确说未发现(self):
        md = cr.render_markdown({'repo': 'x', 'issues': []})
        self.assertIn('未发现问题。', md)

    def test_report子命令写出markdown(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / 'review.json'
            out = Path(d) / 'review.md'
            src.write_text(json.dumps(self.DATA, ensure_ascii=False), encoding='utf-8')
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = cr.main(['report', '--input', str(src), '--out', str(out)])
            self.assertEqual(code, 0)
            self.assertIn('## 阻断（blocker，1 条）', buf.getvalue())
            self.assertEqual(out.read_text(encoding='utf-8'), buf.getvalue())


if __name__ == '__main__':
    unittest.main()
