"""doc-review-comments：锚点命中与未命中候选、apply 改文并备份、多意见部分失败、accept/reject、
diff、select 解析表格、--dry-run 不动文件、目标段之外改太多给 warning。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'lib'))
sys.path.insert(0, str(ROOT / 'skills' / 'doc-review-comments' / 'scripts'))

import doc_review as dr  # noqa: E402

DOC = '''# 方案设计

## 3.2 接口约定

失败后直接返回错误码，不重试。调用方需要自己判断是否继续。

## 4 待你勾选

| 编号 | 问题 | 可选方向 |
|---|---|---|
| Q1 | 重试次数怎么定 | 3 次 / 5 次 / 不重试 |
| Q2 | 超时怎么处理 | 直接失败 / 转异步 |
'''


class ReviewTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.doc = self.dir / '方案设计.md'
        self.doc.write_text(DOC, encoding='utf-8')
        self.calls = []

    def tearDown(self):
        self._tmp.cleanup()

    def fake_runner(self, old, new, how='补充重试次数', section='3.2 接口约定'):
        """按 old → new 改文档，并返回带说明的 JSON；记录调用参数。"""
        def run(engine, prompt, **kwargs):
            self.calls.append({'engine': engine, 'prompt': prompt, 'kwargs': kwargs})
            path = Path(kwargs['cwd']) / '方案设计.md'
            text = path.read_text(encoding='utf-8')
            path.write_text(text.replace(old, new), encoding='utf-8')
            return {'ok': True, 'engine': engine,
                    'text': json.dumps({'id': 'C-01', 'section': section, 'how': how},
                                       ensure_ascii=False)}
        return run

    def add_one(self, anchor='失败后直接返回错误码', text='这里没说失败重试几次'):
        return dr.add(self.dir, '方案设计.md', anchor, text, section='3.2 接口约定')


class AddTest(ReviewTestBase):
    def test_锚点命中生成编号并落盘(self):
        first = self.add_one()
        second = self.add_one('调用方需要自己判断', '调用方怎么判断没说清')

        self.assertEqual((first['id'], second['id']), ('C-01', 'C-02'))
        self.assertEqual(first['status'], 'open')
        self.assertEqual(first['resolution'], '')
        data = json.loads((self.dir / '.review' / 'comments.json').read_text(encoding='utf-8'))
        self.assertEqual(data['doc'], '方案设计.md')
        self.assertEqual([c['id'] for c in data['comments']], ['C-01', 'C-02'])
        self.assertEqual(data['comments'][0]['section'], '3.2 接口约定')

    def test_锚点只存前40字(self):
        long = '失败后直接返回错误码，不重试。调用方需要自己判断是否继续，并记录失败原因和请求标识。'
        self.doc.write_text(DOC.replace('失败后直接返回错误码，不重试。调用方需要自己判断是否继续。', long),
                            encoding='utf-8')
        rec = self.add_one(anchor=long)
        self.assertEqual(len(rec['anchor']), 40)
        self.assertTrue(long.startswith(rec['anchor']))

    def test_锚点未命中给三个候选(self):
        with self.assertRaises(dr.ReviewError) as ctx:
            self.add_one(anchor='失败后不返回错误码也不提示')
        err = ctx.exception
        self.assertIn('找不到', str(err))
        cands = err.extra['candidates']
        self.assertEqual(len(cands), 3)
        self.assertTrue(any('失败后直接返回错误码' in c for c in cands))
        # 没写进 comments.json
        self.assertFalse((self.dir / '.review' / 'comments.json').exists())

    def test_意见为空或锚点为空直接报错(self):
        with self.assertRaises(dr.ReviewError):
            dr.add(self.dir, '方案设计.md', '失败后直接返回错误码', '  ')
        with self.assertRaises(dr.ReviewError):
            dr.add(self.dir, '方案设计.md', '', '缺锚点')


class ApplyTest(ReviewTestBase):
    def test_逐条改文并备份写回说明(self):
        self.add_one()
        data = dr.apply(self.dir, '方案设计.md', engine='kimi',
                        runner=self.fake_runner('不重试', '最多重试 3 次'))

        self.assertTrue(data['ok'])
        self.assertEqual((data['applied_count'], data['failed_count']), (1, 0))
        item = data['applied'][0]
        self.assertTrue(item['ok'])
        self.assertTrue(item['changed'])
        self.assertIn('+失败后直接返回错误码，最多重试 3 次', item['diff'])
        self.assertEqual(item['resolution'], '3.2 接口约定：补充重试次数')
        self.assertEqual(item['warnings'], [])
        self.assertIn('最多重试 3 次', data['diff'])

        backup = Path(data['backup'])
        self.assertTrue(backup.is_file())
        self.assertEqual(backup.parent, (self.dir / '.review' / 'backup').resolve())
        self.assertIn('不重试', backup.read_text(encoding='utf-8'))
        self.assertIn('最多重试 3 次', self.doc.read_text(encoding='utf-8'))

        comment = dr.list_comments(self.dir)['comments'][0]
        self.assertEqual(comment['status'], 'open')          # 等用户接受
        self.assertIn('补充重试次数', comment['resolution'])

        call = self.calls[0]
        self.assertEqual(call['kwargs']['profile'], 'docs')
        self.assertIn('失败后直接返回错误码', call['prompt'])
        self.assertIn('这里没说失败重试几次', call['prompt'])
        self.assertIn('其他段落一个字都不要动', call['prompt'])

    def test_多意见部分失败不影响其他条(self):
        self.add_one()
        self.add_one('调用方需要自己判断', '调用方怎么判断没说清')

        def runner(engine, prompt, **kwargs):
            path = Path(kwargs['cwd']) / '方案设计.md'
            if 'C-01' in prompt:
                return {'ok': False, 'error': '超时（1800 秒）'}
            text = path.read_text(encoding='utf-8')
            path.write_text(text.replace('调用方需要自己判断', '调用方需要自己判断并记录错误码'),
                            encoding='utf-8')
            return {'ok': True, 'text': json.dumps(
                {'id': 'C-02', 'how': '补充调用方要记录错误码'}, ensure_ascii=False)}

        data = dr.apply(self.dir, '方案设计.md', runner=runner)
        self.assertFalse(data['ok'])
        self.assertEqual((data['applied_count'], data['failed_count']), (1, 1))

        first, second = dr.list_comments(self.dir)['comments']
        self.assertEqual(first['status'], 'open')
        self.assertEqual(first['resolution'], '')
        self.assertEqual(first['error'], '超时（1800 秒）')
        self.assertIn('记录错误码', second['resolution'])
        self.assertNotIn('error', second)
        self.assertIn('记录错误码', self.doc.read_text(encoding='utf-8'))

    def test_模型改了却没说明时回滚这一条(self):
        self.add_one()

        def runner(engine, prompt, **kwargs):
            path = Path(kwargs['cwd']) / '方案设计.md'
            path.write_text(path.read_text(encoding='utf-8').replace('不重试', '重试 9 次'),
                            encoding='utf-8')
            return {'ok': True, 'text': '改好了'}

        data = dr.apply(self.dir, '方案设计.md', runner=runner)
        self.assertFalse(data['ok'])
        self.assertIn('没有说明', data['applied'][0]['error'])
        self.assertEqual(self.doc.read_text(encoding='utf-8'), DOC)
        comment = dr.list_comments(self.dir)['comments'][0]
        self.assertEqual(comment['status'], 'open')
        self.assertEqual(comment['resolution'], '')

    def test_目标段之外改太多给warning(self):
        self.add_one()

        def runner(engine, prompt, **kwargs):
            path = Path(kwargs['cwd']) / '方案设计.md'
            path.write_text(path.read_text(encoding='utf-8') + '多余改动\n' * 6,
                            encoding='utf-8')
            return {'ok': True, 'text': json.dumps(
                {'id': 'C-01', 'how': '在文末补充说明'}, ensure_ascii=False)}

        data = dr.apply(self.dir, '方案设计.md', runner=runner)
        self.assertTrue(data['ok'])
        self.assertTrue(data['applied'][0]['warnings'])
        self.assertIn('目标段之外', data['applied'][0]['warnings'][0])

    def test_dry_run只打印提示词不动文件(self):
        self.add_one()

        def boom(*args, **kwargs):
            raise AssertionError('--dry-run 不应该启动模型')

        data = dr.apply(self.dir, '方案设计.md', dry_run=True, runner=boom)
        self.assertTrue(data['dry_run'])
        self.assertEqual(data['applied'], [])
        self.assertEqual(len(data['calls']), 1)
        self.assertEqual(data['calls'][0]['profile'], 'docs')
        self.assertIn('这里没说失败重试几次', data['calls'][0]['prompt'])
        self.assertIn('失败后直接返回错误码', data['calls'][0]['prompt'])
        self.assertEqual(self.doc.read_text(encoding='utf-8'), DOC)
        self.assertFalse((self.dir / '.review' / 'backup').exists())


class DecideTest(ReviewTestBase):
    def test_接受和不采纳改变状态(self):
        self.add_one()
        self.add_one('调用方需要自己判断', '调用方怎么判断没说清')

        accepted = dr.accept(self.dir, 'C-01')
        self.assertEqual(accepted['status'], 'resolved')
        self.assertIn('已接受', accepted['resolution'])

        rejected = dr.reject(self.dir, 'C-02', '口径还没定')
        self.assertEqual(rejected['status'], 'resolved')
        self.assertIn('口径还没定', rejected['resolution'])

        self.assertEqual(dr.list_comments(self.dir, 'open')['count'], 0)
        self.assertEqual(dr.list_comments(self.dir, 'resolved')['count'], 2)
        self.assertEqual(dr.list_comments(self.dir)['counts'], {'open': 0, 'resolved': 2})

        with self.assertRaises(dr.ReviewError):
            dr.accept(self.dir, 'C-01')
        with self.assertRaises(dr.ReviewError):
            dr.reject(self.dir, 'C-02', '')
        with self.assertRaises(dr.ReviewError):
            dr.accept(self.dir, 'C-99')


class DiffTest(ReviewTestBase):
    def test_和上一版备份比较(self):
        self.add_one()
        data = dr.apply(self.dir, '方案设计.md',
                        runner=self.fake_runner('不重试', '最多重试 3 次'))
        text = dr.diff_text(self.dir, '方案设计.md')
        self.assertIn('--- 备份', text)
        self.assertIn('+++ 现在', text)
        self.assertIn('+失败后直接返回错误码，最多重试 3 次', text)
        self.assertIn('-失败后直接返回错误码，不重试', text)

        name = Path(data['backup']).name
        self.assertEqual(dr.diff_text(self.dir, '方案设计.md', name), text)

    def test_没有备份时报错(self):
        with self.assertRaises(dr.ReviewError):
            dr.diff_text(self.dir, '方案设计.md')


class SelectTest(ReviewTestBase):
    def test_按表头解析勾选表(self):
        out = dr.select(self.dir, '方案设计.md', '| 编号 | 问题 | 可选方向 |')
        self.assertTrue(out['ok'])
        self.assertEqual(out['header'], ['编号', '问题', '可选方向'])
        self.assertEqual(out['count'], 2)
        self.assertEqual(out['rows'][0], {'id': 'Q1', 'question': '重试次数怎么定',
                                          'options': ['3 次', '5 次', '不重试']})
        self.assertEqual(out['rows'][1]['options'], ['直接失败', '转异步'])

    def test_表头不存在时报错(self):
        with self.assertRaises(dr.ReviewError) as ctx:
            dr.select(self.dir, '方案设计.md', '| 编号 | 结论 |')
        self.assertIn('找不到表头', str(ctx.exception))


if __name__ == '__main__':
    unittest.main()
