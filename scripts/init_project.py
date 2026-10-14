#!/usr/bin/env python3
"""Create missing portable project-memory files without overwriting existing data."""
import argparse
from pathlib import Path
import shutil
import subprocess


def initialize(root):
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError('项目根目录必须已经存在且为目录')
    result = subprocess.run(['git', '-C', str(root), 'rev-parse', '--show-toplevel'],
                            capture_output=True, text=True, timeout=60)
    if result.returncode or Path(result.stdout.strip()).resolve() != root:
        raise ValueError('本版仅支持 Git 代码项目；请指定 Git 仓库根目录，不自动运行 git init')
    target = root / '.agent'
    if target.is_symlink():
        raise ValueError('拒绝通过 .agent 符号链接写入')
    if target.exists() and not target.is_dir():
        raise ValueError('.agent 位置已被非目录占用')
    templates = Path(__file__).resolve().parent.parent / 'assets' / 'template'
    runtime_source = Path(__file__).resolve().parent / 'relayctl.py'
    if not templates.is_dir() or not runtime_source.is_file():
        raise ValueError('Skill 包不完整：缺少模板目录或 relayctl.py，未开始初始化')
    sources = sorted(p for p in templates.rglob('*') if p.is_file())
    if not sources:
        raise ValueError('Skill 包不完整：模板目录为空，未开始初始化')
    runtime = target / 'tools' / 'relayctl.py'
    destinations = [target / p.relative_to(templates) for p in sources] + [runtime]
    for dest in destinations:
        for component in [dest, *dest.parents]:
            if component == root:
                break
            if component.is_symlink():
                raise ValueError(f'拒绝通过符号链接写入: {component}')
        if dest.exists() and not dest.is_file():
            raise ValueError(f'目标文件位置已被目录占用: {dest}')
    for source in sources:
        dest = target / source.relative_to(templates)
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            with dest.open('x', encoding='utf-8') as output:
                output.write(source.read_text(encoding='utf-8'))
            print(f'创建 {dest.relative_to(root)}')
        except FileExistsError:
            print(f'保留 {dest.relative_to(root)}')
    runtime.parent.mkdir(parents=True, exist_ok=True)
    if runtime.exists():
        print('保留 .agent/tools/relayctl.py；工具升级需明确核对差异，不自动覆盖')
    else:
        with runtime.open('xb') as output:
            with runtime_source.open('rb') as source:
                shutil.copyfileobj(source, output)
        print('创建 .agent/tools/relayctl.py')
    print('仅生成未审核骨架；正式事实须在用户同意同步后起草、展示差异并批准。')
    print('既有 agent.md 保留；旧版协议须通过待审批次升级，不能同时沿用自动维护规则。')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('project_root')
    args = parser.parse_args()
    try:
        initialize(args.project_root)
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        parser.exit(1, f'{exc}\n')
