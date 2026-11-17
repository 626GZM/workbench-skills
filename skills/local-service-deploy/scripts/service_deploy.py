#!/usr/bin/env python3
"""本地服务部署：按 services.json 构建、启动、停止本地服务，查状态、看日志。

所有命令输出 JSON（logs -f 例外，像 tail -f 一样流式打印）。
构建命令、启动命令、端口、健康检查、日志路径全部来自配置，不写死任何具体服务。
只用 Python 标准库。
"""
import argparse
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')
# ps 的一行：状态、启动时间（5 段）、完整命令行
PS_RE = re.compile(r'^\s*(\S+)\s+(\S+\s+\S+\s+\d+\s+\d+:\d+:\d+\s+\d+)\s+(.*)$')
DEFAULT_STATE_DIR = '~/.local/state/local-service-deploy'
TAIL_BYTES = 200000


class DeployError(Exception):
    """配置或用法有问题，退出码 2。"""


def _emit(data):
    print(json.dumps(data, ensure_ascii=False, indent=2))


def now_iso():
    return datetime.now().astimezone().isoformat(timespec='seconds')


def _expand(value, base):
    """展开 ~；相对路径按配置文件所在目录解析。"""
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = Path(base) / path
    return str(path)


# ---------- 配置 ----------

def _normalize(name, spec, base, state_dir):
    """把一条服务配置补齐默认值并校验，返回统一结构。"""
    def text(key, required=False):
        value = spec.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            if required:
                raise DeployError('服务 %s 缺少 %s' % (name, key))
            return ''
        if not isinstance(value, str):
            raise DeployError('服务 %s 的 %s 必须是字符串' % (name, key))
        return value.strip()

    def number(key, default):
        value = spec.get(key, default)
        try:
            value = int(value)
        except (TypeError, ValueError):
            raise DeployError('服务 %s 的 %s 必须是正整数' % (name, key)) from None
        if value <= 0:
            raise DeployError('服务 %s 的 %s 必须是正整数' % (name, key))
        return value

    directory = _expand(text('dir', required=True), base)
    if not Path(directory).is_dir():
        raise DeployError('服务 %s 的 dir 不存在：%s' % (name, directory))
    start = text('start', required=True)
    build = text('build')

    port = spec.get('port')
    if port is not None:
        try:
            port = int(port)
        except (TypeError, ValueError):
            raise DeployError('服务 %s 的 port 必须是整数' % name) from None
        if not 1 <= port <= 65535:
            raise DeployError('服务 %s 的 port 超出范围：%s' % (name, port))

    health = text('health')
    if health and not health.startswith(('http://', 'https://')):
        raise DeployError('服务 %s 的 health 必须是 http(s) 地址' % name)

    log = _expand(text('log') or str(Path(state_dir) / 'logs' / (name + '.log')), base)
    cwd = _expand(text('cwd') or directory, base)
    shell = text('shell') or None

    env = spec.get('env') or {}
    if not isinstance(env, dict) or any(
            not isinstance(k, str) or not isinstance(v, (str, int, float)) for k, v in env.items()):
        raise DeployError('服务 %s 的 env 必须是「变量名 → 值」的对象' % name)
    env = {k: str(v) for k, v in env.items()}

    return {
        'name': name, 'dir': directory, 'start': start, 'build': build,
        'port': port, 'health': health, 'log': log, 'cwd': cwd, 'shell': shell,
        'env': env,
        'wait': number('wait', 60),
        'build_timeout': number('build_timeout', 900),
        'stop_timeout': number('stop_timeout', 10),
        'pid_file': str(Path(state_dir) / 'pids' / (name + '.pid')),
    }


