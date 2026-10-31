"""Codex PR #5 rounds 7-12 regression tests (11 items, batch 4).

Style follows the other suites: real temporary directories, real Git
repos, local bare repos stand in for remotes. PROJECT_RELAY_HOME is
patched so no test ever touches the developer's real ~/.project-relay/.
Each test names the item it pins, so a future regression points straight
at the comment that required the fix.
"""

import base64
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
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
promote = _load('relay_promote', SKILL / 'scripts/relay_promote.py')
fork = _load('relay_fork', SKILL / 'scripts/relay_fork.py')
FlowRelayError = flow.relay.RelayError
SyncRelayError = sync.relay.RelayError

REMOTE_URL = 'https://example.com/demo/proj.git'


class Batch4Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='relay-batch4-')
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

    def tree(self, store):
        return {p.relative_to(store).as_posix(): p.read_bytes()
                for p in store.rglob('*')
                if p.is_file() and '.git' not in p.parts}


class TestTxOtherFailClosed(Batch4Base):
    """Item 1: any 'other' file in an incomplete tx aborts the retry."""

    def _txlog(self, sd, files):
        txdir = sd / 'tx'
        txdir.mkdir(parents=True, exist_ok=True)
        recs = {}
        for name, (before, after_sha, current) in files.items():
            (sd / 'archive' / name).write_bytes(current)
            recs[name] = {'before_b64': base64.b64encode(before).decode('utf-8'),
                          'after_sha': after_sha}
        (txdir / '0001.json').write_text(
            json.dumps({'seq': 1, 'files': recs}), encoding='utf-8')

    def test_after_plus_other_raises(self):
        root = self.init()
        sd = self.store_dir(root)
        after_bytes = b'# state after\n'
        self._txlog(sd, {
            'state.md': (b'# state before\n', flow.digest(after_bytes), after_bytes),
            'decisions.md': (b'# dec before\n', flow.digest(b'# dec after\n'),
                             b'# dec edited by user\n'),
        })
        with self.assertRaises(FlowRelayError) as ctx:
            flow._recover_incomplete_tx(self.store, root, sd)
        self.assertIn('外部改动', str(ctx.exception))

    def test_other_only_raises(self):
        root = self.init()
        sd = self.store_dir(root)
        self._txlog(sd, {
            'decisions.md': (b'# dec before\n', flow.digest(b'# dec after\n'),
                             b'# dec edited by user\n'),
        })
        with self.assertRaises(FlowRelayError):
            flow._recover_incomplete_tx(self.store, root, sd)

    def test_completed_tx_with_later_edit_continues(self):
        # Sync-accept Act 2: a completed transaction whose backlog.md
        # was appended afterwards is legitimate, not incomplete -- the
        # retry must proceed, not abort.
        root = self.init()
        sd = self.store_dir(root)
        after_bytes = b'# state after\n'
        self._txlog(sd, {
            'state.md': (b'# state before\n', flow.digest(after_bytes), after_bytes),
            'backlog.md': (b'# log before\n', flow.digest(b'# log after\n'),
                           b'# log appended after completion\n'),
        })
        rec = sd / 'state' / 'summaries'
        rec.mkdir(parents=True, exist_ok=True)
        (rec / '0001.json').write_text('{"seq": 1}', encoding='utf-8')
        self.assertIsNone(flow._recover_incomplete_tx(self.store, root, sd))

    def test_all_before_still_continues(self):
        root = self.init()
        sd = self.store_dir(root)
        before = b'# dec before\n'
        self._txlog(sd, {
            'decisions.md': (before, flow.digest(b'# dec after\n'), before),
        })
        self.assertIsNone(flow._recover_incomplete_tx(self.store, root, sd))


