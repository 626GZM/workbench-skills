"""task-brief-handoff：init/add 幂等、规则版简报各小节、无 git 仓库不报错、交接单只含未完成项、引擎失败回退。"""
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'lib'))
sys.path.insert(0, str(ROOT / 'skills' / 'task-brief-handoff' / 'scripts'))

import brief_handoff as bh  # noqa: E402


def run_main(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = bh.main(argv)
    return code, out.getvalue(), err.getvalue()


def make_repo(root):
    """在临时目录造一个真 git 仓库，带一条提交。"""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(['git', 'init', '-q', str(root)], check=True, capture_output=True)
    (root / 'README.md').write_text('demo\n', encoding='utf-8')
    subprocess.run(['git', '-C', str(root), 'add', '.'], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(root), '-c', 'user.name=tester',
                    '-c', 'user.email=tester@example.com', 'commit', '-qm', '初始提交'],
                   check=True, capture_output=True)
    return root


def branch_of(repo):
    return subprocess.check_output(
        ['git', '-C', str(repo), 'rev-parse', '--abbrev-ref', 'HEAD'], text=True).strip()


def section(md, name):
    """取 `## name` 到下一个二级标题之间的内容。"""
    return md.split('## ' + name, 1)[1].split('\n## ', 1)[0]


class InitAddTest(unittest.TestCase):
    def test_init重复执行只补缺失字段(self):
        with tempfile.TemporaryDirectory() as d:
            task = Path(d) / 'task'
            first = bh.init_brief(task, title='订单导出优化', acceptance=['提交后返回任务号'],
                                  repos=['~/repos/project-a'])
            second = bh.init_brief(task, title='订单导出优化', acceptance=['提交后返回任务号'])

            self.assertTrue(first['created'])
            self.assertFalse(second['created'])
            self.assertFalse(second['changed'])
            self.assertEqual(second['brief']['title'], '订单导出优化')
            self.assertEqual(len(second['brief']['acceptance']), 1)
            self.assertEqual(second['brief']['repos'], ['~/repos/project-a'])
            self.assertTrue((task / 'brief.json').is_file())

    def test_add按文本去重且可标记完成(self):
        with tempfile.TemporaryDirectory() as d:
            task = Path(d) / 'task'
            bh.init_brief(task, title='x')

            a = bh.add_entry(task, 'decision', '任务状态用状态机')
            b = bh.add_entry(task, 'decision', '任务状态用状态机')
            self.assertTrue(a['added'])
            self.assertFalse(b['added'])
            self.assertEqual(len(b['brief']['decisions']), 1)

            bh.add_entry(task, 'todo', '建导出任务表')
            c = bh.add_entry(task, 'todo', '建导出任务表', done=True)
            self.assertFalse(c['added'])                        # 不重复加一条
            self.assertTrue(c['brief']['todos'][0]['done'])      # 只把已有项标记完成
            self.assertEqual(len(c['brief']['todos']), 1)

    def test_add缺少brief报错(self):
        with tempfile.TemporaryDirectory() as d:
            code, out, err = run_main(['add', '--dir', d, '--kind', 'todo', '--text', 'x'])
            self.assertEqual(code, 2)
            self.assertEqual(out.strip(), '')
            self.assertIn('init', err)


class BuildRuleTest(unittest.TestCase):
    def _task(self, d, repo=None):
        task = Path(d) / 'task'
        repos = [str(repo)] if repo else []
        bh.init_brief(task, title='订单导出优化', background='导出改成异步，避免超时',
                      goal='支持异步提交、进度查询和结果下载',
                      acceptance=['提交后立即返回任务号', '进度查询能看到百分比'],
                      constraints=['不改前端已有接口路径'],
                      repos=repos, support=['前端同学配合联调'])
        bh.add_entry(task, 'todo', '建导出任务表', done=True)
        bh.add_entry(task, 'todo', '补进度查询单测')
        bh.add_entry(task, 'decision', '任务状态用状态机')
        bh.add_entry(task, 'pitfall', '本地跑测试要先启动对象存储')
        bh.add_entry(task, 'fact', '导出结果保留 7 天')
        return task

    def test规则版包含所有小节和git现状(self):
        with tempfile.TemporaryDirectory() as d:
            repo = make_repo(Path(d) / 'repo')
            (repo / 'work.txt').write_text('未提交\n', encoding='utf-8')
            task = self._task(d, repo)

            result = bh.build(task, engine='')
            md = result['markdown']
            for name in bh.BUILD_SECTIONS:
                self.assertIn('## ' + name, md, name)
            self.assertIn('导出改成异步', md)
            self.assertIn('支持异步提交', md)
            self.assertIn('不改前端已有接口路径', md)
            self.assertIn('提交后立即返回任务号', md)
            self.assertIn('初始提交', md)                       # 最近提交
            self.assertIn('分支 `%s`' % branch_of(repo), md)
            self.assertIn('work.txt', md)                       # 脏文件
            self.assertIn('未提交改动 1 个', md)
            self.assertIn('- [x] 建导出任务表', md)
            self.assertIn('- [ ] 补进度查询单测', md)
            self.assertIn('任务状态用状态机', md)
            self.assertIn('本地跑测试要先启动对象存储', md)
            self.assertIn('导出结果保留 7 天', md)
            self.assertIn('继续：补进度查询单测', md)
            self.assertNotIn('按计划推进', md)
            self.assertEqual(result['source'], 'rule')
            self.assertEqual(result['error'], '')
            self.assertTrue(result['repos'][0]['ok'])

    def test没有仓库时跳过git且不报错(self):
        with tempfile.TemporaryDirectory() as d:
            task = self._task(d)
            result = bh.build(task, engine='')
            md = result['markdown']
            self.assertIn('未登记相关仓库，跳过 git 检查', md)
            for name in bh.BUILD_SECTIONS:
                self.assertIn('## ' + name, md, name)

    def test仓库不存在或不是git只注明跳过(self):
        with tempfile.TemporaryDirectory() as d:
            task = self._task(d)
            missing = str(Path(d) / 'no-such-repo')
            result = bh.build(task, git_repos=[missing], engine='')
            self.assertIn('跳过（目录不存在）', result['markdown'])
            self.assertFalse(result['repos'][0]['ok'])

            plain = Path(d) / 'plain'
            plain.mkdir()
            result = bh.build(task, git_repos=[str(plain)], engine='')
            self.assertIn('跳过（不是 git 仓库）', result['markdown'])

    def test_cli_json方便程序消费(self):
        with tempfile.TemporaryDirectory() as d:
            task = self._task(d)
            code, out, _ = run_main(['build', '--dir', str(task), '--engine', 'none', '--json'])
            self.assertEqual(code, 0)
            data = json.loads(out)
            self.assertEqual(data['source'], 'rule')
            self.assertIn('## 下一步', data['markdown'])
            self.assertIn('brief', data)
            self.assertIsInstance(data['repos'], list)

    def test_out把markdown写到文件(self):
        with tempfile.TemporaryDirectory() as d:
            task = self._task(d)
            out = Path(d) / 'out' / 'BRIEF.md'
            result = bh.build(task, out=str(out), engine='')
            self.assertEqual(out.read_text(encoding='utf-8'), result['markdown'])
            self.assertEqual(result['out'], str(out.resolve()))


