#!/usr/bin/env python3
r"""Build a self-contained DSH skill zip for release.

Usage:
    python scripts/package_dsh_skill.py [--out PATH]

The zip contains one top-level folder, project-relay/, with SKILL.md,
scripts/, assets/template-v2/, references/, README.md and LICENSE.
A user unzips it into a DSH-scanned skills directory:

    Windows: %USERPROFILE%\.agents\skills\project-relay
    macOS/Linux: ~/.agents/skills/project-relay
"""
import argparse
import re
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ACTIVE_SCRIPTS = ('relay.py', 'relay_flow.py', 'relay_fork.py',
                  'relay_stats.py', 'relay_sync.py', 'relay_work.py')


def _version():
    text = (ROOT / 'pyproject.toml').read_text(encoding='utf-8')
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return match.group(1) if match else '0.0.0'


def _install_text(version):
    return fr'''# project-relay DSH skill v{version}

1. 解压后得到 project-relay/ 文件夹。
2. 把整个 project-relay/ 放到 DSH 会扫描的用户级技能目录：
   - Windows: %USERPROFILE%\.agents\skills\project-relay
   - macOS / Linux: ~/.agents/skills/project-relay
   也可以放到项目级：<项目根>/.dsh/skills/project-relay。
3. 重启 DSH 会话，在项目里说「接力」或「装上接力」。
4. 需要 Git 和 Python 3.8+。
5. 自动写会话卡还需要安装 DSH 插件：
   在项目里运行
   `dsh plugin --profile <profile> add <project-relay>/dsh-plugin/project-relay-hooks`，
   或在 DSH 的 Plugins 界面选择该本地插件目录。
'''


def build(out=None):
    version = _version()
    dest = Path(out) if out else ROOT / 'dist' / ('project-relay-dsh-skill-v%s.zip' % version)
    dest.parent.mkdir(parents=True, exist_ok=True)
    entries = []
    for name in ('SKILL.md', 'README.md', 'LICENSE', 'pyproject.toml'):
        p = ROOT / name
        if p.is_file():
            entries.append((p, 'project-relay/' + name))
    for name in ACTIVE_SCRIPTS:
        p = ROOT / 'scripts' / name
        if p.is_file():
            entries.append((p, 'project-relay/scripts/' + name))
    for p in sorted((ROOT / 'assets' / 'template-v2').glob('*')):
        if p.is_file():
            entries.append((p, 'project-relay/assets/template-v2/' + p.name))
    for p in sorted((ROOT / 'references').glob('*.md')):
        if p.is_file():
            entries.append((p, 'project-relay/references/' + p.name))
    plugin_root = ROOT / 'dsh-plugin' / 'project-relay-hooks'
    if plugin_root.is_dir():
        for p in sorted(plugin_root.rglob('*')):
            if p.is_file():
                rel = p.relative_to(plugin_root).as_posix()
                entries.append((p, 'project-relay/dsh-plugin/project-relay-hooks/' + rel))
    with zipfile.ZipFile(dest, 'w', zipfile.ZIP_DEFLATED) as zf:
        for source, arcname in entries:
            zf.write(source, arcname)
        zf.writestr('project-relay/INSTALL.md',
                    _install_text(version).encode('utf-8'))
    return dest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--out')
    args = parser.parse_args()
    dest = build(args.out)
    print(dest)


if __name__ == '__main__':
    main()