class TestCaptureReserve(Batch4Base):
    """Item 2: concurrent captures must not overwrite the same cap file."""

    def test_precreated_next_file_bumps_seq(self):
        root = self.init()
        sd = self.store_dir(root)
        self.change(root, 'value = 2\n')
        self.capture(self.store, root, 'first', session='s1')
        first = sd / 'raw' / 'cap-0001.md'
        header, body = flow._parse_entry(first)
        header['seq'] = 2
        flow._write_entry(sd, header, body)
        fake = sd / 'raw' / 'cap-0002.md'
        fake_bytes = fake.read_bytes()
        self.change(root, 'value = 3\n')
        info = self.capture(self.store, root, 'second', session='s2')
        self.assertEqual(info['seq'], 3)
        self.assertEqual(fake.read_bytes(), fake_bytes)
        self.assertTrue((sd / 'raw' / 'cap-0003.md').exists())

    def test_two_threads_capture_without_loss(self):
        root = self.init()
        sd = self.store_dir(root)
        self.change(root, 'value = 2\n')
        results, errors = [], []

        def run(session):
            try:
                results.append(flow.capture(self.store, root, tool='codex',
                                            session=session, message='t-' + session))
            except Exception as exc:  # noqa: BLE001 - collected, asserted below
                errors.append(exc)

        threads = [threading.Thread(target=run, args=(s,)) for s in ('t1', 't2')]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        seqs = sorted(r['seq'] for r in results)
        self.assertEqual(len(set(seqs)), 2)
        for seq in seqs:
            self.assertTrue((sd / 'raw' / ('cap-%04d.md' % seq)).exists())


class TestManifestNames(Batch4Base):
    """Item 3: concurrent devices' manifests must not collide by name."""

    def test_two_devices_same_seq_keep_both_manifests(self):
        store_a, repo_a = self.device('proj-a')
        self.goal(store_a, repo_a, '# 目标\n\n演示。\n')
        self.capture(store_a, repo_a, 'A 设备工作', session='s1')
        self.setup(store_a, device_name='device-a')
        sync.pack(store_a, repo_a)
        store_b, repo_b = self.device('proj-b')
        self.setup(store_b, device_name='device-b')
        sync.pack(store_b, repo_b)
        key = relay.where(store_a, path=repo_a)['row']['key']
        refs = subprocess.run(['git', '--git-dir', str(self.bare),
                               'for-each-ref', '--format=%(refname)'],
                              capture_output=True, check=True, text=True).stdout
        ref = refs.splitlines()[0]
        out = subprocess.run(['git', '--git-dir', str(self.bare), 'ls-tree',
                              '-r', '--name-only', ref],
                             capture_output=True, check=True, text=True).stdout
        manifests = [l for l in out.splitlines()
                     if l.startswith('sync-manifests/%s-' % key)]
        self.assertEqual(len(manifests), 2)
        self.assertEqual(len(set(manifests)), 2)
        self.assertTrue(all(m.endswith('-0001.json') for m in manifests))


class TestTofuDryRun(Batch4Base):
    """Item 4: a dry-run unpack must not persist a TOFU pin."""

    def test_dry_run_writes_no_pin_confirm_pins(self):
        store_a, repo_a = self.device('proj-a')
        self.goal(store_a, repo_a, '# 目标\n\n演示。\n')
        self.setup(store_a, device_name='device-a')
        sync.pack(store_a, repo_a)
        store_b, repo_b = self.device('proj-b')
        self.setup(store_b, device_name='device-b')
        pins_path = sync._sync_pins_path(store_b)
        self.assertFalse(pins_path.exists())
        dry = sync.unpack(store_b, confirm=False)
        self.assertFalse(pins_path.exists())
        self.assertEqual(
            dry['reports'][0]['report']['signature_verification']['status'],
            'tofu-would-pin')
        sync.unpack(store_b, confirm=True)
        self.assertTrue(pins_path.exists())
        pins = json.loads(pins_path.read_text(encoding='utf-8'))
        device_a = sync.load_sync_config(store_a)['device_id']
        self.assertIn(device_a, pins)


