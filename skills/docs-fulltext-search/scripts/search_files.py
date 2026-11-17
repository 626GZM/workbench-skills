#!/usr/bin/env python3
"""目录全文索引与搜索：建索引、搜关键词、看统计、删索引。

索引只是缓存，原文件始终是事实来源；随时可重建，删库不影响任何文件。
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / '_lib'))

from wbkit import index  # noqa: E402


def emit(data):
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _exts(text):
    return [e.strip() for e in text.split(',') if e.strip()] if text else None


def cmd_build(a):
    stats = index.build(a.root, exts=_exts(a.ext),
                        progress=lambda n, body: print('  %d 个文件…' % n, file=sys.stderr))
    emit(stats)


def cmd_search(a):
    hits = index.search(a.root, a.query, limit=a.limit, exts=_exts(a.ext))
    emit({'root': a.root, 'query': a.query, 'count': len(hits), 'hits': hits})


def cmd_stats(a):
    emit(index.stats(a.root))


def cmd_drop(a):
    emit(index.drop(a.root))


def main(argv=None):
    p = argparse.ArgumentParser(description='目录全文索引与搜索（SQLite FTS5）')
    sub = p.add_subparsers(dest='cmd', required=True)

    s = sub.add_parser('build', help='重建索引')
    s.add_argument('--root', required=True)
    s.add_argument('--ext', default='', help='只索引这些扩展名，逗号分隔，如 md,py')
    s.set_defaults(fn=cmd_build)

    s = sub.add_parser('search', help='搜关键词')
    s.add_argument('--root', required=True)
    s.add_argument('--query', required=True)
    s.add_argument('--limit', type=int, default=20)
    s.add_argument('--ext', default='')
    s.set_defaults(fn=cmd_search)

    s = sub.add_parser('stats', help='索引统计')
    s.add_argument('--root', required=True)
    s.set_defaults(fn=cmd_stats)

    s = sub.add_parser('drop', help='删除某目录的索引')
    s.add_argument('--root', required=True)
    s.set_defaults(fn=cmd_drop)

    a = p.parse_args(argv)
    try:
        a.fn(a)
    except (ValueError, OSError) as e:
        print(json.dumps({'error': str(e)}, ensure_ascii=False), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
