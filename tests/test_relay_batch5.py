"""Step-3 Windows re-run regression tests (18 failures, batch 5).

Style follows the other suites: real temporary directories, real Git
repos, PROJECT_RELAY_HOME patched so no test ever touches the
developer's real ~/.project-relay/. Each test names the item it pins.

The gate test is test_crlf_entry_exports_successfully: a cap entry
file rewritten to CRLF must still export. It fails before the batch 5
read-side fix and passes after, on every platform -- unlike the
Windows-only O_BINARY write side, which the Harness re-run verifies.
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

SKILL = Path(__file__).resolve().parents[1]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


relay = _load('relay', SKILL / 'scripts/relay.py')
flow = _load('relay_flow', SKILL / 'scripts/relay_flow.py')
sync = _load('relay_sync', SKILL / 'scripts/relay_sync.py')
init_project = _load('init_project', SKILL / 'scripts/init_project.py')
FlowRelayError = flow.relay.RelayError
SyncRelayError = sync.relay.RelayError


class Batch5Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='relay-batch5-')
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

    def git(self, root, *args, check=True):
        return subprocess.run(['git', '-C', str(root), *args],
                              capture_output=True, check=check)

    def make_repo(self, name):
        root = self.root / name
        root.mkdir()
        self.git(root, 'init', '-b', 'main')
        self.git(root, 'config', 'user.name', 'Tester')
        self.git(root, 'config', 'user.email', 'test@example.invalid')
        self.git(root, 'config', 'commit.gpgsign', 'false')
        (root / 'app.py').write_bytes('value = 1\n'.encode('utf-8'))
        self.git(root, 'add', '.')
        self.git(root, 'commit', '-m', 'baseline')
        return root

    def call(self, fn, *args, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return fn(*args, **kwargs)

    def init(self, name='proj'):
        root = self.make_repo(name)
        self.call(relay.init, self.store, root)
        return root

    def store_dir(self, root):
        return relay.resolve_store_dir(self.store, root)[1]

    def capture(self, store, repo, message, session='s1'):
        return flow.capture(store, repo, tool='codex', session=session,
                            message=message)


class TestCRLFEntryExport(Batch5Base):
    """Main root cause (14 Windows failures): a CRLF cap entry must
    still export. The write side (O_BINARY) stops new CRLF files; the
    read side (normalize before splitting) must accept CRLF entries
    no matter which writer produced them."""

    def test_crlf_entry_exports_successfully(self):
        root = self.init()
        sd = self.store_dir(root)
        self.capture(self.store, root, '做了甲', session='s1')
        entry = sd / 'raw' / 'cap-0001.md'
        _header, body = flow._parse_entry(entry)
        # Rewrite the entry the way a text-mode writer on Windows
        # would have left it: every LF becomes CRLF.
        entry.write_bytes(entry.read_bytes().replace(b'\n', b'\r\n'))
        dest = self.root / 'bundle'
        manifest = sync.export_bundle(self.store, [root], dest,
                                      'device-a', 'id-a')
        self.assertEqual(manifest['entry_count'], 1)
        row = relay.resolve_store_dir(self.store, root)[0]
        exported = (dest / 'projects' / ('%s-%s' % (row['key'], row['slug']))
                    / 'raw' / 'cap-0001.md').read_bytes()
        # The rescan normalizes the entry back to LF.
        self.assertNotIn(b'\r', exported)
        header2, body2 = sync._parse_entry_text(exported.decode('utf-8'))
        self.assertEqual(header2['seq'], 1)
        self.assertEqual(body2, body)

    def test_parse_entry_text_tolerates_crlf(self):
        root = self.init()
        sd = self.store_dir(root)
        self.capture(self.store, root, '做了甲', session='s1')
        entry = sd / 'raw' / 'cap-0001.md'
        text = entry.read_bytes().decode('utf-8').replace('\n', '\r\n')
        header, body = sync._parse_entry_text(text)
        self.assertEqual(header['seq'], 1)
        self.assertEqual(header['user_message'], '做了甲')

    def test_flow_parse_entry_tolerates_crlf(self):
        # flow._parse_entry reads with read_text, whose universal
        # newlines already normalize CRLF -- this pins that tolerance
        # so a future rewrite of the reader cannot silently drop it.
        root = self.init()
        sd = self.store_dir(root)
        self.capture(self.store, root, '做了甲', session='s1')
        entry = sd / 'raw' / 'cap-0001.md'
        entry.write_bytes(entry.read_bytes().replace(b'\n', b'\r\n'))
        header, body = flow._parse_entry(entry)
        self.assertEqual(header['seq'], 1)
        self.assertEqual(header['user_message'], '做了甲')

    def test_reserve_write_is_byte_identical(self):
        # The reserve branch must write exactly the bytes it was
        # given. On Windows before the O_BINARY fix, os.write on a
        # text-mode fd translated \n to \r\n, so this fails there
        # and passes here; on Linux it passes either way.
        root = self.init()
        sd = self.store_dir(root)
        self.capture(self.store, root, '做了甲', session='s1')
        entry = sd / 'raw' / 'cap-0001.md'
        data = entry.read_bytes()
        header, body = flow._parse_entry(entry)
        expected = (json.dumps(header, ensure_ascii=False, sort_keys=True,
                               indent=2) + flow.BODY_DELIM + body
                    ).encode('utf-8')
        self.assertEqual(expected, data)


class TestInitProjectLF(Batch5Base):
    """Item 5: init_project must write LF templates. On Windows the
    old text-mode copy translated them to CRLF, so the committed
    state.md blob was CRLF while draft proposals read back LF --
    draft's filtered-hash no-op check then never matched. On Linux
    this passes with or without the fix; it guards the writer
    discipline, and the Windows re-run of
    test_draft_rejects_content_identical_to_committed_version is the
    real gate."""

    def test_templates_are_lf(self):
        root = self.make_repo('proj-init')
        self.call(init_project.initialize, root)
        agent = root / '.agent'
        md_files = sorted(p for p in agent.rglob('*.md') if p.is_file())
        self.assertTrue(md_files)
        for path in md_files:
            self.assertNotIn(b'\r', path.read_bytes(), str(path))


class TestStopHookWindow(Batch5Base):
    """Item 3: the old 200-char window for 'session=_sid' assumed
    short POSIX temp paths. Under a long path the token sits past
    the window (the Windows failure cut off mid-token, "sess").
    This builds a long path on purpose so the old window provably
    misses while the whole-tail check still finds the token."""

    def test_session_check_survives_long_paths(self):
        long_tmp = tempfile.TemporaryDirectory(prefix='l' * 120 + '-')
        self.addCleanup(long_tmp.cleanup)
        base = Path(long_tmp.name)
        root = base / 'proj'
        root.mkdir()
        self.git(root, 'init', '-b', 'main')
        self.git(root, 'config', 'user.name', 'Tester')
        self.git(root, 'config', 'user.email', 'test@example.invalid')
        self.git(root, 'config', 'commit.gpgsign', 'false')
        (root / 'app.py').write_bytes('value = 1\n'.encode('utf-8'))
        self.git(root, 'add', '.')
        self.git(root, 'commit', '-m', 'baseline')
        store = base / 'store'
        self.call(relay.init, store, root)
        cmd = flow._hook_command(store, root, 'stop')
        cap_pos = cmd.find('hook_capture')
        self.assertNotEqual(cap_pos, -1)
        self.assertNotIn('session=_sid', cmd[cap_pos:cap_pos + 200])
        self.assertIn('session=_sid', cmd[cap_pos:])


if __name__ == '__main__':
    unittest.main()
