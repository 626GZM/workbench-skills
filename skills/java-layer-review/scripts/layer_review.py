#!/usr/bin/env python3
"""Java 分层规范检查：只看本次改动新增的代码行。

把“框架已经做了、代码里又写一遍”的防御性冗余、分层越界和常见坏味道找出来。
规则用正则 + 行上下文实现，可开关、可调严重级别；命中只是提示，不是判决。
只读 git 差异和配置，不改任何代码文件（--out 只写检查结果）。
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / '_lib'))

from wbkit import git  # noqa: E402

SEVERITIES = ('high', 'medium', 'low')
SEV_ORDER = {s: i for i, s in enumerate(SEVERITIES)}

HUNK_RE = re.compile(r'^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@')
COMMENT_PREFIX = ('//', '*', '/*')

# 各层识别：优先看类名后缀，再看目录名
CONTROLLER_DIRS = ('/controller/', '/web/', '/rest/')
SERVICE_DIRS = ('/service/', '/biz/', '/app/')
DAO_DIRS = ('/mapper/', '/dao/', '/repository/')
ENTITY_DIRS = ('/entity/', '/po/', '/domain/po/', '/model/entity/')

# ---------------------------------------------------------------- 正则

NULL_CHECK = re.compile(r'(?:==\s*null\b|null\s*==|Objects\.(?:isNull|nonNull)\s*\()')
LOGIN_USER_NULL = re.compile(
    r'(?i)(?:\b\w*(?:LoginUser|CurrentUser|TokenInfo|UserInfo)\w*\s*(?:\(\s*\))?\s*[!=]=\s*null'
    r'|Objects\.(?:isNull|nonNull)\(\s*\w*(?:[Uu]ser|[Tt]oken)\w*\s*\))')
CATCH_ANY = re.compile(r'\bcatch\s*\(')
MANUAL_VALIDATION = re.compile(
    r'(?:StringUtils\.(?:isBlank|isEmpty|isNotBlank|isNotEmpty)\(\s*\w+\.get\w+\(\))'
    r'|(?:if\s*\(\s*\w+\.get\w+\(\)\s*==\s*null)'
    r'|(?:if\s*\(\s*(?:Objects\.isNull|ObjectUtils\.isEmpty)\(\s*\w+\.get\w+\(\))'
    r'|(?:if\s*\(\s*!\w+\.get\w+\(\)\.matches\()')
TIMESTAMP = re.compile(
    r'(?:new\s+Date\s*\(\s*\)|System\.currentTimeMillis\s*\(|'
    r'LocalDateTime\.now\s*\(|LocalDate\.now\s*\(|DateUtil\.(?:date|now)\s*\()'
    r'|\.set(?:Create|Update|LastUpdate|Modify)Time\s*\(')
CONTROLLER_LOGIC = re.compile(r'\b(?:for|while|switch)\s*\(|@Transactional\b|\belse\s+if\s*\(')
DAO_CALL = re.compile(
    r'\b\w*(?:Mapper|Dao|DAO|Repository)\s*\.\s*\w+\s*\('
    r'|\b(?:mapper|dao|repository)\s*\.\s*\w+\s*\(')
SERVICE_RETURNS_ENTITY = re.compile(
    r'\b(?:public|protected|private)\s+(?:(?:List|Page|Collection|Set)<\s*)?'
    r'\w+(?:Entity|PO|DO)\s*>?\s+\w+\s*\(')
RETHROW = re.compile(r'\bthrow\b|setRollbackOnly')
LOOP_RE = re.compile(r'\b(?:for|while|forEach)\s*\(')
MAGIC_STRING = re.compile(
    r'"[^"\n]{1,40}"\s*\.\s*equals\s*\('
    r'|\b\w+\.equals\s*\(\s*"[^"\n]{1,40}"\s*\)'
    r'|(?:==|!=)\s*"[^"\n]{1,40}"'
    r'|"[^"\n]{1,40}"\s*(?:==|!=)')
SQL_CONCAT = re.compile(
    r'(?i)"[^"\n]*\b(?:select|from|where|insert\s+into|update|delete\s+from|'
    r'order\s+by|group\s+by)\b[^"\n]*"\s*\+'
    r'|\+\s*"[^"\n]*\b(?:where|and|order\s+by|limit|group\s+by)\b')
METHOD_DECL = re.compile(r'^\s*(?:public|private|protected)\s+[\w<>\[\],.\s?]*\b\w+\s*\(')
TX_ANNOTATION = re.compile(r'@Transactional\b')


# ---------------------------------------------------------------- diff 解析

def _is_comment(text):
    return text.strip().startswith(COMMENT_PREFIX)


def _parse_diff(text):
    """把 git diff 拆成“新增行”和“上下文行”，都按文件+行号存放。

    只信任 diff 里的行号，避免从工作区重读文件导致行号错位。
    """
    added, context = {}, {}
    cur, lineno = None, 0
    for raw in text.splitlines():
        if raw.startswith('+++ '):
            cur = raw[6:] if raw.startswith('+++ b/') else None
            continue
        if raw.startswith('--- ') or raw.startswith('diff --git'):
            continue
        m = HUNK_RE.match(raw)
        if m:
            lineno = int(m.group(1))
            continue
        if cur is None or not raw:
            continue
        tag, body = raw[0], raw[1:]
        if tag == '+':
            if not _is_comment(body):
                added.setdefault(cur, {})[lineno] = body
            lineno += 1
        elif tag == ' ':
            context.setdefault(cur, {})[lineno] = body
            lineno += 1
        # '-' 行不属于新文件，行号不动
    return added, context


def _untracked_java(repo):
    out = git.git(repo, 'ls-files', '--others', '--exclude-standard', '--', '*.java', check=False)
    return [line.strip() for line in out.splitlines() if line.strip()]


def _collect(repo, base, head, uncommitted):
    """返回 (新增行, 上下文行)，形如 {文件: {行号: 内容}}。"""
    if uncommitted:
        text = git.git(repo, 'diff', '-U20', '--no-color', 'HEAD', '--', '*.java')
        added, context = _parse_diff(text)
        # 未跟踪的新文件整份算新增
        for rel in _untracked_java(repo):
            path = Path(repo) / rel
            try:
                lines = path.read_text(encoding='utf-8', errors='replace').splitlines()
            except OSError:
                continue
            bucket = added.setdefault(rel, {})
            for i, body in enumerate(lines, 1):
                if not _is_comment(body):
                    bucket[i] = body
        return added, context
    text = git.git(repo, 'diff', '-U20', '--no-color', '%s..%s' % (base, head), '--', '*.java')
    return _parse_diff(text)


# ---------------------------------------------------------------- 上下文小工具

def _line(lines, idx):
    return lines[idx] if 0 <= idx < len(lines) else ''


def _enclosing_loop(lines, idx):
    """向上找包住这一行的 for / while / forEach；没有返回 None。"""
    balance = 0
    for j in range(idx - 1, max(-1, idx - 21), -1):
        line = _line(lines, j)
        balance += line.count('}') - line.count('{')
        if balance < 0:
            if LOOP_RE.search(line):
                return j
            if j > 0 and LOOP_RE.search(_line(lines, j - 1)):
                return j - 1
            balance = 0
    return None


def _in_transactional_method(lines, idx):
    """从 idx 往上找到最近的方法声明，再看它上面几行有没有 @Transactional。"""
    for j in range(idx, max(-1, idx - 80), -1):
        if METHOD_DECL.match(_line(lines, j)):
            for k in range(j - 1, max(-1, j - 8), -1):
                prev = _line(lines, k).strip()
                if not prev:
                    continue
                if prev.startswith('@'):
                    if TX_ANNOTATION.match(prev):
                        return True
                    continue
                break
            return False
    return False


def _catch_body(lines, idx):
    """catch 后面花括号里的内容（不含 catch 行的前缀）。"""
    start = idx
    while start < len(lines) and '{' not in _line(lines, start):
        start += 1
    if start >= len(lines):
        return []
    body, depth = [], 1
    for j in range(start, min(len(lines), start + 80)):
        line = _line(lines, j)
        seg = line[line.find('{') + 1:] if j == start else line
        body.append(seg)
        depth += seg.count('{') - seg.count('}')
        if depth <= 0:
            break
    return body


def _classify(rel):
    """按类名后缀和目录判断所在层。"""
    path = '/' + rel.replace('\\', '/')
    stem = Path(rel).stem
    return {
        'controller': stem.endswith('Controller') or any(d in path for d in CONTROLLER_DIRS),
        'service': stem.endswith(('Service', 'ServiceImpl', 'AppService')) or
                   any(d in path for d in SERVICE_DIRS),
        'mapper': stem.endswith(('Mapper', 'Dao', 'DAO')) or any(d in path for d in DAO_DIRS),
        'entity': stem.endswith(('Entity', 'PO', 'DO')) or any(d in path for d in ENTITY_DIRS),
        'test': '/src/test/' in path,
    }


def _build_lines(context, added):
    merged = dict(context)
    merged.update(added)
    if not merged:
        return []
    size = max(merged) + 2
    lines = [''] * size
    for no, body in merged.items():
        if 0 <= no < size:
            lines[no] = body
    return lines


def _added_hits(ctx, pattern):
    return [(no, ctx['added'][no].strip())
            for no in ctx['added_nos'] if pattern.search(ctx['added'][no])]


# ---------------------------------------------------------------- 规则实现

def check_entity_null(ctx):
    if not ctx['layer']['entity']:
        return []
    return _added_hits(ctx, NULL_CHECK)


def check_login_user(ctx):
    return _added_hits(ctx, LOGIN_USER_NULL)


def check_controller_catch(ctx):
    if not ctx['layer']['controller']:
        return []
    return _added_hits(ctx, CATCH_ANY)


def check_manual_validation(ctx):
    if not ctx['layer']['controller']:
        return []
    return _added_hits(ctx, MANUAL_VALIDATION)


def check_timestamp(ctx):
    return _added_hits(ctx, TIMESTAMP)


def check_controller_logic(ctx):
    if not ctx['layer']['controller']:
        return []
    return _added_hits(ctx, CONTROLLER_LOGIC)


def check_controller_dao(ctx):
    if not ctx['layer']['controller']:
        return []
    return _added_hits(ctx, DAO_CALL)


def check_service_entity(ctx):
    if not ctx['layer']['service']:
        return []
    return _added_hits(ctx, SERVICE_RETURNS_ENTITY)


def check_tx_swallow(ctx):
    if not ctx['layer']['service']:
        return []
    out = []
    for no in ctx['added_nos']:
        body = ctx['added'][no]
        if not CATCH_ANY.search(body):
            continue
        if not _in_transactional_method(ctx['lines'], no):
            continue
        if not RETHROW.search('\n'.join(_catch_body(ctx['lines'], no))):
            out.append((no, body.strip()))
    return out


def check_n_plus_one(ctx):
    out = []
    for no in ctx['added_nos']:
        body = ctx['added'][no]
        if not DAO_CALL.search(body):
            continue
        # 同一行 for(...) mapper.xxx(...) 也算
        if LOOP_RE.search(body) or _enclosing_loop(ctx['lines'], no) is not None:
            out.append((no, body.strip()))
    return out


def check_magic_string(ctx):
    return _added_hits(ctx, MAGIC_STRING)


def check_sql_concat(ctx):
    return _added_hits(ctx, SQL_CONCAT)


# ---------------------------------------------------------------- 规则表

RULE_SPECS = [
    {
        'id': 'entity-null-check',
        'title': '实体类里重复判空',
        'severity': 'low',
        'why': 'PO/Entity 的字段可空由数据库默认值和 MyBatis 映射处理，实体里手写判空会把默认值逻辑散到各处，字段含义也变得不明确。',
        'fix': '删掉判空，把默认值放到建表语句的 DEFAULT 或出参 DTO 的组装处。',
        'patterns': [NULL_CHECK.pattern],
        'check': check_entity_null,
    },
    {
        'id': 'login-user-null-check',
        'title': '已登录接口重复判登录用户为空',
        'severity': 'medium',
        'why': '登录拦截器已经挡掉未登录请求，拿到的当前用户不会是 null；再判一次等于承认拦截器可能失效。',
        'fix': '直接使用登录用户，未登录场景交给拦截器统一返回 401。',
        'patterns': [LOGIN_USER_NULL.pattern],
        'check': check_login_user,
    },
    {
        'id': 'controller-try-catch',
        'title': 'controller 里重复 try-catch 兜底',
        'severity': 'medium',
        'why': '全局异常处理会接住未捕获的异常并转成统一返回；controller 里再 try-catch 会把真实异常包一层，堆栈和错误码都变了。',
        'fix': '删掉 try-catch 让异常上抛；需要区分业务错误时抛业务异常。',
        'patterns': [CATCH_ANY.pattern],
        'check': check_controller_catch,
    },
    {
        'id': 'manual-field-validation',
        'title': '请求体已校验仍手写逐字段校验',
        'severity': 'medium',
        'why': '请求体已经 @Validated 校验过（@NotNull/@NotBlank 等），方法里再逐字段判空属于重复校验，还容易和注解规则不一致。',
        'fix': '把规则写到 DTO 字段的校验注解上，controller 只接参数。',
        'patterns': [MANUAL_VALIDATION.pattern],
        'check': check_manual_validation,
    },
    {
        'id': 'manual-timestamp',
        'title': '手写创建/更新时间',
        'severity': 'low',
        'why': '创建、更新时间应由数据库默认值（CURRENT_TIMESTAMP）或框架自动填充维护，应用层手写会因多实例时钟不一致而错乱。',
        'fix': '表字段设默认值，或用 @TableField(fill = FieldFill.INSERT) 自动填充。',
        'patterns': [TIMESTAMP.pattern],
        'check': check_timestamp,
    },
    {
        'id': 'controller-business-logic',
        'title': 'controller 里写业务逻辑',
        'severity': 'medium',
        'why': 'controller 只负责协议转换；循环、多分支和事务放在 controller 会让业务逻辑无法复用，事务边界也不清晰。',
        'fix': '把逻辑下移到 service / domain service，controller 只调用一次。',
        'patterns': [CONTROLLER_LOGIC.pattern],
        'check': check_controller_logic,
    },
    {
        'id': 'controller-direct-dao',
        'title': 'controller 直接调 mapper/dao',
        'severity': 'high',
        'why': 'controller 直连 mapper/dao 跳过了 service 的事务和业务规则，分层被穿透。',
        'fix': '改为调用 service 方法。',
        'patterns': [DAO_CALL.pattern],
        'check': check_controller_dao,
    },
    {
        'id': 'service-returns-entity',
        'title': 'service 层直接返回 Entity',
        'severity': 'medium',
        'why': 'Entity/PO 带库表字段和持久化注解，直接返给 controller 会泄漏表结构、耦合接口契约。',
        'fix': '在 service 内转换成 DTO/VO 再返回。',
        'patterns': [SERVICE_RETURNS_ENTITY.pattern],
        'check': check_service_entity,
    },
    {
        'id': 'swallowed-exception-in-tx',
        'title': '事务方法里吞异常',
        'severity': 'high',
        'why': '事务方法里 catch 住异常不抛，Spring 认为方法正常结束而提交，前面的写操作不会回滚。',
        'fix': 'catch 后重新抛出，或调用 setRollbackOnly；确实要忽略时写清原因。',
        'patterns': [CATCH_ANY.pattern, RETHROW.pattern],
        'check': check_tx_swallow,
    },
    {
        'id': 'n-plus-one-query',
        'title': '循环里查数据库',
        'severity': 'high',
        'why': '循环里逐条查库是 N+1，数据量一上来就是几百次往返；框架的批量查询和 in 查询能一次拿完。',
        'fix': '先收集 id，用 selectBatchIds / in 一次查回，再在内存里按 id 分组。',
        'patterns': [LOOP_RE.pattern, DAO_CALL.pattern],
        'check': check_n_plus_one,
    },
    {
        'id': 'magic-string-compare',
        'title': '魔法值字符串直接比较',
        'severity': 'low',
        'why': '状态、类型等魔法字符串散落在比较处，改一个值要全库搜，拼错也不报错。',
        'fix': '抽成枚举或常量，比较时用枚举值。',
        'patterns': [MAGIC_STRING.pattern],
        'check': check_magic_string,
    },
    {
        'id': 'sql-concat',
        'title': '拼接 SQL',
        'severity': 'high',
        'why': '拼接 SQL 既容易被注入，也无法被 MyBatis 的参数绑定和 SQL 解析复用。',
        'fix': '用 MyBatis 的 #{} 参数绑定或 QueryWrapper，不拼字符串。',
        'patterns': [SQL_CONCAT.pattern],
        'check': check_sql_concat,
    },
]

RULES_BY_ID = {r['id']: r for r in RULE_SPECS}
RULE_ORDER = {r['id']: i for i, r in enumerate(RULE_SPECS)}


# ---------------------------------------------------------------- 配置

def default_config():
    return {
        'version': 1,
        'rules': {r['id']: {'enabled': True, 'severity': r['severity']}
                  for r in RULE_SPECS},
    }


def load_rules(path=None):
    """内置规则 + 用户配置覆盖。未出现在配置里的规则保持默认。"""
    rules = [dict(spec, enabled=True) for spec in RULE_SPECS]
    if not path:
        return rules
    try:
        data = json.loads(Path(path).expanduser().read_text(encoding='utf-8'))
    except OSError as e:
        raise ValueError('规则文件读不了：%s（%s）' % (path, e))
    except ValueError as e:
        raise ValueError('规则文件不是合法 JSON：%s（%s）' % (path, e))
    conf = data.get('rules') if isinstance(data, dict) else None
    if not isinstance(conf, dict):
        raise ValueError('规则文件格式不对，需要 {"rules": {"规则id": {...}}}')
    by_id = {r['id']: r for r in rules}
    for rid, item in conf.items():
        if rid not in by_id:
            raise ValueError('未知规则 id：%s（用 rules list 查看内置规则）' % rid)
        if isinstance(item, bool):
            by_id[rid]['enabled'] = item
            continue
        if not isinstance(item, dict):
            raise ValueError('规则 %s 的配置必须是对象或布尔值' % rid)
        if 'enabled' in item:
            by_id[rid]['enabled'] = bool(item['enabled'])
        sev = item.get('severity')
        if sev is not None:
            if sev not in SEVERITIES:
                raise ValueError('规则 %s 的 severity 只能是 high/medium/low' % rid)
            by_id[rid]['severity'] = sev
    return rules


# ---------------------------------------------------------------- 检查

def _check_refs(repo, base, head, uncommitted):
    if not git.rev(repo, 'HEAD'):
        raise ValueError('仓库还没有提交，无法取差异')
    if uncommitted:
        return
    if not git.rev(repo, base):
        raise ValueError('基准不存在：%s' % base)
    if not git.rev(repo, head):
        raise ValueError('head 不存在：%s' % head)


def review(repo, base='origin/main', head='HEAD', uncommitted=False, rules=None):
    """只检查 diff 新增行，返回按文件/行号排序的命中列表。"""
    rules = rules if rules is not None else load_rules()
    added, context = _collect(repo, base, head, uncommitted)
    hits = []
    for rel in sorted(added):
        layer = _classify(rel)
        if layer['test']:
            continue
        ctx = {
            'file': rel,
            'added': added[rel],
            'added_nos': sorted(added[rel]),
            'lines': _build_lines(context.get(rel, {}), added[rel]),
            'layer': layer,
        }
        for rule in rules:
            if not rule['enabled']:
                continue
            seen = set()
            for no, snippet in rule['check'](ctx):
                if no in seen:
                    continue
                seen.add(no)
                hits.append({
                    'rule': rule['id'],
                    'severity': rule['severity'],
                    'file': rel,
                    'line': no,
                    'snippet': snippet[:200],
                    'why': rule['why'],
                    'fix': rule['fix'],
                })
    hits.sort(key=lambda h: (h['file'], h['line'], RULE_ORDER.get(h['rule'], 99)))
    return hits


# ---------------------------------------------------------------- 命令行

def emit(data):
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _cmd_review(args):
    repo = str(Path(args.repo).expanduser())
    if not git.repo_root(repo):
        raise ValueError('不是 git 仓库：%s' % repo)
    _check_refs(repo, args.base, args.head, args.uncommitted)
    hits = review(repo, args.base, args.head, args.uncommitted, load_rules(args.rules))
    if args.out:
        out = Path(args.out).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(hits, ensure_ascii=False, indent=2) + '\n',
                       encoding='utf-8')
    emit(hits)
    return 0


def _cmd_rules(args):
    if args.rules_cmd == 'init':
        out = Path(args.out).expanduser()
        if out.exists() and not args.force:
            raise ValueError('文件已存在：%s（加 --force 覆盖）' % out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(default_config(), ensure_ascii=False, indent=2) + '\n',
                       encoding='utf-8')
        emit({'out': str(out), 'rules': len(RULE_SPECS)})
        return 0

    rules = load_rules(args.rules)
    if args.rules_cmd == 'list':
        emit([{'id': r['id'], 'title': r['title'], 'severity': r['severity'],
               'enabled': r['enabled'], 'why': r['why'], 'fix': r['fix']}
              for r in rules])
        return 0

    rule = next((r for r in rules if r['id'] == args.id), None)
    if not rule:
        raise ValueError('未知规则 id：%s（用 rules list 查看内置规则）' % args.id)
    emit({'id': rule['id'], 'title': rule['title'], 'severity': rule['severity'],
          'enabled': rule['enabled'], 'why': rule['why'], 'fix': rule['fix'],
          'patterns': rule['patterns']})
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog='layer_review.py',
        description='检查 Java 改动新增行里的分层问题和重复防御写法，输出 JSON。')
    sub = parser.add_subparsers(dest='cmd', required=True)

    rv = sub.add_parser('review', help='检查 diff 新增行')
    rv.add_argument('--repo', required=True, help='仓库目录')
    rv.add_argument('--base', default='origin/main', help='基准分支，默认 origin/main')
    rv.add_argument('--head', default='HEAD', help='目标提交，默认 HEAD')
    rv.add_argument('--uncommitted', action='store_true',
                    help='检查工作区未提交改动（含未跟踪的新文件）')
    rv.add_argument('--rules', help='规则配置文件（rules init 导出的 JSON）')
    rv.add_argument('--out', help='把结果另存为 JSON 文件')

    ru = sub.add_parser('rules', help='规则清单与配置')
    rs = ru.add_subparsers(dest='rules_cmd', required=True)

    rl = rs.add_parser('list', help='列出内置规则')
    rl.add_argument('--rules', help='规则配置文件')

    re_ = rs.add_parser('explain', help='说明单条规则')
    re_.add_argument('id', help='规则 id')
    re_.add_argument('--rules', help='规则配置文件')

    ri = rs.add_parser('init', help='导出默认规则配置')
    ri.add_argument('--out', required=True, help='配置输出路径')
    ri.add_argument('--force', action='store_true', help='覆盖已存在的文件')
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if args.cmd == 'review':
            return _cmd_review(args)
        return _cmd_rules(args)
    except (ValueError, git.GitError, OSError) as e:
        emit({'error': str(e)})
        return 2


if __name__ == '__main__':
    sys.exit(main())
