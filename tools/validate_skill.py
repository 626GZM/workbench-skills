#!/usr/bin/env python3
"""校验每个 skill 的格式：SKILL.md 的 frontmatter、目录名、入口脚本。

用法：
    python3 tools/validate_skill.py              # 校验全部
    python3 tools/validate_skill.py skills/xxx   # 校验一个

只依赖标准库（不引 yaml，frontmatter 只有 name/description/metadata 三样）。
"""
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAME_RE = re.compile(r'^[a-z0-9]+(-[a-z0-9]+)*$')


def parse_frontmatter(text):
    lines = text.splitlines()
    if not lines or lines[0].strip() != '---':
        raise ValueError('SKILL.md 第一行必须是 ---')
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == '---')
    except StopIteration:
        raise ValueError('frontmatter 没有结束的 ---')
    meta, key = {}, None
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        indent = len(line) - len(line.lstrip())
        if indent == 0 and ':' in line:
            key, _, val = line.partition(':')
            key, val = key.strip(), val.strip()
            if val.startswith('"') and val.endswith('"') and len(val) >= 2:
                val = val[1:-1]
            meta[key] = val
        elif indent > 0 and key:
            sub = line.strip()
            if ':' in sub:
                k, _, v = sub.partition(':')
                if not isinstance(meta[key], dict):
                    meta[key] = {}
                meta[key][k.strip()] = v.strip().strip('"')
    return meta, '\n'.join(lines[end + 1:])


def validate(skill_dir, run_entry=True):
    errors, warns = [], []
    skill_dir = Path(skill_dir)
    name = skill_dir.name
    md = skill_dir / 'SKILL.md'
    if not md.is_file():
        return ['缺少 SKILL.md'], []
    try:
        meta, body = parse_frontmatter(md.read_text(encoding='utf-8'))
    except ValueError as e:
        return [str(e)], []
    if not NAME_RE.match(name):
        errors.append('目录名要用小写 kebab-case：%s' % name)
    if meta.get('name') != name:
        errors.append('frontmatter name（%r）要和目录名一致' % meta.get('name'))
    if not meta.get('description'):
        errors.append('缺少 description')
    elif len(meta['description']) < 10:
        warns.append('description 太短，AI 难以判断什么时候用')
    if not body.strip():
        errors.append('SKILL.md 正文为空')
    if '## 用法' not in body and '## Usage' not in body:
        warns.append('建议有一个“## 用法”小节，写清命令怎么调')
    scripts = skill_dir / 'scripts'
    if not scripts.is_dir():
        errors.append('缺少 scripts/ 目录')
        return errors, warns
    entries = [p for p in scripts.glob('*.py')]
    if not entries:
        errors.append('scripts/ 里没有入口脚本')
    if run_entry:
        for entry in entries:
            try:
                proc = subprocess.run([sys.executable, str(entry), '--help'],
                                      capture_output=True, text=True, timeout=30)
            except subprocess.SubprocessError as e:
                errors.append('%s --help 跑不起来：%s' % (entry.name, e))
                continue
            if proc.returncode != 0:
                errors.append('%s --help 退出码 %d：%s' % (entry.name, proc.returncode,
                                                         (proc.stderr or '').strip()[-300:]))
    return errors, warns


def main(argv):
    targets = [Path(a) for a in argv if not a.startswith('-')]
    if not targets:
        targets = sorted(p for p in (ROOT / 'skills').glob('*') if p.is_dir())
    bad = 0
    for d in targets:
        errors, warns = validate(d)
        if errors:
            bad += 1
            print('✗ %s' % d.name)
            for e in errors:
                print('    ' + e)
        else:
            print('✓ %s' % d.name)
        for w in warns:
            print('    ! ' + w)
    print('\n%d 个 skill，%d 个不通过' % (len(targets), bad))
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
