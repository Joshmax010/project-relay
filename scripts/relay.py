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
    return home_dir() / STORE_DIRNAME


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
    return {'tentative': True, 'hash': digest(draft)}


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