def load_config(path=None):
    """读 services.json。--config > ./services.json > ~/.config/workbench-skills/services.json。"""
    if path:
        cfg_path = Path(path).expanduser()
        if not cfg_path.is_file():
            raise DeployError('配置文件不存在：%s' % cfg_path)
    else:
        cfg_path = next((p for p in (Path.cwd() / 'services.json',
                                     Path.home() / '.config' / 'workbench-skills' / 'services.json')
                         if p.is_file()), None)
        if cfg_path is None:
            raise DeployError('没有找到 services.json，请用 --config 指定')
    cfg_path = cfg_path.resolve()

    try:
        raw = json.loads(cfg_path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as e:
        raise DeployError('配置文件读不了：%s（%s）' % (cfg_path, e)) from None
    if not isinstance(raw, dict):
        raise DeployError('配置文件顶层必须是 JSON 对象')

    entries = raw.get('services')
    if isinstance(entries, dict):
        items = list(entries.items())
    elif isinstance(entries, list):
        items = []
        for item in entries:
            if not isinstance(item, dict) or not item.get('name'):
                raise DeployError('services 数组里的每一项都必须是带 name 的对象')
            items.append((item['name'], item))
    else:
        raise DeployError('services 必须是对象（服务名 → 配置）或数组')
    if not items:
        raise DeployError('services 里没有任何服务')

    base = cfg_path.parent
    state_dir = _expand(raw.get('state_dir') or DEFAULT_STATE_DIR, base)
    services = {}
    for name, spec in items:
        if not isinstance(name, str) or not NAME_RE.match(name):
            raise DeployError('服务名不合法：%r' % (name,))
        if not isinstance(spec, dict):
            raise DeployError('服务 %s 的配置必须是对象' % name)
        if name in services:
            raise DeployError('服务名重复：%s' % name)
        services[name] = _normalize(name, spec, base, state_dir)
    return {'path': str(cfg_path), 'state_dir': state_dir, 'services': services}


def require_service(config, name):
    svc = config['services'].get(name)
    if svc is None:
        raise DeployError('配置里没有叫 %s 的服务（可选：%s）'
                          % (name, '、'.join(sorted(config['services']))))
    return svc


# ---------- 进程、端口、健康检查 ----------

def _ps(pid):
    """读进程状态：alive / stat / lstart / command。读不到就当进程不存在。"""
    if not pid:
        return {'alive': False, 'stat': '', 'lstart': '', 'command': ''}
    try:
        # 固定 C locale：lstart 的时间格式才能稳定解析（否则中文环境会变成“二 11月/17 ...”）
        env = dict(os.environ, LC_ALL='C', LANG='C')
        proc = subprocess.run(['ps', '-ww', '-p', str(pid), '-o', 'stat=', '-o', 'lstart=', '-o', 'command='],
                              capture_output=True, text=True, timeout=5, env=env)
    except (OSError, subprocess.SubprocessError):
        return _ps_fallback(pid)
    text = (proc.stdout or '').strip()
    if proc.returncode != 0 or not text:
        return {'alive': False, 'stat': '', 'lstart': '', 'command': ''}
    match = PS_RE.match(text)
    if not match:
        return {'alive': True, 'stat': '', 'lstart': '', 'command': text}
    stat, lstart, command = match.groups()
    return {'alive': not stat.startswith('Z'), 'stat': stat, 'lstart': lstart, 'command': command}


def _ps_fallback(pid):
    try:
        os.kill(int(pid), 0)
        return {'alive': True, 'stat': '', 'lstart': '', 'command': ''}
    except (OSError, ValueError):
        return {'alive': False, 'stat': '', 'lstart': '', 'command': ''}


def _wait_ps(pid):
    """等 ps 能看到新进程的启动时间，最多约 1 秒。"""
    info = {'alive': False, 'stat': '', 'lstart': '', 'command': ''}
    for _ in range(20):
        info = _ps(pid)
        if info['lstart'] or not info['alive']:
            break
        time.sleep(0.05)
    return info


def _signal(pid, sig):
    """优先发给进程组（启动时开了新会话），不是组长就只发给进程本身。"""
    try:
        if os.getpgid(pid) == pid:
            os.killpg(pid, sig)
        else:
            os.kill(pid, sig)
    except ProcessLookupError:
        return False
    except PermissionError as e:
        raise DeployError('没有权限停止进程 %s：%s' % (pid, e)) from None
    return True


def _terminate(pid, timeout):
    """先 TERM，超时再 KILL。返回是否强制、耗时。"""
    began = time.monotonic()
    if not _ps(pid)['alive']:
        return {'was_running': False, 'forced': False, 'seconds': 0.0}
    _signal(pid, signal.SIGTERM)
    deadline = began + timeout
    while time.monotonic() < deadline:
        if not _ps(pid)['alive']:
            return {'was_running': True, 'forced': False, 'seconds': round(time.monotonic() - began, 2)}
        time.sleep(0.1)
    _signal(pid, signal.SIGKILL)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if not _ps(pid)['alive']:
            break
        time.sleep(0.1)
    return {'was_running': True, 'forced': True, 'seconds': round(time.monotonic() - began, 2)}


def port_listening(port, host='127.0.0.1', timeout=0.3):
    """端口是否有人在听。"""
    if not port:
        return False
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def port_available(port, host='127.0.0.1'):
    """端口能不能绑上；绑不上说明已被占用。"""
    if not port:
        return True
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, int(port)))
        except OSError:
            return False
    return True


