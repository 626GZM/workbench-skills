#!/usr/bin/env python3
"""把 lib/wbkit 同步进每个 skill 的 scripts/_lib/wbkit。

skill 的分发单位是"一个目录"，跨目录 import 在别人拷走后必然失效，所以用 vendoring：
lib/wbkit 是唯一真源，这里复制出一份副本，并写 _lib/.manifest.json 记录哈希。
check_drift.py 在 CI 里比对，防止有人手改副本导致各 skill 行为不一致。

用法：
    python3 tools/sync_lib.py            # 同步
    python3 tools/sync_lib.py --check    # 只检查，不写（有漂移时退出码 1）
"""
import hashlib
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIB = ROOT / 'lib' / 'wbkit'
SKILLS = ROOT / 'skills'
MANIFEST = '.manifest.json'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def lib_files():
    return sorted(p for p in LIB.rglob('*') if p.is_file() and '__pycache__' not in p.parts)


def target_dirs():
    return sorted(p for p in SKILLS.glob('*/scripts') if p.is_dir())


def wanted_manifest():
    return {str(p.relative_to(LIB)): digest(p) for p in lib_files()}


def sync(check=False):
    manifest = wanted_manifest()
    problems = []
    for scripts in target_dirs():
        dest = scripts / '_lib' / 'wbkit'
        if check:
            if not dest.is_dir():
                problems.append('%s 缺少 _lib/wbkit' % scripts.parent.name)
                continue
            for rel, want in manifest.items():
                got = dest / rel
                if not got.is_file():
                    problems.append('%s 缺少 %s' % (scripts.parent.name, rel))
                elif digest(got) != want:
                    problems.append('%s 的 %s 与 lib 不一致' % (scripts.parent.name, rel))
            mf = scripts / '_lib' / MANIFEST
            if not mf.is_file() or json.loads(mf.read_text(encoding='utf-8')) != manifest:
                problems.append('%s 的清单过期' % scripts.parent.name)
            continue
        if dest.exists():
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(LIB, dest, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        (scripts / '_lib' / MANIFEST).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + '\n', encoding='utf-8')
        print('同步 %s' % scripts.parent.name)
    return problems


def main(argv):
    check = '--check' in argv
    problems = sync(check=check)
    if check:
        if problems:
            print('有漂移：')
            for p in problems:
                print('  - ' + p)
            print('\n跑 python3 tools/sync_lib.py 修复')
            return 1
        print('所有 skill 的 _lib 与 lib/wbkit 一致')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
