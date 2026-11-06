"""Batch 9: Codex PR #5 2026-11-06 review -- 4 unfixed items (1 P1 + 3 P2).

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
import shlex
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

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


class Batch9Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='relay-batch9-')
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
        row = relay.where(self.store, path=root)['row']
        return self.store / 'projects' / (row['key'] + '-' + row['slug'])


class TestSftpQuoting(Batch9Base):
    """P1 (Codex 2026-11-06): SFTP remote paths must be shell-quoted."""

    def _fake_run(self, calls):
        def run(argv, **kwargs):
            calls.append(list(argv))
            return mock.Mock(returncode=0, stdout=b'', stderr=b'')
        return run

    def test_put_rename_remove_tree_quote_paths(self):
        backend = sync.SftpBackend('tester@host:/srv/relay')
        calls = []
        with mock.patch('subprocess.run', self._fake_run(calls)):
            backend.put('evil; touch PWNED/f.bin', b'x')
            backend.rename('a; touch PWNED', 'c$(touch PWNED)')
            backend.remove_tree('x; touch PWNED')
        scripts = [argv[2] for argv in calls if argv[:2] == ['ssh', 'host']]
        self.assertEqual('mkdir -p ' + shlex.quote('/srv/relay/evil; touch PWNED'),
                         scripts[0])
        self.assertEqual('mv ' + shlex.quote('/srv/relay/a; touch PWNED')
                         + ' ' + shlex.quote('/srv/relay/c$(touch PWNED)'),
                         scripts[1])
        self.assertEqual('rm -rf ' + shlex.quote('/srv/relay/x; touch PWNED'),
                         scripts[2])

    def test_quoted_script_executes_no_extra_command(self):
        base = self.root / 'srv'
        backend = sync.SftpBackend('host:%s' % base)
        calls = []
        with mock.patch('subprocess.run', self._fake_run(calls)):
            backend.put('evil; touch PWNED/f.bin', b'x')
        script = [argv[2] for argv in calls if argv[:2] == ['ssh', 'host']][0]
        work = self.root / 'cwd'
        work.mkdir()
        subprocess.run(['sh', '-c', script], cwd=str(work), check=True,
                       capture_output=True)
        # The quoted mkdir creates a directory literally named
        # 'evil; touch PWNED'; the touch must never run as a command.
        self.assertTrue((base / 'evil; touch PWNED').is_dir())
        self.assertFalse((work / 'PWNED').exists())


class TestWeakKeyMigrationManifest(Batch9Base):
    """P2 (Codex 2026-11-06): weak-key migration must refresh the manifest."""

    def test_migrate_weak_key_keeps_manifest_valid(self):
        root = self.make_repo('proj-weak')
        self.call(relay.init, self.store, root)
        flow.capture(self.store, root, session='s')
        flow.summarize(self.store, root, source='test')
        sd = self.store_dir(root)
        present, mismatches = relay.verify_checksum_manifest(sd)
        self.assertTrue(present)
        self.assertEqual([], mismatches)
        self.git(root, 'remote', 'add', 'origin', REMOTE_URL)
        relay.migrate_key(self.store, root)
        new_sd = self.store_dir(root)
        present, mismatches = relay.verify_checksum_manifest(new_sd)
        self.assertTrue(present)
        self.assertEqual([], mismatches)
        rec = flow.resume(self.store, root)
        sections = '\n'.join(str(s) for s in rec.get('sections', []))
        self.assertNotIn('校验清单不符', sections)


class TestSemanticRedaction(Batch9Base):
    """P2 (Codex 2026-11-06): caller-supplied semantic text is redacted."""

    def test_semantic_file_secrets_never_land_in_archives(self):
        root = self.init()
        flow.capture(self.store, root, session='s')
        secret_gh = 'ghp_' + 'A' * 20
        secret_sk = 'sk-proj-' + 'B' * 20
        semantic = ('## 已验证\n- 密钥已轮换为 %s（验证：跑测试）\n'
                    '## 决定\n- 选用标准库，密钥 %s（来源：用户明确要求）\n'
                    '## 待办\n- 轮换旧密钥 %s（Owner：用户，验证：文件存在）\n'
                    % (secret_sk, secret_gh, secret_gh))
        rec = flow.summarize(self.store, root, source='test',
                             semantic_text=semantic)
        self.assertGreaterEqual(rec['redacted_semantic'], 3)
        self.assertEqual(['D-001'], rec['appended_decisions'])
        sd = self.store_dir(root)
        archive = sd / 'archive'
        for name in ('decisions.md', 'lessons.md', 'backlog.md', 'state.md'):
            text = (archive / name).read_text(encoding='utf-8')
            self.assertNotIn(secret_gh, text, name)
            self.assertNotIn(secret_sk, text, name)


class TestReservedCaptureAtomic(Batch9Base):
    """P2 (Codex 2026-11-06): reserved captures publish atomically."""

    def test_mid_write_crash_leaves_no_truncated_entry(self):
        sd = self.root / 'sd'
        header = {'version': 1, 'seq': 1, 'queue': 'pending'}
        body = '# 采集条目 cap-0001\n\n正文\n'
        # Crash the byte write on both paths at once: the old reserve
        # path writes straight to the final file with os.write, the new
        # path writes a temp file (C-level buffered write, bypassing
        # os.write) and then fsyncs it. Either way the write must raise
        # here; afterwards the final path must be absent (never
        # truncated) and list_entries must still work.
        with mock.patch.object(os, 'write',
                               side_effect=OSError('simulated crash')), \
             mock.patch.object(os, 'fsync',
                               side_effect=OSError('simulated crash')):
            with self.assertRaises(OSError):
                flow._write_entry(sd, header, body, reserve=True)
        self.assertFalse((sd / 'raw' / 'cap-0001.md').exists())
        self.assertEqual([], flow.list_entries(sd))
        leftovers = [p for p in (sd / 'raw').glob('.relay-cap-*')]
        self.assertEqual([], leftovers)

    def test_seq_bump_still_works(self):
        sd = self.root / 'sd'
        header = {'version': 1, 'seq': 1, 'queue': 'pending'}
        body = '# 采集条目 cap-0001\n\n正文\n'
        path1, seq1 = flow._write_entry(sd, dict(header), body, reserve=True)
        self.assertEqual(1, seq1)
        path2, seq2 = flow._write_entry(sd, dict(header), body, reserve=True)
        self.assertEqual(2, seq2)
        self.assertNotEqual(path1, path2)
        self.assertEqual([1, 2], [e['header']['seq']
                                  for e in flow.list_entries(sd)])

    def test_concurrent_captures_lose_no_entry(self):
        root = self.init()
        sd = self.store_dir(root)
        results, errors = [], []

        def run(session):
            try:
                results.append(flow.capture(self.store, root, tool='codex',
                                            session=session,
                                            message='t-' + session))
            except Exception as exc:  # noqa: BLE001 - collected, asserted below
                errors.append(exc)

        threads = [threading.Thread(target=run, args=(s,))
                   for s in ('t1', 't2')]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual([], errors)
        self.assertEqual(2, len(results))
        seqs = sorted(r['seq'] for r in results)
        self.assertEqual(2, len(set(seqs)))
        for seq in seqs:
            self.assertTrue((sd / 'raw' / ('cap-%04d.md' % seq)).exists())
