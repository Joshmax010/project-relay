"""v2 sync slice 2 tests (plan part 03, steps 2.3-2.5).

Style follows test_relay_sync.py: real temporary directories, real Git
repos, local directories stand in for servers -- no network, no real
server, no real ~/.project-relay/ ever touched (PROJECT_RELAY_HOME is
patched). Sync-raised errors are asserted against relay_sync.relay.
RelayError (importlib double-load). Folder-carrier tests pass the store
explicitly: with PROJECT_RELAY_HOME set, default_store() ignores the
pointer file by design, so pointer-following itself is covered only by
asserting the pointer file's content, not by resolving through it.
"""

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

SKILL = Path(__file__).resolve().parents[1]


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SKILL / 'scripts' / (name + '.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


relay = _load('relay')
flow = _load('relay_flow')
sync = _load('relay_sync')
SyncError = sync.RelayError

REMOTE_URL = 'https://example.com/demo/proj.git'


class Sync2Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='relay-sync2-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
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

    def git(self, root, *args):
        return subprocess.run(['git', '-C', str(root), *args],
                              check=True, capture_output=True)

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

    def device(self, name, remote=REMOTE_URL):
        repo = self.make_repo(name, remote)
        store = self.root / ('store-' + name)
        relay.init(store, repo)
        return store, repo

    def goal(self, store, repo, text='做一个小工具'):
        relay.goal_draft(store, repo, text)
        relay.goal_confirm(store, repo)

    def capture(self, store, repo, message, session='s1'):
        return flow.capture(store, repo, tool='codex', session=session,
                            message=message)

    def store_dir(self, store, repo):
        return relay.resolve_store_dir(store, repo)[1]

    def payload(self, store, repo):
        return {k: v for k, v in
                sync._collect_payload(self.store_dir(store, repo)).items()
                if not k.startswith('receipts/')}

    def key(self, store, repo):
        return relay.resolve_store_dir(store, repo)[0]['key']


class TestFolderCarrier(Sync2Base):
    def test_setup_writes_config_and_pointer(self):
        store, repo = self.device('a')
        result = sync.sync_setup_folder(store, device_name='devA')
        cfg = result['config']
        self.assertEqual(cfg['carrier'], 'folder')
        self.assertNotIn('remote', cfg)
        self.assertEqual(cfg['device_name'], 'devA')
        self.assertTrue(cfg['device_id'])
        pointer = Path(self.root) / '.project-relay-location'
        self.assertEqual(pointer.read_text(encoding='utf-8'), str(store))
        # Reloaded config is accepted by the relaxed loader.
        self.assertEqual(sync.load_sync_config(store)['carrier'], 'folder')

    def test_setup_refuses_when_default_store_has_projects(self):
        default_repo = self.make_repo('default-proj')
        default_store = self.root / '.project-relay'
        relay.init(default_store, default_repo)
        _store2, _repo2 = self.device('b')
        with self.assertRaises(SyncError) as ctx:
            sync.sync_setup_folder(_store2, device_name='devB')
        self.assertIn('不是同一仓', str(ctx.exception))

    def test_folder_pack_then_copy_reads_consistent(self):
        store, repo = self.device('a')
        sync.sync_setup_folder(store, device_name='devA')
        self.goal(store, repo)
        self.capture(store, repo, '实现了登录')
        packed = sync.pack(store, repo)
        self.assertEqual(packed['receipt']['carrier'], 'folder')
        self.assertIsNone(packed['seq'])
        store2 = self.root / 'store-copy'
        shutil.copytree(store, store2)
        dir2 = self.store_dir(store2, repo)
        present, mismatches = relay.verify_checksum_manifest(dir2)
        self.assertTrue(present)
        self.assertEqual(mismatches, [])
        self.assertEqual(self.payload(store, repo), self.payload(store2, repo))
        unpacked = sync.unpack(store2)
        self.assertTrue(unpacked['ready'])
        self.assertEqual(unpacked['ready_problems'], [])

    def test_conflict_copies_isolated_and_legit_untouched(self):
        store, repo = self.device('a')
        sync.sync_setup_folder(store, device_name='devA')
        self.goal(store, repo)
        sd = self.store_dir(store, repo)
        formal_before = (sd / 'archive' / 'goal.md').read_bytes()
        (sd / 'archive' / 'goal （冲突副本）.md').write_bytes('junk\n'.encode('utf-8'))
        (store / 'x (conflicted copy 2026-01-01).md').write_bytes('junk\n'.encode('utf-8'))
        (store / 'y.sync-conflict-20260101-120000-ABCDEF.md').write_bytes('junk\n'.encode('utf-8'))
        (store / 'z (conflict 2026-01-01).md').write_bytes('junk\n'.encode('utf-8'))
        legit = sd / 'state' / 'goal-conflict.json'
        legit.write_bytes('{"at": "x"}\n'.encode('utf-8'))
        result = sync.scan_folder_conflicts(store, isolate=True)
        self.assertEqual(len(result['found']), 4)
        self.assertEqual(len(result['isolated']), 4)
        self.assertIsNotNone(result['report_path'])
        self.assertFalse((sd / 'archive' / 'goal （冲突副本）.md').exists())
        self.assertFalse((store / 'x (conflicted copy 2026-01-01).md').exists())
        self.assertTrue(legit.exists())
        self.assertEqual((sd / 'archive' / 'goal.md').read_bytes(), formal_before)
        # A second scan finds nothing (isolated files live under trash/,
        # which is never scanned).
        again = sync.scan_folder_conflicts(store, isolate=True)
        self.assertEqual(again['found'], [])

    def test_heartbeat_degrades_and_notice_clears(self):
        store, repo = self.device('a')
        sync.sync_setup_folder(store, device_name='devA')
        self.goal(store, repo)
        self.capture(store, repo, '实现了登录')
        sd = self.store_dir(store, repo)
        state_before = (sd / 'archive' / 'state.md').read_bytes()
        (sd / 'heartbeat-otherdevice.json').write_bytes(json.dumps(
            {'device_id': 'otherdevice', 'device_name': 'devB',
             'at': 'now', 'at_epoch': time.time()}, ensure_ascii=False).encode('utf-8'))
        result = flow.summarize(store, repo, source='test')
        self.assertTrue(result.get('degraded'))
        self.assertEqual((sd / 'archive' / 'state.md').read_bytes(), state_before)
        device_id, _name = relay.device_identity()
        tagged = list((sd / 'raw').glob('cap-*-%s.md' % device_id[:8]))
        self.assertEqual(len(tagged), 1)
        self.assertTrue((sd / 'state' / 'device-notice.json').exists())
        rendered = flow.render_resume(flow.resume(store, repo))
        self.assertIn('另一台设备近期在写此项目', rendered)
        self.assertNotIn('同步', rendered)
        # Foreign lock gone: the next summarize succeeds and clears it.
        (sd / 'heartbeat-otherdevice.json').unlink()
        result2 = flow.summarize(store, repo, source='test')
        self.assertNotIn('degraded', result2)
        self.assertFalse((sd / 'state' / 'device-notice.json').exists())

    def test_stale_heartbeat_ignored(self):
        store, repo = self.device('a')
        sync.sync_setup_folder(store, device_name='devA')
        self.goal(store, repo)
        self.capture(store, repo, '实现了登录')
        sd = self.store_dir(store, repo)
        (sd / 'heartbeat-otherdevice.json').write_bytes(json.dumps(
            {'device_id': 'otherdevice', 'device_name': 'devB',
             'at': 'old', 'at_epoch': time.time() - 700}, ensure_ascii=False).encode('utf-8'))
        result = flow.summarize(store, repo, source='test')
        self.assertNotIn('degraded', result)

    def test_checksum_manifest_blocks_and_recovers(self):
        store, repo = self.device('a')
        sync.sync_setup_folder(store, device_name='devA')
        self.goal(store, repo)
        self.capture(store, repo, '实现了登录')
        flow.summarize(store, repo, source='test')
        sd = self.store_dir(store, repo)
        present, mismatches = relay.verify_checksum_manifest(sd)
        self.assertTrue(present)
        self.assertEqual(mismatches, [])
        good = (sd / 'archive' / 'state.md').read_bytes()
        (sd / 'archive' / 'state.md').write_bytes(good + b'\n# tampered\n')
        _present, mismatches = relay.verify_checksum_manifest(sd)
        self.assertTrue(any(m['file'] == 'archive/state.md' for m in mismatches))
        rendered = flow.render_resume(flow.resume(store, repo))
        self.assertIn('校验清单不符', rendered)
        self.assertIn('暂不汇总', rendered)
        self.assertNotIn('同步', rendered)
        (sd / 'archive' / 'state.md').write_bytes(good)
        rendered2 = flow.render_resume(flow.resume(store, repo))
        self.assertNotIn('校验清单不符', rendered2)

    def test_folder_pack_aborts_on_foreign_heartbeat(self):
        store, repo = self.device('a')
        sync.sync_setup_folder(store, device_name='devA')
        self.goal(store, repo)
        self.capture(store, repo, '实现了登录')
        sd = self.store_dir(store, repo)
        (sd / 'heartbeat-otherdevice.json').write_bytes(json.dumps(
            {'device_id': 'otherdevice', 'device_name': 'devB',
             'at': 'now', 'at_epoch': time.time()}, ensure_ascii=False).encode('utf-8'))
        with self.assertRaises(SyncError) as ctx:
            sync.pack(store, repo)
        self.assertIn('另一台设备近期在写此项目', str(ctx.exception))
        self.assertEqual(list((store / 'sync-receipts').glob('pack-*.json')), [])

    def test_folder_pack_summarizes_pending_first(self):
        store, repo = self.device('a')
        sync.sync_setup_folder(store, device_name='devA')
        self.goal(store, repo)
        self.capture(store, repo, '实现了登录')
        packed = sync.pack(store, repo)
        steps = {s['step']: s['status'] for s in packed['receipt']['steps']}
        self.assertEqual(steps[1], 'summarized')
        self.assertEqual(flow.queue_status(
            self.store_dir(store, repo), repair=False)['pending'], 0)

    def test_folder_unpack_checksum_mismatch_not_ready(self):
        store, repo = self.device('a')
        sync.sync_setup_folder(store, device_name='devA')
        self.goal(store, repo)
        self.capture(store, repo, '实现了登录')
        sync.pack(store, repo)
        sd = self.store_dir(store, repo)
        (sd / 'archive' / 'state.md').write_bytes(
            (sd / 'archive' / 'state.md').read_bytes() + b'\n# tampered\n')
        unpacked = sync.unpack(store)
        self.assertFalse(unpacked['ready'])
        self.assertTrue(any('state.md' in p for p in unpacked['ready_problems']))

    def test_rollback_from_bundle(self):
        store, repo = self.device('a')
        sync.sync_setup_folder(store, device_name='devA')
        self.goal(store, repo)
        self.capture(store, repo, '实现了登录')
        flow.summarize(store, repo, source='test')
        bundle_dir = self.root / 'bundle'
        sync.export_bundle(store, [repo], bundle_dir, 'devA', 'devA')
        sd = self.store_dir(store, repo)
        good_goal = (sd / 'archive' / 'goal.md').read_bytes()
        (sd / 'archive' / 'goal.md').write_bytes(good_goal + b'\n# junk\n')
        rolled = sync.rollback_from_bundle(store, repo, bundle_dir)
        self.assertIsNone(rolled['commit'])
        self.assertEqual((sd / 'archive' / 'goal.md').read_bytes(), good_goal)
        self.assertTrue((sd / 'state' / 'rollback-note.json').exists())
        trash = Path(rolled['trash_dir'])
        self.assertTrue(any('goal.md' in p.as_posix() for p in trash.rglob('*')))
        present, mismatches = relay.verify_checksum_manifest(sd)
        self.assertTrue(present)
        self.assertEqual(mismatches, [])
        # Zip form works too.
        (sd / 'archive' / 'goal.md').write_bytes(good_goal + b'\n# junk2\n')
        zip_path = self.root / 'bundle.zip'
        sync.write_bundle_zip(bundle_dir, zip_path)
        sync.rollback_from_bundle(store, repo, zip_path)
        self.assertEqual((sd / 'archive' / 'goal.md').read_bytes(), good_goal)
        # A tampered bundle is refused with zero writes.
        bad = self.root / 'bundle-bad'
        shutil.copytree(bundle_dir, bad)
        key = self.key(store, repo)
        slug = relay.resolve_store_dir(store, repo)[0]['slug']
        target = bad / 'projects' / ('%s-%s' % (key, slug)) / 'archive' / 'goal.md'
        target.write_bytes(target.read_bytes() + b'\n# evil\n')
        before = (sd / 'archive' / 'goal.md').read_bytes()
        with self.assertRaises(SyncError):
            sync.rollback_from_bundle(store, repo, bad)
        self.assertEqual((sd / 'archive' / 'goal.md').read_bytes(), before)

    def test_rollback_dispatch_errors(self):
        store, repo = self.device('a')
        sync.sync_setup_folder(store, device_name='devA')
        self.goal(store, repo)
        with self.assertRaises(SyncError) as ctx:
            sync.sync_rollback(store, repo, ref=None, bundle=None)
        self.assertIn('--bundle', str(ctx.exception))
        store_g, repo_g = self.device('g')
        sync.sync_setup(store_g, str(self.bare), device_name='devG')
        with self.assertRaises(SyncError) as ctx2:
            sync.sync_rollback(store_g, repo_g, ref=None, bundle=self.root / 'x')
        self.assertIn('--bundle 仅用于', str(ctx2.exception))


class TestServerCarrier(Sync2Base):
    def _export(self, store, repo, name, device='devA', pack_seq=None):
        bundle_dir = self.root / name
        pack_seqs = None
        if pack_seq is not None:
            pack_seqs = {self.key(store, repo): pack_seq}
        sync.export_bundle(store, [repo], bundle_dir, device, device,
                           pack_seqs=pack_seqs)
        return bundle_dir

    def _payload_minus_baseline(self, store, repo):
        """Cross-device payload comparison: archive/state.md's baseline
        line names the local checkout's HEAD, which legitimately differs
        per device (merge keeps the local baseline by design)."""
        return {k: v for k, v in self.payload(store, repo).items()
                if k != 'archive/state.md'}

    def test_upload_download_roundtrip(self):
        store_a, repo_a = self.device('a')
        self.goal(store_a, repo_a)
        self.capture(store_a, repo_a, '实现了登录')
        flow.summarize(store_a, repo_a, source='test')
        bundle = self._export(store_a, repo_a, 'bundle-a', pack_seq=1)
        srv = self.root / 'server'
        backend = sync.LocalDirBackend(srv)
        uploaded = sync.upload_server_bundle(backend, self.key(store_a, repo_a),
                                             bundle, 1, 'devA', device_name='devA')
        self.assertEqual(uploaded['status'], 'uploaded')
        dest = self.root / 'downloaded'
        sync.download_server_bundle(backend, self.key(store_a, repo_a), dest)
        manifest, _files, _warnings = sync.verify_bundle(dest)
        self.assertEqual(manifest['projects'][0]['pack_seq'], 1)
        store_b, repo_b = self.device('b')
        report = sync.import_bundle(dest, store_b, confirm=True)
        self.assertEqual(report['projects'][0]['status'], 'matched')
        self.assertEqual(self._payload_minus_baseline(store_a, repo_a),
                         self._payload_minus_baseline(store_b, repo_b))
        self.assertEqual(
            self.payload(store_a, repo_a)['archive/goal.md'],
            self.payload(store_b, repo_b)['archive/goal.md'])

    def test_upload_interrupt_keeps_formal(self):
        store_a, repo_a = self.device('a')
        self.goal(store_a, repo_a)
        bundle1 = self._export(store_a, repo_a, 'bundle-1')
        srv = self.root / 'server'
        backend = sync.LocalDirBackend(srv)
        key = self.key(store_a, repo_a)
        sync.upload_server_bundle(backend, key, bundle1, 1, 'devA')
        before = sync._read_server_current(backend, key)
        self.capture(store_a, repo_a, '改了点东西')
        flow.summarize(store_a, repo_a, source='test')
        bundle2 = self._export(store_a, repo_a, 'bundle-2')
        faulty = sync.FaultBackend(sync.LocalDirBackend(srv), put_fail_after=5)
        with self.assertRaises(SyncError):
            sync.upload_server_bundle(faulty, key, bundle2, 2, 'devA')
        after = sync._read_server_current(backend, key)
        self.assertEqual(after['seq'], before['seq'])
        self.assertEqual(after['bundle_hash'], before['bundle_hash'])

    def test_anti_overwrite_key(self):
        store_a, repo_a = self.device('a')
        self.goal(store_a, repo_a)
        bundle1 = self._export(store_a, repo_a, 'bundle-1')
        srv = self.root / 'server'
        backend = sync.LocalDirBackend(srv)
        key = self.key(store_a, repo_a)
        sync.upload_server_bundle(backend, key, bundle1, 2, 'devA')
        self.capture(store_a, repo_a, '新内容')
        flow.summarize(store_a, repo_a, source='test')
        bundle2 = self._export(store_a, repo_a, 'bundle-2')
        # Older seq from another device: refused.
        with self.assertRaises(SyncError) as ctx:
            sync.upload_server_bundle(backend, key, bundle2, 1, 'devB')
        self.assertIn('先在本机拉取最新包合并', str(ctx.exception))
        # Same seq, different device: refused.
        with self.assertRaises(SyncError):
            sync.upload_server_bundle(backend, key, bundle2, 2, 'devB')
        # Same device, same seq, different bytes: own retry, allowed.
        sync.upload_server_bundle(backend, key, bundle2, 2, 'devA')
        # Identical retransmit: idempotent.
        again = sync.upload_server_bundle(backend, key, bundle2, 2, 'devA')
        self.assertEqual(again['status'], 'unchanged')

    def test_download_tampered_rejected(self):
        store_a, repo_a = self.device('a')
        self.goal(store_a, repo_a)
        bundle = self._export(store_a, repo_a, 'bundle-1')
        srv = self.root / 'server'
        backend = sync.LocalDirBackend(srv)
        key = self.key(store_a, repo_a)
        slug = relay.resolve_store_dir(store_a, repo_a)[0]['slug']
        sync.upload_server_bundle(backend, key, bundle, 1, 'devA')
        target = srv / 'relay' / key / 'current' / 'projects' / ('%s-%s' % (key, slug)) \
            / 'archive' / 'goal.md'
        target.write_bytes(target.read_bytes() + b'\n# evil\n')
        before = self.payload(store_a, repo_a)
        with self.assertRaises(SyncError):
            sync.download_server_bundle(backend, key, self.root / 'dl')
        self.assertEqual(self.payload(store_a, repo_a), before)

    def test_server_pack_seq_only_on_success(self):
        store_a, repo_a = self.device('a')
        sync.sync_setup_server(store_a, 'local', 'local:' + str(self.root / 'server'),
                               device_name='devA')
        self.goal(store_a, repo_a)
        self.capture(store_a, repo_a, '实现了登录')
        good = sync.LocalDirBackend(self.root / 'server')
        packed1 = sync.pack_server(store_a, repo_a, backend=good)
        self.assertEqual(packed1['seq'], 1)
        seq_path = self.store_dir(store_a, repo_a) / 'state' / 'pack-seq.json'
        self.assertEqual(json.loads(seq_path.read_text(encoding='utf-8'))['next_seq'], 2)
        self.capture(store_a, repo_a, '又改了点')
        faulty = sync.FaultBackend(sync.LocalDirBackend(self.root / 'server'),
                                   put_fail_after=5)
        with self.assertRaises(SyncError):
            sync.pack_server(store_a, repo_a, backend=faulty)
        self.assertEqual(json.loads(seq_path.read_text(encoding='utf-8'))['next_seq'], 2)
        packed2 = sync.pack_server(store_a, repo_a, backend=good)
        self.assertEqual(packed2['seq'], 2)  # no skip after the failed pack

    def test_server_pack_unpack_flow_ready(self):
        store_a, repo_a = self.device('a')
        srv = self.root / 'server'
        sync.sync_setup_server(store_a, 'local', 'local:' + str(srv),
                               device_name='devA')
        self.goal(store_a, repo_a)
        self.capture(store_a, repo_a, '实现了登录')
        sync.pack_server(store_a, repo_a, backend=sync.LocalDirBackend(srv))
        store_b, repo_b = self.device('b')
        sync.sync_setup_server(store_b, 'local', 'local:' + str(srv),
                               device_name='devB')
        unpacked = sync.unpack_server(store_b, confirm=True,
                                      backend=sync.LocalDirBackend(srv))
        self.assertTrue(unpacked['landed'])
        self.assertTrue(unpacked['ready'])
        self.assertEqual(unpacked['ready_problems'], [])
        self.assertEqual(relay.goal_status(store_b, repo_b)[0], 'imported')
        self.assertEqual(self._payload_minus_baseline(store_a, repo_a),
                         self._payload_minus_baseline(store_b, repo_b))

    def test_ready_gate_blocks_tampered(self):
        store, repo = self.device('a')
        self.goal(store, repo)
        row = relay.resolve_store_dir(store, repo)[0]
        ready, problems = sync._ready_gate(store, [row])
        self.assertTrue(ready)
        sd = self.store_dir(store, repo)
        (sd / 'archive' / 'goal.md').write_bytes(
            (sd / 'archive' / 'goal.md').read_bytes() + b'\n# tampered\n')
        self.assertEqual(relay.goal_status(store, repo)[0], 'tampered')
        ready2, problems2 = sync._ready_gate(store, [row])
        self.assertFalse(ready2)
        self.assertTrue(any('tampered' in p for p in problems2))


class TestSyncTiming(Sync2Base):
    def test_no_sync_wording_in_same_machine_flows(self):
        store, repo = self.device('a')
        self.goal(store, repo)
        self.capture(store, repo, '实现了登录')
        flow.summarize(store, repo, source='test')
        rendered = flow.render_resume(flow.resume(store, repo))
        self.assertNotIn('同步', rendered)
        injected = flow.inject(store, repo)
        self.assertNotIn('同步', injected['text'])

    def test_resume_no_archive_remote_tip(self):
        repo_remote = self.make_repo('with-remote', remote=REMOTE_URL)
        store = self.root / 'store-tip'
        result = flow.resume(store, repo_remote)
        self.assertTrue(result.get('no_archive'))
        self.assertIn('请回旧设备执行 pack', result['message'])
        self.assertNotIn('同步', result['message'])
        repo_plain = self.make_repo('plain')
        result2 = flow.resume(store, repo_plain)
        self.assertTrue(result2.get('no_archive'))
        self.assertNotIn('请回旧设备执行 pack', result2['message'])

    def test_format_minor_bump_old_bundle_still_imports(self):
        store_a, repo_a = self.device('a')
        self.goal(store_a, repo_a)
        bundle = self.root / 'bundle-old'
        sync.export_bundle(store_a, [repo_a], bundle, 'devA', 'devA')
        manifest_path = bundle / 'manifest.json'
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        manifest['format_minor'] = 0
        manifest_path.write_bytes(json.dumps(manifest, ensure_ascii=False).encode('utf-8'))
        store_b, repo_b = self.device('b')
        report = sync.import_bundle(bundle, store_b, confirm=False)
        self.assertTrue(report['warnings'])
        self.assertEqual(report['projects'][0]['status'], 'matched')

    def test_load_sync_config_accepts_carriers(self):
        store, repo = self.device('a')
        for cfg in ({'carrier': 'folder', 'device_id': 'x'},
                    {'carrier': 'server', 'server_type': 'local',
                     'server_url': 'local:/tmp/x'},
                    {'remote': 'https://example.com/x.git'}):
            relay.save(store / 'sync-config.json', cfg)
            self.assertIsNotNone(sync.load_sync_config(store))
        relay.save(store / 'sync-config.json', {'device_id': 'x'})
        with self.assertRaises(SyncError):
            sync.load_sync_config(store)

    def test_status_carrier_field(self):
        store, repo = self.device('a')
        sync.sync_setup_folder(store, device_name='devA')
        self.goal(store, repo)
        sync.pack(store, repo)
        row = sync.sync_status(store, repo)[0]
        self.assertEqual(row['carrier'], 'folder')
        self.assertTrue(row['packed'])
        self.assertIsNone(row['last_pack_seq'])
        store_g, repo_g = self.device('g')
        sync.sync_setup(store_g, str(self.bare), device_name='devG')
        self.goal(store_g, repo_g)
        sync.pack(store_g, repo_g)
        row_g = sync.sync_status(store_g, repo_g)[0]
        self.assertEqual(row_g['carrier'], 'git')
        self.assertEqual(row_g['last_pack_seq'], 1)

    def test_cli_status_and_folder_unpack_print_ready(self):
        store, repo = self.device('a')
        sync.sync_setup_folder(store, device_name='devA')
        self.goal(store, repo)
        self.capture(store, repo, '实现了登录')
        sync.pack(store, repo)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = relay.main(['--store-root', str(store), '--root', str(repo),
                               'sync-status'])
        self.assertEqual(code, 0)
        self.assertIn('载体 folder', buf.getvalue())
        buf2 = io.StringIO()
        with contextlib.redirect_stdout(buf2):
            code2 = relay.main(['--store-root', str(store), '--root', str(repo),
                                'unpack'])
        self.assertEqual(code2, 0)
        self.assertIn('接力就绪', buf2.getvalue())


if __name__ == '__main__':
    unittest.main()
