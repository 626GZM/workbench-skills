"""目录文件索引：SQLite 元数据 + FTS5 全文。

索引只是缓存，原文件始终是事实来源：删掉库、调用 drop()、或者库坏了，都可以随时重建。
只依赖标准库。
"""
import os
import re
import sqlite3
import stat
from pathlib import Path

from . import config as cfg

TEXT_EXTS = {'.md', '.txt', '.json', '.jsonl', '.yaml', '.yml', '.py', '.sql', '.xml', '.html', '.htm',
             '.js', '.ts', '.css', '.sh', '.log', '.java', '.properties', '.csv', '.ini', '.toml',
             '.diff', '.patch', '.rst', '.go', '.rs', '.rb', '.php', '.kt', '.scala'}
SKIP_NAMES = {'.DS_Store', '.git', '__pycache__', 'node_modules', '.venv', 'target', 'dist', 'build', '.idea'}
MAX_TEXT_BYTES = 4 * 1024 * 1024
SNIPPET_LIMIT = 200
HEAD_BYTES = 8192

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files(root TEXT NOT NULL, path TEXT NOT NULL, size INTEGER, mtime REAL,
                                 text INTEGER DEFAULT 0, PRIMARY KEY (root, path));
CREATE VIRTUAL TABLE IF NOT EXISTS files_fts USING fts5(root UNINDEXED, path UNINDEXED, body, tokenize='unicode61');
CREATE TABLE IF NOT EXISTS scans(root TEXT PRIMARY KEY, at REAL, files INTEGER, bytes INTEGER, error TEXT);
"""


def db_path():
    p = cfg.get('index.db_path') or '~/.local/state/workbench-skills/fileindex.db'
    return Path(p).expanduser()


def _conn():
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10.0)
    conn.executescript(_SCHEMA)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA busy_timeout=10000')
    return conn


def _key(root):
    return os.path.abspath(str(Path(root).expanduser()))


def _looks_text(path):
    try:
        with open(path, 'rb') as f:
            head = f.read(HEAD_BYTES)
    except OSError:
        return False
    return b'\x00' not in head


def _iter_files(root, exts):
    root = Path(root)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_NAMES and not d.startswith('.')]
        for name in filenames:
            if name in SKIP_NAMES or name.startswith('.'):
                continue
            p = Path(dirpath) / name
            if exts and p.suffix.lower() not in exts:
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            yield p, st


def build(root, exts=None, verbose=False, progress=None):
    """全量重建某个根目录的索引。返回统计字典。"""
    root_path = Path(root).expanduser()
    if not root_path.is_dir():
        raise ValueError('目录不存在：%s' % root_path)
    key = _key(root_path)
    exts = {e.lower() if e.startswith('.') else '.' + e.lower() for e in exts} if exts else None
    files = 0
    total_bytes = 0
    rows = []
    for p, st in _iter_files(root_path, exts):
        rel = str(p.relative_to(root_path))
        text = 0
        body = ''
        if st.st_size <= MAX_TEXT_BYTES and _looks_text(p):
            try:
                body = p.read_text(encoding='utf-8', errors='replace')
                text = 1
            except OSError:
                body = ''
        rows.append((key, rel, st.st_size, st.st_mtime, text, body))
        files += 1
        total_bytes += st.st_size
        if progress and files % 200 == 0:
            progress(files, body)
    conn = _conn()
    try:
        with conn:
            conn.execute('DELETE FROM files WHERE root=?', (key,))
            conn.execute('DELETE FROM files_fts WHERE root=?', (key,))
            conn.executemany(
                'INSERT OR REPLACE INTO files(root,path,size,mtime,text) VALUES(?,?,?,?,?)',
                [(r[0], r[1], r[2], r[3], r[4]) for r in rows])
            conn.executemany(
                'INSERT INTO files_fts(root,path,body) VALUES(?,?,?)',
                [(r[0], r[1], r[5]) for r in rows])
            conn.execute('INSERT OR REPLACE INTO scans(root,at,files,bytes,error) VALUES(?,?,?,?,NULL)',
                         (key, __import__('time').time(), files, total_bytes))
    finally:
        conn.close()
    return {'root': key, 'files': files, 'bytes': total_bytes}


def _snippet(body, query):
    body = ' '.join(body.split())
    low, q = body.lower(), query.lower()
    i = low.find(q)
    if i < 0:
        return body[:SNIPPET_LIMIT]
    start = max(0, i - 60)
    return ('…' if start else '') + body[start:start + SNIPPET_LIMIT]


def search(root, query, limit=20, exts=None):
    """全文搜索。FTS 没命中时退回子串匹配（中文用 unicode61 分词，整句往往搜不到）。"""
    key = _key(root)
    conn = _conn()
    try:
        exts = {e.lower() if e.startswith('.') else '.' + e.lower() for e in exts} if exts else None
        ext_sql, ext_params = '', []
        if exts:
            ext_sql = ' AND (' + ' OR '.join('f.path LIKE ?' for _ in exts) + ')'
            ext_params = ['%' + e for e in exts]
        rows = []
        try:
            rows = conn.execute(
                'SELECT f.path, f.size, f.mtime, snippet(files_fts, 2, \'\', \'\', \'…\', 24) '
                'FROM files_fts JOIN files f ON f.root=files_fts.root AND f.path=files_fts.path '
                'WHERE files_fts MATCH ? AND f.root=?' + ext_sql + ' LIMIT ?',
                [query, key] + ext_params + [limit]).fetchall()
        except sqlite3.Error:
            rows = []
        if not rows:
            like = '%' + query.replace('%', '').replace('_', '') + '%'
            rows = conn.execute(
                'SELECT f.path, f.size, f.mtime, '
                'substr(fw.body, max(1, instr(lower(fw.body), lower(?)) - 60), 260) '
                'FROM files_fts fw JOIN files f ON f.root=fw.root AND f.path=fw.path '
                'WHERE fw.root=? AND (f.path LIKE ? OR lower(fw.body) LIKE ?)' + ext_sql + ' LIMIT ?',
                [query, key, like, like] + ext_params + [limit]).fetchall()
        out = []
        for path, size, mtime, snip in rows:
            if not snip:
                try:
                    body = Path(key, path).read_text(encoding='utf-8', errors='replace')
                    snip = _snippet(body, query)
                except OSError:
                    snip = ''
            out.append({'path': path, 'size': size, 'mtime': mtime, 'snippet': snip})
        return out
    finally:
        conn.close()


def stats(root):
    key = _key(root)
    conn = _conn()
    try:
        row = conn.execute('SELECT at, files, bytes, error FROM scans WHERE root=?', (key,)).fetchone()
    finally:
        conn.close()
    if not row:
        return {'root': key, 'indexed': False}
    return {'root': key, 'indexed': True, 'at': row[0], 'files': row[1], 'bytes': row[2], 'error': row[3]}


def drop(root):
    key = _key(root)
    conn = _conn()
    try:
        with conn:
            conn.execute('DELETE FROM files WHERE root=?', (key,))
            conn.execute('DELETE FROM files_fts WHERE root=?', (key,))
            conn.execute('DELETE FROM scans WHERE root=?', (key,))
    finally:
        conn.close()
    return {'root': key, 'dropped': True}
