"""test-issue-triage：规则拆分、编号递增、只读排查写回、fix --dry-run 不真跑、归属回落。"""
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'lib'))
sys.path.insert(0, str(ROOT / 'skills' / 'test-issue-triage' / 'scripts'))

import test_issue as ti  # noqa: E402


class SplitTextTest(unittest.TestCase):
    """规则拆分：编号版、空行版、开场白丢弃、去重、抽接口。"""

    def test_编号版拆成多条并丢掉开场白(self):
        text = ('测试反馈：\n'
                '1. 导出订单接口报 500\n参数是 ids=[]\n'
                '2、列表页金额不显示\n'
                '3）导出按钮点了没反应')
        items = ti.split_text(text)
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0]['title'], '导出订单接口报 500')
        self.assertIn('ids=[]', items[0]['detail'])
        self.assertEqual(items[1]['title'], '列表页金额不显示')
        self.assertEqual(items[2]['title'], '导出按钮点了没反应')
        self.assertNotIn('测试反馈', json.dumps(items, ensure_ascii=False))

    def test_空行版拆分(self):
        text = ('登录之后首页一直转圈\n刷新一次就好\n\n'
                '订单列表翻页后顺序乱了\n第二页比第一页还旧')
        items = ti.split_text(text)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]['title'], '登录之后首页一直转圈')
        self.assertIn('刷新一次就好', items[0]['detail'])
        self.assertEqual(items[1]['title'], '订单列表翻页后顺序乱了')

    def test_重复条目去重(self):
        text = '导出按钮没反应\n\n导出按钮没反应'
        self.assertEqual(len(ti.split_text(text)), 1)

    def test_抽接口和方法路径(self):
        text = '1. 导出失败\nPOST /orders/export 返回 500\n2. 查询慢\nget /orders/list 超过 10 秒'
        items = ti.split_text(text)
        self.assertEqual(items[0]['api'], 'POST /orders/export')
        self.assertEqual(items[1]['api'], 'GET /orders/list')


class AddIssueTest(unittest.TestCase):
    """add：编号按来源前缀递增，落盘字段齐全。"""

    def test_编号递增和字段(self):
        with tempfile.TemporaryDirectory() as d:
            store = Path(d) / 'issues.json'
            a = ti.add_issue(store, '导出订单接口报 500', detail='ids=[] 时必现',
                             api='POST /orders/export', source='测试')
            b = ti.add_issue(store, '列表不显示金额', source='测试')
            c = ti.add_issue(store, '前端拿不到金额', source='联调')
            self.assertEqual([a['id'], b['id'], c['id']], ['ISSUE-01', 'ISSUE-02', 'FE-01'])
            self.assertEqual(a['status'], '待处理')
            self.assertEqual(a['verdict'], '')
            data = json.loads(store.read_text(encoding='utf-8'))
            for key in ('id', 'source', 'title', 'detail', 'api', 'status',
                        'verdict', 'cause', 'plan', 'reply', 'log'):
                self.assertIn(key, data['issues'][0])

    def test_来源不合法报错(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                ti.add_issue(Path(d) / 'issues.json', '随便一条', source='别的')


class DiagnoseTest(unittest.TestCase):
    """diagnose：假 runner 不调真模型，verdict 写回；不在枚举里回落 need_info。"""

    def _setup(self, d):
        store = Path(d) / 'issues.json'
        issue = ti.add_issue(store, '导出订单接口报 500', detail='ids=[] 时必现',
                             api='POST /orders/export', source='测试')
        return store, issue['id']

    def test_假runner写回归属根因和回复(self):
        with tempfile.TemporaryDirectory() as d:
            store, iid = self._setup(d)
            payload = {'verdict': 'backend', 'cause': '导出时没校验空列表',
                       'plan': 'OrderService#export 加空列表校验，回归测 ids=[]',
                       'files': ['order-service/src/OrderService.java#export'],
                       'reply': '是后端的问题，空列表没挡住，已经定位。'}

            def fake(engine, prompt, cwd, **kw):
                self.assertEqual(kw.get('profile'), 'read')
                self.assertIn('**只读**', prompt)
                self.assertIn(iid, prompt)
                return {'ok': True, 'text': json.dumps(payload, ensure_ascii=False)}

            issue = ti.diagnose(store, iid, repos=[d], runner=fake)
            self.assertEqual(issue['verdict'], 'backend')
            self.assertIn('空列表', issue['cause'])
            self.assertEqual(issue['reply'], payload['reply'])
            self.assertEqual(issue['files'], payload['files'])
            saved = json.loads(store.read_text(encoding='utf-8'))['issues'][0]
            self.assertEqual(saved['verdict'], 'backend')
            self.assertEqual(saved['log'][-1]['action'], 'diagnose')

    def test_verdict不在枚举时回落need_info(self):
        with tempfile.TemporaryDirectory() as d:
            store, iid = self._setup(d)

            def fake(engine, prompt, cwd, **kw):
                return {'ok': True, 'text': '{"verdict": "乱写的", "cause": "c", "plan": "p", "reply": "r"}'}

            issue = ti.diagnose(store, iid, runner=fake)
            self.assertEqual(issue['verdict'], 'need_info')


class FixDryRunTest(unittest.TestCase):
    """fix --dry-run 只打印代理参数，不调模型、不改状态、不动仓库。"""

    def test_dry_run不真跑(self):
        with tempfile.TemporaryDirectory() as d:
            store = Path(d) / 'issues.json'
            repo = Path(d) / 'repo'
            repo.mkdir()
            iid = ti.add_issue(store, '导出订单接口报 500', source='测试')['id']
            buf = io.StringIO()
            with mock.patch.object(ti.agent, 'run', side_effect=AssertionError('不应调用模型')):
                with redirect_stdout(buf):
                    code = ti.main(['fix', '--store', str(store), '--id', iid,
                                    '--repo', str(repo), '--engine', 'kimi', '--dry-run'])
            self.assertEqual(code, 0)
            out = json.loads(buf.getvalue())
            self.assertTrue(out['dry_run'])
            self.assertEqual(out['profile'], 'coder')
            self.assertIn(iid, out['prompt'])
            self.assertEqual(list(repo.iterdir()), [])          # 仓库没被写
            saved = json.loads(store.read_text(encoding='utf-8'))['issues'][0]
            self.assertEqual(saved['status'], '待处理')          # 状态没动
            self.assertEqual(saved['verdict'], '')


class StatusTest(unittest.TestCase):
    """status：只能设成约定的状态。"""

    def test_改状态(self):
        with tempfile.TemporaryDirectory() as d:
            store = Path(d) / 'issues.json'
            iid = ti.add_issue(store, '导出订单接口报 500', source='测试')['id']
            issue = ti.set_status(store, iid, '待验证')
            self.assertEqual(issue['status'], '待验证')
            with self.assertRaises(ValueError):
                ti.set_status(store, iid, '随便')


if __name__ == '__main__':
    unittest.main()
