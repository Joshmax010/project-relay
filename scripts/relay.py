#!/usr/bin/env python3
"""project-relay v2 foundation: local-first memory store, project identity, init, goals.

Scope (detailed plan, part 02, steps 1.2-1.5 ONLY):
  1.2  memory store layout under ~/.project-relay/ (Path.home() derived, never hardcoded)
  1.3  project identity keyed by git remote, registry, `where` lookup
  1.4  init: build the store, register the project, legacy .agent preview only
  1.5  goal layer: the ONLY content that needs human confirmation, hash-certified

Provenance (every major piece notes its plan step and source):
  - atomic()/safe()/digest()/save()/read_json()/git()/anchor()/utc()/RelayError:
    old relayctl.py substrate, reused VERBATIM by import (plan 1.2, reuse list
    "original reuse (foundation)"). Nothing here re-implements them.
  - Store layout (projects/ + registry.json + global/ + trash/; six zones per
    project store): plan 1.2 ORIGINAL layout decision; plain-text files only
    borrows agent-beacon's plain-text boundary (fusion #25).
  - Remote-as-identity: Handoff-CLI (fusion #28). Weak-key-to-strong-key
    auto-migration: plan 1.3 ORIGINAL (no upstream handled this case).
  - Goal fields: old project.md template fields, reused; hash-certification
    point + honesty limit borrows planning-with-files (fusion #37); verbatim
    user-quote discipline borrows ctx-handoff-mod (fusion #35); the
    extract->draft->confirm chain, change-same-rule, tentative-as-first-class
    state and no-goal exit are plan 1.5 ORIGINAL combinations.

Honesty boundary (plan 1.5, from planning-with-files #37, kept verbatim in
spirit): a local SHA256 receipt proves "the goal file has not been silently
changed since confirmation". It is NOT a signature and does NOT stop anyone
with local write access from forging a receipt. An auto-generated goal the
user never read word-for-word counts as TENTATIVE, never confirmed.

All writes in this module go through relayctl.atomic(). Any direct open()
write of a store file is a defect (plan 1.2 review red line).

Environment override (tests only): PROJECT_RELAY_HOME replaces Path.home()
when deriving the store root. Production never sets it.
"""
import argparse
import difflib
import importlib.util
import json
import tempfile
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlparse

# --- old substrate, imported verbatim (plan 1.2) ---------------------------
_here = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location('relayctl', _here / 'relayctl.py')
relayctl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(relayctl)

RelayError = relayctl.RelayError
require = relayctl.require
utc = relayctl.utc
digest = relayctl.digest
atomic = relayctl.atomic
save = relayctl.save
read_json = relayctl.read_json
safe = relayctl.safe
git = relayctl.git
anchor = relayctl.anchor

STORE_DIRNAME = '.project-relay'
ZONES = ('raw', 'archive', 'state', 'tx', 'backups', 'receipts')
# Archive skeletons (plan 1.2 fixed names; 1.13 may revise template bodies,
# never these file names).
ARCHIVE_FILES = ('goal.md', 'state.md', 'backlog.md', 'decisions.md', 'lessons.md')
TEMPLATES_DIR = _here.parent / 'assets' / 'template-v2'
GOAL_STATUS_RE = re.compile(r'^状态：(.+?)\s*$', re.MULTILINE)
TENTATIVE_WATERMARK = '【暂定目标】以下目标为暂定状态，不可当作硬约束执行。'


# ---------------------------------------------------------------- 1.2 store
def home_dir():
    """User home; PROJECT_RELAY_HOME overrides it (tests only)."""
    override = os.environ.get('PROJECT_RELAY_HOME')
    return Path(override).expanduser() if override else Path.home()


def default_store():
    # Folder carrier (plan 2.3): the user may point the whole store at a
    # sync folder via ~/.project-relay-location (written by sync-setup
    # --carrier folder). The env override (tests) always wins; a pointer
    # to a missing directory is returned as-is so commands fail loudly
    # instead of silently writing to the wrong store.
    if os.environ.get('PROJECT_RELAY_HOME'):
        return home_dir() / STORE_DIRNAME
    try:
        target = (Path.home() / '.project-relay-location').read_text(
            encoding='utf-8').strip()
    except OSError:
        target = ''
    if target:
        return Path(target).expanduser()
    return home_dir() / STORE_DIRNAME


def device_identity():
    """Stable per-device identity for folder-carrier heartbeat locks.

    Lives in the home directory, never inside a synced store, so two
    devices never fight over it. Generated once on first use (write
    paths only; read-only flows must never call this).
    """
    path = home_dir() / '.project-relay-device.json'
    try:
        data = read_json(path)
        if isinstance(data, dict) and data.get('device_id'):
            return data['device_id'], data.get('device_name')
    except (ValueError, OSError):
        pass
    import socket
    import uuid
    device_id = uuid.uuid4().hex
    device_name = socket.gethostname()
    save(path, {'device_id': device_id, 'device_name': device_name})
    return device_id, device_name


# ---------------------------------------------------------------- checksum manifest (plan 2.3.5)
CHECKSUM_MANIFEST = 'state/checksum-manifest.json'
ROLLBACK_TRASH_PREFIX = 'archive/rollback-trash/'


def _manifest_files(store_dir):
    archive = store_dir / 'archive'
    out = {}
    if archive.is_dir():
        for p in sorted(archive.rglob('*')):
            if p.is_file() and not p.is_symlink():
                rel = 'archive/' + p.relative_to(archive).as_posix()
                if rel.startswith(ROLLBACK_TRASH_PREFIX):
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
        p = store_dir / rel
        if not p.exists():
            mismatches.append({'file': rel, 'expected': expected, 'actual': None})
        else:
            actual = digest(p.read_bytes())
            if actual != expected:
                mismatches.append({'file': rel, 'expected': expected,
                                   'actual': actual})
    return True, mismatches


def store_path(store, *rel):
    """Resolve a relative path inside the store through the path jail."""
    return safe(store, '/'.join(rel))


def load_registry(store):
    path = store / 'registry.json'
    if not path.exists():
        return {'version': 1, 'projects': []}
    value = read_json(path)
    require(value.get('version') == 1 and isinstance(value.get('projects'), list),
            '注册表格式不支持: ' + str(path))
    return value


