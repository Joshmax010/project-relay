"""Batch 13: WorkBuddy-native install -- unified name project-relay and
an uploadable skill package (`recipe package`).

Fail-before: `recipe package` does not exist pre-batch-13, and
install_codebuddy writes to skills/relay, so every test below fails on
the old code (AttributeError for the missing command, wrong install
dir for the rename).
"""
import importlib.util
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

SKILL = Path(__file__).resolve().parents[1]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


flow = _load('relay_flow', SKILL / 'scripts' / 'relay_flow.py')


class PackageBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='relay-batch13-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def package(self, out=None, toplevel=None):
        # Package the real repo checkout (SKILL is the repo root).
        return flow.package_workbuddy(toplevel or SKILL, out=out)

    def _names(self, zip_path):
        with zipfile.ZipFile(zip_path) as zf:
            return zf.namelist()


class TestPackageStructure(PackageBase):
    def test_zip_has_single_top_level_project_relay_folder(self):
        out = self.root / 'pkg.zip'
        info = self.package(out=str(out))
        names = self._names(out)
        tops = {n.split('/')[0] for n in names}
        self.assertEqual(tops, {'project-relay'},
                         'zip must contain exactly one top-level folder')
        self.assertIn('project-relay/SKILL.md', names)
        self.assertIn('project-relay/scripts/relay.py', names)
        self.assertIn('project-relay/scripts/relay_flow.py', names)
        self.assertTrue(any(n.startswith('project-relay/references/v2-')
                            for n in names),
                        'zip must carry the v2 deep specs')
        self.assertFalse(any('__pycache__' in n or n.endswith(('.pyc', '.pyo'))
                             for n in names),
                         'zip must not carry bytecode caches')
        self.assertEqual(names, sorted(names), 'zip entries must be sorted')
        self.assertEqual(names, info['files'])

    def test_packaged_skill_frontmatter_has_agent_created(self):
        out = self.root / 'pkg.zip'
        self.package(out=str(out))
        with zipfile.ZipFile(out) as zf:
            text = zf.read('project-relay/SKILL.md').decode('utf-8')
        self.assertTrue(text.startswith('---\nname: project-relay\n'),
                        'packaged skill name must be project-relay')
        self.assertIn('agent_created: true', text,
                      'packaged skill must let WorkBuddy edit it')
        repo_text = (SKILL / 'SKILL.md').read_text(encoding='utf-8')
        self.assertNotIn('agent_created', repo_text,
                         'repo SKILL.md must stay clean')
        self.assertEqual(text.replace('agent_created: true\n', ''), repo_text,
                         'packaged SKILL.md must be the repo copy + one line')

    def test_unzipped_package_runs_relay_help(self):
        out = self.root / 'pkg.zip'
        self.package(out=str(out))
        dest = self.root / 'unpacked'
        with zipfile.ZipFile(out) as zf:
            zf.extractall(dest)
        pkg = dest / 'project-relay'
        proc = subprocess.run([sys.executable, 'scripts/relay.py', '--help'],
                              cwd=str(pkg), capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('recipe', proc.stdout)

    def test_repackage_overwrites_and_is_reproducible(self):
        out = self.root / 'pkg.zip'
        first = self.package(out=str(out))
        first_bytes = out.read_bytes()
        second = self.package(out=str(out))
        self.assertEqual(out.read_bytes(), first_bytes,
                         're-packaging must be byte-identical')
        self.assertEqual(first['files'], second['files'])

    def test_default_output_is_repo_root_zip(self):
        # A scratch repo (SKILL.md + scripts/ + references/) so the
        # default output lands in a throwaway root, not the real repo.
        repo = self.root / 'repo'
        repo.mkdir()
        subprocess.run(['git', 'init', '-q', '-b', 'main', str(repo)],
                       check=True)
        shutil.copytree(SKILL / 'scripts', repo / 'scripts',
                        ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copytree(SKILL / 'references', repo / 'references')
        shutil.copy2(SKILL / 'SKILL.md', repo / 'SKILL.md')
        info = flow.package_workbuddy(repo)
        self.assertEqual(info['zip_path'],
                         str(repo / 'project-relay-workbuddy.zip'))
        self.assertTrue(Path(info['zip_path']).is_file())


class TestCliPackage(PackageBase):
    def test_cli_package_writes_zip(self):
        out = self.root / 'cli.zip'
        proc = subprocess.run(
            [sys.executable, str(SKILL / 'scripts' / 'relay.py'),
             'recipe', 'package', '--tool', 'workbuddy',
             '--out', str(out)],
            capture_output=True, text=True, cwd=str(SKILL))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('技能包：', proc.stdout)
        self.assertTrue(out.is_file())
        self.assertIn('project-relay/SKILL.md', self._names(out))


class TestInstallUnifiedName(PackageBase):
    """`recipe install` must land in skills/project-relay (batch 13)."""

    def test_install_workbuddy_uses_project_relay_folder(self):
        home = self.root / 'home'
        repo = self.root / 'repo'
        repo.mkdir()
        subprocess.run(['git', 'init', '-q', '-b', 'main', str(repo)],
                       check=True)
        (repo / 'app.py').write_bytes(b'value = 1\n')
        subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(repo), 'commit', '-qm', 'baseline'],
                       check=True, env={**os.environ,
                                        'GIT_AUTHOR_NAME': 'Tester',
                                        'GIT_AUTHOR_EMAIL': 'test@example.invalid',
                                        'GIT_COMMITTER_NAME': 'Tester',
                                        'GIT_COMMITTER_EMAIL': 'test@example.invalid'})
        store = self.root / 'store'
        env = dict(os.environ)
        env['PROJECT_RELAY_HOME'] = str(self.root)
        out = subprocess.run(
            [sys.executable, str(SKILL / 'scripts' / 'relay.py'),
             '--root', str(repo), '--store-root', str(store),
             'init'],
            capture_output=True, text=True, cwd=str(SKILL), env=env)
        self.assertEqual(out.returncode, 0, out.stderr)
        out = subprocess.run(
            [sys.executable, str(SKILL / 'scripts' / 'relay.py'),
             '--root', str(repo), '--store-root', str(store),
             'recipe', 'install', '--tool', 'workbuddy',
             '--target-root', str(home)],
            capture_output=True, text=True, cwd=str(SKILL), env=env)
        self.assertEqual(out.returncode, 0, out.stderr)
        skill = home / '.workbuddy' / 'skills' / 'project-relay' / 'SKILL.md'
        self.assertTrue(skill.is_file(), 'installed SKILL.md missing')
        self.assertFalse((home / '.workbuddy' / 'skills' / 'relay').exists(),
                         'old relay folder must not be created')
        installed = skill.read_bytes()
        self.assertEqual(installed, flow.RELAY_SKILL_TEXT.encode('utf-8'))
        self.assertTrue(installed.startswith(b'---\nname: project-relay\n'),
                        'installed skill name must be project-relay')


if __name__ == '__main__':
    unittest.main()
