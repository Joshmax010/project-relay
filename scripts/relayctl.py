#!/usr/bin/env python3
"""Human-reviewed project memory for Git code projects (Python 3.8+, stdlib).

This is a workflow guard, not an identity/authentication boundary. Agents may
record approval only after the user approves the displayed, specific changes.
"""
import argparse
import base64
import datetime as dt
import difflib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tempfile
import uuid

PRIVATE = ('inbox', '.local', 'conversations/raw')
CORE = ('agent.md', 'human.md', 'project.md', 'state.md', 'structure.md',
        'decisions.md', 'lessons.md', 'changelog.md', 'sessions/index.md',
        'conversations/index.md')


class RelayError(Exception):
    pass


def require(condition, message):
    if not condition:
        raise RelayError(message)


def utc():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def private(rel):
    return any(rel == p or rel.startswith(p + '/') for p in PRIVATE)


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


def formal(agent, rel):
    require(not private(rel) and not rel.startswith(('tools/', 'archive/'))
            and rel.endswith('.md') and not any(p.startswith('.') for p in PurePosixPath(rel).parts),
            '目标必须是正式 Markdown 文档，不能写入草稿、工具或归档: ' + str(rel))
    return safe(agent, rel)


def atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    require(not path.is_symlink(), '拒绝符号链接: ' + str(path))
    fd, name = tempfile.mkstemp(prefix='.relay-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, str(path))
    finally:
        if os.path.exists(name):
            os.unlink(name)


def save(path, value):
    atomic(path, (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode('utf-8'))


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def git(root, *args, check=True, input_data=None):
    result = subprocess.run(['git', '-C', str(root), *args], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, input=input_data, timeout=60)
    if check:
        require(result.returncode == 0, 'Git 操作失败: ' + ' '.join(args))
    return result


def context(root):
    root = Path(root).expanduser().resolve()
    result = git(root, 'rev-parse', '--show-toplevel')
    require(Path(os.fsdecode(result.stdout).strip()).resolve() == root,
            '请指定 Git 仓库根目录；子目录与非 Git 项目不在本版范围')
    agent = safe(root, '.agent')
    require(agent.is_dir(), '先运行 init_project.py 初始化 .agent')
    for rel in PRIVATE:
        safe(agent, rel)
    safe(agent, '.local/approval.json')
    safe(agent, '.local/transaction.json')
    return root, agent


def anchor(root):
    head = git(root, 'rev-parse', '--verify', 'HEAD', check=False)
    branch = git(root, 'symbolic-ref', '-q', 'HEAD', check=False)
    return {'head': head.stdout.decode().strip() if head.returncode == 0 else None,
            'branch': branch.stdout.decode().strip() if branch.returncode == 0 else None}


def snapshot(root):
    """Cover tracked/deleted and nonignored untracked code, and all formal docs."""
    stages = git(root, 'ls-files', '--stage', '-z').stdout.split(b'\0')
    cached_modes = {}
    for entry in filter(None, stages):
        metadata, name = entry.split(b'\t', 1)
        mode, _, stage = metadata.split()
        require(stage == b'0', '存在尚未解决的 Git 合并冲突')
        require(mode != b'160000', '本版提交检查暂不支持 Git 子模块')
        cached_modes[os.fsdecode(name)] = mode
    paths = set(os.fsdecode(p) for p in git(root, 'ls-files', '--cached', '--others',
                '--exclude-standard', '-z').stdout.split(b'\0') if p)
    if anchor(root)['head']:
        paths.update(os.fsdecode(p) for p in git(root, 'ls-tree', '-r', '--name-only', '-z', 'HEAD').stdout.split(b'\0') if p)
    filemode = git(root, 'config', '--get', 'core.filemode', check=False).stdout.strip() != b'false'
    agent = root / '.agent'
    for p in agent.rglob('*.md'):
        rel = p.relative_to(agent).as_posix()
        if not private(rel) and not rel.startswith(('tools/', 'archive/')):
            paths.add('.agent/' + rel)
    code, memory = {}, {}
    for rel in sorted(paths):
        ar = rel[len('.agent/'):] if rel.startswith('.agent/') else None
        if ar is not None and private(ar):
            continue
        p = root / rel
        if ar is not None:
            safe(agent, ar)
        else:
            # A leaf code symlink is versionable, but never read through a linked directory.
            for parent in p.parents:
                if parent == root:
                    break
                require(not parent.is_symlink(), '代码路径经过符号链接目录: ' + rel)
        if p.is_symlink():
            value = {'sha256': digest(os.fsencode(os.readlink(p))), 'kind': 'symlink'}
        elif not p.exists():
            value = None
        else:
            require(p.is_file(), '快照中出现非普通文件: ' + rel)
            value = {'sha256': digest(p.read_bytes()), 'kind': 'file',
                     'executable': bool(p.stat().st_mode & stat.S_IXUSR) if filemode
                                   else cached_modes.get(rel, b'100644') == b'100755'}
        dest = memory if ar is not None and ar.endswith('.md') and not ar.startswith(('tools/', 'archive/')) else code
        dest[rel] = value
    return {'anchor': anchor(root), 'code': code, 'memory': memory}


def changed(before, after):
    return sorted(p for p in before.keys() | after.keys() if before.get(p) != after.get(p))


def batch_path(agent, bid):
    require(re.fullmatch(r'[0-9a-f]{32}', bid or '') is not None, '批次 ID 必须是完整的 32 位 ID')
    return safe(agent, 'inbox/' + bid + '/batch.json')


def load_batch(agent, bid):
    path = batch_path(agent, bid)
    require(path.is_file(), '找不到批次: ' + bid)
    value = read_json(path)
    require(value.get('id') == bid and value.get('version') == 1, '批次格式不匹配')
    return path, value


def no_transaction(agent):
    require(not safe(agent, '.local/transaction.json').exists(),
            '上次应用被中断；先运行 recover，正式文档暂不可作为完整新版本')


def draft(root, agent, mapping, title):
    no_transaction(agent)
    require(isinstance(mapping, dict) and mapping, '输入必须是非空 JSON 对象：相对文档路径 → 完整新内容')
    bid = uuid.uuid4().hex
    items = {}
    for rel, text in mapping.items():
        p = formal(agent, rel)
        require(isinstance(text, str) and text.strip(), '文档内容必须是非空字符串: ' + rel)
        data = text.encode('utf-8')
        old = p.read_bytes() if p.exists() else None
        in_head = git(root, 'cat-file', '-e', 'HEAD:.agent/' + rel, check=False).returncode == 0
        require(old != data or not in_head, '已提交文档没有变化: ' + rel)
        items[rel] = {'base': digest(old) if old is not None else None,
                      'content': text, 'sha256': digest(data), 'status': 'pending'}
    value = {'version': 1, 'id': bid, 'created': utc(), 'title': title,
             'status': 'pending', 'snapshot': snapshot(root), 'items': items}
    save(batch_path(agent, bid), value)
    print('待审批次: ' + bid)
    return bid


def validate_batch(root, agent, batch):
    require(snapshot(root) == batch['snapshot'],
            '草稿已过时：代码、正式文档或 Git 基线已变化；请重新起草并审核')
    for rel, item in batch['items'].items():
        p = formal(agent, rel)
        actual = digest(p.read_bytes()) if p.exists() else None
        require(actual == item['base'], '目标版本冲突: ' + rel)
        require(digest(item['content'].encode('utf-8')) == item['sha256'], '草稿内容被改写: ' + rel)


def review(root, agent, bid, approve=None, reject=None, reviewer=None, reason=None):
    no_transaction(agent)
    path, batch = load_batch(agent, bid)
    require(batch['status'] != 'applied', '批次已经应用')
    choices = approve or reject
    if not choices:
        print('未审核内容；不得当作正式依据。批次: ' + bid)
        print('主题: ' + batch['title'])
        print('绑定 Git 基线: ' + str(batch['snapshot']['anchor']))
        for rel, item in batch['items'].items():
            p = formal(agent, rel)
            old = p.read_text(encoding='utf-8') if p.exists() else ''
            print('\n' + rel + ' [' + item['status'] + ']')
            print('内容 SHA256: ' + item['sha256'])
            print(''.join(difflib.unified_diff(old.splitlines(True), item['content'].splitlines(True),
                                             fromfile=rel + ':current', tofile=rel + ':proposed')))
        if snapshot(root) != batch['snapshot']:
            print('警告：批次已过时；显示的差异不构成可应用的批准对象。')
        return
    require(not (approve and reject), '批准与拒绝请分别执行')
    if approve:
        require(reviewer and reviewer.strip(), '批准必须记录 --reviewed-by；仅在用户实际批准后执行')
        validate_batch(root, agent, batch)
    for rel in choices:
        require(rel in batch['items'], '批次不存在此文档: ' + rel)
    for rel in choices:
        item = batch['items'][rel]
        item['status'] = 'approved' if approve else 'rejected'
        item['reviewed_by'] = reviewer or 'user'
        item['reviewed_at'] = utc()
        item['reason'] = reason or ''
    batch['status'] = 'reviewed' if all(i['status'] != 'pending' for i in batch['items'].values()) else 'pending'
    save(path, batch)
    print('审核结果已记录；正式文档尚未修改。')


def apply(root, agent, bid):
    no_transaction(agent)
    path, batch = load_batch(agent, bid)
    require(batch['status'] == 'reviewed', '仍有未审核条目，或批次已经应用')
    validate_batch(root, agent, batch)
    approved = {rel: item for rel, item in batch['items'].items() if item['status'] == 'approved'}
    require('state.md' in approved and 'sessions/index.md' in approved,
            '同步必须包含已批准的 state.md 和 sessions/index.md')
    require(any(rel.startswith('sessions/') and rel != 'sessions/index.md' and item['base'] is None
                for rel, item in approved.items()), '同步必须包含一份已批准的新执行会话记录')
    backups = {}
    for rel, item in approved.items():
        p = formal(agent, rel)
        backups[rel] = {'before': base64.b64encode(p.read_bytes()).decode() if p.exists() else None,
                        'after': item['sha256']}
    journal = safe(agent, '.local/transaction.json')
    save(journal, {'batch': bid, 'files': backups})
    # Keep the journal on failure; recovery never overwrites unrelated edits.
    for rel, item in approved.items():
        atomic(formal(agent, rel), item['content'].encode('utf-8'))
    save(safe(agent, '.local/approval.json'), {'version': 1, 'batch': bid, 'approved_at': utc(),
         'documents': sorted(approved), 'snapshot': snapshot(root)})
    batch['status'] = 'applied'
    batch['applied_at'] = utc()
    save(path, batch)
    # Immutable, collision-free full backup for each batch, including nested paths.
    save(safe(agent, '.local/backups/' + bid + '.json'), read_json(journal))
    journal.unlink()
    print('已应用获批文档；尚未提交。请核对并暂存代码及文档，再运行 commit-check。')


def recover(root, agent):
    path = safe(agent, '.local/transaction.json')
    require(path.exists(), '没有待恢复的应用事务')
    journal = read_json(path)
    targets = []
    for rel, item in journal['files'].items():
        p = formal(agent, rel)
        before = base64.b64decode(item['before'], validate=True) if item['before'] is not None else None
        actual = digest(p.read_bytes()) if p.exists() else None
        require(actual in (digest(before) if before is not None else None, item['after']),
                '恢复冲突：文件被另行修改，已停止且保留事务: ' + rel)
        targets.append((p, before))
    # Preflight all files before restoring any of them.
    for p, before in targets:
        if before is None:
            if p.exists():
                p.unlink()
        else:
            atomic(p, before)
    receipt = safe(agent, '.local/approval.json')
    if receipt.exists():
        receipt.unlink()
    bp, batch = load_batch(agent, journal['batch'])
    batch['status'] = 'reviewed'
    batch['recovered_at'] = utc()
    save(bp, batch)
    path.unlink()
    print('已恢复应用前的正式文档；草稿与审核记录保留，需重新核对后应用。')


def commit_check(root, agent):
    no_transaction(agent)
    rp = safe(agent, '.local/approval.json')
    require(rp.exists(), '缺少已应用的文档批准记录；不能提交')
    receipt = read_json(rp)
    require(receipt.get('version') == 1, '批准记录版本不支持')
    require(snapshot(root) == receipt['snapshot'], '批准后代码、文档或 Git 基线发生变化；必须重新同步审核')
    tracked = set(os.fsdecode(p) for p in git(root, 'ls-files', '-z').stdout.split(b'\0') if p)
    require(not any(p.startswith('.agent/') and private(p[len('.agent/'):]) for p in tracked),
            '隔离草稿、批准收据或聊天原文被纳入 Git；请先取消跟踪')
    for rel in receipt['documents']:
        require('.agent/' + rel in tracked, '获批文档尚未暂存: ' + rel)
    all_files = {**receipt['snapshot']['code'], **receipt['snapshot']['memory']}
    require(all(p in tracked for p, val in all_files.items() if val is not None),
            '存在未暂存的新文件；只允许提交完整的已审核快照')
    require(git(root, 'diff', '--quiet', '--', check=False).returncode == 0,
            '工作区与暂存区不同；请重新核对并暂存已批准内容')
    # Git diff can hide working edits behind assume-unchanged / skip-worktree.
    # Compare actual index blobs and modes, using Git's configured clean filters.
    entries = {}
    for entry in filter(None, git(root, 'ls-files', '--stage', '-z').stdout.split(b'\0')):
        metadata, name = entry.split(b'\t', 1)
        mode, sha, _ = metadata.split()
        entries[os.fsdecode(name)] = (mode, sha)
    for rel, value in all_files.items():
        if value is None:
            require(rel not in entries, '文件删除尚未暂存: ' + rel)
            continue
        p = root / rel
        if value['kind'] == 'symlink':
            data, mode, filters = os.fsencode(os.readlink(p)), b'120000', []
        else:
            data = p.read_bytes()
            mode = b'100755' if value['executable'] else b'100644'
            filters = ['--path=' + rel]
        sha = git(root, 'hash-object', *filters, '--stdin', input_data=data).stdout.strip()
        require(entries.get(rel) == (mode, sha), '实际暂存内容或模式与获批文件不同: ' + rel)
    staged = git(root, 'diff', '--cached', '--name-only', '-z',
                 *(['HEAD'] if receipt['snapshot']['anchor']['head'] else [])).stdout
    paths = {os.fsdecode(p) for p in staged.split(b'\0') if p}
    formal_changes = {p[len('.agent/'):] for p in paths if p.startswith('.agent/')
                      and p.endswith('.md') and not p.startswith(('.agent/tools/', '.agent/archive/'))}
    require(formal_changes <= set(receipt['documents']),
            '暂存区包含本批次未批准的正式文档；必须一起起草并审核')
    require('.agent/state.md' in paths and '.agent/sessions/index.md' in paths,
            '本次提交未包含获批状态与会话索引')
    require(any('.agent/' + p in paths for p in receipt['documents']
                if p.startswith('sessions/') and p != 'sessions/index.md'), '本次提交缺少获批会话记录')
    print('PASS：暂存内容与获批快照一致；允许在已获用户提交授权时提交。')


def resume(root, agent):
    print('正式记录入口：.agent/agent.md → project.md、state.md、最近已审核会话。')
    for rel in ('project.md', 'state.md'):
        p = formal(agent, rel)
        print('\n' + rel + ':')
        print(p.read_text(encoding='utf-8') if p.exists() else '缺失')
    print('\nGit 工作区（必须与正式记录核对）：')
    sys.stdout.flush()
    print(os.fsdecode(git(root, 'status', '--short').stdout))
    print('隔离草稿：未经审核，只能作为核对线索。')
    for p in sorted(safe(agent, 'inbox').glob('*/batch.json')):
        safe(agent, p.relative_to(agent).as_posix())
        b = read_json(p)
        print(b['id'] + ' [' + b['status'] + '] ' + b['title'])
    if safe(agent, '.local/transaction.json').exists():
        print('应用被中断；先 recover，不能把当前混合文档当作已完成同步。')


def lint(root, agent):
    errors, warnings = [], []
    for rel in CORE:
        p = formal(agent, rel)
        if not p.exists():
            errors.append('缺失正式文档: ' + rel)
    for p in agent.rglob('*.md'):
        rel = p.relative_to(agent).as_posix()
        if private(rel) or rel.startswith(('tools/', 'archive/')):
            continue
        safe(agent, rel)
        text = p.read_text(encoding='utf-8')
        if '[TODO]' in text or '待初始化' in text or '未初始化' in text:
            warnings.append('仍有未初始化内容: ' + rel)
        if len(text) > (8000 if rel == 'state.md' else 30000):
            warnings.append('超过阅读大小参考值: ' + rel)
        for link in re.findall(r'\[[^\]]*\]\(([^)]+)\)', text):
            if '://' in link or link.startswith(('#', 'mailto:')):
                continue
            target = link.split('#', 1)[0]
            if target and not (p.parent / target).exists():
                errors.append('断链: ' + rel + ' → ' + target)
    if safe(agent, '.local/transaction.json').exists():
        errors.append('存在未恢复的应用事务')
    snapshot(root)  # Refuse unresolved merges, unsupported submodules, unsafe memory paths.
    for item in errors:
        print('ERROR ' + item)
    for item in warnings:
        print('WARN ' + item)
    require(not errors, '健康检查失败；正式文档的修正也须起草并审核')
    print('健康检查完成；不代表内容真实、人审完成或可提交。')


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', default='.', help='Git 仓库根目录，放在子命令之前')
    sub = p.add_subparsers(dest='command', required=True)
    sub.add_parser('resume')
    d = sub.add_parser('draft')
    d.add_argument('--file', required=True, help='JSON 文件：文档相对路径 → 完整新内容')
    d.add_argument('--title', required=True)
    r = sub.add_parser('review')
    r.add_argument('batch')
    group = r.add_mutually_exclusive_group()
    group.add_argument('--approve', nargs='+', metavar='DOCUMENT')
    group.add_argument('--reject', nargs='+', metavar='DOCUMENT')
    r.add_argument('--reviewed-by')
    r.add_argument('--reason')
    a = sub.add_parser('apply')
    a.add_argument('batch')
    sub.add_parser('recover')
    sub.add_parser('commit-check')
    sub.add_parser('lint')
    return p


def main(argv=None):
    a = parser().parse_args(argv)
    try:
        root, agent = context(a.root)
        if a.command == 'draft':
            draft(root, agent, read_json(Path(a.file)), a.title)
        elif a.command == 'review':
            review(root, agent, a.batch, a.approve, a.reject, a.reviewed_by, a.reason)
        elif a.command == 'apply':
            apply(root, agent, a.batch)
        else:
            {'resume': resume, 'recover': recover, 'commit-check': commit_check, 'lint': lint}[a.command](root, agent)
    except (RelayError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
        print('ERROR: ' + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