def save_registry(store, registry):
    save(store / 'registry.json', registry)


def project_store_dir(store, row):
    return store / 'projects' / (row['key'] + '-' + row['slug'])


# ---------------------------------------------------------------- 1.3 identity
def normalize_remote(url):
    """Canonical remote identity. Returns None for non-URL remotes.

    Strips protocol/userinfo differences, trailing .git, and case (plan 1.3).
    Handoff-CLI (fusion #28) is the model: the remote address, not the local
    path, is the project identity.
    """
    url = (url or '').strip().rstrip('/')
    if url.endswith('.git'):
        url = url[:-4]
    if '://' in url:
        parsed = urlparse(url)
        host = (parsed.hostname or '').lower()
        path = parsed.path.strip('/').lower()
    else:
        m = re.match(r'^(?:[^@:/\s]+@)?([^:/\s]+):(.+)$', url)
        if not m:
            return None
        host, path = m.group(1).lower(), m.group(2).strip('/').lower()
    if not host or not path:
        return None
    return host + '/' + path


def remote_key(url):
    canonical = normalize_remote(url)
    return digest(canonical.encode('utf-8'))[:16] if canonical else None


def local_key(toplevel):
    """Weak key for remote-less projects: hash of the resolved top-level path."""
    return digest(str(toplevel).encode('utf-8'))[:16]


def slug_for(name):
    slug = re.sub(r'[^a-z0-9._-]+', '-', name.lower()).strip('-.')
    return (slug[:40] or 'project')


def git_toplevel(path):
    """Resolve any path inside a repo to its top level (plan 1.4 preflight)."""
    path = Path(path).expanduser().resolve()
    result = git(path, 'rev-parse', '--show-toplevel', check=False)
    require(result.returncode == 0,
            '请指定 Git 仓库内的路径；非 Git 项目不在本版范围')
    return Path(os.fsdecode(result.stdout).strip()).resolve()


def origin_remote(toplevel):
    result = git(toplevel, 'config', '--get', 'remote.origin.url', check=False)
    if result.returncode != 0:
        return None
    return result.stdout.decode().strip() or None


def _active_rows(registry):
    return [r for r in registry['projects'] if r.get('status', 'active') == 'active']


def find_row_by_path(registry, toplevel):
    for row in _active_rows(registry):
        if str(toplevel) in row.get('paths', []):
            return row
    return None


def find_row_by_key(registry, key):
    for row in registry['projects']:
        if row['key'] == key:
            return row
    return None


def register(store, toplevel):
    """Register a project, self-healing paths and migrating weak keys.

    Returns (row, changed, notes). Writes the registry ONLY when something
    actually changed, so a repeat registration is byte-identical (plan 1.4).
    Never creates a project store directory (plan 1.3: stores are created
    only by init or backfill); a migration rename only moves an existing one.
    """
    toplevel = git_toplevel(toplevel)
    remote_url = origin_remote(toplevel)
    canonical = normalize_remote(remote_url) if remote_url else None
    key = remote_key(remote_url) if canonical else local_key(toplevel)
    key_type = 'remote' if canonical else 'local'
    registry = load_registry(store)
    notes = []
    row = find_row_by_path(registry, toplevel)
    if row is not None:
        if row['key'] == key:
            return row, False, notes
        if row.get('key_type') == 'local' and key_type == 'remote':
            # Weak -> strong migration (plan 1.3 ORIGINAL).
            old_key, old_slug = row['key'], row['slug']
            old_dir = store / 'projects' / (old_key + '-' + old_slug)
            new_dir = store / 'projects' / (key + '-' + row['slug'])
            if old_dir.exists() and not new_dir.exists():
                old_dir.rename(new_dir)
                notes.append('项目键已迁移：本地键 %s -> remote 键 %s，记忆仓已改名' % (old_key, key))
            elif old_dir.exists():
                target = store / 'trash' / (old_key + '-' + utc().replace(':', '-'))
                target.parent.mkdir(parents=True, exist_ok=True)
                old_dir.rename(target)
                notes.append('旧仓已移入 trash（%s），内容未自动合并，请人工核对' % target)
            else:
                notes.append('项目键已迁移：本地键 %s -> remote 键 %s（旧仓不存在，无需改名）' % (old_key, key))
            tombstone = dict(row)
            tombstone.update({'status': 'migrated', 'migrated_to': key,
                              'paths': list(row.get('paths', []))})
            row.update({'key': key, 'key_type': 'remote', 'remote': canonical,
                        'migrated_from': [*row.get('migrated_from', []), old_key],
                        'last_active': utc()})
            registry['projects'].append(tombstone)
            save_registry(store, registry)
            return row, True, notes
        notes.append('remote 已变化但沿用既有项目键，不降级为本地键')
        return row, False, notes
    row = find_row_by_key(registry, key)
    if row is not None and row.get('status', 'active') == 'active':
        # Self-heal: same project at a new path (clone/move) joins the set.
        row['paths'].append(str(toplevel))
        row['last_active'] = utc()
        save_registry(store, registry)
        notes.append('已登记新路径：' + str(toplevel))
        return row, True, notes
    row = {'key': key, 'slug': slug_for(toplevel.name), 'display_name': toplevel.name,
           'paths': [str(toplevel)], 'remote': canonical, 'created_at': utc(),
           'last_active': utc(), 'key_type': key_type, 'status': 'active',
           'migrated_from': []}
    registry['projects'].append(row)
    save_registry(store, registry)
    return row, True, notes


