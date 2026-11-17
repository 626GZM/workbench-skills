#!/usr/bin/env python3
"""跑测试 → 读失败 → 让模型改 → 再跑，直到通过或轮次用完。

只负责"把这次跑失败的测试修到过"：失败的编译、失败的断言、失败的用例。
不做重构、不加功能、不动无关文件。每轮都会重跑同一条命令，改坏了下一轮就能看出来。

子命令：
  run   跑一轮或多轮修复循环
  parse 只解析已有的测试输出（调试用）

示例：
  test_fix_loop.py run --cmd "mvn -q -pl core -Dtest=FooTest test" --cwd ~/repo
  test_fix_loop.py run --cmd "pytest -x tests/test_api.py" --engine codex --max-rounds 3
  test_fix_loop.py run --cmd "npm test -- --run" --dry-run
"""
import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / '_lib'))

from wbkit import agent  # noqa: E402

MAX_OUTPUT = 200 * 1024        # 命令输出只留最后这么多字节
PROMPT_OUTPUT = 12000          # 喂给模型的失败片段上限
MAX_FAILURES = 40              # 单轮最多列这么多条失败
MAX_ROUNDS = 5
FILE_LINE = re.compile(r'([\w./@+-]+\.(?:java|kt|py|ts|tsx|js|jsx|go|rb|rs|cs|php|scala|sh)):(\d+)')
PY_FILE_LINE = re.compile(r'File "([^"]+)", line (\d+)')
NOISE = re.compile(r'^\s*(?:at\s+[\w.$]+\(|\.\.\.\s*\d+\s+more)')


def _clip(text, limit):
    if len(text) <= limit:
        return text
    return '…（前 %d 字符已省略）\n' % (len(text) - limit) + text[-limit:]


def run_command(cmd, cwd, timeout):
    """跑一条命令，返回 (退出码, 输出尾部, 耗时)。超时返回 124。"""
    t0 = time.time()
    try:
        p = subprocess.run(cmd, shell=True, cwd=str(cwd), capture_output=True, text=True,
                           timeout=timeout, errors='replace')
        out = (p.stdout or '') + (p.stderr or '')
        return p.returncode, _clip(out, MAX_OUTPUT), round(time.time() - t0, 1)
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or '') + (e.stderr or '')
        if isinstance(out, bytes):
            out = out.decode('utf-8', 'replace')
        return 124, _clip(out + '\n[超时] 命令跑了 %d 秒没结束' % timeout, MAX_OUTPUT), round(time.time() - t0, 1)
    except OSError as e:
        return 127, '[无法执行] %s' % e, round(time.time() - t0, 1)


SUMMARY_PATTERNS = (
    r'^Tests run:.*$',
    r'^\d+ (?:passed|failed).*$',
    r'^=+ .*(?:passed|failed).*=+$',
    r'^Test Suites?:.*$',
    r'^Tests:\s+.*$',
    r'^FAILED .*$',
    r'^BUILD (?:FAILURE|SUCCESS)$',
    r'^\[ERROR\].*$',
    r'^ERROR:.*$',
)


def parse_failures(text):
    """从测试/构建输出里抠失败信息。跨 Maven、Gradle、pytest、jest、go test 通用。"""
    lines = text.splitlines()
    picked, seen = [], set()
    for line in lines:
        s = line.strip()
        if not s or NOISE.match(line):
            continue
        hit = (any(re.match(p, s) for p in SUMMARY_PATTERNS)
               or ' ✕ ' in s or s.startswith('✕')
               or ' FAILED' in s or 'FAILED:' in s
               or re.search(r'(?i)\b(assertionerror|expect(?:ed)? .* but|expected:<|panic:)\b', s))
        if hit and s not in seen:
            seen.add(s)
            picked.append(s)
        if len(picked) >= MAX_FAILURES:
            break
    files = []
    for f, n in FILE_LINE.findall(text) + PY_FILE_LINE.findall(text):
        pair = '%s:%s' % (f, n)
        if pair not in files:
            files.append(pair)
        if len(files) >= 20:
            break
    summary = next((s for s in reversed(picked) if re.search(r'(?i)tests? run|passed|failed|build ', s)), '')
    return {'summary': summary, 'lines': picked, 'signals': files,
            'empty': not picked}


NOISE_DIRS = ('__pycache__/', '.pytest_cache/', 'node_modules/', 'target/', 'build/', 'dist/', '.venv/')


