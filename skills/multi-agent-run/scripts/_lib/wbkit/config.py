"""配置加载。

优先级（从高到低）：
1. 环境变量 WB_CONFIG 指向的文件
2. 当前目录下的 .wbkit.json
3. ~/.config/workbench-skills/config.json
4. 内置默认值

配置里只存路径，不存密钥。密钥走环境变量，或 secrets.json（权限 600）。
"""
import json
import os
import tempfile
from pathlib import Path

CONFIG_HOME = Path.home() / '.config' / 'workbench-skills'
SECRETS_PATH = CONFIG_HOME / 'secrets.json'
LOCAL_NAME = '.wbkit.json'

DEFAULTS = {
    'workspace_root': '~/workbench',
    'repos_root': '~/repos',
    'projects': [],
    'branches': {'base': 'main', 'dev': 'dev', 'test': 'test'},
    'models': {
        'kimi': {'cli': 'claude', 'model': '', 'key_env': 'KIMI_API_KEY',
                 'usage_url': 'https://api.kimi.com/coding/v1/usages'},
        'deepseek': {'cli': 'claude', 'model': '', 'key_env': 'DEEPSEEK_API_KEY',
                     'balance_url': 'https://api.deepseek.com/user/balance', 'min_balance': 0.5},
        'codex': {'cli': 'codex', 'model': '', 'key_env': 'OPENAI_API_KEY'},
    },
    'java': {'package_prefix': '', 'jar_roots': [], 'synonyms': ''},
    'docs': {
        'prd': '01-需求理解.md',
        'design': '02-方案设计.md',
        'review': '03-代码review.md',
    },
    'e2e': {'root': '', 'envs': []},
    'index': {'db_path': '~/.local/state/workbench-skills/fileindex.db'},
    'output': {'dir': '.wbkit-out'},
}

_cache = {'key': None, 'data': None}


def _expand(value):
    if isinstance(value, str) and value.startswith('~'):
        return str(Path(value).expanduser())
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


def _merge(base, over):
    """深合并：字典递归，其他类型直接覆盖。"""
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _find():
    env = os.environ.get('WB_CONFIG')
    if env:
        return Path(env).expanduser()
    local = Path.cwd() / LOCAL_NAME
    if local.is_file():
        return local
    user = CONFIG_HOME / 'config.json'
    if user.is_file():
        return user
    return None


def load(path=None, reload=False):
    """读配置。找不到文件就用默认值。同一路径按 mtime 缓存。"""
    p = Path(path).expanduser() if path else _find()
    key = str(p) if p else None
    mtime = p.stat().st_mtime if p and p.is_file() else 0
    if not reload and _cache['key'] == (key, mtime):
        return _cache['data']
    data = DEFAULTS
    if p and p.is_file():
        try:
            data = _merge(DEFAULTS, json.loads(p.read_text(encoding='utf-8')))
        except (OSError, ValueError) as e:
            raise ValueError('配置文件读不了：%s（%s）' % (p, e))
    data['_config_path'] = str(p) if p else None
    _cache.update(key=(key, mtime), data=data)
    return data


def get(dotted, default=None, conf=None):
    """按 'a.b.c' 取配置。"""
    cur = conf if conf is not None else load()
    for part in dotted.split('.'):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return _expand(cur)


def path_of(dotted, default=None, conf=None):
    value = get(dotted, default, conf)
    return Path(value).expanduser() if value else None


def api_key(vendor):
    """取某家的 key：环境变量优先，其次 secrets.json。"""
    spec = (load().get('models') or {}).get(vendor) or {}
    env = os.environ.get(spec.get('key_env') or '')
    if env:
        return env
    try:
        secrets = json.loads(SECRETS_PATH.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return secrets.get(vendor) or None


def set_secret(vendor, key):
    """写 secrets.json（600）。已存在的其他厂商保留。"""
    SECRETS_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(SECRETS_PATH.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        data = {}
    data[vendor] = key
    fd, tmp = tempfile.mkstemp(dir=str(SECRETS_PATH.parent), prefix='.secrets-')
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, SECRETS_PATH)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def init(path=None):
    """生成一份空的用户配置（不覆盖已存在的）。返回路径。"""
    p = Path(path).expanduser() if path else CONFIG_HOME / 'config.json'
    if p.exists():
        raise FileExistsError('配置已存在：%s' % p)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {k: v for k, v in DEFAULTS.items()}
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return p


def project(name, conf=None):
    """按名字取项目配置，没有就报错。"""
    for p in (conf if conf is not None else load()).get('projects') or []:
        if p.get('name') == name:
            return _expand(p)
    raise KeyError('没有叫“%s”的项目，先在工作区配置里加一条' % name)