def where(store, path=None, key=None):
    """Read-only lookup (plan 1.3): by path or by key. Never mutates anything."""
    registry = load_registry(store)
    if key:
        row = find_row_by_key(registry, key)
        if row is None:
            return {'found': False, 'key': key, 'note': '注册表无此键，先运行 init'}
        if row.get('status') == 'migrated':
            return {'found': False, 'key': key,
                    'note': '该键已迁移至 ' + str(row.get('migrated_to')), 'row': row}
        return {'found': True, 'row': row, 'note': '按 key 查到'}
    toplevel = git_toplevel(path) if path else None
    require(toplevel is not None, 'where 需要 --path 或 --key')
    row = find_row_by_path(registry, toplevel)
    if row is not None:
        return {'found': True, 'row': row, 'note': '路径已登记'}
    remote_url = origin_remote(toplevel)
    computed = remote_key(remote_url) if remote_url else local_key(toplevel)
    row = find_row_by_key(registry, computed)
    if row is not None and row.get('status', 'active') == 'active':
        return {'found': True, 'row': row, 'note': '路径未登记，但 remote 键已登记（只读，未自动加入）'}
    return {'found': False, 'key': computed, 'note': '未登记，先运行 init'}


def resolve_store_dir(store, toplevel):
    """Everyday commands: find the store, never create one (plan 1.3)."""
    result = where(store, path=toplevel)
    if not result['found']:
        raise RelayError('项目未建仓，先运行 relay init：' + result['note'])
    return result['row'], project_store_dir(store, result['row'])


# ---------------------------------------------------------------- 1.4 init
def _check_skill_package():
    require(TEMPLATES_DIR.is_dir(), 'Skill 包不完整：缺少模板目录，未开始初始化')
    missing = [name for name in ARCHIVE_FILES if not (TEMPLATES_DIR / name).is_file()
               or not (TEMPLATES_DIR / name).read_bytes()]
    require(not missing, 'Skill 包不完整：缺少模板：' + '、'.join(missing) + '，未开始初始化')


def legacy_preview(toplevel):
    """Preview-only mapping for a legacy .agent/ dir. Reads nothing else and
    writes nothing (plan 1.4). Returns human-readable mapping lines."""
    agent = toplevel / '.agent'
    if not agent.is_dir():
        return []
    lines = ['旧 .agent/ 迁移预览（仅预览，未搬迁任何文件）：']
    for p in sorted(q for q in agent.rglob('*') if q.is_file()):
        rel = p.relative_to(agent).as_posix()
        if rel.startswith('inbox/'):
            target = 'raw/legacy-inbox/' + rel[len('inbox/'):]
            note = '未审草稿 -> 原料区'
        elif rel.startswith('.local/'):
            target = 'tx/legacy-' + rel[len('.local/'):]
            note = '事务/凭证 -> 事务区（人工核对）'
        elif rel.startswith('conversations/raw/'):
            target = 'raw/' + rel[len('conversations/raw/'):]
            note = '聊天原文 -> 原料区'
        elif rel.startswith(('tools/', 'archive/')):
            target = None
            note = '不迁移（工具副本/旧归档）'
        elif rel.endswith('.md') and '/' not in rel:
            target = 'archive/' + rel
            note = '正式文档 -> 档案区'
        else:
            target = 'raw/legacy-' + rel
            note = '其他 -> 原料区（人工核对）'
        lines.append('  .agent/%s -> %s（%s）' % (rel, target or '（不迁移）', note))
    lines.append('点头后才执行迁移；旧 .agent/ 保留不动。迁移执行不在卷一 1.4 范围。')
    return lines


def init(store, toplevel):
    """Build the store for a project (plan 1.4). Preflight failures write nothing."""
    # Preflight, in order; any failure exits before a single write.
    toplevel = git_toplevel(toplevel)
    registry = load_registry(store)
    for row in _active_rows(registry):
        for known in row.get('paths', []):
            known_path = Path(known)
            if toplevel != known_path and known_path in toplevel.parents:
                raise RelayError('Git 顶层位于已登记项目之内（嵌套误登记）：' + known)
    _check_skill_package()
    row, changed, notes = register(store, toplevel)
    store_dir = project_store_dir(store, row)
    # Build: six zones + top-level store dirs; skeletons exclusive-create.
    for zone in ZONES:
        (store_dir / zone).mkdir(parents=True, exist_ok=True)
    for top in ('projects', 'global', 'trash'):
        (store / top).mkdir(parents=True, exist_ok=True)
    for name in ARCHIVE_FILES:
        dest = store_path(store_dir, 'archive', name)
        if dest.exists():
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            with dest.open('x', encoding='utf-8') as out:
                out.write((TEMPLATES_DIR / name).read_text(encoding='utf-8'))
        except FileExistsError:
            pass
    preview = legacy_preview(toplevel)
    return {'store_dir': store_dir, 'key': row['key'], 'row': row,
            'registered_changed': changed, 'notes': notes, 'preview': preview}


# ---------------------------------------------------------------- 1.5 goals
def _goal_file(store_dir):
    return store_path(store_dir, 'archive', 'goal.md')


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


def goal_status(store, toplevel):
    """Verify the goal state. States: confirmed, tentative, none, tampered,
    modified, unconfirmed."""
    row, store_dir = resolve_store_dir(store, toplevel)
    data = _goal_bytes(store_dir)
    if data is None or not data.strip() or data == _skeleton_bytes():
        return 'none', {'row': row, 'note': '无目标档案'}
    text = data.decode('utf-8')
    match = GOAL_STATUS_RE.search(text)
    declared = match.group(1) if match else None
    receipt_path, receipt = _latest_receipt(store_dir)
    if receipt and digest(data) == receipt['goal_hash']:
        return 'confirmed', {'row': row, 'receipt': receipt, 'hash': receipt['goal_hash']}
    tentative = _tentative_record(store_dir)
    if tentative and digest(data) == tentative['content_hash']:
        return 'tentative', {'row': row, 'tentative': tentative}
    # Volume-2 (plan part 03, S4 on the v1 layout): a goal imported from
    # another device is usable only after a local one-click confirm.
    imported_path = store_path(store_dir, 'state', 'goal-imported.json')
    if imported_path.exists():
        try:
            imported = read_json(imported_path)
        except ValueError:
            imported = None
        if imported and digest(data) == imported.get('source_goal_hash'):
            return 'imported', {'row': row, 'imported': imported}
    if receipt:
        return 'tampered', {'row': row, 'receipt': receipt,
                            'stale': _receipt_snapshot(store_dir, receipt['seq']),
                            'current_hash': digest(data)}
    if tentative:
        snap = store_path(store_dir, 'state', 'goal-tentative.md')
        stale = snap.read_text(encoding='utf-8') if snap.exists() else None
        return 'modified', {'row': row, 'tentative': tentative, 'stale': stale}
    if declared in ('未建', '无'):
        return 'none', {'row': row, 'note': '无目标档案'}
    return 'unconfirmed', {'row': row, 'note': '目标内容存在但未经确认'}


