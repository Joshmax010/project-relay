#!/usr/bin/env python3
"""Relay v3：项目根目录下的共享记忆（Python 3.8+，stdlib），分 Agent 存放。

档案在项目源码根目录的 `.project-relay/` 里：

- 外层是本项目总的目标与总的记忆，要精简：总目标（goal.md，带确认
  收据）、技术栈（stack.md）、项目未来预期（outlook.md）、共用的待办
  与决定（backlog.md、decisions.md）、代码目前状况（state.md，由 resume
  从各家文件里现算）、一张指路表（index.md）。这层各家 Agent 每轮不许
  直接写，只在目标确认或新 Agent 加入时才改。
- 里面按 Agent 分家，各写各的：agents/deepseek-harness/（DeepSeek
  Harness 即 DSH，目录名 deepseek-harness）。旧版其他 Agent 的目录只在导入
  旧档案时保留为只读兼容层，新产品面不再创建。
  每家目录里是它自己的记忆文件（memory.md）、改动记录（changes.md）、
  日志（log.jsonl）、待办（backlog.md）、目标（goal.md，必须与总目标
  一致）。每个 Agent 只写自己那家目录，不用一把大锁；同一个 Agent 开
  两个会话同时写自己那份，用该目录下的小文件锁排队。
- 两条规矩：公共的东西（目标、技术栈、共用待办和决定）只写在总记忆
  那层，各家文件只写自己那份活；resume 接手简报里加一次跨 Agent 核
  对，把各家记忆里互相矛盾的地方直接列出来。

换设备只走 ZIP 交接（export-zip / import-zip），整包搬：另一台设备上
任何 Agent 既能读别人的记忆，也能读自己的。

这是一份记忆工具，不是身份/鉴权边界。目标确认仍由用户把关。
"""
import argparse
import datetime as dt
import difflib
import hashlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import sysconfig
import tempfile
import uuid
from pathlib import Path, PurePosixPath

if os.name == 'nt':
    import msvcrt
else:
    import fcntl

# One relay module per process. The sibling modules (relay_flow,
# relay_sync, relay_stats, relay_fork) load relay.py through importlib;
# if each got its own copy, every copy would carry its own RelayError
# class and main()'s handler would miss the ones the siblings raise
# (a capture in an un-initialised project used to escape as a bare
# traceback instead of ERROR + exit 1). The first loaded copy claims
# the 'relay' name; the siblings reuse it (see their module tops).
# An unregistered importlib load leaves registration to its loader.
_self = sys.modules.get(__name__)
if _self is not None:
    sys.modules.setdefault('relay', _self)

# Mode for a newly created document (atomic() restores the destination's
# own mode instead of letting mkstemp's 0600 leak through).
DOC_MODE = 0o644

class RelayError(Exception):
    pass


def require(condition, message):
    if not condition:
        raise RelayError(message)


def utc():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def safe(root, rel):
    """Accept relative paths only; reject every existing symlink component."""
    require(isinstance(rel, str) and rel and '\\' not in rel,
            '路径必须是非空的相对路径，使用 / 分隔')
    parts = PurePosixPath(rel).parts
    require(not PurePosixPath(rel).is_absolute() and '..' not in parts
            and ':' not in rel and not rel.startswith('/'), '路径越界: ' + rel)
    require(PurePosixPath(rel).as_posix() == rel, '路径必须规范化: ' + rel)
    require(not root.is_symlink(), '拒绝符号链接目录: ' + str(root))
    p = root
    for part in parts:
        p = p / part
        require(not p.is_symlink(), '拒绝符号链接: ' + str(p))
    return p


def atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    require(not path.is_symlink(), '拒绝符号链接: ' + str(path))
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        mode = DOC_MODE
    fd, name = tempfile.mkstemp(prefix='.relay-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        # mkstemp creates 0600; keep the destination's own mode so a successful
        # apply never silently restricts reviewed documents to the owner.
        os.chmod(name, mode)
        os.replace(name, str(path))
    finally:
        if os.path.exists(name):
            os.unlink(name)


def save(path, value):
    atomic(path, (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode('utf-8'))


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def git(root, *args, check=True, input_data=None, honor_autocrlf=False):
    # Batch 3, A1: every git invocation through this wrapper pins
    # core.autocrlf=false for the process only. Relay hashes working-tree
    # bytes itself, so a checkout-time LF->CRLF conversion (Windows with
    # autocrlf=true) silently breaks bundle hashes and store checkouts.
    # The flag is per-process: the user's
    # global/system autocrlf is never read for writing and never changed.
    # Carve-out (honor_autocrlf): commit_check and resume() compare
    # content the USER staged with their own git against the worktree.
    # The index blob was produced under the repo's own autocrlf setting,
    # so those three comparisons must use the same semantics, or a
    # Windows user with the default autocrlf=true gets a false
    # 'worktree differs from index' on every CRLF file they staged
    # correctly. Relay-operated repos (sync store) never take this path:
    # relay stages there too, so forced-false stays self-consistent.
    cmd = ['git'] + ([] if honor_autocrlf else ['-c', 'core.autocrlf=false'])
    cmd += ['-C', str(root), *args]
    result = subprocess.run(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, input=input_data, timeout=60)
    if check:
        require(result.returncode == 0, 'Git 操作失败: ' + ' '.join(args))
    return result


def anchor(root):
    head = git(root, 'rev-parse', '--verify', 'HEAD', check=False)
    branch = git(root, 'symbolic-ref', '-q', 'HEAD', check=False)
    return {'head': head.stdout.decode().strip() if head.returncode == 0 else None,
            'branch': branch.stdout.decode().strip() if branch.returncode == 0 else None}



# ---------------------------------------------------------------- store
VERSION = '0.2.0'
STORE_DIRNAME = '.project-relay'
HANDOFF_ZIP_PATTERN = '*-project-relay-*.zip'
PROJECT_FILE = 'project.json'
REGISTRY_DIRNAME = '.project-relay'
REGISTRY_FILE = 'registry.json'
# 现役 Agent：DeepSeek Harness（DSH，目录名 deepseek-harness）。
# AGENTS 保留历史名字用于读取旧档案；ACTIVE_AGENTS 决定 init 建哪些目录。
AGENTS = ('codex', 'claude-code', 'workbuddy', 'deepseek-harness')
ACTIVE_AGENTS = ('deepseek-harness',)
# 外层文件：总目标、总记忆（精简）、指路表。
OUTER_FILES = ('goal.md', 'state.md', 'backlog.md', 'decisions.md',
               'stack.md', 'outlook.md', 'index.md')
# 每家目录里的文件：记忆、决定、教训、待办、改动记录、该家目标。
AGENT_FILES = ('memory.md', 'decisions.md', 'lessons.md', 'backlog.md',
               'changes.md', 'goal.md')
AGENT_DIRS = ('raw', 'state', 'tx', 'backups')
ARCHIVE_MARK = ('<!-- relay-archive-begin -->', '<!-- relay-archive-end -->')
GOAL_STATUS_RE = re.compile(r'^状态：(.+?)\s*$', re.MULTILINE)
TENTATIVE_WATERMARK = '【暂定目标】以下目标为暂定状态，不可当作硬约束执行。'
CHECKSUM_MANIFEST = 'checksum-manifest.json'
# Files under this prefix are excluded from the integrity manifest
# (leftovers of the deleted sync-rollback trash; kept so old archives
# still verify).
ROLLBACK_TRASH_PREFIX = 'archive/rollback-trash/'

# 本机状态文件：不进 ZIP 载荷，也不进完整性清单。清单若把它们算进去，
# 正常的 capture/precompress/fingerprint 每次都会改这些文件，下一次
# resume 就会误报“档案被损坏/截断”。relay_sync 复用同一组定义，避免
# “打包排除”与“校验排除”两套标准漂移。
LOCAL_ONLY_OUTER = frozenset({
    'registry.json',
    'config.json',
    'state/config.json',
    'state/fingerprint-cache.json',
    'state/device-notice.json',
    'state/goal-conflict.json',
    'state/goal-imported.json',
    'state/rollback-note.json',
})
LOCAL_ONLY_AGENT_STATE = frozenset({
    'cursor.json', 'baseline.json', 'numbers.json', 'digest.json',
    'enforce.json', 'forks.json', 'backlog-notice.json',
    'fingerprint-cache.json',
})
LOCAL_ONLY_AGENT_FILES = frozenset({'recipe.json'})
LOCAL_ONLY_AGENT_DIRS = frozenset({'tx', 'backups', 'stats'})


def is_local_only_path(rel):
    """True for archive paths that are machine-local, not shared payload."""
    if rel in LOCAL_ONLY_OUTER:
        return True
    if not rel.startswith('agents/'):
        return False
    parts = rel.split('/')
    if len(parts) >= 3 and parts[2] in LOCAL_ONLY_AGENT_DIRS:
        return True
    if (len(parts) >= 4 and parts[2] == 'state'
            and parts[3] in LOCAL_ONLY_AGENT_STATE):
        return True
    if len(parts) == 3 and parts[2] in LOCAL_ONLY_AGENT_FILES:
        return True
    return False


def _resolve_templates_dir():
    """Find template-v2 in a source checkout, a DSH skill, or a pip install.

    pyproject data-files install the tree under
    <prefix>/share/project-relay/template-v2; a source checkout and the
    standalone skill keep it at <root>/assets/template-v2. The old
    constant-only lookup silently produced empty skeleton files (and a
    FileNotFoundError in _skeleton_bytes) after a pip install.
    """
    candidates = [
        Path(__file__).resolve().parent.parent / 'assets' / 'template-v2',
        Path(__file__).resolve().parent / 'assets' / 'template-v2',
    ]
    try:
        data = sysconfig.get_path('data')
        if data:
            candidates.append(Path(data) / 'share' / 'project-relay' / 'template-v2')
    except Exception:
        pass
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return candidates[0]


TEMPLATES_DIR = _resolve_templates_dir()


def main_worktree(path):
    """Return the repository's main worktree, even when called from a linked worktree."""
    root = Path(git_toplevel(path))
    result = git(root, 'worktree', 'list', '--porcelain', check=False)
    if result.returncode == 0:
        for line in os.fsdecode(result.stdout).splitlines():
            if line.startswith('worktree '):
                return Path(line[len('worktree '):]).resolve()
    return root


def store_dir_for(toplevel):
    """<项目根目录>/.project-relay —— v3 的唯一档案位置（从工作树也解析到主工作树）。"""
    return main_worktree(toplevel) / STORE_DIRNAME


def project_file_path(store_dir):
    return Path(store_dir) / PROJECT_FILE


def registry_path():
    """Machine-local project locator cache (never inside the project)."""
    override = os.environ.get('PROJECT_RELAY_HOME')
    base = Path(override).expanduser() if override else (Path.home() / REGISTRY_DIRNAME)
    return base / REGISTRY_FILE


def read_registry():
    path = registry_path()
    if not path.exists():
        return {'version': 1, 'projects': {}}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {'version': 1, 'projects': {}}
    if not isinstance(data, dict):
        return {'version': 1, 'projects': {}}
    projects = data.get('projects')
    if not isinstance(projects, dict):
        projects = {}
    try:
        version = int(data.get('version') or 1)
    except (TypeError, ValueError):
        version = 1
    return {'version': version, 'projects': projects}


def save_registry(data):
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    save(path, data)
    return path


def ensure_project_file(store_dir, root):
    """Create/repair <store>/project.json under the registry lock.

    Minting the ID and registering it must not interleave with another
    init: the read-check-write here runs under the same machine-local
    lock register_project uses, so two concurrent inits on one fresh
    project converge on a single project_id.
    """
    with _StoreLock(registry_path().with_suffix('.lock')):
        return _ensure_project_file_unlocked(store_dir, root)


def _ensure_project_file_unlocked(store_dir, root):
    """Create/repair <store>/project.json; never stores an absolute root."""
    path = project_file_path(store_dir)
    current = {}
    if path.exists():
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
            if isinstance(value, dict):
                current = value
        except (OSError, ValueError):
            current = {}
    project = dict(current)
    changed = not path.exists()
    if not isinstance(project.get('project_id'), str) or \
            not re.fullmatch(r'[0-9a-f]{32}', project['project_id']):
        project['project_id'] = uuid.uuid4().hex
        changed = True
    if not isinstance(project.get('name'), str) or not project['name'].strip():
        project['name'] = Path(root).name
        changed = True
    if not isinstance(project.get('created_at'), str) or not project['created_at'].strip():
        project['created_at'] = utc()
        changed = True
    if changed:
        save(path, project)
    return project


def register_project(root, store_dir, project=None):
    """Upsert this project into the machine-local locator cache.

    The read-modify-write runs under a machine-local lock so concurrent
    initializations cannot drop each other's registry row.
    """
    project = dict(project or ensure_project_file(store_dir, root))
    root = Path(root).resolve()
    with _StoreLock(registry_path().with_suffix('.lock')):
        data = read_registry()
        entry = {
            'project_id': project['project_id'],
            'name': project.get('name') or root.name,
            'path': str(root),
            'last_seen': utc(),
        }
        # The path is the canonical locator; drop stale rows for the same path.
        for pid, old in list(data['projects'].items()):
            if pid != project['project_id'] and isinstance(old, dict) \
                    and Path(str(old.get('path', ''))).expanduser().resolve() == root:
                data['projects'].pop(pid, None)
        data['projects'][project['project_id']] = entry
        save_registry(data)
    return entry


def _open_command(path, shell='auto'):
    """Quote a path for a user's shell; never print executable path text raw."""
    text = str(path)
    if shell == 'auto':
        shell = 'powershell' if os.name == 'nt' else 'posix'
    if shell == 'posix':
        return 'cd ' + shlex.quote(text)
    if shell == 'powershell':
        # Set-Location -Path interprets [ ] and wildcards; use -LiteralPath.
        return "Set-Location -LiteralPath '" + text.replace("'", "''") + "'"
    # CMD: the printed line is pasted into an interactive prompt, so the
    # path goes in double quotes as-is; %% is batch-file syntax and would
    # point the command at a different (wrong) path here.
    return 'cd /d "%s"' % text


def unregister_project_path(root, project_id=None):
    """Remove one project row from the machine-local locator cache."""
    root = Path(root).resolve()
    with _StoreLock(registry_path().with_suffix('.lock')):
        data = read_registry()
        for pid, old in list(data['projects'].items()):
            if pid == project_id or (isinstance(old, dict) and
                                     Path(str(old.get('path', ''))).expanduser().resolve() == root):
                data['projects'].pop(pid, None)
        save_registry(data)


def adopt_project(toplevel):
    """Register an existing .project-relay archive without changing its ID."""
    root = Path(main_worktree(toplevel)).resolve()
    store_dir = root / STORE_DIRNAME
    require(store_dir.is_dir(), '项目尚未建仓：' + str(store_dir))
    _reject_store_symlinks(store_dir)
    project = ensure_project_file(store_dir, root)
    register_project(root, store_dir, project)
    return {'root': str(root), 'project': project}


def move_project(toplevel, destination):
    """Move a project directory and keep its project_id stable.

    destination is the exact new project directory. A move across devices
    uses shutil.move; a normal rename is used within one device.
    """
    root = Path(main_worktree(toplevel)).resolve()
    dest = Path(destination).expanduser()
    if not dest.is_absolute():
        dest = (Path.cwd() / dest).resolve()
    require(dest != root, '目标目录不能是项目根本身')
    require(not dest.exists(), '目标目录已存在：' + str(dest))
    try:
        dest.relative_to(root)
        require(False, '目标目录不能位于项目内部')
    except ValueError:
        pass
    source_store = root / STORE_DIRNAME
    if source_store.exists() or source_store.is_symlink():
        _reject_store_symlinks(source_store)
    dest.parent.mkdir(parents=True, exist_ok=True)
    old_root = root
    old_cwd = Path.cwd()
    cwd_inside = old_cwd == root or root in old_cwd.parents
    if cwd_inside:
        os.chdir(str(root.parent))
    try:
        shutil.move(str(root), str(dest))
    finally:
        if cwd_inside:
            # The old cwd may no longer exist after the move; never leave
            # the process parked inside the moved tree or the temp parent.
            try:
                os.chdir(str(Path.home()))
            except OSError:
                pass
    store_dir = dest / STORE_DIRNAME
    _reject_store_symlinks(store_dir)
    project = ensure_project_file(store_dir, dest)
    unregister_project_path(old_root)
    register_project(dest, store_dir, project)
    return {'from': str(old_root), 'to': str(dest), 'project': project}


def private_agent_root(root):
    """Return the Agent-private home containing root, when applicable."""
    root = Path(root).resolve()
    candidates = [Path.home() / '.dsh', Path.home() / '.agents',
                  Path.home() / '.claude', Path.home() / '.codex',
                  Path.home() / '.workbuddy']
    profile = os.environ.get('DSH_PROFILE_DIR')
    if profile:
        candidates.append(Path(profile))
    for base in candidates:
        try:
            root.relative_to(base.expanduser().resolve())
            return str(base)
        except (ValueError, OSError):
            continue
    return None


def project_root_warning(root):
    private = private_agent_root(root)
    if not private:
        return None
    suggested = Path.home() / 'Projects' / Path(root).name
    return ('项目根位于 Agent 私有区 %s；建议迁移到用户项目区，例如 %s；'
            '可用 relay project move --to <目标目录> 迁移。'
            % (private, suggested))


def locate_projects(name=None):
    data = read_registry()
    query = (name or '').strip().lower()
    rows = []
    for pid, entry in data['projects'].items():
        if not isinstance(entry, dict):
            continue
        path = Path(str(entry.get('path', ''))).expanduser()
        values = [str(entry.get('name', '')), path.name, pid]
        if query and not any(query in v.lower() for v in values):
            continue
        rows.append({'project_id': pid, 'name': entry.get('name') or path.name,
                     'path': str(path), 'exists': path.is_dir(),
                     'last_seen': entry.get('last_seen')})
    rows.sort(key=lambda row: (row['name'].lower(), row['path'].lower()))
    return rows


def _reject_store_symlinks(store_dir):
    """Reject a .project-relay tree whose controlled directories are symlinks.

    safe() already protects payload paths, but many writers address
    store_dir/rel directly; a repo-committed .project-relay symlink could
    redirect those writes outside the project. Check the store root and
    every directory level we create/manage before returning it.
    """
    store_dir = Path(store_dir)
    require(not store_dir.is_symlink(), '拒绝符号链接档案目录: ' + str(store_dir))
    for name in ('agents', 'receipts', 'state', 'archive'):
        p = store_dir / name
        if p.is_symlink() or p.exists():
            require(not p.is_symlink(), '拒绝符号链接: ' + str(p))
    agents = store_dir / 'agents'
    if agents.is_dir() and not agents.is_symlink():
        for child in agents.iterdir():
            if child.is_symlink():
                require(False, '拒绝符号链接: ' + str(child))
            if not child.is_dir():
                continue
            for name in ('raw', 'state', 'tx', 'backups', 'sessions'):
                p = child / name
                if p.is_symlink() or p.exists():
                    require(not p.is_symlink(), '拒绝符号链接: ' + str(p))


def resolve_store_dir(toplevel):
    """Return the project store dir; fail closed when the project has no archive yet."""
    store_dir = store_dir_for(toplevel)
    require(store_dir.is_dir(),
            '项目尚未建仓，先运行 relay init（档案在 <项目根目录>/.project-relay/）')
    _reject_store_symlinks(store_dir)
    return store_dir


def _store_exists(toplevel):
    try:
        return store_dir_for(toplevel).is_dir()
    except RelayError:
        return False


class _StoreLock:
    """排他文件锁：同一份档案同时只许一个写者。

    外层一把（.project-relay/.write.lock），各家目录各一把
    （agents/<agent>/.write.lock）。不同 Agent 写不同目录互不阻塞；
    同一个 Agent 开两个会话同时写自己那份，在该目录的锁上排队。
    锁是阻塞的，写路径上不设超时；跨设备合并不上的情况走分叉检测
    与合并兜底，不走锁。"""

    def __init__(self, lock_path):
        self.path = Path(lock_path)
        self._fh = None

    def __enter__(self):
        self._fh = None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, 'a+')
        try:
            if os.name == 'nt':
                msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
            else:
                fcntl.flock(fh, fcntl.LOCK_EX)
        except BaseException:
            # 加锁失败不能让文件句柄悬空：否则 __exit__ 或下一次 __enter__
            # 会拿到一个没锁住、也没关掉的句柄。
            try:
                fh.close()
            finally:
                self._fh = None
            raise
        self._fh = fh
        return self

    def __exit__(self, *exc):
        fh = self._fh
        if fh is None:
            return False
        self._fh = None
        try:
            if os.name == 'nt':
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fh, fcntl.LOCK_UN)
        except (OSError, ValueError):
            pass
        finally:
            fh.close()
        return False


