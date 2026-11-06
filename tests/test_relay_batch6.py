"""Step-4 follow-up fixes (batch 6): L2222 None unpack + _set_queue same-seq loss.

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
import re
import subprocess
import tempfile
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


class Batch6Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='relay-batch6-')
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

    def setup(self, store, device_name=None):
        return sync.sync_setup(store, str(self.bare), device_name=device_name)

    def goal(self, store, repo, text):
        relay.goal_draft(store, repo, text)
        relay.goal_confirm(store, repo)

    def capture(self, store, repo, message, session='s1'):
        return flow.capture(store, repo, tool='codex', session=session,
                            message=message)

    def change(self, repo, text):
        (repo / 'app.py').write_bytes(text.encode('utf-8'))

    def _make_same_seq(self):
        """Two seq-1 entries in one raw/, as a folder-carrier merge
        would leave them: same seq, different device tags, different
        messages."""
        root = self.init()
        sd = self.store_dir(root)
        self.change(root, 'value = 2\n')
        self.capture(self.store, root, 'device A work', session='s1')
        self.change(root, 'value = 3\n')
        self.capture(self.store, root, 'device B work', session='s2')
        plain_a = sd / 'raw' / 'cap-0001.md'
        header_a, body_a = flow._parse_entry(plain_a)
        flow._write_entry(sd, header_a, body_a, filename_tag='devA0001')
        plain_a.unlink()
        plain_b = sd / 'raw' / 'cap-0002.md'
        header_b, body_b = flow._parse_entry(plain_b)
        header_b['seq'] = 1
        body_b = re.sub(r'^# 采集条目 cap-\d+', '# 采集条目 cap-0001',
                        body_b, count=1)
        flow._write_entry(sd, header_b, body_b, filename_tag='devB0002')
        plain_b.unlink()
        self.assertEqual(
            sorted(p.name for p in (sd / 'raw').glob('cap-*.md')),
            ['cap-0001-devA0001.md', 'cap-0001-devB0002.md'])
        return root, sd


class TestSharedRemoteNewProject(Batch6Base):
    """Item 1: packing a new project to a shared remote that holds
    other projects' manifests must not die unpacking None."""

    def test_pack_new_project_to_shared_remote_without_own_manifest(self):
        store_a, repo_a = self.device('proj-a')
        self.goal(store_a, repo_a, '# 目标\n\n演示。\n')
        self.capture(store_a, repo_a, 'A 设备工作', session='s1')
        self.setup(store_a, device_name='device-a')
        sync.pack(store_a, repo_a)
        # A different remote URL gives proj-b a different project key,
        # so the shared bare holds A's manifest but none for B -- the
        # exact trigger for the old unpack-before-None-check.
        store_b, repo_b = self.device(
            'proj-b', remote='https://example.com/demo/other.git')
        self.setup(store_b, device_name='device-b')
        result = sync.pack(store_b, repo_b)
        self.assertEqual(result['seq'], 1)
        self.assertEqual(result['receipt']['pull_merge']['status'],
                         'skipped')


class TestPackWithoutSetup(Batch6Base):
    """Item 1b: packing (or unpacking) without sync-setup must prompt
    for setup instead of a bare AttributeError."""

    def test_pack_and_unpack_without_setup_prompt_sync_setup(self):
        store, repo = self.device('proj-c')
        with self.assertRaises(SyncRelayError) as cm:
            sync.pack(store, repo, carrier='git')
        self.assertIn('sync-setup', str(cm.exception))
        with self.assertRaises(SyncRelayError) as cm:
            sync.unpack(store, confirm=False, carrier='git')
        self.assertIn('sync-setup', str(cm.exception))


class TestSetQueueSameSeq(Batch6Base):
    """Item 2: _set_queue must update every entry sharing a seq, each
    back to its own device-tagged file."""

    def test_set_queue_updates_all_same_seq_entries_in_place(self):
        root, sd = self._make_same_seq()
        flow._set_queue(sd, 1, 'summarized')
        raw = sd / 'raw'
        self.assertTrue((raw / 'cap-0001-devA0001.md').exists())
        self.assertTrue((raw / 'cap-0001-devB0002.md').exists())
        self.assertFalse((raw / 'cap-0001.md').exists())
        for name in ('cap-0001-devA0001.md', 'cap-0001-devB0002.md'):
            path = raw / name
            header, _body = flow._parse_entry(path)
            self.assertEqual(header['queue'], 'summarized')
            # Still written through atomic() binary rewrite (the
            # step-4 CRLF normalization side effect stays).
            self.assertNotIn(b'\r', path.read_bytes())


class TestSummarizeSameSeq(Batch6Base):
    """Item 2 end to end: both same-seq entries survive a summarize,
    keep their tags, and the cursor does not strand the second one."""

    def test_same_seq_entries_both_survive_summarize(self):
        root, sd = self._make_same_seq()
        record = self.call(flow.summarize, self.store, root, source='test')
        self.assertFalse(record.get('noop'))
        raw = sd / 'raw'
        self.assertTrue((raw / 'cap-0001-devA0001.md').exists())
        self.assertTrue((raw / 'cap-0001-devB0002.md').exists())
        self.assertFalse((raw / 'cap-0001.md').exists())
        entries = flow.list_entries(sd)
        self.assertEqual(len(entries), 2)
        self.assertEqual(
            {e['header'].get('user_message') for e in entries},
            {'device A work', 'device B work'})
        self.assertTrue(all(e['header']['queue'] == 'summarized'
                            for e in entries))
        cursor = json.loads((sd / 'state' / 'cursor.json')
                            .read_text(encoding='utf-8'))
        self.assertEqual(cursor['entry_seq'], 1)
        # Second summarize must noop, not abort on the anchor guard:
        # the cursor anchor came from the last same-seq entry, and the
        # guard must match any entry at that seq.
        again = self.call(flow.summarize, self.store, root, source='test')
        self.assertTrue(again.get('noop'))


if __name__ == '__main__':
    unittest.main()
