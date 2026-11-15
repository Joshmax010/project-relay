#!/usr/bin/env python3
"""relay work：多 Agent 的 Git 分支/工作树事务层。

设计：
- 项目只有一个 Git 仓库；项目根的主工作树留在用户验收的分支（通常是 main）。
- 多 Agent 模式：每个 Agent 一条长期分支 agent/<agent>，在自己的工作区
  （Harness 根）下建立独立的 worktree；所有工作树共享同一个 .git，不复制仓库。
- 单 Agent 模式：项目根等于或位于 Agent 工作区内（或反过来）时，直接改项目根，
  不建额外 worktree；这是正常项目模式，不是降级。
- merge 必须显式 --confirm，冲突自动中止并保留原分支。

本模块不写死任何本机路径：项目根由 relay.git_toplevel 发现，Agent 工作区由
--workspace 或当前目录提供。
"""
import argparse
import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_relay_mod = sys.modules.get('relay')
if _relay_mod is None:
    _spec = importlib.util.spec_from_file_location('relay', _HERE / 'relay.py')
    _relay_mod = importlib.util.module_from_spec(_spec)
    sys.modules['relay'] = _relay_mod
    _spec.loader.exec_module(_relay_mod)
relay = _relay_mod
RelayError = relay.RelayError
require = relay.require

DEFAULT_AGENT = 'deepseek-harness'
BRANCH_PREFIX = 'agent/'


def _decode(result):
    return os.fsdecode(result.stdout).strip()


def _git_text(root, *args):
    return _decode(relay.git(root, *args))


def _git_ok(root, *args):
    return relay.git(root, *args, check=False).returncode == 0


