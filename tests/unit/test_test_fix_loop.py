"""test-fix-loop 的解析与循环行为（不调真实模型）。"""
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / 'skills' / 'test-fix-loop' / 'scripts' / 'test_fix_loop.py'


def load():
    spec = importlib.util.spec_from_file_location('test_fix_loop', SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules['test_fix_loop'] = mod
    spec.loader.exec_module(mod)
    return mod


tfl = load()


def run_cli(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = tfl.main(argv)
    return code, json.loads(buf.getvalue())


class ParseTest(unittest.TestCase):
    def test_认出_maven_失败(self):
        text = ('[INFO] Running com.demo.FooTest\n'
                '[ERROR] Tests run: 3, Failures: 1, Errors: 0, Skipped: 0\n'
                '[ERROR] com.demo.FooTest.testBar:42 expected:<1> but was:<2>\n')
        f = tfl.parse_failures(text)
        self.assertFalse(f['empty'])
        self.assertIn('Tests run: 3', f['summary'])
        self.assertTrue(any('testBar' in x for x in f['lines']))

    def test_认出_pytest_失败(self):
        text = 'FAILED tests/test_api.py::test_x - AssertionError: assert 1 == 2\n1 failed, 4 passed\n'
        f = tfl.parse_failures(text)
        self.assertTrue(any('test_x' in x for x in f['lines']))

    def test_提取文件名行号(self):
        f = tfl.parse_failures('error at src/main/java/com/demo/A.java:12 boom')
        self.assertIn('src/main/java/com/demo/A.java:12', f['signals'])

    def test_跳过堆栈噪音(self):
        f = tfl.parse_failures('    at com.demo.Foo.bar(Foo.java:10)\n... 12 more')
        self.assertTrue(f['empty'])


class LoopTest(unittest.TestCase):
    def test_通过时一轮结束(self):
        with tempfile.TemporaryDirectory() as d:
            code, out = run_cli(['run', '--cmd', '%s -c "print(1)"' % sys.executable,
                                 '--cwd', d, '--dry-run'])
        self.assertEqual(code, 0)
        self.assertTrue(out['ok'])
        self.assertEqual(len(out['rounds']), 1)

    def test_失败时不调模型(self):
        cmd = '%s -c "print(\'Tests run: 1, Failures: 1\'); raise SystemExit(1)"' % sys.executable
        with tempfile.TemporaryDirectory() as d:
            code, out = run_cli(['run', '--cmd', cmd, '--cwd', d, '--dry-run'])
        self.assertEqual(code, 1)
        self.assertFalse(out['ok'])
        self.assertNotIn('agent', out['rounds'][0])
        self.assertIn('试运行', out['reason'])

    def test_轮次上限(self):
        cmd = '%s -c "raise SystemExit(1)"' % sys.executable
        with tempfile.TemporaryDirectory() as d:
            code, out = run_cli(['run', '--cmd', cmd, '--cwd', d, '--dry-run',
                                 '--max-rounds', '99'])
        self.assertEqual(code, 1)
        self.assertLessEqual(len(out['rounds']), tfl.MAX_ROUNDS)

    def test_工作目录不存在(self):
        with self.assertRaises(SystemExit):
            run_cli(['run', '--cmd', 'true', '--cwd', '/no/such/dir'])


class PromptTest(unittest.TestCase):
    def test_测试范围禁止改业务代码(self):
        f = {'lines': ['[ERROR] Tests run: 1'], 'summary': '[ERROR] Tests run: 1'}
        p = tfl.fix_prompt('mvn test', f, 'test', 1, '')
        self.assertIn('只能改测试代码', p)

    def test_any_范围允许改业务代码(self):
        f = {'lines': ['x'], 'summary': 'x'}
        p = tfl.fix_prompt('mvn test', f, 'any', 2, '上次改错了')
        self.assertIn('也可以改被测代码', p)
        self.assertIn('上次改错了', p)


if __name__ == '__main__':
    unittest.main()