def outer_lock(store_dir):
    """档案级写锁：跨 Agent 的整档案事务都拿它。

    目标确认、新 Agent 加入、resume 写 state.md 这类外层写入要拿；
    capture/summarize 与 export-zip/import-zip 也整段持有，确保压缩包
    落盘和任何档案写入不会交错。单 Agent 自己的目录锁仍在其内层。
    """
    return _StoreLock(Path(store_dir) / '.write.lock')


def agent_lock(store_dir, agent):
    """某家目录的写锁：该 Agent 自己的 capture/summarize/日志都走它。"""
    return _StoreLock(agent_dir(store_dir, agent) / '.write.lock')


def ensure_gitignore(toplevel):
    """Make sure .project-relay/ and generated handoff zips stay out of git.

    档案默认不进 git，换设备只走 ZIP。已有的 .gitignore 只追加缺失行，
    不动其他内容（CRLF 文件保持 CRLF）。"""
    root = Path(main_worktree(toplevel))
    gi = root / '.gitignore'
    entries = [STORE_DIRNAME + '/', HANDOFF_ZIP_PATTERN]
    if gi.exists():
        raw = gi.read_bytes()
        newline = b'\r\n' if b'\r\n' in raw else b'\n'
        lines = raw.decode('utf-8', errors='replace').splitlines()
        present = [line.strip() for line in lines]
        missing = [entry for entry in entries if entry not in present]
        if not missing:
            return False
        lines.extend(missing)
        atomic(gi, (newline.decode().join(lines) + newline.decode()).encode('utf-8'))
        return True
    atomic(gi, ('\n'.join(entries) + '\n').encode('utf-8'))
    return True


def agent_dir(store_dir, agent):
    """agents/<agent>/。名字只认字母数字与 -_，未知名字直接拒绝。"""
    require(isinstance(agent, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*',
                                                    agent or ''),
            '未知 Agent 名：%s' % agent)
    return Path(store_dir) / 'agents' / agent


def list_agents(store_dir):
    """已建目录的 Agent 名（排序后）。目录本身就是入伙记录。"""
    base = Path(store_dir) / 'agents'
    if not base.is_dir():
        return []
    return sorted(d.name for d in base.iterdir() if d.is_dir())


def render_index(agents):
    """指路表：哪个 Agent 的记忆文件在哪。"""
    lines = ['# 指路表', '',
             '本项目记忆分家存放：外层是总目标与总记忆，各家 Agent 的记忆在 agents/<名>/。',
             '外层文件只在目标确认或新 Agent 加入时改，平时由 resume 从各家文件里现算。', '',
             '| Agent | 目录 | 记忆文件 | 日志 | 备注 |',
             '|---|---|---|---|---|']
    tiers = {'deepseek-harness': '弱档（技能目录）'}
    for agent in agents:
        note = tiers.get(agent, '旧版兼容（只读）')
        lines.append('| %s | agents/%s/ | agents/%s/memory.md | agents/%s/log.jsonl | %s |'
                     % (agent, agent, agent, agent, note))
    return '\n'.join(lines) + '\n'


def write_index(store_dir):
    atomic(Path(store_dir) / 'index.md', render_index(list_agents(store_dir)).encode('utf-8'))


def ensure_agent_dir(store_dir, agent, update_index=True):
    """建一家目录（新 Agent 加入）：骨架文件 + 子目录 + 指路表。

    已存在时只补缺失的骨架，不动已有内容。现役 DSH 与旧版兼容目录都走这里。"""
    base = agent_dir(store_dir, agent)
    for d in AGENT_DIRS:
        (base / d).mkdir(parents=True, exist_ok=True)
    for name in AGENT_FILES:
        dest = base / name
        if dest.exists():
            continue
        template = 'agent-goal.md' if name == 'goal.md' else name
        src = TEMPLATES_DIR / template
        if src.exists():
            dest.write_bytes(src.read_bytes())
        else:
            dest.write_text('', encoding='utf-8')
    log = base / 'log.jsonl'
    if not log.exists():
        log.write_text('', encoding='utf-8')
    if update_index:
        index = Path(store_dir) / 'index.md'
        if index.exists():
            write_index(store_dir)
    return base


def _agent_log_path(store_dir, agent):
    return agent_dir(store_dir, agent) / 'log.jsonl'


def append_agent_log(store_dir, agent, event, detail=None):
    """Append one JSON line to agents/<agent>/log.jsonl.

    调用方应在已持有该家目录锁的写路径里调（capture/summarize/
    relay log 都如此）；本函数自己不拿锁。"""
    path = _agent_log_path(store_dir, agent)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {'at': utc(), 'agent': agent, 'event': event}
    if detail is not None:
        row['detail'] = detail
    with open(path, 'a', encoding='utf-8') as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + '\n')
    return row


def read_agent_log(store_dir, agent, limit=50):
    path = _agent_log_path(store_dir, agent)
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding='utf-8').splitlines()[-limit:]:
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def _outer_doc(store_dir, name):
    return store_path(store_dir, name)


def read_stack(store_dir):
    p = _outer_doc(store_dir, 'stack.md')
    return p.read_text(encoding='utf-8') if p.exists() else ''


def write_stack(store_dir, text):
    atomic(_outer_doc(store_dir, 'stack.md'), text.encode('utf-8'))
    update_checksum_manifest(store_dir)
    return _outer_doc(store_dir, 'stack.md')


def read_outlook(store_dir):
    p = _outer_doc(store_dir, 'outlook.md')
    return p.read_text(encoding='utf-8') if p.exists() else ''


def write_outlook(store_dir, text):
    atomic(_outer_doc(store_dir, 'outlook.md'), text.encode('utf-8'))
    update_checksum_manifest(store_dir)
    return _outer_doc(store_dir, 'outlook.md')


def overall_goal_hash(store_dir):
    """当前总目标的哈希：已确认用收据里的，否则用 goal.md 内容哈希，未建目标返回 None。"""
    _path, receipt = _latest_receipt(store_dir)
    if receipt:
        return receipt.get('goal_hash')
    data = _goal_bytes(store_dir)
    if data is None or not data.strip():
        return None
    return digest(data)


def read_agent_goal(store_dir, agent):
    p = agent_dir(store_dir, agent) / 'goal.md'
    return p.read_text(encoding='utf-8') if p.exists() else ''


def agent_goal_recorded_hash(store_dir, agent):
    """Agent 目标文件首行记录的总目标哈希（对齐检查用），无记录返回 None。"""
    text = read_agent_goal(store_dir, agent)
    m = re.search(r'^总目标哈希：([0-9a-f]{64})\s*$', text, re.MULTILINE)
    return m.group(1) if m else None


def write_agent_goal(store_dir, agent, text):
    """Write the agent's goal, stamping the overall-goal hash it aligns to.

    只写该 Agent 自己的目录（调用方持 agent 锁）。"""
    current = overall_goal_hash(store_dir)
    header = ('总目标哈希：%s\n\n' % current) if current else ''
    atomic(agent_dir(store_dir, agent) / 'goal.md', (header + text).encode('utf-8'))
    # Any archive write must refresh the integrity manifest, else the next
    # resume reports goal.md as a mismatch. Goal writes used to leave it
    # stale; now that all four agents have goal.md from init, they must sync.
    update_checksum_manifest(store_dir)
    return agent_dir(store_dir, agent) / 'goal.md'


def init(toplevel):
    """Create <root>/.project-relay/ with the per-agent layout.

    外层：总目标/总记忆文件 + receipts/ + state/（目标机件暂存）。
    各家：agents/<agent>/，现役 DeepSeek Harness 给全套骨架。
    幂等：已建仓时只补缺失的骨架文件、不动已有内容，并确保 .gitignore
    兜底。项目身份写在 project.json（稳定 project_id），机器本地注册表只做定位缓存。"""
    root = Path(main_worktree(toplevel))
    store_dir = root / STORE_DIRNAME
    require(not store_dir.is_symlink(), '拒绝符号链接档案目录: ' + str(store_dir))
    if store_dir.exists():
        _reject_store_symlinks(store_dir)
    (store_dir / 'receipts').mkdir(parents=True, exist_ok=True)
    (store_dir / 'state').mkdir(parents=True, exist_ok=True)
    for agent in ACTIVE_AGENTS:
        ensure_agent_dir(store_dir, agent, update_index=False)
    for name in OUTER_FILES:
        dest = store_dir / name
        if dest.exists() or name == 'index.md':
            continue
        src = TEMPLATES_DIR / name
        if src.exists():
            dest.write_bytes(src.read_bytes())
        else:
            dest.write_text('', encoding='utf-8')
    write_index(store_dir)
    ignored = ensure_gitignore(root)
    project = ensure_project_file(store_dir, root)
    register_project(root, store_dir, project)
    return {'store_dir': str(store_dir), 'root': str(root),
            'gitignored': ignored, 'project': project,
            'warning': project_root_warning(root)}

