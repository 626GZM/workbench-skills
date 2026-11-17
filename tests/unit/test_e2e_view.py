"""e2e-artifact-view：造假 run.json，测运行排序过滤、详情、用例过滤、汇总、打码、账号脱敏。"""
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'skills' / 'e2e-artifact-view' / 'scripts'))

import e2e_view as ev  # noqa: E402


def make_run(root, name, env=None, status=None, steps=None, cases=None,
             started=None, finished=None):
    """在临时根目录下造一次运行，返回运行目录。"""
    d = Path(root) / name
    d.mkdir(parents=True)
    data = {'status': status or 'passed', 'steps': steps or []}
    if env is not None:
        data['env'] = env
    if cases is not None:
        data['cases'] = cases
        data.pop('steps', None)
    if started:
        data['started_at'] = started
    if finished:
        data['finished_at'] = finished
    (d / 'run.json').write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    return d


def case(cid, title, status, error='', evidence=None):
    return {'id': cid, 'title': title, 'status': status,
            'error': error, 'evidence': evidence or []}


class RunsTest(unittest.TestCase):
    """runs：按时间新的在前、按环境过滤、limit 截断、缺 env 回落 local。"""

    def test_排序过滤和限制(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_run(tmp, '2026-11-15-090000-local', env='local', status='passed')
            make_run(tmp, '2026-11-16-100000-dev', env='dev', status='failed')
            make_run(tmp, '2026-11-17-110000-local', env='local', status='passed')
            make_run(tmp, '2026-11-14-080000', env=None, status='passed')    # 没有 env

            rows = ev.list_runs(tmp, limit=0)
            self.assertEqual([r['id'] for r in rows],
                             ['2026-11-17-110000-local', '2026-11-16-100000-dev',
                              '2026-11-15-090000-local', '2026-11-14-080000'])
            self.assertEqual(rows[-1]['env'], 'local')

            dev = ev.list_runs(tmp, env='dev')
            self.assertEqual(len(dev), 1)
            self.assertEqual(dev[0]['status'], 'failed')

            limited = ev.list_runs(tmp, limit=2)
            self.assertEqual(len(limited), 2)
            self.assertEqual(limited[0]['id'], '2026-11-17-110000-local')

    def test_cli输出的运行列表(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_run(tmp, '2026-11-17-101500-local', env='local',
                     steps=[{'name': '登录', 'cases': [case('case-01', '能登录', 'passed')]}])
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = ev.main(['runs', '--root', tmp])
            self.assertEqual(code, 0)
            out = json.loads(buf.getvalue())
            self.assertEqual(out['count'], 1)
            self.assertEqual(out['runs'][0]['counts']['passed'], 1)


class ShowTest(unittest.TestCase):
    """show：步骤、状态、失败原因、证据存在性、耗时。"""

    def test_详情和证据路径(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = make_run(
                tmp, '2026-11-17-101500-local', env='local', status='failed',
                started='2026-11-17 10:15:00', finished='2026-11-17 10:16:02',
                steps=[
                    {'name': '登录', 'status': 'passed',
                     'cases': [case('case-01', '能登录', 'passed', evidence=['snaps/login.png'])]},
                    {'name': '下单', 'cases': [
                        case('case-02', '库存不足不能下单', 'failed',
                             error='expected 400, got 200',
                             evidence=['snaps/order.png', 'logs/missing.log'])]},
                ])
            (d / 'snaps').mkdir()
            (d / 'snaps' / 'login.png').write_bytes(b'')
            (d / 'snaps' / 'order.png').write_bytes(b'')

            _, data = ev.read_run(d)
            view = ev.run_view(d, data)
            self.assertEqual(view['status'], 'failed')
            self.assertEqual(view['duration_seconds'], 62)
            self.assertEqual(view['counts'], {'total': 2, 'passed': 1, 'failed': 1,
                                              'skipped': 0, 'unknown': 0})
            self.assertEqual(view['pass_rate'], 0.5)

            steps = ev.norm_steps(d, data)
            self.assertEqual(steps[1]['name'], '下单')
            self.assertEqual(steps[1]['status'], 'failed')          # 没写状态时按用例推导
            failed = steps[1]['cases'][0]
            self.assertEqual(failed['error'], 'expected 400, got 200')
            self.assertEqual(failed['evidence'], ['snaps/order.png'])
            self.assertEqual(failed['missing_evidence'], ['logs/missing.log'])

    def test_没有run_json报错(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / '2026-11-17-101500-local'
            d.mkdir()
            with self.assertRaises(ev.ViewError):
                ev.read_run(d)


class CasesTest(unittest.TestCase):
    """cases：带步骤名，按状态过滤。"""

    def test_按状态过滤(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = make_run(tmp, '2026-11-17-101500-local', env='local', status='failed',
                         steps=[
                             {'name': '登录', 'cases': [case('case-01', '能登录', 'passed')]},
                             {'name': '下单', 'cases': [
                                 case('case-02', '正常下单', 'passed'),
                                 case('case-03', '库存不足报错', 'failed', error='断言：期望 400'),
                             ]},
                         ])
            _, data = ev.read_run(d)
            rows = [c for s in ev.norm_steps(d, data) for c in s['cases']]
            self.assertEqual(len(rows), 3)

            buf = io.StringIO()
            with redirect_stdout(buf):
                code = ev.main(['cases', '--run', str(d), '--status', 'failed'])
            self.assertEqual(code, 0)
            out = json.loads(buf.getvalue())
            self.assertEqual(out['count'], 1)
            self.assertEqual(out['cases'][0]['id'], 'case-03')
            self.assertEqual(out['cases'][0]['step'], '下单')


class SummaryTest(unittest.TestCase):
    """summary：通过率、按日趋势、失败最多的用例、失败原因归类、since 过滤。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name
        make_run(root, '2026-11-15-090000-local', env='local', status='failed',
                 started='2026-11-15 09:00:00',
                 cases=[case('case-01', '查询列表', 'passed'),
                        case('case-02', '等待按钮', 'failed', error='等待元素超时：下单按钮')])
        make_run(root, '2026-11-16-100000-dev', env='dev', status='failed',
                 started='2026-11-16 10:00:00',
                 cases=[case('case-01', '查询列表', 'passed'),
                        case('case-02', '等待按钮', 'failed', error='等待元素超时：下单按钮'),
                        case('case-03', '金额断言', 'failed', error='expected 3, got 2'),
                        case('case-04', '连接服务', 'failed', error='connect ECONNREFUSED 127.0.0.1'),
                        case('case-05', '查记录', 'failed', error='记录不存在'),
                        case('case-06', '奇怪报错', 'failed', error='something odd')])
        make_run(root, '2026-11-17-110000-local', env='local', status='passed',
                 started='2026-11-17 11:00:00',
                 cases=[case('case-01', '查询列表', 'passed'),
                        case('case-02', '等待按钮', 'passed')])

    def tearDown(self):
        self.tmp.cleanup()

    def test_通过率和趋势(self):
        now = datetime(2026, 11, 17, 23, 0, 0)
        out = ev.summarize(self.tmp.name, since='7d', top=3, now=now)
        self.assertEqual(out['runs'], 3)
        # 10 条用例：通过 4、失败 6
        self.assertEqual(out['counts']['total'], 10)
        self.assertEqual(out['counts']['passed'], 4)
        self.assertEqual(out['counts']['failed'], 6)
        self.assertEqual(out['pass_rate'], 0.4)
        self.assertEqual([t['date'] for t in out['trend']],
                         ['2026-11-15', '2026-11-16', '2026-11-17'])
        self.assertEqual(out['trend'][1]['failed'], 5)
        self.assertEqual(out['trend'][2]['pass_rate'], 1.0)

    def test_失败最多的用例和归类(self):
        now = datetime(2026, 11, 17, 23, 0, 0)
        out = ev.summarize(self.tmp.name, since='7d', top=3, now=now)
        self.assertEqual(out['top_failed_cases'][0]['id'], 'case-02')
        self.assertEqual(out['top_failed_cases'][0]['failed'], 2)
        self.assertEqual(out['top_failed_cases'][0]['last_seen'], '2026-11-16 10:00:00')
        self.assertLessEqual(len(out['top_failed_cases']), 3)

        cats = {c['category']: c['count'] for c in out['failure_categories']}
        self.assertEqual(cats['超时'], 2)
        self.assertEqual(cats['断言失败'], 1)
        self.assertEqual(cats['环境错误'], 1)
        self.assertEqual(cats['数据问题'], 1)
        self.assertEqual(cats['未知'], 1)

    def test_since过滤(self):
        now = datetime(2026, 11, 17, 23, 0, 0)
        out = ev.summarize(self.tmp.name, since='1d', top=5, now=now)
        self.assertEqual(out['runs'], 1)
        self.assertEqual(out['counts']['total'], 2)

    def test_看不懂的since报错(self):
        with self.assertRaises(ev.ViewError):
            ev.parse_since('上个月')


class MaskTest(unittest.TestCase):
    """mask：手机号、身份证、邮箱、token 都要打掉，并计数。"""

    def test_各类打码(self):
        text = ('联系电话 13812345678，证件 110101199001011234，'
                '邮箱 zhangsan@example.com，Authorization: Bearer abcdefghijkl\n'
                '"password": "s3cretpass"')
        masked, counts = ev.mask_text(text)
        for raw in ('13812345678', '110101199001011234', 'zhangsan@example.com',
                    'abcdefghijkl', 's3cretpass'):
            self.assertNotIn(raw, masked)
        self.assertIn('138****5678', masked)
        self.assertIn('110101********1234', masked)
        self.assertIn('zh***@example.com', masked)
        self.assertEqual(counts['phone'], 1)
        self.assertEqual(counts['id_card'], 1)
        self.assertEqual(counts['email'], 1)
        self.assertGreaterEqual(counts['token'], 2)

    def test_命令行输出打码文本(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / 'api.log'
            f.write_text('手机 13900001111 登录失败', encoding='utf-8')
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = ev.main(['mask', '--file', str(f)])
            self.assertEqual(code, 0)
            out = json.loads(buf.getvalue())
            self.assertTrue(out['changed'])
            self.assertIn('139****1111', out['text'])
            self.assertNotIn('13900001111', out['text'])
            self.assertEqual(f.read_text(encoding='utf-8'), '手机 13900001111 登录失败')  # 原文件没改


class AccountsTest(unittest.TestCase):
    """accounts：密码固定 ***，token 不外泄，环境归属正确。"""

    def test_脱敏视图(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / 'accounts.json'
            f.write_text(json.dumps({
                'identities': {
                    'admin': {'name': '管理端账号', 'purpose': '测管理端权限',
                              'login': {'account': 'admin01', 'password': 'p@ssw0rd'}},
                },
                'environments': {
                    'dev': {'identities': {
                        'guest': {'name': '访客账号', 'purpose': '只读校验',
                                  'token': 'Bearer topsecrettoken'},
                    }},
                },
            }, ensure_ascii=False), encoding='utf-8')

            out = ev.accounts_view(f)
            rows = {r['code']: r for r in out['identities']}
            self.assertEqual(rows['admin']['account'], 'admin01')
            self.assertEqual(rows['admin']['purpose'], '测管理端权限')
            self.assertEqual(rows['admin']['password'], '***')
            self.assertEqual(rows['admin']['envs'], ['共用'])
            self.assertEqual(rows['guest']['envs'], ['dev'])
            self.assertEqual(rows['guest']['password'], '')
            text = json.dumps(out, ensure_ascii=False)
            self.assertNotIn('p@ssw0rd', text)
            self.assertNotIn('topsecrettoken', text)

    def test_文件不存在报错(self):
        with self.assertRaises(ev.ViewError):
            ev.accounts_view('/nonexistent/accounts.json')


if __name__ == '__main__':
    unittest.main()