class TestPromoteArchived(Batch4Base):
    """Item 5: correction evidence must include archived captures."""

    def test_archived_correction_still_counts(self):
        root = self.init()
        sd = self.store_dir(root)
        self.change(root, 'value = 2\n')
        self.capture(self.store, root, '第一次纠正', session='s1')
        # Correction events ride on captures: attach the marker by hand
        # (capture's correction_of path is exercised through the same
        # header field the promotion scan reads).
        entry = sd / 'raw' / 'cap-0001.md'
        header, body = flow._parse_entry(entry)
        header['correction_of'] = '错误类X'
        flow._write_entry(sd, header, body)
        self.call(flow.summarize, self.store, root, source='test')
        # Archiving moves summarized entries except the latest, so the
        # first entry only lands in raw/archived/ on the third
        # summarize (after the second entry is summarized and is no
        # longer the latest one).
        self.change(root, 'value = 3\n')
        info = flow.capture(self.store, root, tool='codex', session='s2',
                            message='第二次纠正', correction_of='错误类X')
        self.assertFalse(info.get('deduped'))
        self.call(flow.summarize, self.store, root, source='test')
        self.change(root, 'value = 4\n')
        self.capture(self.store, root, '第三次工作', session='s3')
        self.call(flow.summarize, self.store, root, source='test')
        self.assertTrue((sd / 'raw' / 'archived' / 'cap-0001.md').exists())
        count = promote._current_count(
            sd, {'trigger': 'correction', 'evidence_key': '错误类X'})
        self.assertEqual(count, 2)


class TestStopHook(Batch4Base):
    """Items 6 + 8: Stop block reason on stderr, session id to capture."""

    def test_stop_body_threading_and_stderr(self):
        root = self.init()
        cmd = flow._hook_command(self.store, root, 'stop')
        cap_pos = cmd.find('hook_capture')
        self.assertNotEqual(cap_pos, -1)
        self.assertIn('session=_sid', cmd[cap_pos:cap_pos + 200])
        self.assertLess(cmd.find('sys.stdin.read'), cap_pos)
        self.assertIn('file=sys.stderr', cmd)

    def test_stop_run_blocks_on_stderr_with_session(self):
        root = self.init()
        sd = self.store_dir(root)
        self.change(root, 'value = 2\n')
        cmd = flow._hook_command(self.store, root, 'stop')
        proc = subprocess.run(cmd, shell=True, cwd=str(root),
                              input=json.dumps({'session_id': 'sess-stop'}),
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2)
        self.assertIn('补填', proc.stderr)
        self.assertNotIn('补填', proc.stdout)
        entries = flow.list_entries(sd)
        self.assertEqual(entries[-1]['header']['session'], 'sess-stop')


class TestForkManifest(Batch4Base):
    """Item 7: adjudication must refresh the checksum manifest."""

    def test_adjudicate_keeps_manifest_clean(self):
        root = self.init()
        sd = self.store_dir(root)
        (root / 'app.py').write_bytes('value = 2\n'.encode('utf-8'))
        self.call(flow.capture, self.store, root, session='s2', message='做了甲')
        self.call(flow.summarize, self.store, root, source='test')
        relay.update_checksum_manifest(sd)
        present, mismatches = relay.verify_checksum_manifest(sd)
        self.assertTrue(present)
        self.assertEqual(mismatches, [])
        state_path = sd / 'archive' / 'state.md'
        text = state_path.read_text(encoding='utf-8')
        text = fork.append_pending_adjudication(
            text, {'fork': 'br-test', 'field': '正在做什么',
                   'main_value': '汇总 1', 'branch_value': '汇总 2'})
        state_path.write_bytes(text.encode('utf-8'))
        fork.adjudicate(sd, 'br-test', 'branch', '代码现状属实')
        present, mismatches = relay.verify_checksum_manifest(sd)
        self.assertTrue(present)
        self.assertEqual(mismatches, [])