def git_toplevel(path):
    """Resolve any path inside a repo to its top level (plan 1.4 preflight)."""
    path = Path(path).expanduser().resolve()
    result = git(path, 'rev-parse', '--show-toplevel', check=False)
    require(result.returncode == 0,
            '请指定 Git 仓库内的路径；非 Git 项目不在本版范围')
    return Path(os.fsdecode(result.stdout).strip()).resolve()

def store_path(store, *rel):
    """Resolve a relative path inside the store through the path jail."""
    return safe(store, '/'.join(rel))

def slug_for(name):
    slug = re.sub(r'[^a-z0-9._-]+', '-', name.lower()).strip('-.')
    return (slug[:40] or 'project')



def _manifest_files(store_dir):
    """Every file under .project-relay/, as {relative path: bytes}.

    完整性清单覆盖整棵树（外层 + 各家目录）。清单本身、锁文件、
    回滚残留（rollback-trash）、记账（stats/）与 is_local_only_path()
    认定的本机状态不计入：锁文件是空的排队标记，残留本来就不属于
    正式档案，本机状态随写随增（config、fingerprint-cache、cursor、
    digest 等），若算进清单，正常采集后下一次 resume 就会误报。"""
    out = {}
    for p in sorted(Path(store_dir).rglob('*')):
        if not p.is_file() or p.is_symlink():
            continue
        rel = p.relative_to(store_dir).as_posix()
        if rel == CHECKSUM_MANIFEST or rel.endswith('.lock'):
            continue
        if rel.startswith(ROLLBACK_TRASH_PREFIX):
            continue
        if rel.startswith('stats/'):
            continue
        if is_local_only_path(rel):
            continue
        out[rel] = p.read_bytes()
    return out

def update_checksum_manifest(store_dir):
    """Integrity manifest (plan 2.3.5): SHA256 of every formal archive file.

    Refreshed by every archive writer (summarize, import landing, goal
    confirm/decline, rollback). Rollback-trash is excluded: it is
    deliberately not part of the formal archive.
    """
    files = _manifest_files(store_dir)
    save(store_dir / CHECKSUM_MANIFEST,
         {'updated_at': utc(),
          'files': {rel: digest(data) for rel, data in files.items()}})
    return len(files)

def verify_checksum_manifest(store_dir):
    """Returns (present, mismatches). Extra files are ignored: merges and
    goal writes may add files between refreshes; only listed files are
    asserted (missing or hash-changed means possible sync truncation or
    conflict pollution -- never write on top of it, recover first)."""
    path = store_dir / CHECKSUM_MANIFEST
    if not path.exists():
        return False, []
    try:
        manifest = read_json(path)
    except ValueError:
        return True, [{'file': CHECKSUM_MANIFEST, 'expected': '可解析',
                       'actual': '损坏'}]
    mismatches = []
    for rel, expected in (manifest.get('files') or {}).items():
        # Old manifests (pre-0.3) listed machine-local files; they must
        # not turn normal config/cache churn into a corruption report.
        if is_local_only_path(rel):
            continue
        p = store_dir / rel
        if not p.exists():
            mismatches.append({'file': rel, 'expected': expected, 'actual': None})
        else:
            actual = digest(p.read_bytes())
            if actual != expected:
                mismatches.append({'file': rel, 'expected': expected,
                                   'actual': actual})
    return True, mismatches

def _goal_file(store_dir):
    return store_path(store_dir, 'goal.md')

def _goal_bytes(store_dir):
    p = _goal_file(store_dir)
    return p.read_bytes() if p.exists() else None

def _goal_text(store_dir):
    data = _goal_bytes(store_dir)
    return data.decode('utf-8') if data is not None else None

def _skeleton_bytes():
    return (TEMPLATES_DIR / 'goal.md').read_bytes()

def _latest_receipt(store_dir):
    receipts = store_path(store_dir, 'receipts')
    found = sorted(receipts.glob('goal-*.json')) if receipts.exists() else []
    if not found:
        return None, None
    path = found[-1]
    return path, read_json(path)

def _receipt_snapshot(store_dir, seq):
    p = store_path(store_dir, 'receipts', 'goal-%04d.md' % seq)
    return p.read_bytes().decode('utf-8') if p.exists() else None

def _tentative_record(store_dir):
    p = store_path(store_dir, 'state', 'goal-tentative.json')
    return read_json(p) if p.exists() else None

def goal_status(toplevel):
    """Verify the goal state. States: confirmed, tentative, none, tampered,
    modified, unconfirmed."""
    store_dir = resolve_store_dir(toplevel)
    data = _goal_bytes(store_dir)
    if data is None or not data.strip() or data == _skeleton_bytes():
        return 'none', {'note': '无目标档案'}
    text = data.decode('utf-8')
    match = GOAL_STATUS_RE.search(text)
    declared = match.group(1) if match else None
    receipt_path, receipt = _latest_receipt(store_dir)
    if receipt and digest(data) == receipt['goal_hash']:
        return 'confirmed', {'receipt': receipt, 'hash': receipt['goal_hash']}
    tentative = _tentative_record(store_dir)
    if tentative and digest(data) == tentative['content_hash']:
        return 'tentative', {'tentative': tentative}
    # Volume-2 (plan part 03, S4 on the v1 layout): a goal imported from
    # another device is usable only after a local one-click confirm.
    imported_path = store_path(store_dir, 'state', 'goal-imported.json')
    if imported_path.exists():
        try:
            imported = read_json(imported_path)
        except ValueError:
            imported = None
        if imported and digest(data) == imported.get('source_goal_hash'):
            return 'imported', {'imported': imported}
    if receipt:
        return 'tampered', {'receipt': receipt,
                            'stale': _receipt_snapshot(store_dir, receipt['seq']),
                            'current_hash': digest(data)}
    if tentative:
        snap = store_path(store_dir, 'state', 'goal-tentative.md')
        stale = snap.read_text(encoding='utf-8') if snap.exists() else None
        return 'modified', {'tentative': tentative, 'stale': stale}
    if declared in ('未建', '无'):
        return 'none', {'note': '无目标档案'}
    return 'unconfirmed', {'note': '目标内容存在但未经确认'}

def goal_draft(toplevel, content):
    """Stage a goal draft (Agent-written; sources annotated in the text).
    The tool does not judge the draft, it only stores it for diff + confirm."""
    store_dir = resolve_store_dir(toplevel)
    require(isinstance(content, str) and content.strip(), '目标草案内容不能为空')
    data = content.encode('utf-8')
    base = _goal_bytes(store_dir)
    atomic(store_path(store_dir, 'state', 'goal-draft.md'), data)
    save(store_path(store_dir, 'state', 'goal-draft.json'),
         {'content_hash': digest(data),
          'base_hash': digest(base) if base is not None else None,
          'created_at': utc()})
    return {'hash': digest(data)}

def _read_draft(store_dir):
    p = store_path(store_dir, 'state', 'goal-draft.md')
    require(p.exists(), '没有待确认的目标草案，先运行 goal draft')
    return p.read_bytes()

def goal_show(toplevel):
    """Unified diff of current goal -> draft (the ONLY routine diff-to-human
    path in v2, plan 1.5; old review() diff rendering, reused as a pattern)."""
    store_dir = resolve_store_dir(toplevel)
    draft_path = store_path(store_dir, 'state', 'goal-draft.md')
    if draft_path.exists():
        old = _goal_text(store_dir) or ''
        new = draft_path.read_text(encoding='utf-8')
        title = '目标草案 diff（当前 -> 草案）'
    else:
        state, info = goal_status(toplevel)
        require(state == 'tampered', '没有待确认的目标草案，也没有被改动的目标')
        old = info['stale'] or ''
        new = _goal_text(store_dir) or ''
        title = '目标被改动 diff（上一版已确认 -> 当前）'
    return title + '\n' + ''.join(difflib.unified_diff(
        old.splitlines(True), new.splitlines(True),
        fromfile='goal.md:current', tofile='goal.md:proposed'))

def _check_draft_base(store_dir):
    """Codex PR #5, batch 10: a draft is only confirmable against the goal
    it was staged on. goal_draft() records base_hash (the goal's digest
    when the draft was staged); another session may have confirmed,
    declined or imported a goal since, so confirming the old draft would
    silently overwrite the newer goal. A draft staged when there was no
    goal (base_hash None) is likewise stale once a goal exists. Missing
    or unreadable goal-draft.json cannot prove freshness, so it is
    rejected too -- re-draft to get a fresh base. Raises RelayError.
    """
    meta_path = store_path(store_dir, 'state', 'goal-draft.json')
    try:
        meta = read_json(meta_path)
        base_hash = meta.get('base_hash')
    except Exception:
        raise RelayError('目标草案元数据缺失或损坏，无法确认草案是否仍基于当前目标；'
                         '请重新起草（relay goal draft）后再确认')
    current = _goal_bytes(store_dir)
    current_hash = digest(current) if current is not None else None
    require(current_hash == base_hash,
            '目标草案已过期：起草后目标已被另一会话确认/否决/导入；'
            '请重新起草（relay goal draft）后再确认')