def changed_files(cwd):
    """工作区里被改动的文件。不是 git 仓库返回 None。"""
    try:
        p = subprocess.run(['git', 'status', '--porcelain'], cwd=str(cwd), capture_output=True,
                           text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode != 0:
        return None
    out = []
    for line in p.stdout.splitlines():
        path = line[3:].strip()
        if ' -> ' in path:
            path = path.split(' -> ', 1)[1]
        if path and not path.startswith(NOISE_DIRS):
            out.append(path)
    return out


def signature(failures):
    """失败签名：两次一样说明没进展，继续改只会空转。"""
    return '|'.join(failures.get('lines') or [])[:2000] or (failures.get('summary') or '')


def fix_prompt(cmd, failures, scope, round_no, last_text):
    allow = ('只能改测试代码（src/test 下），不能改业务代码' if scope == 'test'
             else '可以改测试，也可以改被测代码；但只能改与这次失败直接相关的部分')
    return """你在修测试失败。第 %d 轮。

命令：%s

规则：
1. %s。
2. 只解决下面这些失败，不要重构、不要顺手改别的、不要加新功能。
3. 不要跑完整测试套件（很慢）；改完直接结束，我会重新跑。
4. 如果判断是测试本身写错了，可以改测试，但要说明理由。
5. 如果信息不足、或者问题是环境/依赖缺失，不要瞎改，直接说明卡在哪。

失败输出：
```
%s
```

上一轮你说了什么（可参考，别重复同样的改法）：
```
%s
```
""" % (round_no, cmd, allow, _clip('\n'.join(failures.get('lines') or []), PROMPT_OUTPUT),
       _clip(last_text or '', 1500))


def one_round(a, round_no, last_text):
    code, out, seconds = run_command(a.cmd, a.cwd, a.timeout)
    failures = parse_failures(out)
    rec = {'round': round_no, 'exit': code, 'seconds': seconds,
           'summary': failures.get('summary'), 'failures': failures.get('lines')[:10],
           'signals': failures.get('signals')}
    if code == 0:
        rec['passed'] = True
        return rec, failures, out
    if failures.get('empty') and code == 124:
        rec['note'] = '命令超时，没有可修的失败信息'
        return rec, failures, out
    if a.dry_run:
        return rec, failures, out
    profile = 'read' if a.analyze_only else ('verify' if a.scope == 'test' else 'coder')
    prompt = fix_prompt(a.cmd, failures, a.scope, round_no, last_text)
    try:
        res = agent.run(a.engine, prompt, a.cwd, model=a.model, profile=profile,
                        timeout=a.agent_timeout, conf=None)
    except agent.AgentError as e:
        rec['agent'] = {'ok': False, 'error': str(e)}
        return rec, failures, out
    rec['agent'] = {'ok': res.get('ok'), 'engine': res.get('engine'), 'seconds': res.get('seconds'),
                    'error': res.get('error'), 'text': _clip(res.get('text') or '', 1500)}
    rec['changed'] = changed_files(a.cwd)
    return rec, failures, out


def cmd_run(a):
    cwd = Path(a.cwd).expanduser()
    if not cwd.is_dir():
        raise SystemExit('工作目录不存在：%s' % cwd)
    a.cwd = str(cwd)
    a.max_rounds = max(1, min(MAX_ROUNDS, a.max_rounds))
    t0 = time.time()
    rounds, last_sig, last_text, ok, reason = [], None, '', False, ''
    for i in range(1, a.max_rounds + 1):
        rec, failures, out = one_round(a, i, last_text)
        rounds.append(rec)
        if rec.get('passed'):
            ok, reason = True, '通过'
            break
        if rec.get('agent') and not rec['agent'].get('ok'):
            reason = '模型调用失败：%s' % (rec['agent'].get('error') or '未知')
            break
        sig = signature(failures)
        if sig and sig == last_sig:
            reason = '连续两轮失败一模一样，模型没改动到点子上，停'
            break
        last_sig = sig
        last_text = (rec.get('agent') or {}).get('text') or ''
        if a.analyze_only:
            reason = '只分析不改（--analyze-only）'
            break
        if a.dry_run:
            reason = '试运行：只跑了一次命令'
            break
    if not reason:
        reason = '轮次用完仍失败'
    result = {'ok': ok, 'reason': reason, 'command': a.cmd, 'cwd': a.cwd,
              'rounds': rounds, 'seconds': round(time.time() - t0, 1),
              'changed_files': sorted({f for r in rounds for f in (r.get('changed') or [])})}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if ok else 1


def cmd_parse(a):
    text = Path(a.file).expanduser().read_text(encoding='utf-8', errors='replace') if a.file else sys.stdin.read()
    print(json.dumps(parse_failures(text), ensure_ascii=False, indent=2))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description='测试失败自动修复循环（只用本地 CLI，不连外部系统）')
    sub = p.add_subparsers(dest='cmd', required=True)

    s = sub.add_parser('run', help='跑测试并自动修到过')
    s.add_argument('--cmd', required=True, help='测试/构建命令，例如 "mvn -q test"')
    s.add_argument('--cwd', default='.', help='命令和模型的工作目录')
    s.add_argument('--engine', default='claude', choices=list(agent.ENGINES))
    s.add_argument('--model', default='', help='覆盖配置里的模型名')
    s.add_argument('--max-rounds', type=int, default=3, help='最多几轮，1-5，默认 3')
    s.add_argument('--scope', default='test', choices=['test', 'any'],
                   help='test=只让改测试代码（默认）；any=也允许改业务代码')
    s.add_argument('--timeout', type=int, default=1800, help='单次命令超时秒数，默认 1800')
    s.add_argument('--agent-timeout', type=int, default=1200, help='单轮模型超时秒数，默认 1200')
    s.add_argument('--analyze-only', action='store_true', help='只让模型给诊断，不改文件')
    s.add_argument('--dry-run', action='store_true', help='只跑一次命令并列出失败，不调模型')
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser('parse', help='解析已有的测试输出')
    s.add_argument('--file', default='', help='输出文件，不传则读 stdin')
    s.set_defaults(fn=cmd_parse)

    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == '__main__':
    sys.exit(main())
