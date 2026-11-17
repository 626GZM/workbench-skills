"""todo-scanner：kind 识别、目录排除、limit、diff 只报新增、report 分组、无 git 时 blame 不报错。"""
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'lib'))
sys.path.insert(0, str(ROOT / 'skills' / 'todo-scanner' / 'scripts'))

import scan_todos as st  # noqa: E402

JAVA = """package com.example;

// TODO: 补充参数校验
// FIXME: 并发下会重复扣款
// HACK: 超时时间先写死
// XXX: 这里的顺序不能动
public class OrderService {
    public void a() {
        System.out.println("debug");
    }

    public void b() {
        try {
            work();
        } catch (Exception e) {
        }
    }

    public void c() {
        try {
            work();
        } catch (Exception e) {
            e.printStackTrace();
        }
    }

    /*
     * public void d() {
     *     int x = 1;
     *     System.out.println(x);
     * }
     */
}
"""

PY = """# TODO: 换成批量接口


def foo():
    try:
        bar()
    except Exception:
        pass
"""


def run_json(args):
    buf, err = io.StringIO(), io.StringIO()
    with redirect_stdout(buf), redirect_stderr(err):
        code = st.main(args)
    text = buf.getvalue().strip()
    return code, (json.loads(text) if text else None)


def run_text(args):
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = st.main(args)
    return code, buf.getvalue()


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, rel, text):
        path = self.tmp / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return path

    def scan(self, *extra):
        return run_json(['scan', '--repo', str(self.tmp)] + list(extra))


class KindTest(Base):
    def test_识别各类kind(self):
        self.write('src/main/java/com/example/OrderService.java', JAVA)
        self.write('src/main/java/com/example/task.py', PY)
        code, out = self.scan()
        self.assertEqual(code, 0)

        kinds = {e['kind'] for e in out['entries']}
        self.assertEqual(kinds, {'todo', 'fixme', 'hack', 'debug',
                                 'empty_catch', 'swallowed_exception', 'commented_code'})

        tags = {e['tag'] for e in out['entries']}
        self.assertIn('XXX', tags)
        self.assertIn('FIXME', tags)
        self.assertIn('HACK', tags)
        self.assertIn('PRINT_STACK_TRACE', tags)
        self.assertIn('COMMENTED_CODE', tags)

    def test_严重程度映射(self):
        self.write('a.java', JAVA)
        code, out = self.scan()
        self.assertEqual(code, 0)
        by_kind = {}
        for e in out['entries']:
            by_kind.setdefault(e['kind'], set()).add(e['severity'])
        self.assertEqual(by_kind['todo'], {'medium'})
        self.assertEqual(by_kind['fixme'], {'high'})
        self.assertEqual(by_kind['hack'], {'high'})
        self.assertEqual(by_kind['debug'], {'high'})
        self.assertEqual(by_kind['empty_catch'], {'high'})
        self.assertEqual(by_kind['swallowed_exception'], {'high'})
        self.assertEqual(by_kind['commented_code'], {'low'})

    def test_只认注释里的标记和真实调试语句(self):
        self.write('b.js', '\n'.join([
            'const s = "TODO: 不是注释";',
            '// TODO: 真注释',
            'const t = "console.log(1)";',
            'console.log("残留");',
        ]))
        code, out = self.scan()
        self.assertEqual(code, 0)
        todo = [e for e in out['entries'] if e['kind'] == 'todo']
        self.assertEqual(len(todo), 1)
        self.assertEqual(todo[0]['line'], 2)
        debug = [e for e in out['entries'] if e['kind'] == 'debug']
        self.assertEqual(len(debug), 1)
        self.assertEqual(debug[0]['line'], 4)


class ExcludeTest(Base):
    def test_整棵跳过排除目录(self):
        self.write('node_modules/pkg/index.js', '// TODO: 依赖里的\n')
        self.write('target/out/App.java', '// TODO: 构建产物里的\n')
        self.write('src/main/java/com/example/App.java', '// TODO: 业务代码里的\n')
        code, out = self.scan()
        self.assertEqual(code, 0)
        self.assertEqual([e['file'] for e in out['entries']],
                         ['src/main/java/com/example/App.java'])
        self.assertEqual(out['files'], 1)

    def test_自定义exclude(self):
        self.write('vendor/a.js', '// TODO: vendor 里的\n')
        self.write('src/a.js', '// TODO: 源码里的\n')
        code, out = self.scan('--ext', '.js', '--exclude', 'vendor')
        self.assertEqual(code, 0)
        self.assertEqual([e['file'] for e in out['entries']], ['src/a.js'])


class LimitTest(Base):
    def test_limit截断并标记(self):
        lines = ['# TODO: 第 %d 条' % i for i in range(1, 11)]
        self.write('a.py', '\n'.join(lines) + '\n')
        code, out = self.scan('--ext', '.py', '--limit', '3')
        self.assertEqual(code, 0)
        self.assertEqual(len(out['entries']), 3)
        self.assertEqual(out['total'], 3)
        self.assertTrue(out['truncated'])
        self.assertEqual([e['line'] for e in out['entries']], [1, 2, 3])

    def test_不超限时不标记(self):
        self.write('a.py', '# TODO: 只有一条\n')
        code, out = self.scan('--ext', '.py', '--limit', '50')
        self.assertEqual(code, 0)
        self.assertFalse(out['truncated'])
        self.assertEqual(out['matched'], 1)


