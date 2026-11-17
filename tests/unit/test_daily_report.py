"""daily-report：明细生成、brief 规则回退、空日期、revise 缺文本报错。"""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'lib'))
sys.path.insert(0, str(ROOT / 'skills' / 'daily-report' / 'scripts'))

import daily_report as dr  # noqa: E402


def make_task(root, name, meta, rows):
    d = Path(root) / name
    d.mkdir(parents=True)
    (d / 'meta.json').write_text(json.dumps(meta, ensure_ascii=False), encoding='utf-8')
    (d / 'timeline.jsonl').write_text(
        '\n'.join(json.dumps(r, ensure_ascii=False) for r in rows) + '\n', encoding='utf-8')


def run_main(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = dr.main(argv)
    return code, out.getvalue(), err.getvalue()


class DetailTest(unittest.TestCase):
    def test_明细按需求分组且只取当天(self):
        with tempfile.TemporaryDirectory() as d:
            make_task(d, 'task-a',
                      {'title': '【REQ-101】订单导出优化', 'stage': '开发',
                       'modules': ['导出任务', '模板配置'], 'progress': {'开发': 80}},
                      [{'at': '2026-11-17T10:00:00', 'role': '写代码-T1', 'ok': True,
                        'note': '完成导出任务列表接口'},
                       {'at': '2026-11-17T11:00:00', 'ok': True, 'note': '本地 e2e 联调通过'},
                       {'at': '2026-11-17T12:00:00', 'ok': False, 'note': '失败重试'},
                       {'at': '2026-11-16T10:00:00', 'ok': True, 'note': '昨天做的事'}])
            make_task(d, 'task-b',
                      {'title': '【ISSUE-123】导出失败排查', 'stage': '开发',
                       'progress': {'开发': 90}},
                      [{'at': '2026-11-17T11:30:00', 'ok': True, 'note': '修复导出超时问题'}])

            code, out, _ = run_main(['build', '--workspace', d, '--date', '2026-11-17'])
            self.assertEqual(code, 0)
            data = json.loads(out)
            self.assertEqual(data['date'], '2026-11-17')
            self.assertEqual(data['source'], 'rule')
            self.assertIsNone(data['out'])
            md = data['markdown']
            self.assertIn('**REQ-101 订单导出优化**', md)
            self.assertIn('**ISSUE-123 导出失败排查**', md)
            self.assertIn('当前进度：开发 进行中 80%', md)
            self.assertIn('完成导出任务列表接口', md)
            self.assertNotIn('昨天做的事', md)
            self.assertNotIn('失败重试', md)
            self.assertIn('## 明日计划', md)
            self.assertIn('继续开发', md)
            self.assertNotIn('e2e', md.lower())
            self.assertNotIn('dev', md.lower())


class BriefRuleFallbackTest(unittest.TestCase):
    def _workspace(self, d, count=6):
        notes = ['完成导出任务列表接口', '完成导出任务详情接口', '完成模板字段校验',
                 '完成模板保存逻辑', '完成导出任务补偿逻辑', '完成导出任务日志记录'][:count]
        rows = [{'at': '2026-11-17T0%d:00:00' % (i + 1), 'ok': True, 'note': n}
                for i, n in enumerate(notes)]
        make_task(d, 'task-a', {'title': '【REQ-101】订单导出优化',
                                'modules': ['导出任务', '模板配置'],
                                'stage': '开发', 'progress': {'开发': 80}}, rows)

    def test_模型失败回退规则版(self):
        with tempfile.TemporaryDirectory() as d:
            self._workspace(d)
            with mock.patch.object(dr.agent, 'run',
                                   return_value={'ok': False, 'error': '没有找到 kimi 命令'}):
                code, out, _ = run_main(['build', '--workspace', d, '--date', '2026-11-17',
                                         '--style', 'brief', '--n', '6', '--engine', 'kimi'])
            self.assertEqual(code, 0)
            data = json.loads(out)
            self.assertEqual(data['source'], 'rule')
            self.assertEqual(len(data['lines']), 6)
            self.assertTrue(data['lines'][0].startswith('1. [REQ-101 订单导出优化] '))
            self.assertIn('进度：进行中 80%', data['lines'][0])
            self.assertNotIn('e2e', '\n'.join(data['lines']).lower())

    def test_模型可用走AI(self):
        with tempfile.TemporaryDirectory() as d:
            self._workspace(d, count=2)
            reply = ('1. [REQ-101 订单导出优化] 导出任务：完成列表、详情两个接口，进度：已完成\n'
                     '2. [REQ-101 订单导出优化] 模板配置：完成字段校验和保存，进度：进行中 80%')
            with mock.patch.object(dr.agent, 'run', return_value={'ok': True, 'text': reply}):
                code, out, _ = run_main(['build', '--workspace', d, '--date', '2026-11-17',
                                         '--style', 'brief', '--n', '2'])
            self.assertEqual(code, 0)
            data = json.loads(out)
            self.assertEqual(data['source'], 'ai')
            self.assertEqual(len(data['lines']), 2)


class EmptyDayTest(unittest.TestCase):
    def test_没有记录的一天不编内容(self):
        with tempfile.TemporaryDirectory() as d:
            make_task(d, 'task-a', {'title': '【REQ-101】订单导出优化', 'stage': '开发',
                                    'progress': {'开发': 80}},
                      [{'at': '2026-11-16T10:00:00', 'ok': True, 'note': '完成导出任务列表接口'}])

            code, out, _ = run_main(['build', '--workspace', d, '--date', '2026-11-17',
                                     '--style', 'brief'])
            self.assertEqual(code, 0)
            data = json.loads(out)
            self.assertEqual(data['source'], 'none')
            self.assertEqual(data['lines'], [])
            self.assertIn('这一天没有记录到进展', data['message'])

            code, out, _ = run_main(['build', '--workspace', d, '--date', '2026-11-17'])
            self.assertEqual(code, 0)
            self.assertIn('这一天没有记录到进展', json.loads(out)['markdown'])


class ReviseErrorTest(unittest.TestCase):
    def test_缺文本文件报错(self):
        with tempfile.TemporaryDirectory() as d:
            missing = str(Path(d) / 'nope.md')
            code, out, err = run_main(['revise', '--text', missing, '--ask', '把第 2 条写具体'])
            self.assertEqual(code, 2)
            self.assertEqual(out.strip(), '')
            self.assertIn('不存在', err)


class RulesTest(unittest.TestCase):
    def test_固定要求增删查(self):
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / 'rules.json')
            code, out, _ = run_main(['rules', 'add', '--text', '每条都要带进度', '--rules', path])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)['rules'], ['每条都要带进度'])

            run_main(['rules', 'add', '--text', '不要写文件名', '--rules', path])
            code, out, _ = run_main(['rules', 'drop', '--index', '0', '--rules', path])
            self.assertEqual(json.loads(out)['rules'], ['不要写文件名'])
            self.assertTrue(dr.read_rules(path))


if __name__ == '__main__':
    unittest.main()
