"""拷走测试：把每个 skill 目录单独复制到临时目录，仍然要能跑。

skill 的分发单位是一个目录，这个测试防的是"偷偷依赖了仓库里的其他文件"。
"""
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class CopyAway(unittest.TestCase):
    def test_每个skill目录拷走后仍可运行(self):
        skills = sorted(p for p in (ROOT / 'skills').glob('*') if p.is_dir())
        self.assertTrue(skills, '没有找到任何 skill')
        for skill in skills:
            with self.subTest(skill=skill.name):
                tmp = Path(tempfile.mkdtemp()) / skill.name
                shutil.copytree(skill, tmp)
                entries = sorted((tmp / 'scripts').glob('*.py'))
                self.assertTrue(entries, '%s 没有入口脚本' % skill.name)
                for entry in entries:
                    proc = subprocess.run([sys.executable, str(entry), '--help'],
                                          capture_output=True, text=True, timeout=60, cwd=str(tmp))
                    self.assertEqual(proc.returncode, 0,
                                     '%s 拷走后跑不起来：%s' % (entry.name, proc.stderr[-500:]))

    def test_没有跨目录引用(self):
        """入口脚本里不能出现 ../.. 这种跳出 skill 目录的路径。"""
        for skill in sorted(p for p in (ROOT / 'skills').glob('*') if p.is_dir()):
            for entry in (skill / 'scripts').glob('*.py'):
                text = entry.read_text(encoding='utf-8')
                with self.subTest(skill=skill.name, entry=entry.name):
                    self.assertNotIn("'../../", text)
                    self.assertNotIn('"../../', text)


class NoLeak(unittest.TestCase):
    """公开前的基本脱敏检查：代码里不能出现本机路径和公司标识。"""
    PATTERNS = ['/Users/gzm', 'wanlianyida', 'vfl-', 'openapi-rdc', 'gpt-6-luna']

    def test_没有敏感字样(self):
        hits = []
        for p in list((ROOT / 'skills').rglob('*.py')) + list((ROOT / 'lib').rglob('*.py')) + \
                list((ROOT / 'skills').rglob('SKILL.md')):
            text = p.read_text(encoding='utf-8', errors='ignore')
            for pat in self.PATTERNS:
                if pat in text:
                    hits.append('%s → %s' % (p.relative_to(ROOT), pat))
        self.assertEqual(hits, [], '发现敏感内容：\n' + '\n'.join(hits))


if __name__ == '__main__':
    unittest.main()