def goal_draft(store, toplevel, content):
    """Stage a goal draft (Agent-written; sources annotated in the text).
    The tool does not judge the draft, it only stores it for diff + confirm."""
    row, store_dir = resolve_store_dir(store, toplevel)
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


def goal_show(store, toplevel):
    """Unified diff of current goal -> draft (the ONLY routine diff-to-human
    path in v2, plan 1.5; old review() diff rendering, reused as a pattern)."""
    row, store_dir = resolve_store_dir(store, toplevel)
    draft_path = store_path(store_dir, 'state', 'goal-draft.md')
    if draft_path.exists():
        old = _goal_text(store_dir) or ''
        new = draft_path.read_text(encoding='utf-8')
        title = '目标草案 diff（当前 -> 草案）'
    else:
        state, info = goal_status(store, toplevel)
        require(state == 'tampered', '没有待确认的目标草案，也没有被改动的目标')
        old = info['stale'] or ''
        new = _goal_text(store_dir) or ''
        title = '目标被改动 diff（上一版已确认 -> 当前）'
    return title + '\n' + ''.join(difflib.unified_diff(
        old.splitlines(True), new.splitlines(True),
        fromfile='goal.md:current', tofile='goal.md:proposed'))


def goal_confirm(store, toplevel, channel='cli', tentative=False):
    """Confirm a draft. Normal path: write goal + hash-chained receipt.
    --tentative path: write goal WITHOUT a receipt; the goal counts as
    tentative only (plan 1.5: auto-generated, unread goals are never confirmed)."""
    row, store_dir = resolve_store_dir(store, toplevel)
    draft = _read_draft(store_dir)
    current = _goal_bytes(store_dir)
    if not tentative:
        receipt_path, receipt = _latest_receipt(store_dir)
        if receipt and current is not None and digest(current) == receipt['goal_hash'] \
                and digest(draft) == receipt['goal_hash']:
            return {'already': True, 'receipt': receipt}
        seq = (receipt['seq'] + 1) if receipt else 1
        if current is not None and current != _skeleton_bytes() and current != draft:
            history = store_path(store_dir, 'archive', 'goal-history')
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
        update_checksum_manifest(store_dir)
        return {'already': False, 'receipt': new_receipt}
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


def _confirm_imported(store, toplevel):
    """One-click local confirmation of an imported goal (plan part 03, S4).

    The local receipt chain keeps local continuity (seq = latest local
    seq + 1, prev_receipt_hash chained to the local previous receipt);
    cross-device version continuity is recorded, not forced, in the
    imported_from block (v1 receipts have no goal_version field)."""
    row, store_dir = resolve_store_dir(store, toplevel)
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


def goal_decline(store, toplevel):
    """User declines to build a goal file (plan 1.5 no-goal exit).

    Codex PR #5 finding 2: merely writing the 未建 text left a prior
    confirmed receipt in place, so goal_status() then reported tampered
    and goal_inject() kept surfacing the old confirmed goal. A decline
    is an explicit user decision, not a tamper: retire the receipt chain
    (archive the superseded goal text, delete receipts and tentative
    state) so the project returns to a clean 'none' state.
    """
    row, store_dir = resolve_store_dir(store, toplevel)
    receipt_path, receipt = _latest_receipt(store_dir)
    if receipt is not None:
        current = _goal_bytes(store_dir)
        if current is not None and current.strip():
            history = store_path(store_dir, 'archive', 'goal-history')
            atomic(history / ('%04d-declined.md' % receipt['seq']), current)
        receipts = store_path(store_dir, 'receipts')
        if receipts.exists():
            for p in receipts.glob('goal-*.json'):
                p.unlink(missing_ok=True)
            for p in receipts.glob('goal-*.md'):
                p.unlink(missing_ok=True)
    store_path(store_dir, 'state', 'goal-tentative.md').unlink(missing_ok=True)
    store_path(store_dir, 'state', 'goal-tentative.json').unlink(missing_ok=True)
    text = '状态：未建\n\n用户于 %s 决定不建目标档案。\n' % utc()
    atomic(_goal_file(store_dir), text.encode('utf-8'))
    store_path(store_dir, 'state', 'goal-draft.md').unlink(missing_ok=True)
    store_path(store_dir, 'state', 'goal-draft.json').unlink(missing_ok=True)
    update_checksum_manifest(store_dir)
    return {'declined': True}


def goal_inject(store, toplevel):
    """Text a new session may inject, by goal state (plan 1.5)."""
    state, info = goal_status(store, toplevel)
    row, store_dir = resolve_store_dir(store, toplevel)
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


def goal_check(store, toplevel):
    """Injection gate: passes for every state except tampered (plan 1.5)."""
    state, info = goal_status(store, toplevel)
    if state == 'tampered':
        diff = goal_show(store, toplevel)
        print(diff)
        raise RelayError('目标已被改动，注入已停止；请重新确认目标后再接手')
    return state, info


