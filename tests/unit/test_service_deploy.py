"""local-service-deploy：配置解析、端口检测、health 超时、构建结果、start/status/stop 生命周期。"""
import io
import json
import socket
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'lib'))
sys.path.insert(0, str(ROOT / 'skills' / 'local-service-deploy' / 'scripts'))

import service_deploy as sd  # noqa: E402


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def run(args):
    """跑一次 CLI，返回 (退出码, 解析后的 JSON)。"""
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = sd.main(args)
    text = buf.getvalue().strip()
    return code, (json.loads(text) if text else None)


def kill_quietly(pid):
    if pid and sd._ps(pid)['alive']:
        try:
            sd._terminate(pid, 1)
        except sd.DeployError:
            pass


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.state = self.tmp / 'state'

    def tearDown(self):
        self._tmp.cleanup()

    def write_config(self, services, **top):
        data = {'state_dir': str(self.state), 'services': services}
        data.update(top)
        path = self.tmp / 'services.json'
        path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        return str(path)

    def script(self, name, body):
        """写一个临时 Python 脚本，返回带解释器路径的命令行。"""
        path = self.tmp / name
        path.write_text(body, encoding='utf-8')
        return '"%s" "%s"' % (sys.executable, path)


class ConfigTest(Base):
    def test_对象形式与默认值(self):
        path = self.write_config({'order-service': {'dir': str(self.tmp), 'start': 'echo hi', 'port': 18081}})
        cfg = sd.load_config(path)
        svc = cfg['services']['order-service']
        self.assertEqual(svc['cwd'], str(self.tmp))
        self.assertEqual(svc['wait'], 60)
        self.assertEqual(svc['build_timeout'], 900)
        self.assertEqual(svc['stop_timeout'], 10)
        self.assertEqual(svc['env'], {})
        self.assertIsNone(svc['shell'])
        self.assertTrue(svc['log'].endswith('order-service.log'))
        self.assertEqual(svc['pid_file'], str(self.state / 'pids' / 'order-service.pid'))

    def test_数组形式(self):
        path = self.write_config([{'name': 'payment-service', 'dir': str(self.tmp), 'start': 'echo hi'}])
        cfg = sd.load_config(path)
        self.assertIn('payment-service', cfg['services'])

    def test_相对路径按配置目录解析(self):
        (self.tmp / 'repo').mkdir()
        path = self.write_config({'order-service': {'dir': './repo', 'start': 'echo hi', 'log': 'out/app.log'}})
        svc = sd.load_config(path)['services']['order-service']
        # load_config 会 resolve 真实路径（macOS 上 /var 是指向 /private/var 的软链）
        self.assertEqual(svc['dir'], str((self.tmp / 'repo').resolve()))
        self.assertEqual(svc['log'], str((self.tmp / 'out' / 'app.log').resolve()))
        self.assertEqual(svc['cwd'], str((self.tmp / 'repo').resolve()))

    def test_缺少必填字段(self):
        path = self.write_config({'order-service': {'dir': str(self.tmp)}})
        with self.assertRaises(sd.DeployError):
            sd.load_config(path)

    def test_服务名不合法(self):
        path = self.write_config({'../evil': {'dir': str(self.tmp), 'start': 'echo hi'}})
        with self.assertRaises(sd.DeployError):
            sd.load_config(path)

    def test_端口和健康检查校验(self):
        path = self.write_config({'order-service': {'dir': str(self.tmp), 'start': 'echo hi',
                                                    'port': 70000}})
        with self.assertRaises(sd.DeployError):
            sd.load_config(path)
        path = self.write_config({'order-service': {'dir': str(self.tmp), 'start': 'echo hi',
                                                    'health': 'localhost:8081'}})
        with self.assertRaises(sd.DeployError):
            sd.load_config(path)

    def test_配置文件不存在(self):
        with self.assertRaises(sd.DeployError):
            sd.load_config(str(self.tmp / 'nope.json'))

    def test_找不到服务(self):
        path = self.write_config({'order-service': {'dir': str(self.tmp), 'start': 'echo hi'}})
        cfg = sd.load_config(path)
        with self.assertRaises(sd.DeployError):
            sd.require_service(cfg, 'payment-service')


