#!/usr/bin/env python3
"""无头调用一家 AI 编程 CLI，拿回结构化结果。

子命令：
  run       跑一次（提示词从 --prompt / --prompt-file / stdin 取）
  check     看本机哪些引擎可用、用的是哪个可执行文件
  set-key   把某家的 key 写进 secrets.json（权限 600）
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / '_lib'))

from wbkit import agent, config as cfg  # noqa: E402


def emit(data):
    print(json.dumps(data, ensure_ascii=False, indent=2))


def read_prompt(a):
    if a.prompt:
        return a.prompt
    if a.prompt_file:
        return Path(a.prompt_file).expanduser().read_text(encoding='utf-8')
    if not sys.stdin.isatty():
        return sys.stdin.read()
    raise SystemExit('没有提示词：用 --prompt、--prompt-file，或从 stdin 传入')


def cmd_run(a):
    if a.dry_run:
        argv, env = agent.command(a.engine, '', a.cwd, model=a.model, profile=a.profile,
                                   dirs=a.dirs, resume=a.resume)
        # argv 里可能带 key 的环境变量名，不打印 env 的值
        emit({'argv': argv, 'env_keys': sorted(k for k in env if 'KEY' in k or 'TOKEN' in k or 'MODEL' in k),
              'cwd': str(Path(a.cwd).resolve()), 'profile': a.profile})
        return 0
    prompt = read_prompt(a)
    try:
        result = agent.run(a.engine, prompt, a.cwd, model=a.model, profile=a.profile,
                           timeout=a.timeout, dirs=a.dirs, log=a.log, conf=None)
    except agent.AgentError as e:
        print(json.dumps({'ok': False, 'engine': a.engine, 'error': str(e)}, ensure_ascii=False,
                         indent=2), file=sys.stderr)
        return 2
    emit(result)
    return 0 if result.get('ok') else 1


def cmd_check(a):
    out = []
    for engine in agent.ENGINES:
        item = {'engine': engine}
        try:
            item['cli'] = agent.find_cli(engine)
            item['ready'] = True
        except agent.AgentError as e:
            item['cli'] = None
            item['ready'] = False
            item['error'] = str(e)
        if engine in agent.VENDORS:
            item['has_key'] = bool(cfg.api_key(engine))
        out.append(item)
    emit({'engines': out, 'config': cfg.load().get('_config_path'),
          'secrets': str(cfg.SECRETS_PATH)})
    return 0


def cmd_set_key(a):
    key = a.key or (sys.stdin.read().strip() if not sys.stdin.isatty() else '')
    if not key:
        raise SystemExit('没有 key：用 --key，或从 stdin 传入')
    cfg.set_secret(a.vendor, key)
    emit({'ok': True, 'vendor': a.vendor, 'path': str(cfg.SECRETS_PATH),
          'hint': 'key 已写入，权限 600；调用时只会传给这一家模型的子进程'})
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description='无头调用 AI 编程 CLI，输出 JSON')
    sub = p.add_subparsers(dest='cmd', required=True)

    s = sub.add_parser('run', help='跑一次')
    s.add_argument('--engine', required=True, choices=list(agent.ENGINES))
    s.add_argument('--prompt', default='', help='提示词（也可用 --prompt-file 或 stdin）')
    s.add_argument('--prompt-file', default='')
    s.add_argument('--cwd', default='.', help='工作目录（模型看到的世界）')
    s.add_argument('--model', default='', help='覆盖配置里的模型名')
    s.add_argument('--profile', default='read', choices=sorted(agent.PROFILES))
    s.add_argument('--timeout', type=int, default=1200, help='秒，默认 1200')
    s.add_argument('--dirs', action='append', default=[], help='额外允许访问的目录，可重复')
    s.add_argument('--resume', default='', help='续上一次的会话 id')
    s.add_argument('--log', default='', help='日志文件路径，默认临时文件')
    s.add_argument('--dry-run', action='store_true', help='只打印将执行的命令，不真跑')
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser('check', help='检查本机哪些引擎可用')
    s.set_defaults(fn=cmd_check)

    s = sub.add_parser('set-key', help='保存某家的 key（权限 600）')
    s.add_argument('--vendor', required=True, choices=list(agent.VENDORS))
    s.add_argument('--key', default='', help='不传则从 stdin 读')
    s.set_defaults(fn=cmd_set_key)

    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == '__main__':
    sys.exit(main())