class TestTentativeDecline(Batch4Base):
    """Item 9: declining a tentative goal must archive it first."""

    def test_tentative_goal_archived_before_delete(self):
        root = self.init()
        sd = self.store_dir(root)
        relay.goal_draft(self.store, root, '# 目标\n\n暂定目标文本。\n')
        relay.goal_confirm(self.store, root, tentative=True)
        self.assertTrue((sd / 'state' / 'goal-tentative.md').exists())
        relay.goal_decline(self.store, root)
        self.assertFalse((sd / 'state' / 'goal-tentative.md').exists())
        hist = sd / 'archive' / 'goal-history'
        matches = list(hist.glob('tentative-declined-*.md'))
        self.assertEqual(len(matches), 1)
        self.assertIn('暂定目标文本', matches[0].read_text(encoding='utf-8'))


class TestCodebuddyGuard(Batch4Base):
    """Item 10: installing over a user-authored SKILL.md must refuse."""

    def test_user_skill_refuses_and_identical_reinstalls(self):
        root = self.init()
        skill_dir = root / '.codebuddy' / 'skills' / 'relay'
        skill_dir.mkdir(parents=True)
        (skill_dir / 'SKILL.md').write_text('我自己的 skill\n', encoding='utf-8')
        with self.assertRaises(FlowRelayError):
            flow.install_codebuddy(self.store, root, root, flavor='codebuddy')
        self.assertEqual((skill_dir / 'SKILL.md').read_text(encoding='utf-8'),
                         '我自己的 skill\n')
        (skill_dir / 'SKILL.md').write_bytes(flow.RELAY_SKILL_TEXT.encode('utf-8'))
        info = flow.install_codebuddy(self.store, root, root, flavor='codebuddy')
        self.assertEqual(info['skill_dir'], str(skill_dir))


class TestUnclaimedRepair(Batch4Base):
    """Item 11: unclaimed import writes atomically and repairs damage."""

    def _unclaimed_setup(self):
        store_a, repo_a = self.device('proj-a')
        self.goal(store_a, repo_a, '# 目标\n\n演示。\n')
        self.capture(store_a, repo_a, '第一条', session='s1')
        self.change(repo_a, 'value = 2\n')
        self.capture(store_a, repo_a, '第二条', session='s2')
        bundle = self.root / 'bundle-u'
        sync.export_bundle(store_a, [repo_a], bundle, 'device-a', 'id-a')
        store_u = self.root / 'store-unclaimed'
        store_u.mkdir(parents=True)
        rep = sync.import_bundle(bundle, store_u, confirm=True)
        self.assertTrue(rep['landed'])
        proj = rep['projects'][0]
        dest = store_u / 'unclaimed' / ('%s-%s' % (proj['key'], proj['slug']))
        return bundle, store_u, dest, rep

    def test_truncated_entry_repaired_not_preserved(self):
        bundle, store_u, dest, _ = self._unclaimed_setup()
        raw1 = dest / 'raw' / 'cap-0001.md'
        orig = raw1.read_bytes()
        raw1.write_bytes(orig[:len(orig) // 2])
        rep = sync.import_bundle(bundle, store_u, confirm=True)
        self.assertEqual(raw1.read_bytes(), orig)
        self.assertIn('raw/cap-0001.md', rep['projects'][0].get('repaired', []))
        conflicts = dest / '_conflicts'
        if conflicts.exists():
            self.assertEqual(
                [p for p in conflicts.iterdir() if 'cap-0001' in p.name], [])

    def test_valid_different_entry_still_preserved(self):
        bundle, store_u, dest, _ = self._unclaimed_setup()
        store_c, repo_c = self.device('proj-c')
        self.capture(store_c, repo_c, '外来有效条目', session='s9')
        foreign = (relay.resolve_store_dir(store_c, repo_c)[1]
                   / 'raw' / 'cap-0001.md').read_bytes()
        raw1 = dest / 'raw' / 'cap-0001.md'
        raw1.write_bytes(foreign)
        rep = sync.import_bundle(bundle, store_u, confirm=True)
        self.assertEqual(raw1.read_bytes(), foreign)
        preserved = rep['projects'][0].get('unclaimed_conflicts', [])
        self.assertTrue(any(p.get('file') == 'raw/cap-0001.md' for p in preserved))


if __name__ == '__main__':
    unittest.main()