class PortTest(unittest.TestCase):
    def test_端口监听和占用(self):
        port = free_port()
        self.assertTrue(sd.port_available(port))
        self.assertFalse(sd.port_listening(port))
        with socket.socket() as server:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(('127.0.0.1', port))
            server.listen(1)
            self.assertFalse(sd.port_available(port))
            self.assertTrue(sd.port_listening(port))
        self.assertFalse(sd.port_listening(port))


class BuildTest(Base):
    def test_构建成功(self):
        cmd = self.script('build_ok.py', "print('built-ok')\n")
        path = self.write_config({'order-service': {'dir': str(self.tmp), 'start': 'echo hi',
                                                    'build': cmd}})
        code, out = run(['build', '--service', 'order-service', '--config', path])
        self.assertEqual(code, 0)
        self.assertTrue(out['ok'])
        self.assertEqual(out['exit_code'], 0)
        self.assertGreaterEqual(out['seconds'], 0)
        self.assertIn('built-ok', Path(out['log']).read_text(encoding='utf-8'))

    def test_构建失败带回stderr末尾(self):
        cmd = self.script('build_fail.py',
                          "import sys\nsys.stderr.write('boom-detail\\n')\nsys.exit(3)\n")
        path = self.write_config({'order-service': {'dir': str(self.tmp), 'start': 'echo hi',
                                                    'build': cmd}})
        code, out = run(['build', '--service', 'order-service', '--config', path])
        self.assertEqual(code, 1)
        self.assertFalse(out['ok'])
        self.assertEqual(out['exit_code'], 3)
        self.assertIn('boom-detail', out['stderr_tail'])

    def test_没有build命令(self):
        path = self.write_config({'order-service': {'dir': str(self.tmp), 'start': 'echo hi'}})
        code, out = run(['build', '--service', 'order-service', '--config', path])
        self.assertEqual(code, 2)
        self.assertIsNone(out)


class HealthTimeoutTest(Base):
    def test_health超时会停掉刚起的进程(self):
        port = free_port()                       # 没有任何东西在听
        cmd = self.script('idle.py', 'import time\ntime.sleep(30)\n')
        path = self.write_config({'order-service': {
            'dir': str(self.tmp), 'start': cmd, 'port': port,
            'health': 'http://127.0.0.1:%d/' % port}})
        code, out = run(['start', '--service', 'order-service', '--config', path, '--wait', '1'])
        self.addCleanup(kill_quietly, out.get('pid') if out else None)
        self.assertEqual(code, 1)
        self.assertFalse(out['ok'])
        self.assertEqual(out['status'], 'failed')
        self.assertIn('超时', out['error'])
        self.assertFalse(out['healthy'])
        self.assertGreaterEqual(out['waited_seconds'], 1)
        self.assertFalse((self.state / 'pids' / 'order-service.pid').exists())
        self.assertFalse(sd._ps(out['pid'])['alive'])


