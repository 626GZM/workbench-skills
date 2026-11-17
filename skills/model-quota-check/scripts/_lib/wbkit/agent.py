"""多模型无头调用。

- claude：本机 Claude Code（`claude -p`）
- codex：本机 Codex CLI（`codex exec`）
- deepseek / kimi：借 Claude Code 的壳，只对这一个子进程把地址、key、模型换成对应厂商
  （DeepSeek 官方 Anthropic 兼容地址；Kimi 走 Kimi Code 的订阅地址）

权限用 profile 表达（read / verify / coder / docs），无头运行不能弹窗，白名单外的工具一律拒绝。
key 从环境变量或 secrets.json 读，只通过子进程环境变量传递。
"""
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import config as cfg

VENDORS = {
    'deepseek': {'base_url': 'https://api.deepseek.com/anthropic',
                 'key_env': 'ANTHROPIC_AUTH_TOKEN', 'model': 'deepseek-chat'},
    'kimi': {'base_url': 'https://api.kimi.com/coding/',
             'key_env': 'ANTHROPIC_API_KEY', 'model': 'kimi-for-coding'},
}
ENGINES = ('claude', 'codex') + tuple(VENDORS)

READ_ONLY_TOOLS = ('Read Grep Glob Bash(git log:*) Bash(git diff:*) Bash(git show:*) '
                   'Bash(git status:*) Bash(ls:*)')
GIT_READ = 'Bash(git log:*) Bash(git diff:*) Bash(git show:*) Bash(git status:*) Bash(ls:*)'

PROFILES = {
    'read': {'allow': READ_ONLY_TOOLS, 'write': False},
    'verify': {'allow': 'Read Grep Glob Bash(mvn:*) Bash(gradle:*) ' + GIT_READ +
                        ' Edit(**/src/test/**) Write(**/src/test/**)', 'write': True},
    'coder': {'allow': 'Read Grep Glob Edit Write MultiEdit Task Agent Bash', 'write': True},
    'docs': {'allow': 'Read Grep Glob', 'write': True},
}
QUOTA_WORDS = ('rate limit', 'rate_limit', 'quota', 'usage limit', 'insufficient', '额度', '限流',
               'too many requests', '429', 'credit')
FIRST_REPLY_SECONDS = 150
IDLE_SECONDS = 600


class AgentError(Exception):
    pass