def _goal_confirm_inner(toplevel, channel='cli', tentative=False):
    """Confirm a draft. Normal path: write goal + hash-chained receipt.
    --tentative path: write goal WITHOUT a receipt; the goal counts as
    tentative only (plan 1.5: auto-generated, unread goals are never confirmed)."""
    store_dir = resolve_store_dir(toplevel)
    draft = _read_draft(store_dir)
    current = _goal_bytes(store_dir)
    if not tentative:
        receipt_path, receipt = _latest_receipt(store_dir)
        if receipt and current is not None and digest(current) == receipt['goal_hash'] \
                and digest(draft) == receipt['goal_hash']:
            # Re-confirming the current goal also lifts a goal-fork
            # freeze: the user has decided this version stands.
            store_path(store_dir, 'state', 'goal-fork.json').unlink(missing_ok=True)
            return {'already': True, 'receipt': receipt}
        # Batch 10: the already-path above returns first -- a
        # re-confirmation is not an overwrite, so a base that moved away
        # and back must not reject it. Every other confirm must prove
        # the draft is still based on the current goal.
        _check_draft_base(store_dir)
        seq = (receipt['seq'] + 1) if receipt else 1
        if current is not None and current != _skeleton_bytes() and current != draft:
            history = store_path(store_dir, 'receipts', 'goal-history')
            atomic(history / ('%04d.md' % seq), current)
        atomic(_goal_file(store_dir), draft)
        prev_hash = digest(receipt_path.read_bytes()) if receipt_path else None
        new_receipt = {'version': 1, 'seq': seq, 'goal_hash': digest(draft),
                       'confirmed_at': utc(), 'channel': channel,
                       'prev_receipt_hash': prev_hash}
        save(store_path(store_dir, 'receipts', 'goal-%04d.json' % seq), new_receipt)
        atomic(store_path(store_dir, 'receipts', 'goal-%04d.md' % seq), draft)
        store_path(store_dir, 'state', 'goal-draft.md').unlink(missing_ok=True)
        store_path(store_dir, 'state', 'goal-draft.json').unlink(missing_ok=True)
        store_path(store_dir, 'state', 'goal-tentative.md').unlink(missing_ok=True)
        store_path(store_dir, 'state', 'goal-tentative.json').unlink(missing_ok=True)
        store_path(store_dir, 'state', 'goal-imported.json').unlink(missing_ok=True)
        # Plan part 03, step 3.3 item 4③: a normal (non-tentative)
        # confirmation IS the S4 reconfirmation that lifts a goal-fork
        # freeze; tentative adoption is not confirmation, so it does not.
        store_path(store_dir, 'state', 'goal-fork.json').unlink(missing_ok=True)
        update_checksum_manifest(store_dir)
        return {'already': False, 'receipt': new_receipt}
    # Batch 10: the tentative path also writes the goal file, so the same
    # staleness check applies -- a stale draft must not become the goal
    # just because no receipt is issued.
    _check_draft_base(store_dir)
    text = draft.decode('utf-8')
    if not GOAL_STATUS_RE.search(text):
        text = '状态：暂定\n\n' + text
        draft = text.encode('utf-8')
    atomic(_goal_file(store_dir), draft)
    atomic(store_path(store_dir, 'state', 'goal-tentative.md'), draft)
    save(store_path(store_dir, 'state', 'goal-tentative.json'),
         {'content_hash': digest(draft), 'adopted_at': utc(),
          'revisit': ['下次里程碑到达时', '下次交接时', '用户主动提起时']})
    store_path(store_dir, 'state', 'goal-draft.md').unlink(missing_ok=True)
    store_path(store_dir, 'state', 'goal-draft.json').unlink(missing_ok=True)
    store_path(store_dir, 'state', 'goal-imported.json').unlink(missing_ok=True)
    update_checksum_manifest(store_dir)
    return {'tentative': True, 'hash': digest(draft)}

def _confirm_imported(toplevel):
    """One-click local confirmation of an imported goal (plan part 03, S4).

    The local receipt chain keeps local continuity (seq = latest local
    seq + 1, prev_receipt_hash chained to the local previous receipt);
    cross-device version continuity is recorded, not forced, in the
    imported_from block (v1 receipts have no goal_version field)."""
    store_dir = resolve_store_dir(toplevel)
    imported_path = store_path(store_dir, 'state', 'goal-imported.json')
    require(imported_path.exists(), '没有待本机确认的导入目标')
    imported = read_json(imported_path)
    current = _goal_bytes(store_dir)
    require(current is not None and digest(current) == imported.get('source_goal_hash'),
            '目标已被改动，与导入记录不一致；请走正常草案确认流程')
    receipt_path, receipt = _latest_receipt(store_dir)
    seq = (receipt['seq'] + 1) if receipt else 1
    prev_hash = digest(receipt_path.read_bytes()) if receipt_path else None
    new_receipt = {'version': 1, 'seq': seq, 'goal_hash': digest(current),
                   'confirmed_at': utc(), 'channel': 'import',
                   'prev_receipt_hash': prev_hash,
                   'imported_from': {'source_device': imported.get('source_device'),
                                     'source_device_id': imported.get('source_device_id'),
                                     'source_receipt_seq': imported.get('source_receipt_seq'),
                                     'source_goal_hash': imported.get('source_goal_hash')}}
    save(store_path(store_dir, 'receipts', 'goal-%04d.json' % seq), new_receipt)
    atomic(store_path(store_dir, 'receipts', 'goal-%04d.md' % seq), current)
    imported_path.unlink(missing_ok=True)
    return {'receipt': new_receipt}

def _goal_decline_inner(toplevel):
    """User declines to build a goal file (plan 1.5 no-goal exit).

    Codex PR #5 finding 2: merely writing the 未建 text left a prior
    confirmed receipt in place, so goal_status() then reported tampered
    and goal_inject() kept surfacing the old confirmed goal. A decline
    is an explicit user decision, not a tamper: retire the receipt chain
    (archive the superseded goal text, delete receipts and tentative
    state) so the project returns to a clean 'none' state.
    """
    store_dir = resolve_store_dir(toplevel)
    receipt_path, receipt = _latest_receipt(store_dir)
    if receipt is not None:
        current = _goal_bytes(store_dir)
        if current is not None and current.strip():
            history = store_path(store_dir, 'receipts', 'goal-history')
            atomic(history / ('%04d-declined.md' % receipt['seq']), current)
        receipts = store_path(store_dir, 'receipts')
        if receipts.exists():
            for p in receipts.glob('goal-*.json'):
                p.unlink(missing_ok=True)
            for p in receipts.glob('goal-*.md'):
                p.unlink(missing_ok=True)
    # Codex PR #5 rounds 7-12, item 9: a tentative goal has no receipt,
    # so the receipt-only archive above skips it and the cleanup below
    # used to delete it permanently. Archive the tentative text (and
    # its metadata, when present) under a distinct tentative-declined
    # name BEFORE deleting, so a declined tentative goal is recoverable
    # from archive/goal-history/ like any declined confirmed goal.
    tentative_md = store_path(store_dir, 'state', 'goal-tentative.md')
    tentative_json = store_path(store_dir, 'state', 'goal-tentative.json')
    if tentative_md.exists() and tentative_md.read_bytes().strip():
        history = store_path(store_dir, 'receipts', 'goal-history')
        stem = 'tentative-declined-%s' % utc().replace('-', '').replace(
            ':', '').replace('.', '').replace('+', '')
        atomic(history / (stem + '.md'), tentative_md.read_bytes())
        if tentative_json.exists():
            atomic(history / (stem + '.json'), tentative_json.read_bytes())
    tentative_md.unlink(missing_ok=True)
    tentative_json.unlink(missing_ok=True)
    text = '状态：未建\n\n用户于 %s 决定不建目标档案。\n' % utc()
    atomic(_goal_file(store_dir), text.encode('utf-8'))
    store_path(store_dir, 'state', 'goal-draft.md').unlink(missing_ok=True)
    store_path(store_dir, 'state', 'goal-draft.json').unlink(missing_ok=True)
    update_checksum_manifest(store_dir)
    return {'declined': True}



def goal_confirm(toplevel, channel='cli', tentative=False):
    store_dir = resolve_store_dir(toplevel)
    with outer_lock(store_dir):
        return _goal_confirm_inner(toplevel, channel=channel, tentative=tentative)


def goal_decline(toplevel):
    store_dir = resolve_store_dir(toplevel)
    with outer_lock(store_dir):
        return _goal_decline_inner(toplevel)

def goal_inject(toplevel):
    """Text a new session may inject, by goal state (plan 1.5)."""
    state, info = goal_status(toplevel)
    store_dir = resolve_store_dir(toplevel)
    # Plan part 03, step 3.3 item 4③: a forked goal never auto-merges --
    # injection freezes and the S4 reconfirmation flow takes over. The
    # canonical definition lives in relay_fork.goal_fork_active (flag
    # file or volume-2 sync goal-conflict marker); this inline check
    # keeps goal_inject free of an import cycle with the fork module.
    if (store_dir / 'state' / 'goal-fork.json').exists() \
            or (store_dir / 'state' / 'goal-conflict.json').exists():
        return ('【目标已冻结】目标文件存在分叉，注入已停止；请走 S4 重新确认流程'
                '（relay goal confirm 或 relay sync-resolve --goal）后再接手。')
    text = _goal_text(store_dir) or ''
    if state == 'confirmed':
        return text
    if state == 'tentative':
        return TENTATIVE_WATERMARK + '\n' + text
    if state == 'imported':
        return ('【目标待本机确认】以下目标未经本机确认，不可当作已确认目标执行；'
                '请先运行 relay goal confirm --imported 一键确认：\n' + text)
    if state == 'none':
        return '本项目无目标档案。\n'
    if state == 'tampered':
        out = '【目标待确认】目标文件已被改动，注入已停止；以下是上一版已确认目标的只读副本（可能已过期），请重新确认后再使用：\n'
        return out + (info.get('stale') or '')
    if state == 'modified':
        out = '【暂定目标已被改动】以下是暂定时的只读副本（可能已过期）：\n'
        return out + (info.get('stale') or '')
    return '【目标未确认】以下内容未经确认，仅供参考：\n' + text

def goal_check(toplevel):
    """Injection gate: passes for every state except tampered (plan 1.5)."""
    state, info = goal_status(toplevel)
    if state == 'tampered':
        diff = goal_show(toplevel)
        print(diff)
        raise RelayError('目标已被改动，注入已停止；请重新确认目标后再接手')
    return state, info


# ---------------------------------------------------------------- CLI