_OPENER = None


def _opener():
    global _OPENER
    if _OPENER is None:
        # 本地健康检查不走系统代理，否则 localhost 可能被代理拦掉
        _OPENER = build_opener(ProxyHandler({}))
    return _OPENER


def probe_health(url, timeout=2.0):
    """请求一次健康检查地址，2xx 算通过。"""
    began = time.monotonic()
    try:
        with _opener().open(Request(url, headers={'User-Agent': 'service-deploy'}), timeout=timeout) as response:
            status = response.status
    except HTTPError as e:
        return {'url': url, 'ok': False, 'status': e.code,
                'seconds': round(time.monotonic() - began, 2), 'error': 'HTTP %s' % e.code}
    except (OSError, ValueError) as e:
        return {'url': url, 'ok': False, 'status': None,
                'seconds': round(time.monotonic() - began, 2), 'error': str(e)}
    ok = 200 <= status < 300
    return {'url': url, 'ok': ok, 'status': status,
            'seconds': round(time.monotonic() - began, 2),
            'error': None if ok else 'HTTP %s' % status}


# ---------- pid 文件 ----------

def read_pid_file(path):
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_pid_file(path, data):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix('.tmp')
    with tmp.open('w', encoding='utf-8') as f:
        os.chmod(tmp, 0o600)
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def _pid_valid(record):
    """校验 pid 记录还属于当初那个进程：服务名、存活、启动时间（防 pid 复用）。"""
    if not isinstance(record, dict) or not record.get('pid'):
        return False, 'no_record'
    info = _ps(record['pid'])
    if not info['alive']:
        return False, 'not_running'
    if record.get('lstart'):
        if info['lstart'] != record['lstart']:
            return False, 'pid_reused'
    elif record.get('marker') and record['marker'] not in (info['command'] or ''):
        return False, 'pid_reused'
    return True, ''


# ---------- 日志 ----------

def read_log(path, lines):
    """读日志末尾若干行；大文件只读尾部，避免把整份日志拖进内存。"""
    p = Path(path)
    if not p.is_file():
        return ''
    try:
        size = p.stat().st_size
        with p.open('rb') as f:
            if size > TAIL_BYTES:
                f.seek(size - TAIL_BYTES)
            data = f.read()
    except OSError:
        return ''
    text = data.decode('utf-8', 'replace')
    if size > TAIL_BYTES:
        text = text.split('\n', 1)[-1]      # 丢掉被截断的首行
    return '\n'.join(text.splitlines()[-lines:])


def _append_log(path, header, body):
    """把构建输出追加到服务日志，失败不影响构建结果。"""
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open('a', encoding='utf-8') as f:
            f.write('\n== %s ==\n' % header)
            f.write(body or '')
    except OSError:
        pass