def system_proxy():
    """macOS 系统代理（Clash 之类）。从程序坞启动的 App 没有 HTTPS_PROXY，命令行工具不会自己读系统代理。"""
    try:
        out = subprocess.run(['scutil', '--proxy'], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return None
    kv = dict(re.findall(r'^\s*(\w+)\s*:\s*(\S+)\s*$', out, re.M))
    for kind in ('HTTPS', 'HTTP'):
        if kv.get(kind + 'Enable') == '1' and kv.get(kind + 'Proxy') and kv.get(kind + 'Port'):
            return 'http://%s:%s' % (kv[kind + 'Proxy'], kv[kind + 'Port'])
    return None


def with_proxy(env):
    """环境里没设代理、系统设了，就补上（不覆盖已有的）。"""
    if any(env.get(k) for k in ('HTTPS_PROXY', 'https_proxy', 'ALL_PROXY', 'all_proxy')):
        return env
    px = system_proxy()
    if px:
        env.update(HTTPS_PROXY=px, HTTP_PROXY=px, https_proxy=px, http_proxy=px)
        env.setdefault('NO_PROXY', 'localhost,127.0.0.1,::1')
    return env


DESKTOP_CLAUDE = Path.home() / 'Library' / 'Application Support' / 'Claude' / 'claude-code'


def _version(binary):
    try:
        out = subprocess.run([binary, '--version'], capture_output=True, text=True, timeout=20).stdout
        return tuple(int(x) for x in out.split()[0].split('.'))
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return ()


def _claude_candidates():
    """命令行装的 + Claude 桌面端自带的（随 App 自动更新）。"""
    return ([shutil.which('claude')] if shutil.which('claude') else []) + \
        [str(p) for p in DESKTOP_CLAUDE.glob('*/*/claude.app/Contents/MacOS/claude')]


def find_cli(engine, conf=None):
    """找可执行文件。claude 优先挑版本最高的（桌面端自带的往往比命令行新）；
    codex 从 PATH 找；配置里 models.<engine>.cli 可以指定绝对路径。"""
    spec = ((conf or cfg.load()).get('models') or {}).get(engine) or {}
    binary = spec.get('cli') or ('codex' if engine == 'codex' else 'claude')
    if os.path.isabs(binary):
        path = binary if os.path.exists(binary) else None
    elif os.path.basename(binary) == 'claude':
        candidates = _claude_candidates()
        path = max(candidates, key=_version) if candidates else None
    else:
        path = shutil.which(binary)
    if not path:
        raise AgentError('没有找到 %s 命令（%s）。先装好，或在配置里把 models.%s.cli 指向它的绝对路径'
                         % (binary, engine, engine))
    return path


def command(engine, prompt, cwd, model=None, profile='read', dirs=(), resume=None, conf=None):
    """返回 (argv, env)。提示词不进 argv，由调用方写进 stdin。"""
    conf = conf or cfg.load()
    env = with_proxy(dict(os.environ))
    prof = PROFILES.get(profile) or PROFILES['read']
    model = model or (((conf.get('models') or {}).get(engine) or {}).get('model') or '').strip() or None
    if engine == 'codex':
        argv = [find_cli('codex', conf), 'exec', '--skip-git-repo-check', '-C', str(cwd),
                '-s', 'workspace-write' if prof['write'] else 'read-only']
        if resume:
            argv += ['resume', str(resume)]
        for d in dirs:
            argv += ['--add-dir', str(d)]
        if model:
            argv += ['-m', model]
        return argv, env

    allow = prof['allow']
    argv = [find_cli('claude', conf), '-p', '--output-format', 'json', '--no-session-persistence',
            '--permission-mode', 'dontAsk', '--allowedTools', allow,
            '--disallowedTools', 'WebFetch WebSearch NotebookEdit']
    if resume:
        argv += ['--resume', str(resume)]
    for d in dirs:
        argv += ['--add-dir', str(d)]
    if engine in VENDORS:
        v = VENDORS[engine]
        key = cfg.api_key(engine)
        if not key:
            raise AgentError('没有 %s 的 key：设环境变量 %s，或写进 ~/.config/workbench-skills/secrets.json'
                             % (engine, (conf.get('models') or {}).get(engine, {}).get('key_env') or 'API_KEY'))
        # 只改这一个子进程：清掉继承来的 Anthropic 登录，换成对应厂商的地址、key、模型
        for k in [k for k in env if k.startswith(('ANTHROPIC_', 'CLAUDE_CODE_', 'CLAUDECODE'))]:
            env.pop(k)
        env['ANTHROPIC_BASE_URL'] = v['base_url']
        env[v['key_env']] = key
        model = model or v['model']
        env['ANTHROPIC_MODEL'] = env['ANTHROPIC_SMALL_FAST_MODEL'] = model
        for alias in ('OPUS', 'SONNET', 'HAIKU'):
            env['ANTHROPIC_DEFAULT_%s_MODEL' % alias] = model
    if model:
        argv += ['--model', model]
    return argv, env


def _json_lines(text):
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith('{'):
            continue
        try:
            yield json.loads(line)
        except ValueError:
            continue


def _has_reply(log):
    try:
        return Path(log).stat().st_size > 200
    except OSError:
        return False


def kill_group(proc):
    """连它起的子孙进程（maven、node、java）一起结束，不然残留占着端口。"""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        proc.kill()
    proc.wait()


def _watch(proc, log, engine, start, timeout):
    last_size, last_change = -1, time.time()
    while True:
        try:
            proc.wait(timeout=5)
            return None
        except subprocess.TimeoutExpired:
            pass
        now = time.time()
        size = Path(log).stat().st_size if Path(log).exists() else 0
        if size != last_size:
            last_size, last_change = size, now
        reason = None
        if now - start > timeout:
            reason = '超时（%d 秒）' % timeout
        elif engine not in ('codex',) and now - start > FIRST_REPLY_SECONDS and not _has_reply(log):
            reason = '没有回应：启动 %d 秒了模型还没回一个字，多半是额度用完被限流' % (now - start)
        elif now - last_change > IDLE_SECONDS:
            reason = '卡住：%d 秒没有任何新动作' % (now - last_change)
        if reason:
            kill_group(proc)
            return reason


def quota_exhausted(result):
    return not result.get('ok') and any(w in (result.get('error') or '').lower() for w in QUOTA_WORDS)


def run(engine, prompt, cwd, model=None, profile='read', timeout=1200, dirs=(), log=None,
        env_extra=None, conf=None):
    """跑一次，返回 {ok, engine, seconds, text, models, error, quota, log}。超时、限流都返回 ok=False，不抛异常。"""
    cwd = Path(cwd)
    if not cwd.is_dir():
        raise AgentError('工作目录不存在：%s' % cwd)
    argv, env = command(engine, prompt, cwd, model, profile, dirs, conf=conf)
    env.update(env_extra or {})
    if engine == 'codex':
        at = 3 if argv[2:3] == ['resume'] else 2
        argv = argv[:at] + ['--json'] + argv[at:]
    else:
        i = argv.index('--output-format')
        argv[i + 1] = 'stream-json'
        argv.insert(i + 2, '--verbose')
    log = Path(log) if log else Path(tempfile.mkstemp(prefix='agent-', suffix='.log')[1])
    log.parent.mkdir(parents=True, exist_ok=True)
    marker = Path(str(log) + '.running')
    start = time.time()
    try:
        with open(log, 'w', encoding='utf-8') as out_f:
            proc = subprocess.Popen(
                argv, cwd=str(cwd), env=env, stdout=out_f, stderr=subprocess.STDOUT, text=True,
                start_new_session=True, stdin=subprocess.DEVNULL if engine == 'codex' else subprocess.PIPE)
            marker.write_text(json.dumps({'engine': engine, 'pid': proc.pid, 'started_at': start,
                                          'log': str(log)}, ensure_ascii=False))
            if engine != 'codex':
                proc.stdin.write(prompt)
                proc.stdin.close()
            stalled = _watch(proc, log, engine, start, timeout)
    except FileNotFoundError:
        return {'ok': False, 'engine': engine, 'seconds': 0, 'error': '没有找到 %s 命令' % argv[0],
                'quota': False, 'log': str(log)}
    finally:
        marker.unlink(missing_ok=True)
    text_out = log.read_text(encoding='utf-8', errors='replace')
    out = {'engine': engine, 'seconds': round(time.time() - start), 'log': str(log)}
    if stalled:
        return dict(out, ok=False, error=stalled, quota=stalled.startswith('没有回应'))
    if engine == 'codex':
        text, session = '', None
        for e in _json_lines(text_out):
            if e.get('type') == 'thread.started' and e.get('thread_id'):
                session = e['thread_id']
            item = e.get('item') or e.get('msg') or {}
            if isinstance(item, dict) and item.get('type') in ('agent_message', 'assistant_message') and item.get('text'):
                text = item['text']
        out.update(ok=proc.returncode == 0, text=text or text_out[-4000:],
                   models=[model or 'codex 默认'],
                   error=None if proc.returncode == 0 else (text_out.strip()[-500:] or '退出码 %d' % proc.returncode))
        out['quota'] = quota_exhausted(out)
        out['session'] = session
        return out
    res = next((e for e in reversed(list(_json_lines(text_out))) if e.get('type') == 'result'), None)
    if res is None:
        return dict(out, ok=False, error=text_out.strip()[-500:] or '退出码 %d' % proc.returncode,
                    quota=quota_exhausted({'ok': False, 'error': text_out[-2000:]}))
    out = dict(out, ok=not res.get('is_error') and proc.returncode == 0,
               text=str(res.get('result') or '').strip(),
               models=sorted((res.get('modelUsage') or {}).keys()),
               error=str(res.get('result'))[-500:] if res.get('is_error') else None)
    out['quota'] = quota_exhausted(out)
    return out


# ---------- 轻量纯文本补全：不起 CLI 进程，直接 POST chat/completions ----------

CHAT_VENDORS = {
    'deepseek': {'url': 'https://api.deepseek.com/chat/completions',
                 'model': 'deepseek-chat', 'key_env': 'DEEPSEEK_API_KEY'},
}


def chat(prompt, model=None, temperature=0.3, max_tokens=12000, vendor='deepseek', conf=None):
    """轻量纯文本补全：把一段提示词变成一段文本（仿写、改写这类），HTTP 直调厂商 chat/completions。

    要读文件、跑命令的还是用 run()。没配 key、网络失败都抛 AgentError，由调用方自己兜底。
    模型默认取配置 models.<vendor>.chat_model，再退到 CHAT_VENDORS 里的默认。
    """
    conf = conf or cfg.load()
    spec = CHAT_VENDORS.get(vendor)
    if not spec:
        raise AgentError('chat 不支持 %s（可选 %s）' % (vendor, '/'.join(CHAT_VENDORS)))
    key = cfg.api_key(vendor)
    if not key:
        raise AgentError('没有 %s 的 key：设环境变量 %s，或写进 ~/.config/workbench-skills/secrets.json'
                         % (vendor, spec['key_env']))
    model = model or cfg.get('models.%s.chat_model' % vendor, conf=conf) or spec['model']
    body = json.dumps({'model': model, 'messages': [{'role': 'user', 'content': str(prompt)}],
                       'max_tokens': max_tokens, 'temperature': temperature}).encode('utf-8')
    req = urllib.request.Request(spec['url'], data=body,
                                 headers={'Authorization': 'Bearer ' + key,
                                          'Content-Type': 'application/json'})
    env = with_proxy(dict(os.environ))
    px = next((env[k] for k in ('HTTPS_PROXY', 'https_proxy') if env.get(k)), None)
    try:
        if px:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({'http': px, 'https': px}))
            resp = opener.open(req, timeout=180)
        else:
            resp = urllib.request.urlopen(req, timeout=180)
        with resp:
            data = json.loads(resp.read().decode('utf-8'))
        return data['choices'][0]['message']['content'] or ''
    except urllib.error.HTTPError as e:
        raise AgentError('%s 返回 %s：%s' % (vendor, e.code, e.read().decode('utf-8', 'replace')[:300]))
    except urllib.error.URLError as e:
        raise AgentError('%s 连不上：%s' % (vendor, e.reason))
    except (KeyError, IndexError, ValueError) as e:
        raise AgentError('%s 返回格式不对：%r' % (vendor, e))