@unittest.skipUnless(shutil.which('git'), '需要 git')
class DiffTest(Base):
    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.tmp)] + list(args),
                              capture_output=True, text=True, check=True)

    def setUp(self):
        super().setUp()
        self.git('init', '-q')
        self.git('config', 'user.email', 'tester@example.com')
        self.git('config', 'user.name', 'Tester')

    def commit_all(self, message='init'):
        self.git('add', '-A')
        self.git('commit', '-q', '-m', message)

    def test_diff只报新增(self):
        self.write('app.py', '# TODO: 旧的\nprint(1)\n')
        self.commit_all()
        self.write('app.py', '# TODO: 旧的\nprint(1)\n# FIXME: 这次新增的\n')
        self.write('extra.py', '# HACK: 新文件里的\n')
        code, out = run_json(['diff', '--repo', str(self.tmp), '--base', 'HEAD', '--ext', '.py'])
        self.assertEqual(code, 0)
        self.assertTrue(out['merge_base'])
        texts = [e['text'] for e in out['entries']]
        self.assertCountEqual(texts, ['这次新增的', '新文件里的'])
        self.assertNotIn('旧的', texts)

    def test_base不存在时报错(self):
        self.write('app.py', '# TODO: 旧的\n')
        self.commit_all()
        code, out = run_json(['diff', '--repo', str(self.tmp), '--base', 'origin/nope'])
        self.assertEqual(code, 2)
        self.assertIsNone(out)


class ReportTest(Base):
    def write_input(self):
        data = {
            'repo': '~/repos/project-a',
            'entries': [
                {'file': 'src/main/java/com/example/A.java', 'line': 3, 'kind': 'fixme',
                 'tag': 'FIXME', 'text': '并发问题', 'author': '张三',
                 'commit': 'abcdef1234567890', 'age_days': 12, 'severity': 'high'},
                {'file': 'src/main/java/com/example/B.java', 'line': 9, 'kind': 'todo',
                 'tag': 'TODO', 'text': '补校验', 'author': '',
                 'commit': '', 'age_days': None, 'severity': 'medium'},
            ],
        }
        path = self.tmp / 'scan.json'
        path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        return str(path)

    def test_按kind分组(self):
        path = self.write_input()
        code, md = run_text(['report', '--input', path, '--group-by', 'kind'])
        self.assertEqual(code, 0)
        self.assertIn('## fixme（1 条）', md)
        self.assertIn('## todo（1 条）', md)
        self.assertIn('| 行 | 文件 | 类型 |', md)
        self.assertIn('张三', md)
        self.assertIn('未归属', md)
        self.assertIn('12 天', md)
        self.assertIn('abcdef12', md)

    def test_按author分组并写文件(self):
        path = self.write_input()
        out_file = self.tmp / 'todos.md'
        code, md = run_text(['report', '--input', path, '--group-by', 'author',
                             '--out', str(out_file)])
        self.assertEqual(code, 0)
        self.assertIn('## 张三（1 条）', md)
        self.assertIn('## 未归属（1 条）', md)
        self.assertEqual(out_file.read_text(encoding='utf-8'), md)

    def test_按file分组默认(self):
        path = self.write_input()
        code, md = run_text(['report', '--input', path])
        self.assertEqual(code, 0)
        self.assertIn('## src/main/java/com/example/A.java（1 条）', md)
        self.assertIn('| 行 | 类型 |', md)
        self.assertIn('严重度：high 1 / medium 1', md)

    def test_空清单(self):
        path = self.tmp / 'empty.json'
        path.write_text(json.dumps({'entries': []}), encoding='utf-8')
        code, md = run_text(['report', '--input', str(path)])
        self.assertEqual(code, 0)
        self.assertIn('（没有发现需要跟踪的条目）', md)


class NoGitTest(Base):
    def test_没有git时blame为空且不报错(self):
        self.write('src/main/java/com/example/App.java', '// TODO: 无仓库里的\n')
        code, out = self.scan()
        self.assertEqual(code, 0)
        self.assertEqual(len(out['entries']), 1)
        entry = out['entries'][0]
        self.assertEqual(entry['author'], '')
        self.assertEqual(entry['commit'], '')
        self.assertIsNone(entry['age_days'])

    def test_stats在无git时可用(self):
        self.write('a.py', '# TODO: 一条\n# FIXME: 两条\n')
        code, out = run_json(['stats', '--repo', str(self.tmp), '--ext', '.py'])
        self.assertEqual(code, 0)
        self.assertEqual(out['total'], 2)
        self.assertEqual(out['by_kind'], {'fixme': 1, 'todo': 1})
        self.assertEqual(out['by_author'], {'未归属': 2})
        self.assertEqual(out['by_age']['unknown'], 2)
        self.assertEqual(out['by_severity'], {'high': 1, 'medium': 1, 'low': 0})


if __name__ == '__main__':
    unittest.main()