def _tail_text(text, lines=20, chars=4000):
    return '\n'.join((text or '').splitlines()[-lines:])[-chars:]


def follow_log(path, lines):
    """像 tail -f：先打末尾若干行，再持续跟写。"""
    p = Path(path)
    while not p.exists():
        print('等待日志文件出现：%s' % p, file=sys.stderr, flush=True)
        time.sleep(0.5)
    text = read_log(p, lines)
    if text:
        print(text, flush=True)
    pos = p.stat().st_size
    try:
        while True:
            try:
                size = p.stat().st_size
            except OSError:
                size = pos
            if size < pos:                  # 日志被截断或轮转
                pos = 0
            if size > pos:
                with p.open('rb') as f:
                    f.seek(pos)
                    chunk = f.read(size - pos)
                pos += len(chunk)
                sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()
            else:
                time.sleep(0.3)
    except KeyboardInterrupt:
        pass


# ---------- 构建、启动、停止 ----------

def _env(svc):
    env = dict(os.environ)
    env.update(svc['env'])
    return env


def do_build(svc):
    if not svc['build']:
        raise DeployError('服务 %s 没有配置 build 命令' % svc['name'])
    began = time.monotonic()
    proc = subprocess.Popen(svc['build'], shell=True, executable=svc['shell'], cwd=svc['cwd'],
                            env=_env(svc), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, errors='replace', start_new_session=True)
    timed_out = False
    try:
        out, err = proc.communicate(timeout=svc['build_timeout'])
        code = proc.returncode
    except subprocess.TimeoutExpired:
        timed_out = True
        _signal(proc.pid, signal.SIGTERM)
        try:
            out, err = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            _signal(proc.pid, signal.SIGKILL)
            out, err = proc.communicate()
        code = None
    seconds = round(time.monotonic() - began, 2)
    _append_log(svc['log'], 'build %s（%s）' % (svc['name'], now_iso()), (out or '') + (err or ''))
    return {
        'service': svc['name'], 'ok': code == 0,
        'exit_code': code, 'timed_out': timed_out, 'seconds': seconds,
        'cmd': svc['build'], 'cwd': svc['cwd'], 'log': svc['log'],
        'stderr_tail': _tail_text(err or out),
    }, (0 if code == 0 else 1)


def do_start(svc, wait=None):
    name = svc['name']
    wait = svc['wait'] if wait is None else int(wait)
    pid_path = Path(svc['pid_file'])

    record = read_pid_file(pid_path)
    running, _ = _pid_valid(record)
    if running and record.get('service') != name:   # pid 文件内容对不上，不当作本服务在跑
        running = False
    if running:
        return {'service': name, 'ok': True, 'already_running': True, 'status': 'running',
                'pid': record['pid'], 'port': svc['port'], 'pid_file': str(pid_path),
                'log': svc['log']}, 0
    if record:
        pid_path.unlink(missing_ok=True)     # 失效记录先清掉，避免误判

    if svc['port'] and not port_available(svc['port']):
        raise DeployError('端口 %s 已被占用；不会去停别的进程，请先自行处理' % svc['port'])

    Path(svc['log']).parent.mkdir(parents=True, exist_ok=True)
    began = time.monotonic()
    with open(svc['log'], 'ab') as out:
        out.write(('\n== start %s ==\n' % now_iso()).encode('utf-8'))
        out.flush()
        proc = subprocess.Popen(svc['start'], shell=True, executable=svc['shell'], cwd=svc['cwd'],
                                env=_env(svc), stdin=subprocess.DEVNULL, stdout=out,
                                stderr=subprocess.STDOUT, start_new_session=True)
    info = _wait_ps(proc.pid)
    record = {
        'service': name, 'pid': proc.pid, 'started_at': now_iso(), 'started_ts': int(time.time()),
        'lstart': info['lstart'], 'command': info['command'],
        'marker': svc['start'].replace('"', '').replace("'", '')[:200],
        'port': svc['port'], 'log': svc['log'],
    }
    write_pid_file(pid_path, record)

    probe, error, healthy = None, '', None
    if svc['health']:
        deadline = began + wait
        while True:
            if not _ps(proc.pid)['alive']:
                error = '进程启动后立刻退出'
                break
            probe = probe_health(svc['health'])
            if probe['ok']:
                healthy = True
                break
            if time.monotonic() >= deadline:
                error = '健康检查超时（%s 秒）' % wait
                break
            time.sleep(0.3)
    else:
        time.sleep(0.3)
        if not _ps(proc.pid)['alive']:
            error = '进程启动后立刻退出'

    if error:
        if _ps(proc.pid)['alive']:
            _terminate(proc.pid, svc['stop_timeout'])
        pid_path.unlink(missing_ok=True)
        return {'service': name, 'ok': False, 'status': 'failed', 'error': error,
                'pid': proc.pid, 'port': svc['port'], 'healthy': False,
                'waited_seconds': round(time.monotonic() - began, 2),
                'health': probe, 'log': svc['log'],
                'log_tail': read_log(svc['log'], 10)}, 1

    return {'service': name, 'ok': True, 'status': 'running', 'pid': proc.pid,
            'port': svc['port'], 'healthy': healthy,
            'port_listening': port_listening(svc['port']) if svc['port'] else None,
            'waited_seconds': round(time.monotonic() - began, 2),
            'health': probe, 'log': svc['log'], 'pid_file': str(pid_path)}, 0


