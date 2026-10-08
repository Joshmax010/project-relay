#!/usr/bin/env python3
"""Create missing portable project-memory files without overwriting existing data."""
import argparse
from pathlib import Path


def initialize(root):
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError('项目根目录必须已经存在且为目录')
    target = root / '.agent'
    if target.is_symlink():
        raise ValueError('拒绝通过 .agent 符号链接写入')
    templates = Path(__file__).resolve().parent.parent / 'assets' / 'template'
    sources = sorted(p for p in templates.rglob('*') if p.is_file())
    for source in sources:
        dest = target / source.relative_to(templates)
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
    print('骨架已就绪；agent 仍须阅读项目资料、填充事实并建立初始化交接记录。')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('project_root')
    args = parser.parse_args()
    try:
        initialize(args.project_root)
    except (ValueError, OSError) as exc:
        parser.exit(1, f'{exc}\n')
