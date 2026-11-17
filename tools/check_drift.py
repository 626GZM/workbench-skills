#!/usr/bin/env python3
"""CI 用：检查各 skill 里的 _lib/wbkit 是否与 lib/wbkit 一致。

等价于 python3 tools/sync_lib.py --check，单独放一个文件是为了让 CI 里语义更清楚。
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

if __name__ == '__main__':
    sys.exit(subprocess.call([sys.executable, str(ROOT / 'tools' / 'sync_lib.py'), '--check'])) 
