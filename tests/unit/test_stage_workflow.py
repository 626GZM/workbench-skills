"""req-stage-workflow：init/status 进度、顺序推进、越级被拒、--force、skip、变更流程、history 顺序、导出、坏 JSON。"""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'lib'))
sys.path.insert(0, str(ROOT / 'skills' / 'req-stage-workflow' / 'scripts'))

import stage_workflow as sw  # noqa: E402


class StageWorkflowTest(unittest.TestCase):
    """每个用例在自己的临时目录里建看板，不碰真实配置。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        # 不让开发机上的配置 stages 影响“默认七阶段”的断言
        self._orig_get = sw.cfg.get
        sw.cfg.get = lambda dotted, default=None, conf=None: None
        self.addCleanup(setattr, sw.cfg, 'get', self._orig_get)

    def tearDown(self):
        self._tmp.cleanup()

    # ---------- 工具 ----------

    def run_cmd(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = sw.main(list(args))
            except SystemExit as e:          # argparse 参数错
                code = e.code or 0
        return code, out.getvalue(), err.getvalue()

    def ok(self, *args):
        code, out, err = self.run_cmd(*args)
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def fail(self, *args):
        code, out, err = self.run_cmd(*args)
        self.assertEqual(code, 2, out)
        return json.loads(err)['error']

    def init(self, name='req', title='【REQ-101】订单导出优化', stages=None):
        d = str(self.tmp / name)
        args = ['init', '--dir', d, '--title', title]
        if stages:
            args += ['--stages', stages]
        return d, self.ok(*args)

    def read(self, d):
        return json.loads((Path(d) / 'workflow.json').read_text(encoding='utf-8'))

    # ---------- init / status ----------

    def test_init_默认阶段和初始状态(self):
        d, out = self.init()
        self.assertTrue((Path(d) / 'workflow.json').is_file())
        self.assertEqual(out['stages'], sw.DEFAULT_STAGES)
        data = self.read(d)
        self.assertEqual(list(data['stages']), sw.DEFAULT_STAGES)
        self.assertTrue(all(v == '未开始' for v in data['stages'].values()))
        self.assertEqual(data['changes'], [])

        status = self.ok('status', '--dir', d)
        self.assertEqual(status['current'], '需求分析')
        self.assertEqual(status['progress'], 0)
        self.assertEqual(status['counts']['pending'], 7)
        self.assertEqual(status['next']['action'], '开始')
        self.assertIn('advance', status['next']['command'])

    def test_init_自定义阶段(self):
        d, out = self.init('custom', stages='分析,实现,验证，上线')
        self.assertEqual(out['stages'], ['分析', '实现', '验证', '上线'])
        self.assertEqual(list(self.read(d)['stages']), ['分析', '实现', '验证', '上线'])
        self.assertEqual(self.ok('status', '--dir', d)['current'], '分析')

    def test_init_重复建看板报错(self):
        d, _ = self.init()
        self.assertIn('已存在', self.fail('init', '--dir', d, '--title', '另一个'))

    def test_init_配置里的stages生效(self):
        sw.cfg.get = lambda dotted, default=None, conf=None: (
            ['分析', '实现'] if dotted == 'stages' else default)
        d, out = self.init('conf')
        self.assertEqual(out['stages'], ['分析', '实现'])

    def test_status_进度随阶段推进增长(self):
        d, _ = self.init()
        self.assertEqual(self.ok('status', '--dir', d)['progress'], 0)

        self.ok('advance', '--dir', d)
        self.assertEqual(self.ok('status', '--dir', d)['progress'], 7)

        self.ok('advance', '--dir', d)
        self.assertEqual(self.ok('status', '--dir', d)['progress'], 21)

        for _ in range(6):                      # 还剩 6 个阶段要逐个确认
            self.ok('advance', '--dir', d)
        final = self.ok('status', '--dir', d)
        self.assertEqual(final['progress'], 100)
        self.assertEqual(final['counts']['done'], 7)
        self.assertIsNone(final['current'])
        self.assertIsNone(final['next']['stage'])

    # ---------- advance ----------

    def test_advance_按顺序一格格走(self):
        d, _ = self.init('顺序')
        out = self.ok('advance', '--dir', d)
        self.assertEqual((out['stage'], out['from'], out['to']), ('需求分析', '未开始', '进行中'))
        self.assertIsNone(out['started'])
        self.assertEqual(out['current'], '需求分析')

        out = self.ok('advance', '--dir', d, '--note', '需求已对齐')
        self.assertEqual((out['stage'], out['from'], out['to']), ('需求分析', '进行中', '已确认'))
        self.assertEqual(out['started'], '方案设计')
        self.assertEqual(out['current'], '方案设计')
        self.assertEqual(self.read(d)['stages']['方案设计'], '进行中')
        self.assertEqual(out['history_entry']['note'], '需求已对齐')

    def test_advance_越级被拒(self):
        d, _ = self.init('越级')
        self.assertIn('只能推进当前阶段', self.fail(
            'advance', '--dir', d, '--stage', '开发', '--to', '已确认'))
        self.assertEqual(self.read(d)['stages']['开发'], '未开始')

    def test_advance_前面没结束不能确认(self):
        d, _ = self.init('前面')
        self.ok('advance', '--dir', d)          # 需求分析 进行中
        # 指定更后面的阶段：先被顺序规则挡住
        self.assertIn('只能推进当前阶段', self.fail(
            'advance', '--dir', d, '--stage', '方案设计', '--to', '已确认'))
        self.assertEqual(self.read(d)['stages']['需求分析'], '进行中')

    def test_advance_force_允许跳级(self):
        d, _ = self.init('force')
        out = self.ok('advance', '--dir', d, '--stage', '开发', '--to', '已确认', '--force')
        self.assertEqual(out['stages'], [{'name': '需求分析', 'status': '未开始'},
                                         {'name': '方案设计', 'status': '未开始'},
                                         {'name': '开发', 'status': '已确认'},
                                         {'name': '自测', 'status': '未开始'},
                                         {'name': '联调', 'status': '未开始'},
                                         {'name': '测试', 'status': '未开始'},
                                         {'name': '上线', 'status': '未开始'}])
        self.assertEqual(out['current'], '需求分析')
        self.assertEqual(out['history_entry']['to'], '已确认')

    def test_advance_已结束或非法状态被拒(self):
        d, _ = self.init('非法')
        self.assertIn('只能改成', self.fail('advance', '--dir', d, '--to', '已跳过'))
        self.assertIn('未知阶段', self.fail('advance', '--dir', d, '--stage', '不存在'))
        self.ok('advance', '--dir', d)
        self.ok('advance', '--dir', d)         # 需求分析 已确认
        self.assertIn('已经结束', self.fail('advance', '--dir', d, '--stage', '需求分析'))

    # ---------- skip ----------

    def test_skip_跳过当前阶段并自动开始下一个(self):
        d, _ = self.init('skip')
        out = self.ok('skip', '--dir', d, '--stage', '需求分析', '--reason', '由业务方直接确认')
        self.assertEqual((out['stage'], out['from'], out['to']), ('需求分析', '未开始', '已跳过'))
        self.assertEqual(out['started'], '方案设计')
        self.assertEqual(out['current'], '方案设计')
        self.assertEqual(out['counts']['skipped'], 1)
        self.assertEqual(out['history_entry']['note'], '由业务方直接确认')

    def test_skip_只能跳当前阶段(self):
        d, _ = self.init('skip2')
        self.assertIn('只能跳过当前阶段', self.fail(
            'skip', '--dir', d, '--stage', '开发', '--reason', 'x'))
        self.assertIn('未知阶段', self.fail(
            'skip', '--dir', d, '--stage', '不存在', '--reason', 'x'))

    # ---------- change ----------

    def test_change_登记与完成(self):
        d, _ = self.init('change')
        out = self.ok('change', 'add', '--dir', d, '--title', '新增导出模板')
        self.assertEqual(out['change_id'], 'CH-01')
        self.assertEqual(out['change']['status'], '待分析')

        out = self.ok('change', 'add', '--dir', d, '--title', '导出字段增加备注')
        self.assertEqual(out['change_id'], 'CH-02')
        self.assertEqual(self.ok('status', '--dir', d)['changes'], {'total': 2, 'open': 2})

        out = self.ok('change', 'done', '--dir', d, '--id', 'CH-01', '--summary', '模板配置页已上线')
        self.assertEqual(out['change']['status'], '已处理')
        self.assertEqual(out['change']['summary'], '模板配置页已上线')
        self.assertTrue(out['change']['done_at'])
        self.assertEqual(self.ok('status', '--dir', d)['changes'], {'total': 2, 'open': 1})

        self.assertIn('已经处理过', self.fail('change', 'done', '--dir', d, '--id', 'CH-01'))
        self.assertIn('没有编号', self.fail('change', 'done', '--dir', d, '--id', 'CH-99'))

    def test_change_编号继续自增(self):
        d, _ = self.init('change2')
        self.ok('change', 'add', '--dir', d, '--title', 'A')
        self.ok('change', 'add', '--dir', d, '--title', 'B')
        out = self.ok('change', 'add', '--dir', d, '--title', 'C')
        self.assertEqual(out['change_id'], 'CH-03')

    # ---------- history ----------

    def test_history_按时间顺序且limit取最近(self):
        d, _ = self.init('hist')
        self.ok('advance', '--dir', d)
        self.ok('advance', '--dir', d)
        self.ok('advance', '--dir', d)

        out = self.ok('history', '--dir', d)
        self.assertEqual(out['total'], 4)       # init + 3 次 advance
        actions = [h['action'] for h in out['history']]
        self.assertEqual(actions, ['init', 'stage', 'stage', 'stage'])
        self.assertEqual(out['history'][-1]['stage'], '方案设计')
        self.assertEqual(out['history'][-1]['to'], '已确认')

        tail = self.ok('history', '--dir', d, '--limit', '2')
        self.assertEqual(tail['count'], 2)
        self.assertEqual([h['stage'] for h in tail['history']], ['需求分析', '方案设计'])

    # ---------- export ----------

    def test_export_markdown(self):
        d, _ = self.init('export')
        self.ok('advance', '--dir', d, '--note', '开始分析')
        self.ok('change', 'add', '--dir', d, '--title', '新增导出模板')
        out_file = self.tmp / 'board.md'
        out = self.ok('export', '--dir', d, '--out', str(out_file))

        self.assertTrue(out_file.is_file())
        self.assertEqual(out['bytes'], out_file.stat().st_size)
        text = out_file.read_text(encoding='utf-8')
        self.assertEqual(text, out['markdown'])
        self.assertIn('# 【REQ-101】订单导出优化', text)
        self.assertIn('## 阶段', text)
        self.assertIn('| 需求分析 | 进行中 |', text)
        self.assertIn('进度：', text)
        self.assertIn('CH-01', text)
        self.assertIn('开始分析', text)

    # ---------- 坏数据 ----------

    def test_坏json报错(self):
        d = str(self.tmp / 'bad')
        Path(d).mkdir(parents=True)
        (Path(d) / 'workflow.json').write_text('{ 不是 json', encoding='utf-8')
        self.assertIn('不是合法 JSON', self.fail('status', '--dir', d))

    def test_结构或状态非法报错(self):
        d = str(self.tmp / 'bad2')
        Path(d).mkdir(parents=True)
        (Path(d) / 'workflow.json').write_text(
            json.dumps({'title': 'x', 'stages': {'开发': '做完了'}}, ensure_ascii=False),
            encoding='utf-8')
        self.assertIn('不在', self.fail('status', '--dir', d))

        (Path(d) / 'workflow.json').write_text(json.dumps({'title': 'x'}), encoding='utf-8')
        self.assertIn('stages', self.fail('status', '--dir', d))

    def test_目录里没有看板报错(self):
        self.assertIn('不存在', self.fail('status', '--dir', str(self.tmp / 'empty')))


if __name__ == '__main__':
    unittest.main()