def do_stop(svc):
    pid_path = Path(svc['pid_file'])
    record = read_pid_file(pid_path)
    if not record:
        return {'service': svc['name'], 'stopped': False, 'reason': '没有 pid 记录',
                'port_listening': port_listening(svc['port']) if svc['port'] else None}, 0
    if record.get('service') != svc['name']:
        return {'service': svc['name'], 'stopped': False,
                'reason': 'pid 文件里的服务名不符，未动进程'}, 1

    valid, why = _pid_valid(record)
    if not valid:
        pid_path.unlink(missing_ok=True)
        return {'service': svc['name'], 'pid': record.get('pid'), 'stopped': False,
                'stale': True, 'reason': why,
                'port_listening': port_listening(svc['port']) if svc['port'] else None}, 0

    result = _terminate(record['pid'], svc['stop_timeout'])
    pid_path.unlink(missing_ok=True)
    return {'service': svc['name'], 'pid': record['pid'], 'stopped': True,
            'forced': result['forced'], 'seconds': result['seconds'],
            'port_listening': port_listening(svc['port']) if svc['port'] else None}, 0


def do_status(svc):
    record = read_pid_file(svc['pid_file'])
    running, why = _pid_valid(record)
    info = _ps(record['pid']) if record.get('pid') else {'lstart': '', 'command': ''}
    uptime = None
    if running and record.get('started_ts'):
        uptime = max(0, int(time.time() - record['started_ts']))
    probe = probe_health(svc['health']) if svc['health'] else None
    return {
        'service': svc['name'],
        'status': 'running' if running else ('stale' if record else 'stopped'),
        'process': {
            'pid': record.get('pid'),
            'alive': running,
            'uptime_seconds': uptime,
            'lstart': (info.get('lstart') or record.get('lstart')) if running else None,
            'command': (info.get('command') or None) if running else None,
            'reason': why if record and not running else '',
        },
        'port': svc['port'],
        'port_listening': port_listening(svc['port']) if svc['port'] else None,
        'health': probe,
        'log': svc['log'],
        'log_tail': read_log(svc['log'], 20),
    }, 0


