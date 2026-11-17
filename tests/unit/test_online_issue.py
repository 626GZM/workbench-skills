"""online-issue-investigate：告警解析、问题编号递增、investigate 全流程（假查询命令 + 假模型）、报告结构。"""
import json
import re
import shlex
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'lib'))
sys.path.insert(0, str(ROOT / 'skills' / 'online-issue-investigate' / 'scripts'))

import online_issue as oi  # noqa: E402

TRACE = '0123456789abcdef0123456789abcdef'

ALERT = '''2026-11-17 10:00:00 [ERROR] [%s] [http-nio-8080-exec-1] [order-service] [com.example.OrderService] - 下单失败，用户手机 13800138000
java.lang.IllegalStateException: coupon invalid
\tat com.example.OrderService.create(OrderService.java:12)
生产环境告警：order-service 下单接口报错
''' % TRACE


class ParseAlertTest(unittest.TestCase):
    def test_抠出环境_trace_服务_时间_异常(self):
        out = oi.parse_alert(ALERT, ['order-service', 'payment-service'])
        self.assertEqual(out['env'], 'prod')
        self.assertEqual(out['trace'], TRACE)
        self.assertIn('order-service', out['services'])
        self.assertEqual(out['services'], ['order-service'])       # 候选里只挑出现的
        self.assertEqual(out['time'], '2026-11-17 10:00:00')
        self.assertIn('IllegalStateException', out['exceptions'])
        self.assertEqual(out['keywords'][0], 'IllegalStateException')
        self.assertTrue(out['end'])

    def test_非候选服务不认(self):
        out = oi.parse_alert(ALERT, ['payment-service'])
        self.assertNotIn('order-service', out['services'])
        self.assertEqual(out['services'], [])

    def test_没有线索时留空(self):
        out = oi.parse_alert('服务抖了一下', [])
        self.assertEqual(out['trace'], '')
        self.assertEqual(out['exceptions'], [])
        self.assertEqual(out['services'], [])

    def test_关键词兜底(self):
        out = oi.parse_alert('ERROR 连接池获取连接超时，重试 3 次仍失败', [])
        self.assertTrue(out['keywords'])
        self.assertIn('超时', ' '.join(out['keywords']))


