"""dev-metrics-report：聚合数值、时间过滤、trend 分桶、异常信号、git 统计、空数据。"""
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'lib'))
sys.path.insert(0, str(ROOT / 'skills' / 'dev-metrics-report' / 'scripts'))

import metrics_report as mr  # noqa: E402


def write_runs(path, rows):
    Path(path).write_text(
        '\n'.join(json.dumps(r, ensure_ascii=False) for r in rows) + '\n', encoding='utf-8')


def row(at, role='开发', engine='kimi', ok=True, seconds=60,
        tokens_in=0, tokens_out=0, cache_read=0):
    return {'at': at, 'role': role, 'engine': engine, 'ok': ok, 'seconds': seconds,
            'tokens_in': tokens_in, 'tokens_out': tokens_out, 'cache_read': cache_read}


def run_main(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = mr.main(argv)
    return code, out.getvalue(), err.getvalue()


class SummaryTest(unittest.TestCase):
    def test_聚合数值正确(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'runs.jsonl'
            write_runs(path, [
                row('2026-11-17T10:00:00', role='方案设计', engine='kimi', ok=True,
                    seconds=600, tokens_in=1000, tokens_out=500, cache_read=9000),
                row('2026-11-17T11:00:00', role='方案设计', engine='kimi', ok=False,
                    seconds=1200, tokens_in=1000, tokens_out=100, cache_read=1000),
                row('2026-11-18T09:00:00', role='开发', engine='deepseek', ok=True,
                    seconds=300, tokens_in=2000, tokens_out=800, cache_read=2000),
            ])
            s = mr.summarize(mr.load_runs(path))

            self.assertEqual(s['total'], 3)
            self.assertEqual(s['ok'], 2)
            self.assertEqual(s['failed'], 1)
            self.assertAlmostEqual(s['success_rate'], 0.6667, places=4)
            self.assertEqual(s['total_seconds'], 2100.0)
            self.assertEqual(s['avg_seconds'], 700.0)
            self.assertEqual(s['tokens'], {'in': 4000, 'out': 1400,
                                           'cache_read': 12000, 'total_input': 16000})
            self.assertAlmostEqual(s['cache_ratio'], 0.75, places=4)

            roles = {g['role']: g for g in s['by_role']}
            self.assertEqual(roles['方案设计']['count'], 2)
            self.assertEqual(roles['方案设计']['seconds'], 1800.0)
            self.assertEqual(roles['方案设计']['avg_seconds'], 900.0)
            self.assertEqual(roles['方案设计']['failed'], 1)
            self.assertEqual(roles['开发']['count'], 1)
            self.assertEqual(roles['开发']['seconds'], 300.0)
            self.assertEqual(roles['开发']['failed'], 0)

            engines = {g['engine']: g for g in s['by_engine']}
            self.assertEqual(engines['kimi']['count'], 2)
            self.assertEqual(engines['kimi']['seconds'], 1800.0)
            self.assertEqual(engines['kimi']['failed'], 1)
            self.assertEqual(engines['deepseek']['count'], 1)

            self.assertEqual(s['cache_read_top'][0]['role'], '方案设计')
            self.assertEqual(s['cache_read_top'][0]['total_input'], 12000)
            self.assertAlmostEqual(s['cache_read_top'][0]['ratio'], 0.8333, places=4)
            self.assertEqual(s['cache_read_top'][1]['role'], '开发')


class FilterTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / 'runs.jsonl'
        write_runs(self.path, [
            row('2026-11-16T10:00:00', role='需求分析', seconds=60),
            row('2026-11-17T10:00:00', role='方案设计', seconds=120),
            row('2026-11-18T10:00:00', role='开发', seconds=180),
        ])

    def tearDown(self):
        self._tmp.cleanup()

    def test_since和until过滤(self):
        only_17 = mr.load_runs(self.path, since='2026-11-17', until='2026-11-17')
        self.assertEqual([r['role'] for r in only_17], ['方案设计'])

        since_18 = mr.load_runs(self.path, since='2026-11-18')
        self.assertEqual([r['role'] for r in since_18], ['开发'])

        until_16 = mr.load_runs(self.path, until='2026-11-16')
        self.assertEqual([r['role'] for r in until_16], ['需求分析'])

    def test_非法时间报错(self):
        with self.assertRaises(mr.MetricsError):
            mr.load_runs(self.path, since='2026-13-99')

    def test_坏行被跳过(self):
        with open(self.path, 'a', encoding='utf-8') as f:
            f.write('not json\n')
            f.write('{"role": "没有时间"}\n')
        self.assertEqual(len(mr.load_runs(self.path)), 3)


class TrendTest(unittest.TestCase):
    def test_按天和按周分桶(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'runs.jsonl'
            write_runs(path, [
                row('2026-11-16T09:00:00', seconds=60, ok=True),
                row('2026-11-16T10:00:00', seconds=60, ok=False),
                row('2026-11-23T09:00:00', seconds=120, ok=True),
            ])
            rows = mr.load_runs(path)

            day = mr.build_trend(rows, 'day')
            self.assertEqual(day['by'], 'day')
            self.assertEqual([p['bucket'] for p in day['points']],
                             ['2026-11-16', '2026-11-23'])
            self.assertEqual([p['count'] for p in day['points']], [2, 1])
            self.assertEqual(day['points'][0]['seconds'], 120.0)
            self.assertEqual(day['points'][0]['failed'], 1)
            self.assertEqual(day['points'][1]['failed'], 0)

            week = mr.build_trend(rows, 'week')
            self.assertEqual(len(week['points']), 2)
            self.assertEqual([p['count'] for p in week['points']], [2, 1])
            for p in week['points']:
                self.assertRegex(p['bucket'], r'^2026-W\d{2}$')


class AnomalyTest(unittest.TestCase):
    def _signals(self, rows):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'runs.jsonl'
            write_runs(path, rows)
            return mr.anomaly_signals(mr.load_runs(path))

    def test_环节平均耗时超过总体两倍(self):
        rows = [row('2026-11-17T09:00:00', role='方案设计', seconds=3000)]
        for i in range(3):
            rows.append(row('2026-11-17T1%d:00:00' % i, role='开发', seconds=100))
        signals = self._signals(rows)
        self.assertEqual([s['rule'] for s in signals], ['role_avg_over_2x'])
        self.assertEqual(signals[0]['role'], '方案设计')
        self.assertEqual(signals[0]['value'], 3000.0)
        self.assertEqual(signals[0]['threshold'], 1650.0)

    def test_失败率超过两成(self):
        rows = [row('2026-11-17T09:00:00', seconds=60) for _ in range(3)]
        rows[1]['ok'] = False
        signals = self._signals(rows)
        self.assertEqual([s['rule'] for s in signals], ['failure_rate_over_20'])
        self.assertAlmostEqual(signals[0]['value'], 0.3333, places=4)

    def test_缓存占比超过八成(self):
        rows = [row('2026-11-17T09:00:00', seconds=60, tokens_in=1000, cache_read=9000)]
        signals = self._signals(rows)
        self.assertEqual([s['rule'] for s in signals], ['cache_ratio_over_80'])
        self.assertAlmostEqual(signals[0]['value'], 0.9, places=4)

    def test_单日耗时超过八小时(self):
        rows = [row('2026-11-17T09:00:00', seconds=8 * 3600 + 60)]
        signals = self._signals(rows)
        self.assertEqual([s['rule'] for s in signals], ['day_over_8h'])
        self.assertEqual(signals[0]['date'], '2026-11-17')

    def test_指标平稳不触发(self):
        rows = [
            row('2026-11-17T09:00:00', role='方案设计', seconds=600,
                tokens_in=1000, cache_read=1000),
            row('2026-11-17T10:00:00', role='开发', seconds=600,
                tokens_in=1000, cache_read=1000),
        ]
        self.assertEqual(self._signals(rows), [])


class GitStatsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name) / 'repo'
        self.repo.mkdir()
        self._git('init', '-q', '-b', 'main')
        (self.repo / 'README.md').write_text('a\nb\nc\n', encoding='utf-8')
        self._git('add', 'README.md')
        self._commit('Alice', '初始化')
        (self.repo / 'README.md').write_text('a\nB\nc\nd\n', encoding='utf-8')
        self._git('add', 'README.md')
        self._commit('Alice', '调整 README')
        (self.repo / 'new.txt').write_text('x\ny\n', encoding='utf-8')
        self._git('add', 'new.txt')
        self._commit('Bob', '新增文件')

    def tearDown(self):
        self._tmp.cleanup()

    def _git(self, *args):
        proc = subprocess.run(['git', '-C', str(self.repo)] + list(args),
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    def _commit(self, author, message):
        proc = subprocess.run(
            ['git', '-C', str(self.repo),
             '-c', 'user.name=%s' % author,
             '-c', 'user.email=%s@example.com' % author.lower(),
             'commit', '-q', '-m', message],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_提交增删行活跃天数和作者(self):
        data = mr.git_stats(str(self.repo))
        self.assertEqual(data['commits'], 3)
        self.assertEqual(data['added'], 7)
        self.assertEqual(data['deleted'], 1)
        self.assertEqual(data['changed_lines'], 8)
        self.assertEqual(data['active_days'], 1)
        self.assertEqual(len(data['daily']), 1)
        self.assertEqual(data['daily'][0]['commits'], 3)

        authors = {a['author']: a for a in data['by_author']}
        self.assertEqual(authors['Alice']['commits'], 2)
        self.assertEqual(authors['Alice']['added'], 5)
        self.assertEqual(authors['Alice']['deleted'], 1)
        self.assertEqual(authors['Bob']['commits'], 1)
        self.assertEqual(authors['Bob']['added'], 2)

    def test_按作者和since过滤(self):
        alice = mr.git_stats(str(self.repo), author='Alice')
        self.assertEqual(alice['commits'], 2)
        self.assertEqual(alice['added'], 5)

        future = mr.git_stats(str(self.repo), since='2099-01-01')
        self.assertEqual(future['commits'], 0)
        self.assertEqual(future['by_author'], [])

    def test_不是git仓库报错(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(mr.MetricsError):
                mr.git_stats(d)


class EmptyDataTest(unittest.TestCase):
    def test_空runs各子命令不崩(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'empty.jsonl'
            path.write_text('', encoding='utf-8')

            code, out, _ = run_main(['summary', '--runs', str(path)])
            self.assertEqual(code, 0)
            data = json.loads(out)
            self.assertEqual(data['total'], 0)
            self.assertEqual(data['success_rate'], 0.0)
            self.assertEqual(data['by_role'], [])
            self.assertEqual(data['cache_read_top'], [])

            code, out, _ = run_main(['trend', '--runs', str(path)])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)['points'], [])

            code, out, _ = run_main(['report', '--runs', str(path)])
            self.assertEqual(code, 0)
            self.assertIn('本期没有记录', out)
            self.assertIn('未触发任何异常规则', out)

    def test_空仓库不崩(self):
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / 'repo'
            repo.mkdir()
            proc = subprocess.run(['git', '-C', str(repo), 'init', '-q', '-b', 'main'],
                                  capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            data = mr.git_stats(str(repo))
            self.assertEqual(data['commits'], 0)
            self.assertEqual(data['by_author'], [])
            self.assertEqual(data['daily'], [])


class ReportTest(unittest.TestCase):
    def test_报告写文件且包含五个小节(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'runs.jsonl'
            out_file = Path(d) / 'report.md'
            write_runs(path, [
                row('2026-11-17T09:00:00', role='需求分析', seconds=300,
                    tokens_in=1000, tokens_out=500, cache_read=1000),
                row('2026-11-17T10:00:00', role='方案设计', seconds=300,
                    tokens_in=1000, tokens_out=500, cache_read=1000),
                row('2026-11-17T11:00:00', role='开发', ok=False, seconds=3600,
                    tokens_in=1000, tokens_out=500, cache_read=1000),
            ])
            code, stdout, err = run_main(['report', '--runs', str(path),
                                          '--since', '2026-11-01',
                                          '--out', str(out_file)])
            self.assertEqual(code, 0)
            self.assertIn('# 开发度量报告', stdout)
            self.assertTrue(out_file.is_file())

            text = out_file.read_text(encoding='utf-8')
            for section in ('## 本期概览', '## 时间都花在哪', '## 哪些环节最费',
                            '## 异常信号', '## 建议'):
                self.assertIn(section, text)
            self.assertIn('是总体平均', text)
            self.assertIn('失败率', text)
            self.assertIn(str(out_file.resolve()), err)


if __name__ == '__main__':
    unittest.main()
