"""Batch 7: manual folder carrier -- export-zip / import-zip.

Style follows the other suites: real temporary directories, real Git
repos, local bare repos stand in for remotes. PROJECT_RELAY_HOME is
patched so no test ever touches the developer's real ~/.project-relay/.
Each test names the item it pins, so a future regression points straight
at the finding that required the fix.
"""

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

SKILL = Path(__file__).resolve().parents[1]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


relay = _load('relay', SKILL / 'scripts/relay.py')
flow = _load('relay_flow', SKILL / 'scripts/relay_flow.py')
sync = _load('relay_sync', SKILL / 'scripts/relay_sync.py')
FlowRelayError = flow.relay.RelayError
SyncRelayError = sync.relay.RelayError

REMOTE_URL = 'https://example.com/demo/proj.git'


class Batch7Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='relay-batch7-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = self.root / 'store'
        env = {
            'PROJECT_RELAY_HOME': str(self.root),
            'GIT_AUTHOR_NAME': 'Tester',
            'GIT_AUTHOR_EMAIL': 'test@example.invalid',
            'GIT_COMMITTER_NAME': 'Tester',
            'GIT_COMMITTER_EMAIL': 'test@example.invalid',
        }
        self._env = mock.patch.dict(os.environ, env)
        self._env.start()
        self.addCleanup(self._env.stop)
        self.bare = self.root / 'remote.git'
        subprocess.run(['git', 'init', '--bare', str(self.bare)],
                       check=True, capture_output=True)

    def git(self, root, *args, check=True):
        return subprocess.run(['git', '-C', str(root), *args],
                              capture_output=True, check=check)

    def make_repo(self, name, remote=None):
        root = self.root / name
        root.mkdir()
        self.git(root, 'init', '-b', 'main')
        self.git(root, 'config', 'user.name', 'Tester')
        self.git(root, 'config', 'user.email', 'test@example.invalid')
        self.git(root, 'config', 'commit.gpgsign', 'false')
        (root / 'app.py').write_bytes('value = 1\n'.encode('utf-8'))
        self.git(root, 'add', '.')
        self.git(root, 'commit', '-m', 'baseline')
        if remote:
            self.git(root, 'remote', 'add', 'origin', remote)
        return root

    def call(self, fn, *args, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return fn(*args, **kwargs)

    def init(self, name='proj', remote=None):
        root = self.make_repo(name, remote=remote or REMOTE_URL)
        self.call(relay.init, self.store, root)
        return root

    def store_dir(self, root):
        return relay.resolve_store_dir(self.store, root)[1]

    def device(self, name, remote=REMOTE_URL):
        repo = self.make_repo(name, remote)
        store = self.root / ('store-' + name)
        relay.init(store, repo)
        return store, repo

    def goal(self, store, repo, text):
        relay.goal_draft(store, repo, text)
        relay.goal_confirm(store, repo)

    def capture(self, store, repo, message, session='s1'):
        return flow.capture(store, repo, tool='codex', session=session,
                            message=message)

    def change(self, repo, text):
        (repo / 'app.py').write_bytes(text.encode('utf-8'))

    def snapshot(self, store):
        """{relpath: bytes} of every file under a store ({} if absent)."""
        store = Path(store)
        out = {}
        if store.is_dir():
            for path in sorted(store.rglob('*')):
                if path.is_file():
                    out[path.relative_to(store).as_posix()] = path.read_bytes()
        return out

    def _export(self, store, repo):
        """Two captured entries, exported to a zip. No sync-setup:
        the manual carrier must not require it."""
        self.goal(store, repo, '# 目标\n\n演示。\n')
        self.capture(store, repo, '第一条', session='s1')
        self.change(repo, 'value = 2\n')
        self.capture(store, repo, '第二条', session='s2')
        zip_path = self.root / 'bundle.zip'
        result = sync.export_zip(store, repo, zip_path)
        return zip_path, result

    def _craft_slip_zip(self, path):
        """A good entry first, then a zip-slip entry, an absolute path,
        and a symlink entry. A correct reader must refuse the whole
        zip before writing anything."""
        with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.writestr('projects/x/cap-0001.md', b'good bytes')
            zf.writestr('../evil.txt', b'evil')
            zf.writestr('/abs/evil.txt', b'evil')
            link = zipfile.ZipInfo('link')
            link.external_attr = (0o120777 << 16)
            zf.writestr(link, b'projects/x/cap-0001.md')
        return path

    def _tamper_zip(self, src, dst):
        """Rebuild a zip with one project file's bytes changed. The
        manifest still lists the old hash, so verification must fail."""
        with zipfile.ZipFile(src) as zin:
            items = [(info.filename, zin.read(info.filename))
                     for info in zin.infolist()]
        tampered = False
        with zipfile.ZipFile(dst, 'w', zipfile.ZIP_DEFLATED) as zout:
            for name, data in items:
                if not tampered and name.startswith('projects/') \
                        and name.endswith('cap-0001.md'):
                    data = data + '\n篡改。\n'.encode('utf-8')
                    tampered = True
                zout.writestr(name, data)
        self.assertTrue(tampered, 'tamper target not found in zip')
        return dst


class TestZipRoundtrip(Batch7Base):
    """Item 1: export -> zip -> import lands byte-identical entries."""

    def test_export_import_zip_roundtrip_matched(self):
        store_a, repo_a = self.device('proj-a')
        zip_path, result = self._export(store_a, repo_a)
        self.assertEqual(result['entry_count'], 2)
        self.assertGreater(result['file_count'], 0)
        self.assertEqual(result['redacted_count'], 0)
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
        self.assertIn('manifest.json', names)
        self.assertTrue(any(n.startswith('projects/') for n in names))
        # No device-local config or key material ever enters the bundle.
        self.assertFalse(any('sync-config' in n or 'sync-key' in n
                             or 'private' in n for n in names))
        with zipfile.ZipFile(zip_path) as zf:
            manifest = json.loads(zf.read('manifest.json'))
        self.assertNotIn('sync-config', json.dumps(manifest))
        # Same remote URL -> same project key, so B matches.
        store_b, repo_b = self.device('proj-b')
        before = self.snapshot(store_b)
        report = sync.import_zip(store_b, zip_path, confirm=True)
        self.assertTrue(report['landed'])
        self.assertEqual(report['report']['projects'][0]['status'], 'matched')
        self.assertGreater(len(self.snapshot(store_b)), len(before))
        sd_a = relay.resolve_store_dir(store_a, repo_a)[1]
        sd_b = relay.resolve_store_dir(store_b, repo_b)[1]
        for name in ('cap-0001.md', 'cap-0002.md'):
            self.assertEqual(
                (sd_b / 'raw' / name).read_bytes(),
                (sd_a / 'raw' / name).read_bytes())


class TestZipSlip(Batch7Base):
    """Item 2: zip-slip / absolute / symlink entries fail closed
    before any write, at the reader and at import."""

    def test_read_bundle_zip_prevalidates_before_any_write(self):
        zip_path = self._craft_slip_zip(self.root / 'slip.zip')
        dest = self.root / 'extracted'
        with self.assertRaises(SyncRelayError):
            sync.read_bundle_zip(zip_path, dest)
        extracted = [p for p in dest.rglob('*') if p.is_file()] \
            if dest.exists() else []
        self.assertEqual(extracted, [])

    def test_zip_slip_import_rejected_with_zero_writes(self):
        zip_path = self._craft_slip_zip(self.root / 'slip.zip')
        store_b, _repo_b = self.device('proj-b')
        before = self.snapshot(store_b)
        with self.assertRaises(SyncRelayError):
            sync.import_zip(store_b, zip_path, confirm=True)
        self.assertEqual(self.snapshot(store_b), before)


class TestTamperedZip(Batch7Base):
    """Item 3: a zip whose contents were changed after export is
    rejected by the manifest hash gate, with zero writes."""

    def test_tampered_zip_rejected_with_zero_writes(self):
        store_a, repo_a = self.device('proj-a')
        zip_path, _result = self._export(store_a, repo_a)
        bad = self._tamper_zip(zip_path, self.root / 'tampered.zip')
        store_b, _repo_b = self.device('proj-b')
        before = self.snapshot(store_b)
        with self.assertRaises(SyncRelayError) as cm:
            sync.import_zip(store_b, bad, confirm=True)
        self.assertIn('哈希不符', str(cm.exception))
        self.assertEqual(self.snapshot(store_b), before)


class TestZipUnclaimedAndDryRun(Batch7Base):
    """Items 4-5: zip import lands unmatched projects in unclaimed,
    and a dry-run writes nothing."""

    def test_zip_import_lands_unclaimed_without_match(self):
        store_a, repo_a = self.device('proj-a')
        zip_path, _result = self._export(store_a, repo_a)
        store_u = self.root / 'store-unclaimed'
        report = sync.import_zip(store_u, zip_path, confirm=True)
        self.assertTrue(report['landed'])
        proj = report['report']['projects'][0]
        self.assertEqual(proj['status'], 'unclaimed')
        dest = store_u / 'unclaimed' / ('%s-%s' % (proj['key'], proj['slug']))
        self.assertTrue((dest / 'raw' / 'cap-0001.md').is_file())
        self.assertTrue((dest / 'raw' / 'cap-0002.md').is_file())

    def test_dry_run_writes_nothing(self):
        store_a, repo_a = self.device('proj-a')
        zip_path, _result = self._export(store_a, repo_a)
        store_b, _repo_b = self.device('proj-b')
        before = self.snapshot(store_b)
        report = sync.import_zip(store_b, zip_path, confirm=False)
        self.assertFalse(report['landed'])
        self.assertEqual(self.snapshot(store_b), before)

    def test_export_refuses_existing_zip(self):
        store_a, repo_a = self.device('proj-a')
        zip_path, _result = self._export(store_a, repo_a)
        with self.assertRaises(SyncRelayError):
            sync.export_zip(store_a, repo_a, zip_path)


class TestZipCLI(Batch7Base):
    """CLI wiring: export-zip / import-zip run end to end via relay.py."""

    def _cli(self, *args):
        return subprocess.run(
            [sys.executable, str(SKILL / 'scripts' / 'relay.py'), *args],
            capture_output=True, text=True, cwd=str(SKILL))

    def test_cli_export_import_zip_roundtrip(self):
        store_a, repo_a = self.device('proj-a')
        self.goal(store_a, repo_a, '# 目标\n\n演示。\n')
        self.capture(store_a, repo_a, '第一条', session='s1')
        zip_path = self.root / 'cli.zip'
        out = self._cli('--root', str(repo_a), '--store-root', str(store_a),
                        'export-zip', '--zip', str(zip_path))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn('导出完成', out.stdout)
        self.assertTrue(zip_path.is_file())
        store_b, repo_b = self.device('proj-b')
        out = self._cli('--root', str(repo_b), '--store-root', str(store_b),
                        'import-zip', '--zip', str(zip_path), '--confirm')
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn('导入已落盘', out.stdout)
        sd_b = relay.resolve_store_dir(store_b, repo_b)[1]
        self.assertTrue((sd_b / 'raw' / 'cap-0001.md').is_file())


if __name__ == '__main__':
    unittest.main()