class CreateIssueTest(unittest.TestCase):
    def test_编号递增和字段(self):
        with tempfile.TemporaryDirectory() as d:
            a = oi.create_issue(d, title='下单报错', alert_text=ALERT, services=['order-service'])
            b = oi.create_issue(d, title='支付超时', alert_text='', env='uat')
            self.assertRegex(a['id'], r'^OL-\d{8}-01$')
            self.assertRegex(b['id'], r'^OL-\d{8}-02$')
            self.assertEqual(b['env'], 'uat')
            data = json.loads((Path(d) / a['id'] / 'issue.json').read_text(encoding='utf-8'))
            for key in ('id', 'title', 'env', 'status', 'created_at', 'trace', 'services',
                        'keywords', 'detail', 'alert_file', 'report', 'log'):
                self.assertIn(key, data)
            self.assertEqual(data['status'], '待排查')
            self.assertEqual(data['trace'], TRACE)
            self.assertEqual(data['alert_file'], '告警原文.txt')

    def test_告警原文落盘前打码(self):
        with tempfile.TemporaryDirectory() as d:
            issue = oi.create_issue(d, alert_text=ALERT, services=['order-service'])
            saved = (Path(d) / issue['id'] / '告警原文.txt').read_text(encoding='utf-8')
            self.assertNotIn('13800138000', saved)
            self.assertIn('138****8000', saved)

    def test_环境不合法报错(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                oi.create_issue(d, title='x', env='生产')


class InvestigateTest(unittest.TestCase):
    """假 query_cmd（cat 日志文件）+ 假 runner，走一遍完整排查并检查落盘。"""

    def _setup(self, d):
        base = Path(d)
        code = base / 'order-service'
        code.mkdir()
        src = ['package com.example;', ''] + [
            '// 第 %d 行，凑够上下文' % i for i in range(1, 20)]
        src[11] = '        if (coupon == null) { throw new IllegalStateException("coupon invalid"); }'
        (code / 'OrderService.java').write_text('\n'.join(src) + '\n', encoding='utf-8')

        log = base / 'app.log'
        log.write_text(ALERT + '\n\tat com.example.OrderService.create(OrderService.java:12)\n',
                       encoding='utf-8')
        conf = {'logs': {'query_cmd': 'cat ' + shlex.quote(str(log))},
                'services': ['order-service']}
        issue = oi.create_issue(base / 'issues', title='下单接口 500', alert_text=ALERT,
                                services=['order-service'], conf=conf)
        return conf, issue, code

    def test_全流程生成三份文件并写回结论(self):
        with tempfile.TemporaryDirectory() as d:
            conf, issue, code = self._setup(d)
            seen = {}

            def fake_runner(engine, prompt, **kwargs):
                seen['engine'] = engine
                seen['prompt'] = prompt
                seen['dirs'] = kwargs.get('dirs')
                body = {'summary': '优惠券为空时未校验，抛了 IllegalStateException',
                        'confirmed': '日志里 10:00 开始持续报，是同一个问题',
                        'cause': 'OrderService#create 没判空', 'evidence': ['日志 10:00:00 ERROR', 'OrderService.java:12'],
                        'impact': '下单接口全部 500，约 12 次/分钟', 'need_code_change': True,
                        'fix_plan': '在 create 入口补 coupon 判空，返回业务错误码', 'files': ['order-service/OrderService.java#create'],
                        'workaround': '先在下单入口关闭优惠券活动开关', 'data_fix_sql': '', 'confidence': '高'}
                return {'ok': True, 'engine': engine, 'text': '```json\n' + json.dumps(body, ensure_ascii=False) + '\n```'}

            out = oi.investigate(issue['id'], root=Path(d) / 'issues', conf=conf, engine='claude',
                                 runner=fake_runner, code=[str(code)], base='')

            self.assertEqual(out['status'], '待处理')
            self.assertFalse(out.get('error'))
            self.assertEqual(out['report']['summary'], '优惠券为空时未校验，抛了 IllegalStateException')
            self.assertTrue(out['report']['need_code_change'])
            self.assertEqual(out['report']['engine'], 'claude')
            self.assertGreaterEqual(out['stats']['errors'], 1)

            issue_dir = Path(d) / 'issues' / issue['id']
            for name in ('日志摘要.md', '堆栈代码.md', '排查报告.md', '告警原文.txt'):
                self.assertTrue((issue_dir / name).is_file(), name)

            summary = (issue_dir / '日志摘要.md').read_text(encoding='utf-8')
            self.assertIn('报错归类', summary)
            self.assertNotIn('13800138000', summary)

            stack = (issue_dir / '堆栈代码.md').read_text(encoding='utf-8')
            self.assertIn('OrderService.java:12', stack)

            report = (issue_dir / '排查报告.md').read_text(encoding='utf-8')
            self.assertIn('## 根因', report)
            self.assertIn('优惠券为空时未校验', report)

            self.assertIn('只读', seen['prompt'])
            self.assertIn('堆栈对应的代码', seen['prompt'])
            self.assertIn('日志摘要', seen['prompt'])
            self.assertIn(str(code), seen['dirs'])

            saved = json.loads((issue_dir / 'issue.json').read_text(encoding='utf-8'))
            self.assertEqual(saved['status'], '待处理')
            self.assertEqual(saved['report']['cause'], 'OrderService#create 没判空')

    def test_模型输出不是JSON时给出明确错误(self):
        with tempfile.TemporaryDirectory() as d:
            conf, issue, code = self._setup(d)

            def bad_runner(engine, prompt, **kwargs):
                return {'ok': True, 'engine': engine, 'text': '我觉得是缓存问题'}

            out = oi.investigate(issue['id'], root=Path(d) / 'issues', conf=conf,
                                 engine='claude', runner=bad_runner, code=[str(code)], base='')
            self.assertEqual(out['status'], '待排查')
            self.assertIn('JSON', out['error'])

    def test_模型失败时不写报告(self):
        with tempfile.TemporaryDirectory() as d:
            conf, issue, code = self._setup(d)

            def timeout_runner(engine, prompt, **kwargs):
                return {'ok': False, 'engine': engine, 'error': '超时（2400 秒）'}

            out = oi.investigate(issue['id'], root=Path(d) / 'issues', conf=conf,
                                 engine='claude', runner=timeout_runner, code=[str(code)], base='')
            self.assertEqual(out['status'], '待排查')
            self.assertIn('超时', out['error'])
            self.assertFalse((Path(d) / 'issues' / issue['id'] / '排查报告.md').exists())


class ReportTest(unittest.TestCase):
    def test_markdown结构(self):
        issue = {'id': 'OL-20260101-01', 'title': '下单接口 500', 'env': 'prod',
                 'created_at': '2026-01-01T10:00:00', 'stats': {'errors': 12}}
        report = {'summary': '一句话结论', 'confirmed': '现象', 'cause': '根因',
                  'evidence': ['日志 10:00', 'OrderService.java:12'], 'impact': '影响下单',
                  'need_code_change': True, 'fix_plan': '补判空',
                  'files': ['order-service/OrderService.java#create'],
                  'workaround': '关掉活动开关', 'data_fix_sql': 'update t set x=1 where id=2;',
                  'confidence': '高', 'engine': 'claude', 'at': '2026-01-01T10:05:00'}
        md = oi.report_md(issue, report)
        for section in ('## 结论', '## 现象确认', '## 根因', '## 证据', '## 影响范围',
                        '## 修复意见（要改代码）', '## 止血建议', '## 数据修复 SQL'):
            self.assertIn(section, md)
        self.assertIn('- 日志 10:00', md)
        self.assertIn('- order-service/OrderService.java#create', md)
        self.assertIn('```sql', md)

    def test_不用改代码的措辞(self):
        issue = {'id': 'OL-20260101-02', 'title': 'x', 'env': 'uat'}
        report = {'summary': 's', 'cause': 'c', 'need_code_change': False, 'confidence': '中'}
        md = oi.report_md(issue, report)
        self.assertIn('## 修复意见（不用改代码）', md)
        self.assertNotIn('```sql', md)


class ParseJsonTest(unittest.TestCase):
    def test_代码块包裹也能解析(self):
        self.assertEqual(oi.parse_json('说明\n```json\n{"a": 1}\n```')['a'], 1)

    def test_没有JSON报错(self):
        with self.assertRaises(ValueError):
            oi.parse_json('纯文本')


class ListShowTest(unittest.TestCase):
    def test_list按时间倒序(self):
        with tempfile.TemporaryDirectory() as d:
            oi.create_issue(d, title='先', env='prod')
            oi.create_issue(d, title='后', env='prod')
            items = oi.list_issues(d)
            self.assertEqual([x['title'] for x in items], ['后', '先'])
            self.assertTrue(re.match(r'^OL-\d{8}-02$', items[0]['id']))


if __name__ == '__main__':
    unittest.main()
