"""doc-consistency-review：六类一致性规则、断链、空章节、命名归一、--code 找不到、
review 用假 runner、坏 JSON 容错、ask、--help 与参数错误退出码。"""
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'lib'))
sys.path.insert(0, str(ROOT / 'skills' / 'doc-consistency-review' / 'scripts'))

import doc_check as dc  # noqa: E402


class CheckBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.docs = self.dir / 'docs'
        self.docs.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, name, text, base=None):
        p = (base or self.docs) / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding='utf-8')
        return p

    def kinds(self, findings):
        return {f['kind'] for f in findings}

    def pick(self, findings, kind):
        return [f for f in findings if f['kind'] == kind]


class ApiPathTest(CheckBase):
    def test_接口路径只在部分文档出现(self):
        self.write('a.md', '# A\n\n## 接口\n\n创建订单用 /api/orders。\n')
        self.write('b.md', '# B\n\n## 接口\n\n查询用户用 /api/users。\n')
        findings = dc.check(self.docs)
        api = self.pick(findings, 'api-path-partial')
        self.assertEqual(len(api), 2)
        self.assertEqual({f['evidence'] for f in api}, {'/api/orders', '/api/users'})
        self.assertTrue(all(f['severity'] == 'low' for f in api))
        self.assertTrue(any('缺失：b.md' in f['message'] for f in api))


class NamingTest(CheckBase):
    def test_命名归一后比较(self):
        self.assertEqual(dc._norm_words('order_no'),
                         dc._norm_words('orderNo'))
        self.assertEqual(dc._norm_words('orderNo'),
                         dc._norm_words('OrderNo'))

    def test_同一实体多种写法(self):
        self.write('a.md', '# A\n\n## 字段\n\n订单号 order_no 必填。\n')
        self.write('b.md', '# B\n\n## 字段\n\n订单号 orderNo 必填。\n')
        findings = self.pick(dc.check(self.docs), 'naming-inconsistent')
        self.assertTrue(findings)
        self.assertIn('order_no', findings[0]['message'])
        self.assertIn('orderNo', findings[0]['message'])
        self.assertEqual(findings[0]['severity'], 'medium')

    def test_普通单词不算命名问题(self):
        self.write('a.md', '# A\n\n## 说明\n\nThe order is ready.\n\n## 补充\n\n'
                          'order 字段稍后补。\n')
        self.assertFalse(self.pick(dc.check(self.docs), 'naming-inconsistent'))


class LinkTest(CheckBase):
    def test_相对链接文件不存在(self):
        self.write('a.md', '# A\n\n## 参考\n\n见 [方案](design-missing.md)。\n')
        findings = self.pick(dc.check(self.docs), 'broken-link')
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]['line'], 5)
        self.assertEqual(findings[0]['evidence'], 'design-missing.md')

    def test_存在的链接和外链不报(self):
        self.write('design.md', '# 设计\n\n内容。\n')
        self.write('a.md', '# A\n\n## 参考\n\n[方案](design.md) 和 '
                          '[外链](https://example.com/x) 与 [锚点](#a)。\n')
        self.assertFalse(self.pick(dc.check(self.docs), 'broken-link'))


class EmptySectionTest(CheckBase):
    def test_标题下没有内容(self):
        self.write('a.md', '# 标题\n\n## 一\n\n## 二\n\n正文。\n')
        findings = self.pick(dc.check(self.docs), 'empty-section')
        self.assertEqual([f['line'] for f in findings], [3])
        self.assertIn('一', findings[0]['message'])

    def test_有下级标题不算空(self):
        self.write('a.md', '# 标题\n\n## 一\n\n### 一点一\n\n正文。\n')
        self.assertFalse(self.pick(dc.check(self.docs), 'empty-section'))


class TodoTest(CheckBase):
    def test_残留待办标记(self):
        self.write('a.md', '# A\n\n## 待办\n\nTODO: 补充示例。\n\n这里待补充。\n')
        findings = self.pick(dc.check(self.docs), 'todo-residue')
        self.assertEqual([f['line'] for f in findings], [5, 7])

    def test_代码块里的TODO不算(self):
        self.write('a.md', '# A\n\n## 说明\n\n```\nTODO: 这是示例\n```\n')
        self.assertFalse(self.pick(dc.check(self.docs), 'todo-residue'))