def do_list(config):
    rows = []
    for name in sorted(config['services']):
        svc = config['services'][name]
        record = read_pid_file(svc['pid_file'])
        running, _ = _pid_valid(record)
        uptime = None
        if running and record.get('started_ts'):
            uptime = max(0, int(time.time() - record['started_ts']))
        rows.append({
            'name': name, 'dir': svc['dir'], 'port': svc['port'],
            'port_listening': port_listening(svc['port']) if svc['port'] else None,
            'process': {'pid': record.get('pid'), 'alive': running,
                        'uptime_seconds': uptime,
                        'stale': bool(record) and not running},
            'health': svc['health'], 'log': svc['log'],
        })
    return {'config': config['path'], 'state_dir': config['state_dir'], 'services': rows}, 0


# ---------- 子命令 ----------

def cmd_list(args, config):
    return do_list(config)


def cmd_build(args, config):
    return do_build(require_service(config, args.service))


def cmd_start(args, config):
    return do_start(require_service(config, args.service), args.wait)


def cmd_stop(args, config):
    return do_stop(require_service(config, args.service))


def cmd_restart(args, config):
    svc = require_service(config, args.service)
    out = {'service': svc['name']}
    out['stop'], _ = do_stop(svc)
    if not args.skip_build and svc['build']:
        out['build'], code = do_build(svc)
        if code:
            out.update(ok=False, error='构建失败，未启动')
            return out, code
    else:
        out['build'] = {'skipped': True}
    out['start'], code = do_start(svc, args.wait)
    out['ok'] = bool(out['start'].get('ok'))
    return out, code


def cmd_status(args, config):
    return do_status(require_service(config, args.service))


def cmd_logs(args, config):
    svc = require_service(config, args.service)
    path = Path(svc['log'])
    if args.follow:
        follow_log(path, args.lines)
        return None, 0
    return {'service': svc['name'], 'path': str(path), 'exists': path.is_file(),
            'bytes': path.stat().st_size if path.is_file() else 0,
            'lines': args.lines, 'text': read_log(path, args.lines)}, 0


def build_parser():
    parser = argparse.ArgumentParser(prog='service_deploy',
                                     description='本地服务部署：构建、起停、状态、日志（JSON 输出）')
    sub = parser.add_subparsers(dest='cmd', required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--config', default='',
                        help='services.json 路径（默认 ./services.json 或 '
                             '~/.config/workbench-skills/services.json）')

    p = sub.add_parser('list', parents=[common], help='列出服务、进程状态、端口监听')
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser('build', parents=[common], help='跑 build 命令，返回耗时和退出码')
    p.add_argument('--service', required=True)
    p.set_defaults(fn=cmd_build)

    p = sub.add_parser('start', parents=[common], help='后台启动服务并等待健康检查')
    p.add_argument('--service', required=True)
    p.add_argument('--wait', type=int, default=None, help='健康检查超时秒数（默认取配置 wait，60）')
    p.set_defaults(fn=cmd_start)

    p = sub.add_parser('stop', parents=[common], help='按 pid 文件停止服务（先 TERM 后 KILL）')
    p.add_argument('--service', required=True)
    p.set_defaults(fn=cmd_stop)

    p = sub.add_parser('restart', parents=[common], help='stop + build + start')
    p.add_argument('--service', required=True)
    p.add_argument('--skip-build', action='store_true', help='跳过构建直接重启')
    p.add_argument('--wait', type=int, default=None)
    p.set_defaults(fn=cmd_restart)

    p = sub.add_parser('status', parents=[common], help='进程、端口、健康检查、最近 20 行日志')
    p.add_argument('--service', required=True)
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser('logs', parents=[common], help='看服务日志')
    p.add_argument('--service', required=True)
    p.add_argument('-n', dest='lines', type=int, default=200, help='末尾行数，默认 200')
    p.add_argument('-f', dest='follow', action='store_true', help='持续跟随输出（非 JSON）')
    p.set_defaults(fn=cmd_logs)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
        result, code = args.fn(args, config)
    except DeployError as e:
        print(json.dumps({'error': str(e)}, ensure_ascii=False), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    if result is not None:
        _emit(result)
    return code


if __name__ == '__main__':
    sys.exit(main())