def _validate_agent(agent):
    require(isinstance(agent, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', agent or ''),
            '未知 Agent 名：%s' % agent)


def _current_branch(root):
    result = relay.git(root, 'symbolic-ref', '--short', '-q', 'HEAD', check=False)
    return _decode(result) if result.returncode == 0 else None


def _branch_exists(root, branch):
    return _git_ok(root, 'show-ref', '--verify', '--quiet', 'refs/heads/' + branch)


def _clean(root):
    # Compare with the user's own git autocrlf setting: commits made by the
    # user's git may store CRLF while relay's default wrapper pins LF.
    result = relay.git(root, 'status', '--porcelain', honor_autocrlf=True,
                       check=False)
    return result.returncode == 0 and os.fsdecode(result.stdout).strip() == ''


def _default_base(project_root):
    current = _current_branch(project_root)
    if current and not current.startswith(BRANCH_PREFIX):
        return current
    for name in ('main', 'master'):
        if _branch_exists(project_root, name):
            return name
    return 'main'


def _worktrees(project_root):
    """Parse 'git worktree list --porcelain' into dicts; first is the main tree."""
    out = _git_text(project_root, 'worktree', 'list', '--porcelain')
    records = []
    current = {}
    for line in out.splitlines():
        if line.startswith('worktree '):
            if current:
                records.append(current)
            current = {'path': Path(line[len('worktree '):]).resolve()}
        elif line.startswith('HEAD '):
            current['head'] = line[len('HEAD '):]
        elif line.startswith('branch '):
            ref = line[len('branch '):]
            current['branch'] = ref[len('refs/heads/'):] if ref.startswith('refs/heads/') else ref
        elif line == 'detached':
            current['detached'] = True
        elif line == 'bare':
            current['bare'] = True
    if current:
        records.append(current)
    for index, record in enumerate(records):
        record['is_main'] = index == 0
    return records


def worktree_for_branch(project_root, branch):
    for record in _worktrees(project_root):
        if record.get('branch') == branch:
            return record
    return None


def _is_within(child, parent):
    child = os.path.realpath(str(child))
    parent = os.path.realpath(str(parent))
    try:
        return os.path.commonpath([child, parent]) == parent
    except ValueError:
        return False


def _default_worktree_root(workspace):
    return Path(workspace).expanduser().resolve() / 'worktrees'


def _worktree_path(project_root, workspace, agent, worktree_root=None):
    base = (Path(worktree_root).expanduser().resolve()
            if worktree_root else _default_worktree_root(workspace))
    return base / relay.slug_for(Path(project_root).name) / agent


def enter(project_root, workspace=None, agent=DEFAULT_AGENT, branch=None,
          base=None, worktree_root=None):
    """Enter the Agent's branch/worktree; returns a mode descriptor."""
    original_root = Path(relay.git_toplevel(project_root)).resolve()
    project_root = Path(relay.main_worktree(project_root))
    relay.resolve_store_dir(project_root)
    _validate_agent(agent)
    workspace = Path(workspace or os.getcwd()).expanduser().resolve()
    branch = branch or (BRANCH_PREFIX + agent)
    base = base or _default_base(project_root)
    require(_branch_exists(project_root, base),
            '基线分支不存在：%s（先创建或换 --base）' % base)
    # Already inside the Agent's linked worktree: return it instead of
    # creating a nested one.
    if original_root != project_root and _current_branch(original_root) == branch:
        return {'mode': 'multi', 'project_root': str(project_root),
                'workspace': str(workspace), 'branch': branch, 'base': base,
                'worktree': str(original_root), 'created': False}

    # 单 Agent：项目根与 Agent 工作区重合/互相包含，直接改项目根。
    if _is_within(project_root, workspace) or _is_within(workspace, project_root):
        current = _current_branch(project_root)
        return {'mode': 'single', 'project_root': str(project_root),
                'workspace': str(workspace), 'branch': current, 'base': base,
                'worktree': None, 'created': False}

    worktree = _worktree_path(project_root, workspace, agent, worktree_root)
    existing = worktree_for_branch(project_root, branch)
    if existing is not None:
        return {'mode': 'multi', 'project_root': str(project_root),
                'workspace': str(workspace), 'branch': branch, 'base': base,
                'worktree': str(existing['path']), 'created': False}
    if worktree.exists():
        raise RelayError('工作树路径已存在但未登记为分支 %s：%s' % (branch, worktree))

    worktree.parent.mkdir(parents=True, exist_ok=True)
    if _branch_exists(project_root, branch):
        relay.git(project_root, 'worktree', 'add', str(worktree), branch)
    else:
        relay.git(project_root, 'worktree', 'add', '-b', branch, str(worktree), base)
    return {'mode': 'multi', 'project_root': str(project_root),
            'workspace': str(workspace), 'branch': branch, 'base': base,
            'worktree': str(worktree), 'created': True}


def status(project_root, agent=None):
    project_root = Path(relay.main_worktree(project_root))
    relay.resolve_store_dir(project_root)
    rows = []
    for record in _worktrees(project_root):
        if agent and not record['is_main'] and record.get('branch') != (BRANCH_PREFIX + agent):
            continue
        path = record['path']
        rows.append({'path': str(path), 'branch': record.get('branch'),
                     'head': (record.get('head') or '')[:12],
                     'is_main': record['is_main'],
                     'dirty': (not _clean(path)) if path.exists() else True})
    return {'project_root': str(project_root), 'worktrees': rows}


def review(project_root, agent=DEFAULT_AGENT, branch=None, base=None):
    project_root = Path(relay.main_worktree(project_root))
    relay.resolve_store_dir(project_root)
    _validate_agent(agent)
    branch = branch or (BRANCH_PREFIX + agent)
    base = base or _default_base(project_root)
    require(_branch_exists(project_root, branch), '分支不存在：%s' % branch)
    require(_branch_exists(project_root, base), '基线分支不存在：%s' % base)
    count = int(_git_text(project_root, 'rev-list', '--count', '%s..%s' % (base, branch)))
    commits = _git_text(project_root, 'log', '--oneline', '--no-decorate',
                        '%s..%s' % (base, branch)).splitlines()
    stat = _git_text(project_root, 'diff', '--stat', '%s...%s' % (base, branch))
    files = _git_text(project_root, 'diff', '--name-status', '%s...%s' % (base, branch))
    record = worktree_for_branch(project_root, branch)
    dirty = bool(record and record['path'].exists() and not _clean(record['path']))
    return {'project_root': str(project_root), 'agent': agent, 'branch': branch,
            'base': base, 'commit_count': count, 'commits': commits,
            'diff_stat': stat, 'files': files, 'dirty': dirty,
            'worktree': str(record['path']) if record else None}


def merge(project_root, agent=DEFAULT_AGENT, branch=None, base=None, confirm=False):
    project_root = Path(relay.main_worktree(project_root))
    relay.resolve_store_dir(project_root)
    _validate_agent(agent)
    require(confirm, '合并需要显式 --confirm：先把 review 给用户看，批准后再合并')
    branch = branch or (BRANCH_PREFIX + agent)
    base = base or _default_base(project_root)
    require(_branch_exists(project_root, branch), '分支不存在：%s' % branch)
    require(_branch_exists(project_root, base), '基线分支不存在：%s' % base)
    current = _current_branch(project_root)
    require(current == base,
            '主工作树当前在 %s，不在基线 %s；先切回基线再合并' % (current or '(detached)', base))
    require(_clean(project_root), '主工作树有未提交改动，先提交或清干净再合并')
    record = worktree_for_branch(project_root, branch)
    if record and record['path'].exists() and not _clean(record['path']):
        raise RelayError('Agent 工作树有未提交改动，先提交再合并：%s' % record['path'])
    count = int(_git_text(project_root, 'rev-list', '--count', '%s..%s' % (base, branch)))
    if count == 0:
        return {'merged': False, 'branch': branch, 'base': base, 'commit_count': 0,
                'head': _git_text(project_root, 'rev-parse', '--short', 'HEAD'),
                'message': '没有待合并提交'}
    result = relay.git(project_root, 'merge', '--no-ff', '-m',
                       'relay: merge %s into %s' % (branch, base), branch, check=False)
    if result.returncode != 0:
        relay.git(project_root, 'merge', '--abort', check=False)
        detail = os.fsdecode(result.stderr or result.stdout).strip()
        raise RelayError('合并失败，已中止：%s\n%s' % (branch, detail))
    return {'merged': True, 'branch': branch, 'base': base, 'commit_count': count,
            'head': _git_text(project_root, 'rev-parse', '--short', 'HEAD'),
            'message': '已合并 %s 到 %s' % (branch, base)}


def render_enter(info):
    if info['mode'] == 'single':
        return '单 Agent 模式：%s（分支 %s，直接改项目根，不建额外工作树）' % (
            info['project_root'], info['branch'] or '(当前分支)')
    action = '已创建并检出' if info['created'] else '已存在'
    return '多 Agent 隔离模式：%s %s 到 %s（基于 %s）' % (
        action, info['branch'], info['worktree'], info['base'])


def render_status(info):
    lines = []
    for row in info['worktrees']:
        kind = '主工作树' if row['is_main'] else 'Agent 工作树'
        dirty = ' 有未提交改动' if row['dirty'] else ''
        lines.append('%s：%s  分支：%s  提交：%s%s' % (
            kind, row['path'], row['branch'] or '(detached)', row['head'], dirty))
    if len(info['worktrees']) == 1:
        lines.append('（尚无 Agent 工作树；单 Agent 直接改项目根即可）')
    return '\n'.join(lines)


def render_review(info):
    lines = ['分支：%s  基线：%s  待合并提交：%d' % (
        info['branch'], info['base'], info['commit_count'])]
    if info['dirty']:
        lines.append('注意：Agent 工作树有未提交改动，review 只显示已提交内容。')
    if info['commits']:
        lines.extend('  ' + line for line in info['commits'])
    else:
        lines.append('  （无待合并提交）')
    if info['diff_stat']:
        lines.extend(['', info['diff_stat']])
    if info['files']:
        lines.extend(['', '改动文件：'])
        lines.extend('  ' + line for line in info['files'].splitlines())
    return '\n'.join(lines)


def render_merge(info):
    if not info.get('merged'):
        return '无需合并：%s' % info['message']
    return '%s；HEAD %s' % (info['message'], info['head'])


def _build_parser():
    parser = argparse.ArgumentParser(prog='relay work', description='多 Agent 的 Git 分支/工作树事务层')
    parser.add_argument('--root', default='.', help='项目仓库内的路径（默认当前目录）')
    sub = parser.add_subparsers(dest='work_command', required=True)
    for name in ('enter', 'status', 'review', 'merge'):
        command = sub.add_parser(name)
        command.add_argument('--agent', default=DEFAULT_AGENT)
        command.add_argument('--workspace', default=None,
                             help='Agent 工作区（默认当前目录）')
        command.add_argument('--branch', default=None,
                             help='默认 agent/<agent>')
        command.add_argument('--base', default=None,
                             help='默认主工作树当前分支，否则 main/master')
        command.add_argument('--worktree-root', default=None,
                             help='工作树根（默认 <workspace>/worktrees）')
        if name == 'merge':
            command.add_argument('--confirm', action='store_true',
                                 help='用户批准后才能真正合并')
    return parser


def main(argv=None):
    args = _build_parser().parse_args(argv)
    try:
        if args.work_command == 'enter':
            info = enter(args.root, workspace=args.workspace, agent=args.agent,
                         branch=args.branch, base=args.base,
                         worktree_root=args.worktree_root)
            print(render_enter(info))
            return 0
        if args.work_command == 'status':
            print(render_status(status(args.root, agent=args.agent)))
            return 0
        if args.work_command == 'review':
            print(render_review(review(args.root, agent=args.agent,
                                       branch=args.branch, base=args.base)))
            return 0
        if args.work_command == 'merge':
            print(render_merge(merge(args.root, agent=args.agent,
                                     branch=args.branch, base=args.base,
                                     confirm=args.confirm)))
            return 0
    except (RelayError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
        print('ERROR: ' + str(exc), file=sys.stderr)
        return 1
    return 1


if __name__ == '__main__':
    sys.exit(main())