def _session_from_stdin():
    """Session id from a hook payload on stdin (Codex PR #5 finding 4).

    Claude Code hook commands receive the hook payload as JSON on stdin,
    which carries session_id. The installed Stop hook previously passed
    no --session, so every conversation shared the literal 'default' key
    and five blocks across any conversations permanently disabled
    enforcement. Returns None when stdin is a TTY, empty, or not JSON
    with a session id, so manual runs keep the old behavior; the
    installed hook command itself needs no change.
    """
    try:
        if sys.stdin.isatty():
            return None
        raw = sys.stdin.read()
    except Exception:
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return None
    if isinstance(data, dict):
        sid = data.get('session_id') or data.get('sessionId') or data.get('session')
        return sid if isinstance(sid, str) and sid.strip() else None
    return None

def _print_sync_project(entry):
    """One line per imported project plus its conflicts/discards."""
    counts = entry.get('counts', {})
    print('项目 %s：%s，新增条目 %d，跳过 %d，冲突 %d，目标动作 %s'
          % (entry['slug'], entry['status'], counts.get('entries_added', 0),
             counts.get('entries_skipped', 0), len(entry.get('conflicts', [])),
             entry.get('goal_action')))
    for c in entry.get('conflicts', []):
        # Batch 2, B6: a fork conflict line must name its section --
        # the old rendering printed rule + file + a generic note, so
        # six fork conflicts printed six identical lines.
        key_suffix = '（%s）' % c['key'] if c.get('key') else ''
        detail = c.get('note')
        if detail is None and c.get('incoming') is not None:
            detail = '对方：%s' % c['incoming']
        if detail is None:
            detail = '双方保留，见合并日志'
        print('  冲突[%s] %s%s：%s'
              % (c.get('rule'), c.get('file', c.get('key', '')),
                 key_suffix, detail))
    for d in entry.get('discarded', []):
        # Batch 2, B2: nothing reported here is deleted -- discards are
        # quarantined (merge-log quarantined/ or unclaimed _quarantine/),
        # never in raw/ and never counted -- so the wording is 隔离,
        # not 丢弃, to match what actually happened to the bytes.
        print('  隔离 %s：%s（未进 raw/，未计汇总）' % (d['file'], d['reason']))
    for q in entry.get('quarantined', []):
        print('  隔离区：' + q)
    for note in entry.get('notes', []):
        print('  注：' + note)


# ---------------------------------------------------------------- CLI


# ---------------------------------------------------------------- CLI
def _parser():
    p = argparse.ArgumentParser(prog='relay', description='项目根目录下的共享记忆，分 Agent 存放（v3）')
    p.add_argument('--root', default='.', help='项目根目录（默认当前目录）')
    p.add_argument('--version', action='version', version='relay ' + VERSION)
    sub = p.add_subparsers(dest='command', required=True)
    sub.add_parser('init')
    loc = sub.add_parser('locate', help='按项目名/ID 查本机项目定位缓存')
    loc.add_argument('--name')
    loc.add_argument('--json', action='store_true')
    op = sub.add_parser('open', help='打印项目的 cd 路径（供 shell 执行）')
    op.add_argument('name')
    op.add_argument('--shell', choices=['auto', 'posix', 'powershell', 'cmd'],
                    default='auto')
    prj = sub.add_parser('project', help='项目归属与迁移')
    prjsub = prj.add_subparsers(dest='project_command', required=True)
    pm = prjsub.add_parser('move', help='移动项目目录并保留 project_id')
    pm.add_argument('--to', required=True, help='新的项目目录（精确路径）')
    prjsub.add_parser('adopt', help='把已有 .project-relay 登记到本机定位缓存')
    sub.add_parser('agents')
    g = sub.add_parser('goal')
    gsub = g.add_subparsers(dest='goal_command', required=True)
    d = gsub.add_parser('draft')
    d.add_argument('--file', required=True, help='UTF-8 文件：目标草案完整内容')
    gsub.add_parser('show')
    c = gsub.add_parser('confirm')
    c.add_argument('--channel', default='cli')
    c.add_argument('--tentative', action='store_true')
    c.add_argument('--imported', action='store_true',
                   help='一键确认已导入的目标（跨设备确认）')
    gsub.add_parser('check')
    gsub.add_parser('inject')
    gsub.add_parser('decline')
    gsub.add_parser('status')
    ag = sub.add_parser('agent-goal')
    ag.add_argument('--agent', required=True, help='Agent 名，如 deepseek-harness')
    ag.add_argument('--set', help='写入该家目标全文（不给则只显示）')
    ag.add_argument('--file', help='从 UTF-8 文件读该家目标全文写入')
    # Implemented in scripts/relay_flow.py; imported lazily in main() to
    # avoid a relay.py <-> relay_flow.py import cycle (relay_flow loads
    # relay.py itself through importlib for the foundation helpers).
    cap = sub.add_parser('capture')
    cap.add_argument('--agent', required=True, help='采集进哪家目录，如 deepseek-harness')
    cap.add_argument('--session')
    cap.add_argument('--message')
    card = sub.add_parser('card', help='在会话结束或阶段节点写一张短会话卡')
    card.add_argument('--agent', required=True)
    card.add_argument('--session', required=True)
    card.add_argument('--stage', required=True, choices=['session-end', 'milestone'])
    card.add_argument('--file', required=True, help='UTF-8 短卡正文，不读取原始聊天')
    card.add_argument('--llm-usage', help='JSON：{"calls":n,"input_tokens":n,"output_tokens":n}')
    q = sub.add_parser('queue')
    q.add_argument('--agent', help='只看某家，不给则看全部')
    enf = sub.add_parser('enforce')
    enf.add_argument('--agent', required=True)
    enf.add_argument('--session')
    hk = sub.add_parser('hook')
    hksub = hk.add_subparsers(dest='hook_command', required=True)
    hksub.add_parser('resume')
    hs = hksub.add_parser('stop')
    hs.add_argument('--agent', default='claude-code')
    hs.add_argument('--session')
    hs.add_argument('--card-file', help='DSH Stop 传入的 UTF-8 短卡正文')
    hs.add_argument('--llm-usage', help='JSON：{"calls":n,"input_tokens":n,"output_tokens":n}')
    hg = hksub.add_parser('stage')
    hg.add_argument('--agent', required=True)
    hg.add_argument('--session', required=True)
    hg.add_argument('--file', required=True, help='UTF-8 短卡正文')
    hg.add_argument('--llm-usage')

    r = sub.add_parser('recipe')
    rsub = r.add_subparsers(dest='recipe_command', required=True)
    ri = rsub.add_parser('install')
    ri.add_argument('--tool', required=True,
                    choices=['deepseek-harness'])
    ri.add_argument('--target-root')
    ri.add_argument('--home')
    ru = rsub.add_parser('uninstall')
    ru.add_argument('--tool', required=True,
                    choices=['deepseek-harness'])
    ru.add_argument('--target-root')
    ru.add_argument('--home')
    pc = sub.add_parser('precompress')
    pc.add_argument('--agent', required=True)
    fp = sub.add_parser('fingerprint')
    fp.add_argument('--agent', required=True)
    sc = sub.add_parser('scales')
    sc.add_argument('--agent', required=True)
    sc.add_argument('--session')
    su = sub.add_parser('summarize')
    su.add_argument('--agent', required=True, help='汇总哪家目录，如 deepseek-harness')
    su.add_argument('--source', default='scales')
    su.add_argument('--semantic-file')
    su.add_argument('--session')
    su.add_argument('--trigger', choices=['milestone', 'volume', 'time',
                                          'handoff', 'manual'])
    su.add_argument('--llm-usage', help='JSON：{"calls":n,"input_tokens":n,'
                                        '"output_tokens":n}，有则记为实测')
    rc = sub.add_parser('recover')
    rc.add_argument('--agent', required=True)
    inj = sub.add_parser('inject')
    inj.add_argument('--budget', type=int)
    inj.add_argument('--agent', help='只看某家会话卡；--full 时同时带其待办与决定')
    inj.add_argument('--full', action='store_true', help='旧全档案注入')
    res = sub.add_parser('resume')
    res.add_argument('--full', action='store_true',
                     help='旧五段接手：读全档案、Git 对账并补汇总（会写入）')
    doc = sub.add_parser('doctor', help='项目体检：档案/卡/清单/定位缓存/分区')
    doc.add_argument('--json', action='store_true', help='输出 JSON 供脚本使用')
    wk = sub.add_parser('work', help='多 Agent 的分支与工作树事务层')
    wksub = wk.add_subparsers(dest='work_command', required=True)
    for name in ('enter', 'status', 'review', 'merge'):
        wp = wksub.add_parser(name)
        wp.add_argument('--agent', default='deepseek-harness')
        wp.add_argument('--workspace', default=None,
                        help='Agent 工作区（默认当前目录）')
        wp.add_argument('--branch', default=None,
                        help='默认 agent/<agent>')
        wp.add_argument('--base', default=None,
                        help='默认主工作树当前分支，否则 main/master')
        wp.add_argument('--worktree-root', default=None,
                        help='工作树根（默认 <workspace>/worktrees）')
        if name == 'merge':
            wp.add_argument('--confirm', action='store_true',
                            help='用户批准后才能真正合并')
    # Implemented in scripts/relay_sync.py; imported lazily in main() like
    # relay_flow. The sync branch wraps its own errors (belt and braces);
    # its RelayError is the same class main() catches, since the sibling
    # modules share one process-wide relay copy.
    ez = sub.add_parser('export-zip')
    ez.add_argument('--zip', help='输出 zip；默认 <项目根>/<项目名>-project-relay-<时间>.zip')
    ez.add_argument('--strict', action='store_true',
                    help='导出质量缺口为阻断级时直接失败，不产出包')
    iz = sub.add_parser('import-zip')
    iz.add_argument('--zip', required=True)
    iz.add_argument('--confirm', action='store_true')
    sr = sub.add_parser('sync-resolve')
    sr.add_argument('--list', action='store_true')
    sr.add_argument('--goal', action='store_true')
    sr.add_argument('--fork')
    sr.add_argument('--agent',
                    help='分叉 key 在多家 memory.md 里都有时，指定裁哪一家')
    sr.add_argument('--backlog')
    sr.add_argument('--decision', choices=['keep-local', 'adopt-incoming'])
    sr.add_argument('--file')
    st = sub.add_parser('stack')
    st.add_argument('--set', help='写入技术栈全文（不给则只显示）')
    st.add_argument('--file', help='从 UTF-8 文件读技术栈全文写入')
    ou = sub.add_parser('outlook')
    ou.add_argument('--set', help='写入未来预期全文（不给则只显示）')
    ou.add_argument('--file', help='从 UTF-8 文件读未来预期全文写入')
    lg = sub.add_parser('log')
    lg.add_argument('--agent', required=True, help='Agent 名，如 deepseek-harness')
    lg.add_argument('--append', help='追加一条日志（正文）')
    lg.add_argument('--limit', type=int, default=20)
    return p