# ---------------------------------------------------------------- CLI
def _parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', default='.', help='项目路径（Git 仓库内任意位置），放在子命令之前')
    p.add_argument('--store-root', default=None,
                   help='记忆仓根目录（默认 ~/.project-relay；测试用）')
    sub = p.add_subparsers(dest='command', required=True)
    sub.add_parser('init')
    w = sub.add_parser('where')
    w.add_argument('--key')
    w.add_argument('--path')
    g = sub.add_parser('goal')
    gsub = g.add_subparsers(dest='goal_command', required=True)
    d = gsub.add_parser('draft')
    d.add_argument('--file', required=True, help='UTF-8 文件：目标草案完整内容')
    gsub.add_parser('show')
    c = gsub.add_parser('confirm')
    c.add_argument('--channel', default='cli')
    c.add_argument('--tentative', action='store_true')
    c.add_argument('--imported', action='store_true',
                   help='一键确认已导入的目标（S4 跨设备确认）')
    gsub.add_parser('check')
    gsub.add_parser('inject')
    gsub.add_parser('decline')
    gsub.add_parser('status')
    # --- volume-1 slice 2 (plan part 02, steps 1.6-1.14) -------------------
    # Implemented in scripts/relay_flow.py; imported lazily in main() to
    # avoid a relay.py <-> relay_flow.py import cycle (relay_flow loads
    # relay.py itself through importlib for the foundation helpers).
    cap = sub.add_parser('capture')
    cap.add_argument('--tool')
    cap.add_argument('--session')
    cap.add_argument('--message')
    sub.add_parser('queue')
    enf = sub.add_parser('enforce')
    enf.add_argument('--tool')
    enf.add_argument('--session')
    r = sub.add_parser('recipe')
    rsub = r.add_subparsers(dest='recipe_command', required=True)
    ri = rsub.add_parser('install')
    ri.add_argument('--tool', required=True, choices=['claude-code', 'codebuddy', 'workbuddy', 'codex'])
    ri.add_argument('--target-root')
    ri.add_argument('--home')
    ru = rsub.add_parser('uninstall')
    ru.add_argument('--tool', required=True, choices=['claude-code', 'codebuddy', 'workbuddy', 'codex'])
    ru.add_argument('--target-root')
    ru.add_argument('--home')
    sub.add_parser('precompress')
    sub.add_parser('fingerprint')
    sc = sub.add_parser('scales')
    sc.add_argument('--session')
    su = sub.add_parser('summarize')
    su.add_argument('--source', default='scales')
    su.add_argument('--semantic-file')
    su.add_argument('--session')
    sub.add_parser('recover')
    sub.add_parser('handoff')
    inj = sub.add_parser('inject')
    inj.add_argument('--budget', type=int)
    res = sub.add_parser('resume')
    res.add_argument('--tool')
    # --- volume-1 slice 3 (plan part 02, steps 1.15-1.16) -------------------
    # Implemented in scripts/relay_backfill.py / scripts/relay_accept.py.
    bf = sub.add_parser('backfill')
    bf.add_argument('--log-root')
    bf.add_argument('--tool', action='append')
    bf.add_argument('--tier', default='metadata', choices=['metadata', 'excerpt'])
    bf.add_argument('--deep', action='store_true')
    bf.add_argument('--summarize', action='store_true')
    bf.add_argument('--session-limit', type=int, default=5)
    ac = sub.add_parser('accept')
    ac.add_argument('--log-root')
    ac.add_argument('--workdir')
    ac.add_argument('--keep-dir', action='store_true')
    # --- volume-2 slice 1 (plan part 03, steps 2.1-2.2) --------------------
    # Implemented in scripts/relay_sync.py; imported lazily in main() like
    # relay_flow/relay_backfill. The sync branch wraps its own errors
    # because relay_sync's RelayError class is not this module's.
    # Slice 2 (2.3-2.5) adds --carrier overrides and folder/server setup;
    # the carrier default is inferred (remote -> git, folder -> folder,
    # server-url -> server) so old invocations keep working.
    pk = sub.add_parser('pack')
    pk.add_argument('--remote')
    pk.add_argument('--device-name')
    pk.add_argument('--carrier', choices=['git', 'folder', 'server'])
    pk.add_argument('--overwrite-unconsumed', action='store_true',
                    help='覆盖上一份未在任何设备还原的 WIP 快照（2.6 显式确认）')
    un = sub.add_parser('unpack')
    un.add_argument('--remote')
    un.add_argument('--confirm', action='store_true')
    un.add_argument('--project', action='append', dest='projects')
    un.add_argument('--carrier', choices=['git', 'folder', 'server'])
    ss = sub.add_parser('sync-setup')
    ss.add_argument('--remote')
    ss.add_argument('--device-name')
    ss.add_argument('--carrier', choices=['git', 'folder', 'server'])
    ss.add_argument('--folder')
    ss.add_argument('--server-type', choices=['webdav', 'sftp', 'https', 'local'])
    ss.add_argument('--server-url')
    ss.add_argument('--server-user')
    ss.add_argument('--server-token')
    sc = sub.add_parser('sync-claim')
    sc.add_argument('--key', required=True)
    sc.add_argument('--confirm', action='store_true')
    rb = sub.add_parser('sync-rollback')
    rb.add_argument('--to')
    rb.add_argument('--bundle')
    rb.add_argument('--reason', default='')
    sub.add_parser('sync-status')
    # --- volume-2 slice 3 (plan part 03, steps 2.6-2.7) -------------------
    sr = sub.add_parser('sync-resolve')
    sr.add_argument('--list', action='store_true')
    sr.add_argument('--goal', action='store_true')
    sr.add_argument('--fork')
    sr.add_argument('--backlog')
    sr.add_argument('--decision', choices=['keep-local', 'adopt-incoming'])
    sr.add_argument('--file')
    sub.add_parser('sync-accept')
    return p


def _store_from_args(a):
    return Path(a.store_root).expanduser() if a.store_root else default_store()


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
        print('  冲突[%s] %s：%s'
              % (c.get('rule'), c.get('file', c.get('key', '')),
                 c.get('note', '双方保留，见合并日志')))
    for d in entry.get('discarded', []):
        print('  丢弃 %s：%s' % (d['file'], d['reason']))
    for note in entry.get('notes', []):
        print('  注：' + note)


def _print_sync_report(rep):
    if 'report' not in rep:
        print('项目 %s：跳过（%s）' % (rep['key'], rep.get('note')))
        return
    for entry in rep['report']['projects']:
        _print_sync_project(entry)