class CodeTest(CheckBase):
    def test_类名找到但接口路径找不到(self):
        code = self.dir / 'code'
        code.mkdir()
        (code / 'OrderService.java').write_text(
            'public class OrderService {}\n', encoding='utf-8')
        self.write('a.md', '# A\n\n## 接口\n\nOrderService 提供 /api/orders。\n')
        findings = self.pick(dc.check(self.docs, [code]), 'code-missing')
        self.assertEqual([f['evidence'] for f in findings], ['/api/orders'])

    def test_不传代码目录时不做这条规则(self):
        self.write('a.md', '# A\n\n## 接口\n\nOrderService 提供 /api/orders。\n')
        self.assertFalse(self.pick(dc.check(self.docs), 'code-missing'))


class ReviewTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.doc = self.dir / '设计.md'
        self.doc.write_text('# 设计\n\n## 一\n\n内容。\n', encoding='utf-8')
        self._old_runner = dc._runner
        self.calls = []

    def tearDown(self):
        dc._runner = self._old_runner
        self._tmp.cleanup()

    def fake(self, text):
        def run(engine, prompt, cwd, profile='read'):
            self.calls.append({'engine': engine, 'prompt': prompt,
                               'cwd': cwd, 'profile': profile})
            return {'ok': True, 'engine': engine, 'text': text}
        return run

    def test_评审解析代码块里的JSON数组(self):
        dc._runner = self.fake('```json\n[{"severity":"high","section":"一",'
                               '"quote":"内容","problem":"太笼统","suggestion":"补细节"}]\n```')
        data = dc.review(self.doc, engine='kimi')

        self.assertTrue(data['ok'])
        self.assertEqual(data['count'], 1)
        self.assertEqual(data['reviews'][0]['severity'], 'high')
        self.assertEqual(data['reviews'][0]['section'], '一')
        call = self.calls[0]
        self.assertEqual(call['engine'], 'kimi')
        self.assertEqual(call['profile'], 'read')
        self.assertIn('内容。', call['prompt'])
        self.assertIn('JSON 数组', call['prompt'])

    def test_坏JSON给出原始输出位置(self):
        dc._runner = self.fake('这不是 JSON，只是一段废话。')
        out = self.dir / 'out.r.json'
        data = dc.review(self.doc, out=str(out))

        self.assertFalse(data['ok'])
        self.assertIn('JSON', data['error'])
        raw = Path(data['raw_output'])
        self.assertTrue(raw.is_file())
        self.assertIn('这不是 JSON', raw.read_text(encoding='utf-8'))

    def test_空数组表示没有发现问题(self):
        dc._runner = self.fake('[]')
        data = dc.review(self.doc)
        self.assertTrue(data['ok'])
        self.assertEqual(data['reviews'], [])

    def test_模型调用失败(self):
        dc._runner = lambda engine, prompt, cwd, profile='read': {
            'ok': False, 'error': '超时（1200 秒）'}
        data = dc.review(self.doc)
        self.assertFalse(data['ok'])
        self.assertEqual(data['error'], '超时（1200 秒）')

    def test_问答返回纯文本(self):
        dc._runner = self.fake('答案是先校验再落库。')
        data = dc.ask(self.doc, '下单流程是什么？', engine='claude')
        self.assertTrue(data['ok'])
        self.assertEqual(data['answer'], '答案是先校验再落库。')
        self.assertEqual(self.calls[0]['engine'], 'claude')
        self.assertIn('下单流程是什么？', self.calls[0]['prompt'])

    def test_文件不存在(self):
        with self.assertRaises(dc.DocError):
            dc.review(self.dir / 'nope.md')
        with self.assertRaises(dc.DocError):
            dc.check(self.dir / 'nope')


class CliTest(unittest.TestCase):
    def test_help正常退出(self):
        with redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                dc.main(['--help'])
        self.assertEqual(ctx.exception.code, 0)

    def test_参数错误退出码2(self):
        with redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                dc.main([])
        self.assertEqual(ctx.exception.code, 2)

    def test_engine非法退出码2(self):
        with tempfile.TemporaryDirectory() as d:
            doc = Path(d) / 'a.md'
            doc.write_text('# A\n\n内容。\n', encoding='utf-8')
            with redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as ctx:
                    dc.main(['review', '--file', str(doc), '--engine', 'other'])
        self.assertEqual(ctx.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