def main(argv=None):
    a = _parser().parse_args(argv)
    try:
        if a.command == 'init':
            result = init(a.root)
            print('记忆仓：' + str(result['store_dir']))
            print('档案默认不进 git（已写 .gitignore 兜底）' if result['gitignored']
                  else '档案已在 .gitignore 里')
            print('项目身份证：' + result['project']['project_id'])
            if result.get('warning'):
                print('提醒：' + result['warning'])
            print('下一步：用 goal draft 提交目标草案，再用 goal show 查看 diff、goal confirm 确认')
        elif a.command == 'project':
            if a.project_command == 'move':
                info = move_project(a.root, a.to)
                print('项目已迁移：%s -> %s' % (info['from'], info['to']))
                print('project_id：' + info['project']['project_id'])
            else:
                info = adopt_project(a.root)
                print('项目已登记：' + info['root'])
                print('project_id：' + info['project']['project_id'])
        elif a.command == 'locate':
            rows = locate_projects(a.name)
            if a.json:
                print(json.dumps(rows, ensure_ascii=False, indent=2))
            elif not rows:
                print('未找到项目；可先在本机对项目运行 relay init 注册')
            else:
                for row in rows:
                    suffix = '' if row['exists'] else '（不存在）'
                    print('%s\t%s\t%s%s' % (row['name'], row['project_id'],
                                               row['path'], suffix))
        elif a.command == 'open':
            rows = locate_projects(a.name)
            if not rows:
                print('未找到项目：' + a.name, file=sys.stderr)
                return 1
            if len(rows) > 1:
                print('匹配多个项目，请用 relay locate --name 查看：' + a.name, file=sys.stderr)
                return 1
            if not rows[0]['exists']:
                print('项目路径不存在：' + rows[0]['path'] + '；请重新 init 或清理缓存', file=sys.stderr)
                return 1
            print(_open_command(rows[0]['path'], a.shell))
        elif a.command == 'agents':
            store_dir = resolve_store_dir(a.root)
            print((store_dir / 'index.md').read_text(encoding='utf-8')
                  if (store_dir / 'index.md').exists() else '（无指路表，先 relay init）')
        elif a.command == 'goal':
            root = a.root
            if a.goal_command == 'draft':
                content = Path(a.file).read_text(encoding='utf-8')
                info = goal_draft(root, content)
                print('目标草案已暂存，内容 SHA256：' + info['hash'])
            elif a.goal_command == 'show':
                print(goal_show(root))
            elif a.goal_command == 'confirm':
                if a.imported:
                    info = _confirm_imported(root)
                    print('导入目标已在本机确认，收据：goal-%04d.json' % info['receipt']['seq'])
                    return 0
                info = goal_confirm(root, channel=a.channel, tentative=a.tentative)
                if info.get('already'):
                    print('目标已确认，无需重复确认')
                elif info.get('tentative'):
                    print('目标已以暂定状态落盘（无确认收据）；注入时将带暂定水印')
                else:
                    print('目标已确认，收据：goal-%04d.json' % info['receipt']['seq'])
            elif a.goal_command == 'check':
                state, _info = goal_check(root)
                print('PASS：目标状态 ' + state + '，可注入')
            elif a.goal_command == 'inject':
                print(goal_inject(root))
            elif a.goal_command == 'decline':
                goal_decline(root)
                print('已记录：用户决定不建目标档案')
            elif a.goal_command == 'status':
                state, info = goal_status(root)
                print('目标状态：' + state)
                if info.get('receipt'):
                    print('最新收据：goal-%04d.json，目标哈希 %s'
                          % (info['receipt']['seq'], info['receipt']['goal_hash']))
                if info.get('tentative'):
                    print('暂定记录：' + str(info['tentative'].get('adopted_at')))
                if info.get('note'):
                    print('注：' + info['note'])
        elif a.command == 'agent-goal':
            store_dir = resolve_store_dir(a.root)
            ensure_agent_dir(store_dir, a.agent)
            text = getattr(a, 'set')
            if a.file:
                text = Path(a.file).read_text(encoding='utf-8')
            if text is None:
                current = read_agent_goal(store_dir, a.agent)
                print(current if current else '（空）')
            else:
                with outer_lock(store_dir):
                    with agent_lock(store_dir, a.agent):
                        write_agent_goal(store_dir, a.agent, text)
                print('%s 的目标已写入（已记当前总目标哈希备查）' % a.agent)
        elif a.command in ('export-zip', 'import-zip', 'sync-resolve'):
            # The sync branch wraps its own errors (belt and braces):
            # its RelayError is the same class main() catches, since the
            # sibling modules share one process-wide relay copy -- but
            # export/import must never leak a traceback either way.
            try:
                _sspec = importlib.util.spec_from_file_location(
                    'relay_sync', Path(__file__).resolve().parent / 'relay_sync.py')
                sync = importlib.util.module_from_spec(_sspec)
                _sspec.loader.exec_module(sync)
                if a.command == 'export-zip':
                    result = sync.export_zip(a.root, a.zip, strict=a.strict)
                    print('导出完成：%s' % result['zip'])
                    print('条目 %d 个，文件 %d 个，脱敏命中 %d 次'
                          % (result['entry_count'], result['file_count'],
                             result['redacted_count']))
                    gaps = result.get('quality_gaps') or []
                    if gaps:
                        print('质量缺口 %d 项（已写入 manifest.quality_gaps）：'
                              % len(gaps))
                        for gap in gaps:
                            print('  [%s] %s'
                                  % (gap.get('severity'), gap.get('message')))
                elif a.command == 'import-zip':
                    result = sync.import_zip(a.root, a.zip, confirm=a.confirm)
                    if not result['landed']:
                        print('干跑预览（未写入任何内容）：')
                    else:
                        print('导入已落盘：%s' % result['zip'])
                    for entry in result['report']['projects']:
                        _print_sync_project(entry)
                        if entry.get('repaired'):
                            print('  修复：' + '、'.join(entry['repaired']))
                elif a.command == 'sync-resolve':
                    if a.list:
                        result = sync.sync_resolve(a.root, list_only=True)
                        if not result['pending']:
                            print('无待裁项')
                        for item in result['pending']:
                            if item['kind'] == 'goal':
                                print('目标冲突：来自 %s（seq %s），本地版本被保留'
                                      % (item.get('source_device'),
                                         item.get('source_seq')))
                            elif item['kind'] == 'fork':
                                print('分叉待裁：%s，对方值：%s'
                                      % (item['key'], item['incoming']))
                            else:
                                print('待办冲突：%s，本地：%s，对方：%s'
                                      % (item['item'], item.get('local'),
                                         item.get('incoming')))
                    else:
                        result = sync.sync_resolve(
                            a.root, goal=a.goal or None,
                            fork=a.fork, backlog=a.backlog,
                            decision=a.decision, file=a.file,
                            agent=a.agent)
                        for action in result['actions']:
                            print(action)
            except Exception as exc:
                print('ERROR: ' + str(exc), file=sys.stderr)
                return 1
        elif a.command in ('stack', 'outlook'):
            store_dir = resolve_store_dir(a.root)
            read_fn = read_stack if a.command == 'stack' else read_outlook
            write_fn = write_stack if a.command == 'stack' else write_outlook
            label = '技术栈' if a.command == 'stack' else '未来预期'
            text = getattr(a, 'set')
            if a.file:
                text = Path(a.file).read_text(encoding='utf-8')
            if text is None:
                current = read_fn(store_dir)
                print(current if current else '（空）')
            else:
                with outer_lock(store_dir):
                    write_fn(store_dir, text)
                print('%s已写入' % label)
        elif a.command == 'log':
            store_dir = resolve_store_dir(a.root)
            if a.append is not None:
                with outer_lock(store_dir):
                    with agent_lock(store_dir, a.agent):
                        append_agent_log(store_dir, a.agent, 'note', a.append)
                        update_checksum_manifest(store_dir)
                print('日志已追加到 agents/%s/log.jsonl' % a.agent)
            else:
                rows = read_agent_log(store_dir, a.agent, limit=a.limit)
                if not rows:
                    print('（无日志）')
                for row in rows:
                    detail = row.get('detail')
                    print('%s %s%s' % (row.get('at', ''), row.get('event', ''),
                                       ('：' + str(detail)) if detail is not None else ''))
        elif a.command == 'work':
            _wspec = importlib.util.spec_from_file_location(
                'relay_work', Path(__file__).resolve().parent / 'relay_work.py')
            work = importlib.util.module_from_spec(_wspec)
            _wspec.loader.exec_module(work)
            if a.work_command == 'enter':
                info = work.enter(a.root, workspace=a.workspace, agent=a.agent,
                                  branch=a.branch, base=a.base,
                                  worktree_root=a.worktree_root)
                print(work.render_enter(info))
            elif a.work_command == 'status':
                print(work.render_status(work.status(a.root, agent=a.agent)))
            elif a.work_command == 'review':
                print(work.render_review(work.review(a.root, agent=a.agent,
                                                     branch=a.branch, base=a.base)))
            else:
                print(work.render_merge(work.merge(a.root, agent=a.agent,
                                                   branch=a.branch, base=a.base,
                                                   confirm=a.confirm)))
        elif a.command in ('capture', 'card', 'queue', 'enforce', 'hook', 'recipe', 'precompress',
                           'fingerprint', 'scales', 'summarize', 'recover',
                           'inject', 'resume', 'doctor'):
            _fspec = importlib.util.spec_from_file_location(
                'relay_flow', Path(__file__).resolve().parent / 'relay_flow.py')
            flow = importlib.util.module_from_spec(_fspec)
            _fspec.loader.exec_module(flow)
            root = a.root
            if a.command == 'card':
                card_text = Path(a.file).read_text(encoding='utf-8')
                usage = json.loads(a.llm_usage) if a.llm_usage else None
                info = flow.write_session_card(
                    root, agent=a.agent, session=a.session, stage=a.stage,
                    text=card_text, llm_usage=usage)
                print('会话卡：card-%04d（%s）' % (info['seq'], info['path']))
                print('正文字符：%d / %d；本月 LLM 已记录：%d tokens；月度上限：%s'
                      % (info['semantic_chars'], info['char_cap'],
                         info['month_llm_tokens'],
                         '未设置' if info['monthly_cap'] is None else info['monthly_cap']))
            elif a.command == 'capture':
                info = flow.capture(root, agent=a.agent, session=a.session,
                                    message=a.message)
                if info.get('deduped'):
                    if a.message is not None:
                        # Batch 2, B4: the dedup key is the repo state
                        # (plan 1.6, by design -- not changed here), but
                        # a --message dropped by that dedup used to
                        # vanish silently behind a "returned existing
                        # entry" line. Say so, and say how to re-take it.
                        print('新消息文本已被去重丢弃，未留存（已有条目 cap-%04d）；'
                              '请换 session 或改变仓库状态后重采' % info['seq'])
                    else:
                        print('采集去重：事实未变，返回已有条目 cap-%04d' % info['seq'])
                else:
                    print('采集条目：cap-%04d（%s）' % (info['seq'], info['path']))
            elif a.command == 'queue':
                st = flow.queue_status(flow.relay.resolve_store_dir(root),
                                       agent=a.agent, repair=False)
                print('pending：%d  summarized：%d  summarizing：%d'
                      % (st['pending'], st['summarized'], st['summarizing']))
                print('raw 总量：%s MB  最老 pending：%s  脱敏累计：%d'
                      % (st['raw_mb'], st['oldest_pending'], st['redacted_total']))
                print('超时 summarizing：%s  回退超 3 次：%s'
                      % (st['stale_summarizing'], st['over_retries']))
            elif a.command == 'enforce':
                session = a.session or _session_from_stdin()
                info = flow.enforce(root, agent=a.agent, session=session)
                print(info.get('instruction') or ('PASS：' + info['note']))
                if info['action'] == 'block':
                    return 2
            elif a.command == 'hook':
                # Project entry for the Claude Code hooks: the project is
                # the session cwd. Anything unresolvable fails closed
                # with the skip note.
                try:
                    hook_root = git_toplevel(a.root)
                except Exception:
                    print('当前目录不在 Git 仓库里，钩子不对账')
                    return 0
                if a.hook_command == 'resume':
                    r = flow.hook_resume(hook_root)
                    print(r['note'] if r.get('skipped') else
                          (r['message'] if r.get('no_archive') else flow.render_resume(r)))
                elif a.hook_command == 'stage':
                    text = None
                    if a.file:
                        try:
                            text = Path(a.file).read_text(encoding='utf-8')
                        except (OSError, UnicodeDecodeError) as exc:
                            print('阶段卡读取失败，跳过写卡：%r' % (exc,), file=sys.stderr)
                            return 0
                    usage = None
                    if a.llm_usage:
                        try:
                            usage = json.loads(a.llm_usage)
                        except ValueError as exc:
                            print('阶段卡 llm_usage 解析失败，改按估算成本：%r' % (exc,),
                                  file=sys.stderr)
                    r = flow.hook_stage(hook_root, a.agent, a.session, text, usage)
                    if r.get('skipped'):
                        print(r['note'])
                    else:
                        print('阶段卡：card-%04d（%s）' % (r['seq'], r['path']))
                else:
                    session = a.session or _session_from_stdin()
                    text = None
                    if a.card_file:
                        try:
                            text = Path(a.card_file).read_text(encoding='utf-8')
                        except (OSError, UnicodeDecodeError) as exc:
                            # A bad card input must not take capture/enforce with it.
                            print('会话卡读取失败，跳过写卡：%r' % (exc,), file=sys.stderr)
                    usage = None
                    if a.llm_usage:
                        try:
                            usage = json.loads(a.llm_usage)
                        except ValueError as exc:
                            print('llm_usage 解析失败，改按估算成本：%r' % (exc,),
                                  file=sys.stderr)
                    r = flow.hook_stop(hook_root, agent=a.agent, session=session,
                                       card_text=text, card_usage=usage)
                    cap, enf = r['capture'], r['enforce']
                    print(cap['note'] if cap.get('skipped') else
                          '采集条目：cap-%04d（%s）' % (cap['seq'], cap.get('path', '')))
                    if r.get('card'):
                        if r['card'].get('error'):
                            print('会话卡未写入：' + r['card']['error'], file=sys.stderr)
                        elif not r['card'].get('skipped'):
                            print('会话卡：card-%04d（%s）' % (r['card']['seq'], r['card']['path']))
                    msg = enf.get('instruction') or ('PASS：' + enf.get('note', ''))
                    if enf.get('action') == 'block':
                        print(msg, file=sys.stderr)
                        return 2
                    print(msg)
            elif a.command == 'recipe':
                home = Path(a.home).expanduser() if a.home else None
                if a.recipe_command == 'install':
                    info = flow.install_deepseek_harness(root,
                        target_root=a.target_root, home_dir=home)
                    print('skill 目录：' + info['skill_dir'])
                    print(info['note'])
                else:
                    info = flow.uninstall_deepseek_harness(root)
                    print('已移除：' + info['removed'])
            elif a.command == 'precompress':
                doc = flow.precompress(root, agent=a.agent)
                print('整理稿：条目区间 %s，改动文件 %d 个，失败样本 %d 个'
                      % (doc['entry_range'], len(doc['files_changed']),
                         len(doc['degraded_samples'])))
            elif a.command == 'fingerprint':
                m = flow.measure_change(root, agent=a.agent)
                c = m['counts']
                print('新增 %d / 修改 %d / 删除 %d / 改名 %d；行数 +%d/-%d'
                      % (len(c['added']), len(c['modified']), len(c['deleted']),
                         len(c['renamed']), m['lines']['added'], m['lines']['deleted']))
                for g in m['snapshot']['gaps']:
                    print('缺口：子模块 %s 未深入（指针 %s）' % (g['path'], g['pointer']))
                t = m['snapshot'].get('timing') or {}
                print('指纹耗时 %.2fs（文件 %d 个，缓存命中 %d / 未命中 %d）'
                      % (t.get('elapsed_s', 0.0), t.get('files', 0),
                         t.get('cache_hits', 0), t.get('cache_misses', 0)))
                if t.get('exceeded'):
                    print('指纹耗时超过阈值（%.1fs），可配置排除目录：'
                          '.project-relay/state/config.json 的 fingerprint_excludes，'
                          '排除项写入保真度声明' % t.get('threshold_s', 30.0))
                for e in m['snapshot'].get('excluded', []):
                    print('已排除：%s' % e)
            elif a.command == 'scales':
                rec = flow.scales(root, agent=a.agent, session=a.session)
                for row in rec['rows']:
                    print('%s：%s，阈值 %s，到线：%s（%s）'
                          % (row['scale'], row['value'], row['threshold'],
                             row['reached'], row['reason']))
                print('触发：' + str(rec['triggered']))
            elif a.command == 'summarize':
                semantic = (Path(a.semantic_file).read_text(encoding='utf-8')
                            if a.semantic_file else None)
                usage = json.loads(a.llm_usage) if a.llm_usage else None
                info = flow.summarize(root, agent=a.agent, source=a.source,
                                      semantic_text=semantic, session=a.session,
                                      trigger=a.trigger, llm_usage=usage)
                if info.get('noop'):
                    print(info['note'])
                else:
                    print('汇总序号：%d，覆盖条目 %s，触发源 %s'
                          % (info['seq'], info['entry_range'], info['source']))
            elif a.command == 'recover':
                info = flow.recover(root, agent=a.agent)
                print('已恢复事务：' + info['recovered'])
                # Batch 2, A2: recover rolls the whole transaction back,
                # not just the archive files -- print the rolled-back
                # state so the operator can see cursor/baseline/summary
                # moved with the archive instead of trusting it.
                print('回滚后游标：cap-%04d，基线汇总序号：%s，最近汇总序号：%s，'
                      '条目回 pending：%s'
                      % (info.get('cursor', {}).get('entry_seq', 0),
                         info.get('baseline_summary_seq'),
                         info.get('last_summary_seq'),
                         info.get('entries_reverted_to_pending')))
            elif a.command == 'inject':
                info = flow.inject(root, budget=a.budget, agent=a.agent, full=a.full)
                print(info['text'])
                print('\n[注入 token 粗估：%d / 预算 %d]' % (info['tokens_estimate'], info['budget']))
            elif a.command == 'doctor':
                result = flow.doctor(root)
                if a.json:
                    print(json.dumps(result, ensure_ascii=False, indent=2))
                else:
                    print(flow.render_doctor(result))
                return 1 if result.get('errors') else 0
            elif a.command == 'resume':
                result = flow.resume(root, full=a.full)
                if result.get('no_archive'):
                    print(result['message'])
                else:
                    print(flow.render_resume(result))
    except (RelayError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
        print('ERROR: ' + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