def main(argv=None):
    a = _parser().parse_args(argv)
    try:
        store = _store_from_args(a)
        if a.command == 'init':
            result = init(store, a.root)
            print('记忆仓：' + str(result['store_dir']))
            print('项目键：' + result['key'])
            print('下一步：用 goal draft 提交目标草案，再用 goal show 查看 diff、goal confirm 确认')
            for note in result['notes']:
                print('注：' + note)
            for line in result['preview']:
                print(line)
        elif a.command == 'where':
            result = where(store, path=a.path or a.root, key=a.key)
            if result['found']:
                row = result['row']
                print('项目键：' + row['key'])
                print('显示名：' + row['display_name'])
                print('键类型：' + row['key_type'])
                print('remote：' + str(row.get('remote')))
                print('已知路径：')
                for path in row.get('paths', []):
                    print('  ' + path)
                print('来源：' + result['note'])
            else:
                print('未找到：' + result['note'])
                if result.get('key'):
                    print('项目键：' + result['key'])
        elif a.command == 'goal':
            root = a.root
            if a.goal_command == 'draft':
                content = Path(a.file).read_text(encoding='utf-8')
                info = goal_draft(store, root, content)
                print('目标草案已暂存，内容 SHA256：' + info['hash'])
            elif a.goal_command == 'show':
                print(goal_show(store, root))
            elif a.goal_command == 'confirm':
                if a.imported:
                    info = _confirm_imported(store, root)
                    print('导入目标已在本机确认，收据：goal-%04d.json' % info['receipt']['seq'])
                    return 0
                info = goal_confirm(store, root, channel=a.channel, tentative=a.tentative)
                if info.get('already'):
                    print('目标已确认，无需重复确认')
                elif info.get('tentative'):
                    print('目标已以暂定状态落盘（无确认收据）；注入时将带暂定水印')
                else:
                    print('目标已确认，收据：goal-%04d.json' % info['receipt']['seq'])
            elif a.goal_command == 'check':
                state, _info = goal_check(store, root)
                print('PASS：目标状态 ' + state + '，可注入')
            elif a.goal_command == 'inject':
                print(goal_inject(store, root))
            elif a.goal_command == 'decline':
                goal_decline(store, root)
                print('已记录：用户决定不建目标档案')
            elif a.goal_command == 'status':
                state, info = goal_status(store, root)
                print('目标状态：' + state)
                if info.get('receipt'):
                    print('最新收据：goal-%04d.json，目标哈希 %s'
                          % (info['receipt']['seq'], info['receipt']['goal_hash']))
                if info.get('tentative'):
                    print('暂定记录：' + str(info['tentative'].get('adopted_at')))
                if info.get('note'):
                    print('注：' + info['note'])
        elif a.command == 'backfill':
            _bspec = importlib.util.spec_from_file_location(
                'relay_backfill', Path(__file__).resolve().parent / 'relay_backfill.py')
            backfill_mod = importlib.util.module_from_spec(_bspec)
            _bspec.loader.exec_module(backfill_mod)
            report = backfill_mod.backfill(
                store, a.root, home=a.log_root, tools=a.tool, tier=a.tier,
                deep=a.deep, session_limit=a.session_limit,
                summarize_after=a.summarize)
            print('回溯层级：%s（深扒：%s）' % (report['tier'], report['deep']))
            for tool, info in report['tools'].items():
                print('%s：发现 %d 个会话，匹配 %d 个'
                      % (tool, info['found'], info['matched']))
            print('新建回溯条目：%s  已回溯过（跳过）：%s  暂缓深扒：%d 个会话'
                  % (report['entries_created'], report['already_backfilled'],
                     len(report['deferred'])))
            if report['summary_seq'] is not None:
                print('建档汇总序号：%d，保真度声明：%s'
                      % (report['summary_seq'], report['fidelity_doc']))
            for w in report['fidelity'].get('warnings', []):
                print('保真警告：' + w)
            for gap in report['gaps']:
                print('缺料[%s·影响%s] %s' % (gap['category'], gap['impact'], gap['item']))
        elif a.command == 'accept':
            _aspec = importlib.util.spec_from_file_location(
                'relay_accept', Path(__file__).resolve().parent / 'relay_accept.py')
            accept_mod = importlib.util.module_from_spec(_aspec)
            _aspec.loader.exec_module(accept_mod)
            # Acceptance isolation (regression: accept used to inherit the
            # persistent default store, so fixture projects keyed by their
            # fixed remote URLs collided across runs and the second run
            # measured the first run's leftovers instead of its own acts).
            # Default: a fresh throwaway store, like workdir/log_home.
            # An explicit --store-root is still honored for debugging.
            if a.store_root is None:
                store = Path(tempfile.mkdtemp(prefix='relay-accept-store-'))
            record = accept_mod.run_acceptance(
                store, workdir=a.workdir, log_home=a.log_root,
                keep_dir=a.keep_dir)
            print(accept_mod.render_record(record))
            if record['verdict'] != 'PASS':
                return 1
        elif a.command in ('pack', 'unpack', 'sync-setup', 'sync-claim',
                           'sync-rollback', 'sync-status', 'sync-resolve',
                           'sync-accept'):
            # relay_sync's RelayError is a different class from this
            # module's (importlib double-load), so the sync branch wraps
            # its own errors instead of relying on main()'s except.
            try:
                _sspec = importlib.util.spec_from_file_location(
                    'relay_sync', Path(__file__).resolve().parent / 'relay_sync.py')
                sync = importlib.util.module_from_spec(_sspec)
                _sspec.loader.exec_module(sync)
                if a.command == 'pack':
                    result = sync.pack(store, a.root, remote=a.remote,
                                       device_name=a.device_name,
                                       carrier=a.carrier,
                                       overwrite_unconsumed=a.overwrite_unconsumed)
                    rec = result['receipt']
                    if rec['carrier'] == 'folder':
                        print('pack 完成（文件夹载体，无包序列号）')
                    elif rec['carrier'] == 'server':
                        print('pack 完成：序列号 %d，服务器 %s'
                              % (rec['seq'], rec.get('server_url')))
                    else:
                        print('pack 完成：序列号 %d，提交 %s' % (rec['seq'], rec['commit']))
                    print('条目 %d 个，文件 %d 个，脱敏命中 %d 次'
                          % (rec['entry_count'], rec['file_count'], rec['redacted_count']))
                    print('回执：' + result['receipt_path'])
                    for step in rec['steps']:
                        print('步骤 %s：%s' % (step['step'], step['status']))
                    wip = rec.get('wip') or {}
                    if wip.get('status') == 'created':
                        print('未提交改动快照已推送：%s，文件 %d 个'
                              % (wip['sha'][:12], len(wip.get('changed_files') or [])))
                        if wip.get('ignored_recent'):
                            print('被忽略且近 7 天改过、未进快照：%s'
                                  % '、'.join(wip['ignored_recent']))
                        if wip.get('overwritten_unconsumed'):
                            print('注：已覆盖上一份未还原的快照')
                    elif wip.get('status') == 'skipped':
                        print('快照未推送：' + wip.get('reason', ''))
                elif a.command == 'unpack':
                    try:
                        _top = git_toplevel(a.root)
                    except Exception:
                        _top = None
                    result = sync.unpack(store, remote=a.remote, confirm=a.confirm,
                                         project_keys=a.projects,
                                         carrier=a.carrier, toplevel=_top)
                    if not result.get('landed'):
                        if result.get('note'):
                            print(result['note'])
                        else:
                            print('干跑预览（未写入任何内容）：')
                        for rep in result.get('reports', []):
                            _print_sync_report(rep)
                    else:
                        if result.get('carrier') == 'folder':
                            print('文件夹载体对账完成')
                            found = result.get('conflicts', {}).get('found') or []
                            if found:
                                print('发现 %d 个同步冲突副本，已隔离，未自动合并'
                                      % len(found))
                        elif result.get('carrier') == 'server':
                            print('unpack 已落盘（服务器载体）')
                        else:
                            print('unpack 已落盘并提交：%s' % result['commit'])
                        for rep in result.get('reports', []):
                            _print_sync_report(rep)
                        for note in result.get('reconcile', []):
                            print('对账 %s：目标状态 %s，pending %s，分支 %s'
                                  % (note['slug'], note.get('goal_status'),
                                     note.get('pending'), note.get('branch')))
                        if result.get('ready'):
                            print('接力就绪')
                        else:
                            for problem in result.get('ready_problems', []):
                                print('对账差异：' + problem)
                        for rest in result.get('wip_restores', []):
                            if rest.get('status') == 'restored':
                                print('未提交改动已还原为未暂存改动：快照 %s'
                                      % rest['sha'][:12])
                            elif rest.get('status') == 'refused-dirty':
                                print('快照未还原：本机工作区不干净，你的改动无损；'
                                      '请先提交或清理后再 unpack')
                            elif rest.get('status') not in ('none', 'already-consumed'):
                                print('快照还原：%s' % rest.get('status'))
                        print('回执：' + result['receipt_path'])
                elif a.command == 'sync-setup':
                    carrier = a.carrier
                    if a.folder and not a.store_root:
                        store = Path(a.folder).expanduser()
                    if carrier is None:
                        if a.server_url:
                            carrier = 'server'
                        elif a.folder:
                            carrier = 'folder'
                        elif a.remote:
                            carrier = 'git'
                        else:
                            raise Exception(
                                'sync-setup 需要指定载体：git 用 --remote，'
                                'folder 用 --folder，server 用 --server-url')
                    if carrier == 'folder':
                        result = sync.sync_setup_folder(
                            store, device_name=a.device_name)
                        print('同步配置已写入：设备 %s（%s），文件夹 %s'
                              % (result['config']['device_name'],
                                 result['config']['device_id'], result['store']))
                        for note in result['notes']:
                            print('注：' + note)
                    elif carrier == 'server':
                        if not a.server_url:
                            raise Exception('server 载体需要 --server-url')
                        stype = a.server_type
                        if stype is None:
                            if a.server_url.startswith('local:'):
                                stype = 'local'
                            else:
                                raise Exception(
                                    'server 载体需要 --server-type'
                                    '（webdav/sftp/https/local）')
                        token = a.server_token or os.environ.get('RELAY_SERVER_TOKEN')
                        result = sync.sync_setup_server(
                            store, stype, a.server_url,
                            server_user=a.server_user, token=token,
                            device_name=a.device_name)
                        print('同步配置已写入：设备 %s（%s），服务器 %s %s'
                              % (result['config']['device_name'],
                                 result['config']['device_id'], stype,
                                 a.server_url))
                        for note in result['notes']:
                            print('注：' + note)
                    else:
                        if not a.remote:
                            raise Exception('git 载体需要 --remote')
                        result = sync.sync_setup(store, a.remote, device_name=a.device_name)
                        print('同步配置已写入：设备 %s（%s），远端 %s'
                              % (result['config']['device_name'],
                                 result['config']['device_id'], a.remote))
                        for claim in result['claims']:
                            if claim['status'] == 'matched':
                                print('可认领：%s ↔ 本机项目 %s'
                                      % (claim['slug'], claim['local_display']))
                            else:
                                print('待认领区：%s（本机无对应项目）' % claim['slug'])
                        for note in result['notes']:
                            print('注：' + note)
                elif a.command == 'sync-claim':
                    result = sync.sync_claim(store, a.key, a.root, confirm=a.confirm)
                    if not result['landed']:
                        print('干跑预览（未写入任何内容）：')
                    _print_sync_project(result['report'])
                    if result['landed']:
                        print('认领完成，合并日志：' + result['report']['merge_log'])
                elif a.command == 'sync-rollback':
                    result = sync.sync_rollback(store, a.root, ref=a.to,
                                                reason=a.reason, bundle=a.bundle)
                    if result['commit'] is None:
                        print('已回退（无提交载体），恢复文件 %d 个'
                              % result['files_restored'])
                    else:
                        print('已回退到 %s，恢复文件 %d 个，提交 %s'
                              % (a.to, result['files_restored'], result['commit']))
                    print('回退前内容备份于：' + result['backup_dir'])
                    if result.get('trash_dir'):
                        print('被替换的旧文件已归档到：' + result['trash_dir'])
                elif a.command == 'sync-status':
                    for row in sync.sync_status(store, a.root):
                        if row.get('conflicts_found'):
                            print('发现 %d 个同步冲突副本，已隔离，未自动合并'
                                  % len(row['conflicts_found']))
                        carrier = row.get('carrier') or '—'
                        if not row['packed']:
                            print('%s：未 pack 过，当前原料 %d 条、汇总 %d 个，载体 %s'
                                  % (row['slug'], row['current_entries'],
                                     row['current_summaries'], carrier))
                        else:
                            seq = row['last_pack_seq'] \
                                if row['last_pack_seq'] is not None else '—'
                            commit = row['last_pack_commit'] or '—'
                            print('%s：上次 pack %s（seq %s，提交 %s），'
                                  '自 pack 后新增汇总 %s 个、新原料 %d 条，载体 %s'
                                  % (row['slug'], row['last_pack_at'], seq,
                                     commit, row['new_summaries_since_pack'],
                                     row['new_entries_since_pack'], carrier))
                elif a.command == 'sync-resolve':
                    if a.list:
                        result = sync.sync_resolve(store, a.root, list_only=True)
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
                            store, a.root, goal=a.goal or None,
                            fork=a.fork, backlog=a.backlog,
                            decision=a.decision, file=a.file)
                        for action in result['actions']:
                            print(action)
                elif a.command == 'sync-accept':
                    record = sync.run_sync_acceptance()
                    print(sync.render_sync_record(record))
                    if record['verdict'] != 'PASS':
                        return 1
            except Exception as exc:
                print('ERROR: ' + str(exc), file=sys.stderr)
                return 1
        elif a.command in ('capture', 'queue', 'enforce', 'recipe', 'precompress',
                           'fingerprint', 'scales', 'summarize', 'recover',
                           'handoff', 'inject', 'resume'):
            _fspec = importlib.util.spec_from_file_location(
                'relay_flow', Path(__file__).resolve().parent / 'relay_flow.py')
            flow = importlib.util.module_from_spec(_fspec)
            _fspec.loader.exec_module(flow)
            root = a.root
            if a.command == 'capture':
                info = flow.capture(store, root, tool=a.tool, session=a.session,
                                    message=a.message)
                if info.get('deduped'):
                    print('采集去重：事实未变，返回已有条目 cap-%04d' % info['seq'])
                else:
                    print('采集条目：cap-%04d（%s）' % (info['seq'], info['path']))
            elif a.command == 'queue':
                st = flow.queue_status(flow.relay.resolve_store_dir(store, root)[1],
                                       repair=False)
                print('pending：%d  summarized：%d  summarizing：%d'
                      % (st['pending'], st['summarized'], st['summarizing']))
                print('raw 总量：%s MB  最老 pending：%s  脱敏累计：%d'
                      % (st['raw_mb'], st['oldest_pending'], st['redacted_total']))
                print('超时 summarizing：%s  回退超 3 次：%s'
                      % (st['stale_summarizing'], st['over_retries']))
            elif a.command == 'enforce':
                session = a.session or _session_from_stdin()
                info = flow.enforce(store, root, tool=a.tool, session=session)
                print(info.get('instruction') or ('PASS：' + info['note']))
                if info['action'] == 'block':
                    return 2
            elif a.command == 'recipe':
                home = Path(a.home).expanduser() if a.home else None
                target = Path(a.target_root).expanduser() if a.target_root else Path.home()
                if a.recipe_command == 'install':
                    if a.tool == 'claude-code':
                        info = flow.install_claude(store, root, home_dir=home)
                        print('\n'.join(info['notes']))
                    elif a.tool in ('codebuddy', 'workbuddy'):
                        info = flow.install_codebuddy(store, root, target, flavor=a.tool)
                        print('skill 目录：' + info['skill_dir'])
                        print(info['note'])
                    else:
                        info = flow.install_codex(store, root)
                        print(info['note'])
                else:
                    if a.tool == 'claude-code':
                        info = flow.uninstall_claude(store, root, home_dir=home)
                        print('\n'.join(info['notes']))
                    elif a.tool in ('codebuddy', 'workbuddy'):
                        info = flow.uninstall_codebuddy(store, root, flavor=a.tool)
                        print('已移除：' + info['removed'])
                    else:
                        info = flow.uninstall_codex(store, root)
                        print(info['note'])
            elif a.command == 'precompress':
                doc = flow.precompress(store, root)
                print('整理稿：条目区间 %s，改动文件 %d 个，失败样本 %d 个'
                      % (doc['entry_range'], len(doc['files_changed']),
                         len(doc['degraded_samples'])))
            elif a.command == 'fingerprint':
                m = flow.measure_change(store, root)
                c = m['counts']
                print('新增 %d / 修改 %d / 删除 %d / 改名 %d；行数 +%d/-%d'
                      % (len(c['added']), len(c['modified']), len(c['deleted']),
                         len(c['renamed']), m['lines']['added'], m['lines']['deleted']))
                for g in m['snapshot']['gaps']:
                    print('缺口：子模块 %s 未深入（指针 %s）' % (g['path'], g['pointer']))
            elif a.command == 'scales':
                rec = flow.scales(store, root, session=a.session)
                for row in rec['rows']:
                    print('%s：%s，阈值 %s，到线：%s（%s）'
                          % (row['scale'], row['value'], row['threshold'],
                             row['reached'], row['reason']))
                print('触发：' + str(rec['triggered']))
            elif a.command == 'summarize':
                semantic = (Path(a.semantic_file).read_text(encoding='utf-8')
                            if a.semantic_file else None)
                info = flow.summarize(store, root, source=a.source,
                                      semantic_text=semantic, session=a.session)
                if info.get('noop'):
                    print(info['note'])
                else:
                    print('汇总序号：%d，覆盖条目 %s，触发源 %s'
                          % (info['seq'], info['entry_range'], info['source']))
            elif a.command == 'recover':
                info = flow.recover(store, root)
                print('已恢复事务：' + info['recovered'])
            elif a.command == 'handoff':
                info = flow.make_handoff(store, root)
                print('交接包：' + info['package'])
            elif a.command == 'inject':
                info = flow.inject(store, root, budget=a.budget)
                print(info['text'])
                print('\n[注入 token 粗估：%d / 预算 %d]' % (info['tokens_estimate'], info['budget']))
            elif a.command == 'resume':
                result = flow.resume(store, root, tool=a.tool)
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