class HandoffTest(unittest.TestCase):
    def test交接单只把未完成项列进未完成(self):
        with tempfile.TemporaryDirectory() as d:
            task = Path(d) / 'task'
            bh.init_brief(task, title='订单导出优化', status='下载接口还没开始',
                          support=['前端同学配合联调'])
            bh.add_entry(task, 'todo', '建导出任务表', done=True)
            bh.add_entry(task, 'todo', '补进度查询单测')
            bh.add_entry(task, 'pitfall', '本地跑测试要先启动对象存储')

            out = Path(d) / 'HANDOFF.md'
            result = bh.handoff(task, str(out), engine='')
            md = result['markdown']
            self.assertEqual(md, out.read_text(encoding='utf-8'))
            for name in bh.HANDOFF_SECTIONS:
                self.assertIn('## ' + name, md, name)

            done = section(md, '已完成')
            opened = section(md, '未完成')
            self.assertIn('建导出任务表', done)
            self.assertNotIn('建导出任务表', opened)
            self.assertIn('补进度查询单测', opened)
            self.assertNotIn('补进度查询单测', done)
            self.assertIn('下载接口还没开始', section(md, '卡在哪'))
            self.assertIn('本地跑测试要先启动对象存储', section(md, '风险'))
            self.assertIn('前端同学配合联调', section(md, '需要谁配合'))
            self.assertNotIn('按计划推进', md)

    def test全部完成时不编造未完成项(self):
        with tempfile.TemporaryDirectory() as d:
            task = Path(d) / 'task'
            bh.init_brief(task, title='x')
            bh.add_entry(task, 'todo', '建导出任务表', done=True)
            result = bh.handoff(task, str(Path(d) / 'HANDOFF.md'), engine='')
            self.assertIn('- 未记录', section(result['markdown'], '未完成'))


class EngineFallbackTest(unittest.TestCase):
    def test模型失败回退规则版(self):
        with tempfile.TemporaryDirectory() as d:
            task = Path(d) / 'task'
            bh.init_brief(task, title='订单导出优化', goal='支持异步导出')
            with mock.patch.object(bh.agent, 'run',
                                   return_value={'ok': False, 'error': '没有找到 kimi 命令'}):
                result = bh.build(task, engine='kimi')
            self.assertEqual(result['source'], 'rule')
            self.assertIn('没有找到 kimi 命令', result['error'])
            for name in bh.BUILD_SECTIONS:
                self.assertIn('## ' + name, result['markdown'], name)

    def test模型输出缺小节也回退(self):
        with tempfile.TemporaryDirectory() as d:
            task = Path(d) / 'task'
            bh.init_brief(task, title='订单导出优化')
            with mock.patch.object(bh.agent, 'run',
                                   return_value={'ok': True, 'text': '## 项目背景\n\n- 未记录\n'}):
                result = bh.build(task, engine='kimi')
            self.assertEqual(result['source'], 'rule')
            self.assertIn('缺少小节', result['error'])
            self.assertIn('## 下一步', result['markdown'])

    def test模型成功时用整理后的正文(self):
        with tempfile.TemporaryDirectory() as d:
            task = Path(d) / 'task'
            bh.init_brief(task, title='订单导出优化')
            doc = '\n'.join('## %s\n\n- 未记录' % name for name in bh.BUILD_SECTIONS) + '\n'
            with mock.patch.object(bh.agent, 'run', return_value={'ok': True, 'text': doc}):
                result = bh.build(task, engine='kimi')
            self.assertEqual(result['source'], 'ai')
            self.assertEqual(result['error'], '')
            self.assertIn('## 下一步', result['markdown'])


if __name__ == '__main__':
    unittest.main()
