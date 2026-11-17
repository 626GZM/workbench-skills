"""config 与 git 的小单测。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'lib'))

from wbkit import config as cfg  # noqa: E402
from wbkit import git  # noqa: E402


class ConfigTest(unittest.TestCase):
    def test_默认值可用(self):
        data = cfg.load(reload=True)
        self.assertIn('branches', data)
        self.assertIn('base', data['branches'])

    def test_深合并(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'c.json'
            p.write_text(json.dumps({'branches': {'base': 'trunk'}, 'projects': [{'name': 'a'}]}),
                         encoding='utf-8')
            data = cfg.load(p, reload=True)
            self.assertEqual(data['branches']['base'], 'trunk')
            self.assertEqual(data['branches']['dev'], 'dev')   # 没覆盖的保留默认
            self.assertEqual(len(data['projects']), 1)

    def test_点路径取值(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'c.json'
            p.write_text(json.dumps({'models': {'kimi': {'model': 'kimi-x'}}}), encoding='utf-8')
            conf = cfg.load(p, reload=True)
            self.assertEqual(cfg.get('models.kimi.model', conf=conf), 'kimi-x')
            self.assertIsNone(cfg.get('models.nope.model', conf=conf))


class GitRefTest(unittest.TestCase):
    def test_合法分支名(self):
        self.assertEqual(git.ref('origin/main'), 'origin/main')
        self.assertEqual(git.ref('feature/x-1'), 'feature/x-1')

    def test_非法分支名(self):
        for bad in ('-x', 'a..b', 'a b', '', 'x/..'):
            with self.subTest(bad=bad):
                with self.assertRaises(git.GitError):
                    git.ref(bad)


if __name__ == '__main__':
    unittest.main()
