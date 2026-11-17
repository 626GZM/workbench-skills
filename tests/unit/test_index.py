"""文件索引与搜索的单测。"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'lib'))

from wbkit import config as cfg  # noqa: E402
from wbkit import index  # noqa: E402


class IndexTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.docs = base / 'docs'
        self.docs.mkdir()
        (self.docs / 'a.md').write_text('# 渠道审批\n渠道审批节点需要校验权限。\n', encoding='utf-8')
        (self.docs / 'b.py').write_text('def foo():\n    return "审批流程"\n', encoding='utf-8')
        (self.docs / 'c.bin').write_bytes(b'\x00\x01\x02binary')
        conf = base / 'c.json'
        conf.write_text(json.dumps({'index': {'db_path': str(base / 'idx.db')}}), encoding='utf-8')
        os.environ['WB_CONFIG'] = str(conf)
        cfg.load(reload=True)

    def tearDown(self):
        os.environ.pop('WB_CONFIG', None)
        cfg.load(reload=True)
        self.tmp.cleanup()

    def test_建索引与中文搜索(self):
        stats = index.build(self.docs)
        self.assertEqual(stats['files'], 3)
        hits = index.search(self.docs, '审批')
        self.assertEqual({h['path'] for h in hits}, {'a.md', 'b.py'})
        self.assertTrue(all(h['snippet'] for h in hits))

    def test_英文搜索(self):
        index.build(self.docs)
        hits = index.search(self.docs, 'foo')
        self.assertEqual([h['path'] for h in hits], ['b.py'])

    def test_扩展名过滤(self):
        index.build(self.docs)
        hits = index.search(self.docs, '审批', exts=['md'])
        self.assertEqual([h['path'] for h in hits], ['a.md'])

    def test_统计与删除(self):
        index.build(self.docs)
        self.assertTrue(index.stats(self.docs)['indexed'])
        index.drop(self.docs)
        self.assertFalse(index.stats(self.docs)['indexed'])

    def test_目录不存在要报错(self):
        with self.assertRaises(ValueError):
            index.build(self.docs / 'nope')


if __name__ == '__main__':
    unittest.main()