class LifecycleTest(Base):
    def test_起停全流程和pid复用校验(self):
        port = free_port()
        cmd = '"%s" -m http.server %d --bind 127.0.0.1' % (sys.executable, port)
        path = self.write_config({'order-service': {
            'dir': str(self.tmp), 'start': cmd, 'port': port,
            'health': 'http://127.0.0.1:%d/' % port}})
        pid_file = self.state / 'pids' / 'order-service.pid'

        code, out = run(['start', '--service', 'order-service', '--config', path, '--wait', '20'])
        pid = out.get('pid')
        self.addCleanup(kill_quietly, pid)
        self.assertEqual(code, 0, out)
        self.assertTrue(out['ok'])
        self.assertTrue(out['healthy'])
        self.assertTrue(sd.port_listening(port))

        record = json.loads(pid_file.read_text(encoding='utf-8'))
        self.assertEqual(record['service'], 'order-service')
        self.assertTrue(record['started_at'])
        self.assertTrue(record['lstart'])
        self.assertEqual(record['pid'], pid)

        # 重复启动：不重复起进程
        code, again = run(['start', '--service', 'order-service', '--config', path])
        self.assertEqual(code, 0)
        self.assertTrue(again['already_running'])
        self.assertEqual(again['pid'], pid)

        # list：进程活着、端口在听
        code, listed = run(['list', '--config', path])
        self.assertEqual(code, 0)
        row = listed['services'][0]
        self.assertEqual(row['name'], 'order-service')
        self.assertTrue(row['process']['alive'])
        self.assertTrue(row['port_listening'])

        # status：运行中，健康检查通过，带日志尾部
        code, status = run(['status', '--service', 'order-service', '--config', path])
        self.assertEqual(code, 0)
        self.assertEqual(status['status'], 'running')
        self.assertTrue(status['process']['alive'])
        self.assertIsNotNone(status['process']['uptime_seconds'])
        self.assertTrue(status['health']['ok'])
        self.assertIn('log_tail', status)

        # pid 复用校验：篡改启动时间后 stop 只删记录，不杀进程
        tampered = dict(record, lstart='Mon Jan  1 00:00:00 2001')
        pid_file.write_text(json.dumps(tampered), encoding='utf-8')
        code, stopped = run(['stop', '--service', 'order-service', '--config', path])
        self.assertEqual(code, 0)
        self.assertFalse(stopped['stopped'])
        self.assertTrue(stopped['stale'])
        self.assertEqual(stopped['reason'], 'pid_reused')
        self.assertFalse(pid_file.exists())
        self.assertTrue(sd._ps(pid)['alive'])

        # 记录复原后正常停止
        pid_file.write_text(json.dumps(record), encoding='utf-8')
        code, stopped = run(['stop', '--service', 'order-service', '--config', path])
        self.assertEqual(code, 0)
        self.assertTrue(stopped['stopped'])
        self.assertFalse(pid_file.exists())
        for _ in range(20):
            if not sd.port_listening(port):
                break
            time.sleep(0.1)
        self.assertFalse(sd.port_listening(port))
        self.assertFalse(sd._ps(pid)['alive'])

        # 没有 pid 记录时停止也算成功
        code, stopped = run(['stop', '--service', 'order-service', '--config', path])
        self.assertEqual(code, 0)
        self.assertFalse(stopped['stopped'])

    def test_端口被占用时拒绝启动(self):
        port = free_port()
        with socket.socket() as server:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(('127.0.0.1', port))
            server.listen(1)
            cmd = '"%s" -m http.server %d --bind 127.0.0.1' % (sys.executable, port)
            path = self.write_config({'order-service': {'dir': str(self.tmp), 'start': cmd,
                                                        'port': port}})
            code, out = run(['start', '--service', 'order-service', '--config', path])
        self.assertEqual(code, 2)
        self.assertIsNone(out)


class RestartTest(Base):
    def test_skip_build重启换新进程(self):
        port = free_port()
        cmd = '"%s" -m http.server %d --bind 127.0.0.1' % (sys.executable, port)
        path = self.write_config({'order-service': {
            'dir': str(self.tmp), 'start': cmd, 'port': port,
            'health': 'http://127.0.0.1:%d/' % port}})
        code, first = run(['start', '--service', 'order-service', '--config', path, '--wait', '20'])
        self.addCleanup(kill_quietly, first.get('pid') if first else None)
        self.assertEqual(code, 0, first)

        code, out = run(['restart', '--service', 'order-service', '--config', path,
                         '--skip-build', '--wait', '20'])
        self.addCleanup(kill_quietly, (out or {}).get('start', {}).get('pid'))
        self.assertEqual(code, 0, out)
        self.assertTrue(out['ok'])
        self.assertTrue(out['build']['skipped'])
        self.assertTrue(out['stop']['stopped'])
        self.assertNotEqual(out['start']['pid'], first['pid'])
        self.assertTrue(sd.port_listening(port))
        self.assertFalse(sd._ps(first['pid'])['alive'])


class LogsTest(Base):
    def test_读末尾若干行(self):
        log = self.tmp / 'app.log'
        log.write_text('\n'.join('line-%d' % i for i in range(1, 301)) + '\n', encoding='utf-8')
        path = self.write_config({'order-service': {'dir': str(self.tmp), 'start': 'echo hi',
                                                    'log': str(log)}})
        code, out = run(['logs', '--service', 'order-service', '--config', path, '-n', '5'])
        self.assertEqual(code, 0)
        self.assertTrue(out['exists'])
        self.assertEqual(out['text'].splitlines(),
                         ['line-296', 'line-297', 'line-298', 'line-299', 'line-300'])

    def test_日志不存在(self):
        path = self.write_config({'order-service': {'dir': str(self.tmp), 'start': 'echo hi',
                                                    'log': str(self.tmp / 'none.log')}})
        code, out = run(['logs', '--service', 'order-service', '--config', path])
        self.assertEqual(code, 0)
        self.assertFalse(out['exists'])
        self.assertEqual(out['text'], '')


if __name__ == '__main__':
    unittest.main()
