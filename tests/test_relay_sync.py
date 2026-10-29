"""v2 sync tests (plan part 03, steps 2.1-2.2 + S6 on the v1 layout).

Style follows the old suite: real temporary directories, real Git repos,
local bare repos stand in for remotes -- no network, no real remote ever
touched. Each device gets its own store root; PROJECT_RELAY_HOME is
patched as a belt and braces so no test ever touches the developer's
real ~/.project-relay/. Git identity comes from env vars (no global
config assumed). Sync-raised errors are asserted against
relay_sync.relay.RelayError (the importlib double-load means it is not
the relay module's class).
"""

import base64
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
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
OTHER_URL = 'https://example.com/other/proj.git'


class SyncBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='relay-sync-')
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

    def change_code(self, repo, text):
        (repo / 'app.py').write_bytes(text.encode('utf-8'))
        self.git(repo, 'add', '.')
        self.git(repo, 'commit', '-m', 'change')

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

    def store_dir(self, store, repo):
        return relay.resolve_store_dir(store, repo)[1]

    def payload(self, store, repo):
        # Receipts are info-only by design (S4): they are never copied
        # into the local receipts/ dir, so roundtrip comparisons exclude
        # them and compare exactly the merged file classes.
        return {k: v for k, v in
                sync._collect_payload(self.store_dir(store, repo)).items()
                if not k.startswith('receipts/')}

    def tree(self, store):
        return {p.relative_to(store).as_posix(): p.read_bytes()
                for p in store.rglob('*')
                if p.is_file() and '.git' not in p.parts}

    def entry_messages(self, store, repo):
        entries = sync._parse_local_entries(self.store_dir(store, repo))
        return sorted(e['header'].get('user_message') or ''
                      for e in entries.values())


class TestExportImport(SyncBase):
    def test_roundtrip_bytes_identical(self):
        store_a, repo_a = self.device('proj-a')
        self.goal(store_a, repo_a, '# 目标\n\n做一个同步演示项目。\n')
        self.capture(store_a, repo_a, '实现了登录接口')
        self.change_code(repo_a, 'value = 2\n')
        self.capture(store_a, repo_a, '修复了登录接口的空指针')
        store_b, repo_b = self.device('proj-b')
        bundle = self.root / 'bundle'
        sync.export_bundle(store_a, [repo_a], bundle, 'device-a', 'id-a')
        report = sync.import_bundle(bundle, store_b, confirm=True)
        self.assertTrue(report['landed'])
        self.assertEqual(report['projects'][0]['status'], 'matched')
        self.assertEqual(self.payload(store_b, repo_b),
                         self.payload(store_a, repo_a))
        self.assertEqual(relay.goal_status(store_b, repo_b)[0], 'imported')

    def test_goal_imported_banner_and_one_click_confirm(self):
        store_a, repo_a = self.device('proj-a')
        self.goal(store_a, repo_a, '# 目标\n\n做一个同步演示项目。\n')
        self.setup(store_a, device_name='device-a')
        sync.pack(store_a, repo_a)
        store_b, repo_b = self.device('proj-b')
        self.setup(store_b, device_name='device-b')
        sync.unpack(store_b, confirm=True)
        self.assertEqual(relay.goal_status(store_b, repo_b)[0], 'imported')
        injected = relay.goal_inject(store_b, repo_b)
        self.assertTrue(injected.startswith('【目标待本机确认】'))
        self.assertIn('做一个同步演示项目', injected)
        self.assertEqual(relay.goal_check(store_b, repo_b)[0], 'imported')
        info = relay._confirm_imported(store_b, repo_b)
        self.assertEqual(info['receipt']['channel'], 'import')
        self.assertEqual(info['receipt']['imported_from']['source_device'], 'device-a')
        self.assertEqual(relay.goal_status(store_b, repo_b)[0], 'confirmed')
        report = sync.unpack(store_b, confirm=True)
        actions = [p.get('goal_action')
                   for r in report['reports'] for p in r['report']['projects']]
        self.assertIn('skip', actions)

    def test_tampered_byte_rejected_with_filename_and_zero_writes(self):
        store_a, repo_a = self.device('proj-a')
        self.goal(store_a, repo_a, '# 目标\n\n演示。\n')
        store_b, repo_b = self.device('proj-b')
        bundle = self.root / 'bundle'
        sync.export_bundle(store_a, [repo_a], bundle, 'device-a', 'id-a')
        key = relay.resolve_store_dir(store_a, repo_a)[0]['key']
        target = bundle / 'projects' / (key + '-' + repo_a.name) / 'archive' / 'state.md'
        target.write_bytes(target.read_bytes() + b'X')
        before = self.tree(store_b)
        with self.assertRaises(SyncError) as ctx:
            sync.import_bundle(bundle, store_b, confirm=True)
        self.assertIn('archive/state.md', str(ctx.exception))
        self.assertEqual(self.tree(store_b), before)

    def test_major_rejected_minor_tolerated(self):
        store_a, repo_a = self.device('proj-a')
        self.goal(store_a, repo_a, '# 目标\n\n演示。\n')
        store_b, repo_b = self.device('proj-b')
        bundle = self.root / 'bundle'
        sync.export_bundle(store_a, [repo_a], bundle, 'device-a', 'id-a')
        manifest_path = bundle / 'manifest.json'
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        manifest['format_major'] = 2
        manifest_path.write_bytes(json.dumps(manifest).encode('utf-8'))
        with self.assertRaises(SyncError) as ctx:
            sync.import_bundle(bundle, store_b, confirm=True)
        self.assertIn('主版本', str(ctx.exception))
        manifest['format_major'] = 1
        manifest['format_minor'] = 9
        manifest_path.write_bytes(json.dumps(manifest).encode('utf-8'))
        report = sync.import_bundle(bundle, store_b, confirm=True)
        self.assertTrue(report['landed'])
        self.assertTrue(any('次版本' in w for w in report['warnings']))

    def test_zip_roundtrip(self):
        store_a, repo_a = self.device('proj-a')
        self.goal(store_a, repo_a, '# 目标\n\n演示。\n')
        self.capture(store_a, repo_a, '实现了登录接口')
        store_b, repo_b = self.device('proj-b')
        bundle = self.root / 'bundle'
        sync.export_bundle(store_a, [repo_a], bundle, 'device-a', 'id-a')
        zip_path = sync.write_bundle_zip(bundle, self.root / 'bundle.zip')
        restored = sync.read_bundle_zip(zip_path, self.root / 'restored')
        sync.import_bundle(restored, store_b, confirm=True)
        self.assertEqual(self.payload(store_b, repo_b),
                         self.payload(store_a, repo_a))

    def test_redaction_rescan_repairs_local(self):
        store_a, repo_a = self.device('proj-a')
        self.goal(store_a, repo_a, '# 目标\n\n演示。\n')
        secret = 'sk-abcdefghij1234567890'
        decisions = self.store_dir(store_a, repo_a) / 'archive' / 'decisions.md'
        decisions.write_bytes((decisions.read_text(encoding='utf-8')
                             + '\n密钥在代码里：' + secret + '\n').encode('utf-8'))
        store_b, repo_b = self.device('proj-b')
        bundle = self.root / 'bundle'
        manifest = sync.export_bundle(store_a, [repo_a], bundle, 'device-a', 'id-a')
        self.assertGreaterEqual(manifest['redacted_count'], 1)
        self.assertNotIn(secret, decisions.read_text(encoding='utf-8'))
        sync.import_bundle(bundle, store_b, confirm=True)
        imported = self.store_dir(store_b, repo_b) / 'archive' / 'decisions.md'
        self.assertNotIn(secret, imported.read_text(encoding='utf-8'))

    def test_same_key_different_body_conflict_keeps_both(self):
        store_a, repo_a = self.device('proj-a')
        self.goal(store_a, repo_a, '# 目标\n\n演示。\n')
        self.capture(store_a, repo_a, '实现了登录接口')
        store_b, repo_b = self.device('proj-b')
        bundle = self.root / 'bundle'
        sync.export_bundle(store_a, [repo_a], bundle, 'device-a', 'id-a')
        sync.import_bundle(bundle, store_b, confirm=True)
        key = relay.resolve_store_dir(store_a, repo_a)[0]['key']
        proj_dir = bundle / 'projects' / (key + '-' + repo_a.name)
        entry_path = proj_dir / 'raw' / 'cap-0001.md'
        text = entry_path.read_text(encoding='utf-8')
        edited = text + '\n<!-- 本地补充一行 -->\n'
        entry_path.write_bytes(edited.encode('utf-8'))
        manifest_path = bundle / 'manifest.json'
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        rel = 'projects/%s-%s/raw/cap-0001.md' % (key, repo_a.name)
        manifest['files'][rel] = sync._sha256(edited.encode('utf-8'))
        manifest_path.write_bytes(json.dumps(manifest).encode('utf-8'))
        report = sync.import_bundle(bundle, store_b, confirm=True)
        entry = report['projects'][0]
        self.assertEqual(entry['counts']['entries_conflicted'], 1)
        self.assertTrue(any(c['rule'] == 'entry-same-key-different-body'
                            for c in entry['conflicts']))
        # Read files directly: same-key duplicates share one merge key,
        # so the dict enumeration would collapse them (last wins).
        raw_dir = self.store_dir(store_b, repo_b) / 'raw'
        bodies = []
        for f in sorted(raw_dir.glob('cap-*.md')):
            _header, body = sync._parse_entry_text(f.read_text(encoding='utf-8'))
            bodies.append(body)
        self.assertIn(text.split('\n---relay-body---\n', 1)[1], bodies)
        self.assertIn(edited.split('\n---relay-body---\n', 1)[1], bodies)

    def test_conflict_marker_file_excluded_and_preserved(self):
        store_a, repo_a = self.device('proj-a')
        self.goal(store_a, repo_a, '# 目标\n\n演示。\n')
        store_b, repo_b = self.device('proj-b')
        bundle = self.root / 'bundle'
        sync.export_bundle(store_a, [repo_a], bundle, 'device-a', 'id-a')
        key = relay.resolve_store_dir(store_a, repo_a)[0]['key']
        proj_dir = bundle / 'projects' / (key + '-' + repo_a.name)
        decisions_path = proj_dir / 'archive' / 'decisions.md'
        marked = decisions_path.read_text(encoding='utf-8') + '\n<<<<<<< HEAD\n'
        decisions_path.write_bytes(marked.encode('utf-8'))
        manifest_path = bundle / 'manifest.json'
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        rel = 'projects/%s-%s/archive/decisions.md' % (key, repo_a.name)
        manifest['files'][rel] = sync._sha256(marked.encode('utf-8'))
        manifest_path.write_bytes(json.dumps(manifest).encode('utf-8'))
        report = sync.import_bundle(bundle, store_b, confirm=True)
        entry = report['projects'][0]
        self.assertIn('archive/decisions.md', entry['marker_files'])
        self.assertTrue(any(d['reason'] == 'conflict-markers'
                            for d in entry['discarded']))
        local_decisions = self.store_dir(store_b, repo_b) / 'archive' / 'decisions.md'
        self.assertNotIn('<<<<<<<', local_decisions.read_text(encoding='utf-8'))
        log_path = Path(entry['merge_log'])
        preserved = Path(str(log_path)[:-5]) / 'archive__decisions.md'
        self.assertTrue(preserved.exists())
        self.assertIn('<<<<<<<', preserved.read_text(encoding='utf-8'))
        self.assertEqual(entry['goal_action'], 'adopt')

    def test_goal_conflict_keeps_local_and_flags(self):
        store_a, repo_a = self.device('proj-a')
        self.goal(store_a, repo_a, '# 目标A\n\nA 的目标。\n')
        store_b, repo_b = self.device('proj-b')
        self.goal(store_b, repo_b, '# 目标B\n\nB 的目标。\n')
        self.setup(store_a, device_name='device-a')
        sync.pack(store_a, repo_a)
        self.setup(store_b, device_name='device-b')
        report = sync.unpack(store_b, confirm=True)
        entry = report['reports'][0]['report']['projects'][0]
        self.assertEqual(entry['goal_action'], 'conflict')
        self.assertTrue(any(c['rule'] == 'goal-never-auto-merge'
                            for c in entry['conflicts']))
        local_goal = self.store_dir(store_b, repo_b) / 'archive' / 'goal.md'
        self.assertIn('目标B', local_goal.read_text(encoding='utf-8'))
        self.assertTrue((self.store_dir(store_b, repo_b) / 'state'
                         / 'goal-conflict.json').exists())
        self.assertEqual(relay.goal_status(store_b, repo_b)[0], 'confirmed')


class TestGitCarrier(SyncBase):
    def test_pack_unpack_bidirectional(self):
        store_a, repo_a = self.device('proj-a')
        self.goal(store_a, repo_a, '# 目标\n\n演示。\n')
        self.capture(store_a, repo_a, '实现了登录接口')
        self.setup(store_a, device_name='device-a')
        sync.pack(store_a, repo_a)
        store_b, repo_b = self.device('proj-b')
        self.setup(store_b, device_name='device-b')
        sync.unpack(store_b, confirm=True)
        self.assertIn('实现了登录接口', self.entry_messages(store_b, repo_b))
        self.change_code(repo_b, 'value = 2\n')
        self.capture(store_b, repo_b, 'B 设备补充了缓存策略说明', session='s2')
        sync.pack(store_b, repo_b)
        sync.unpack(store_a, confirm=True)
        self.assertIn('B 设备补充了缓存策略说明',
                      self.entry_messages(store_a, repo_a))

    def test_dry_run_zero_writes(self):
        store_a, repo_a = self.device('proj-a')
        self.goal(store_a, repo_a, '# 目标\n\n演示。\n')
        self.setup(store_a, device_name='device-a')
        sync.pack(store_a, repo_a)
        store_b, repo_b = self.device('proj-b')
        self.setup(store_b, device_name='device-b')
        before = self.tree(store_b)
        report = sync.unpack(store_b, confirm=False)
        self.assertFalse(report['landed'])
        self.assertEqual(self.tree(store_b), before)
        self.assertEqual(report['reports'][0]['report']['projects'][0]['goal_action'],
                         'adopt')

    def test_pack_summarizes_pending(self):
        store_a, repo_a = self.device('proj-a')
        self.goal(store_a, repo_a, '# 目标\n\n演示。\n')
        self.capture(store_a, repo_a, '实现了登录接口')
        self.change_code(repo_a, 'value = 2\n')
        self.capture(store_a, repo_a, '修复了登录接口的空指针')
        self.setup(store_a, device_name='device-a')
        receipt = sync.pack(store_a, repo_a)
        self.assertIsNotNone(receipt['receipt']['summary_seq'])
        self.assertGreaterEqual(receipt['receipt']['summary_count'], 1)
        store_b, repo_b = self.device('proj-b')
        self.setup(store_b, device_name='device-b')
        sync.unpack(store_b, confirm=True)
        status = flow.queue_status(self.store_dir(store_b, repo_b), repair=False)
        self.assertEqual(status['pending'], 0)
        self.assertGreaterEqual(status['summarized'], 1)

    def test_pack_seq_and_receipts_and_status(self):
        store_a, repo_a = self.device('proj-a')
        self.goal(store_a, repo_a, '# 目标\n\n演示。\n')
        self.setup(store_a, device_name='device-a')
        sync.pack(store_a, repo_a)
        self.capture(store_a, repo_a, '实现了登录接口')
        sync.pack(store_a, repo_a)
        seq = json.loads((self.store_dir(store_a, repo_a) / 'state'
                          / 'pack-seq.json').read_text(encoding='utf-8'))
        self.assertEqual(seq['next_seq'], 3)
        receipts = sorted((self.root / 'store-proj-a' / 'sync-receipts').glob('pack-*.json'))
        self.assertEqual(len(receipts), 2)
        status = sync.sync_status(store_a, repo_a)
        self.assertEqual(len(status), 1)
        self.assertEqual(status[0]['last_pack_seq'], 2)
        self.assertEqual(status[0]['new_entries_since_pack'], 0)
        self.capture(store_a, repo_a, '又补充了一条', session='s2')
        status = sync.sync_status(store_a, repo_a)
        self.assertEqual(status[0]['new_entries_since_pack'], 1)

    def test_pack_step0_pull_merge_no_clobber(self):
        store_a, repo_a = self.device('proj-a')
        self.goal(store_a, repo_a, '# 目标\n\n演示。\n')
        self.capture(store_a, repo_a, '实现了登录接口')
        self.setup(store_a, device_name='device-a')
        sync.pack(store_a, repo_a)
        store_b, repo_b = self.device('proj-b')
        self.setup(store_b, device_name='device-b')
        sync.unpack(store_b, confirm=True)
        self.change_code(repo_a, 'value = 2\n')
        self.capture(store_a, repo_a, 'A 补充了鉴权说明', session='s2')
        sync.pack(store_a, repo_a)
        sync.pack(store_b, repo_b)
        self.assertIn('A 补充了鉴权说明', self.entry_messages(store_b, repo_b))
        store_c, repo_c = self.device('proj-c')
        self.setup(store_c, device_name='device-c')
        sync.unpack(store_c, confirm=True)
        self.assertIn('A 补充了鉴权说明', self.entry_messages(store_c, repo_c))

    def test_setup_claim_lists(self):
        store_a, repo_a = self.device('proj-a')
        self.goal(store_a, repo_a, '# 目标\n\n演示。\n')
        self.setup(store_a, device_name='device-a')
        sync.pack(store_a, repo_a)
        store_b, repo_b = self.device('proj-b')
        result_b = self.setup(store_b, device_name='device-b')
        self.assertTrue(any(c['status'] == 'matched' for c in result_b['claims']))
        store_c, repo_c = self.device('proj-c', remote=OTHER_URL)
        result_c = self.setup(store_c, device_name='device-c')
        self.assertTrue(any(c['status'] == 'unclaimed' for c in result_c['claims']))

    def test_unclaimed_then_claim(self):
        store_a, repo_a = self.device('proj-a')
        self.goal(store_a, repo_a, '# 目标\n\n演示。\n')
        self.setup(store_a, device_name='device-a')
        sync.pack(store_a, repo_a)
        key = relay.resolve_store_dir(store_a, repo_a)[0]['key']
        store_c, repo_c = self.device('proj-c', remote=OTHER_URL)
        self.setup(store_c, device_name='device-c')
        report = sync.unpack(store_c, confirm=True)
        self.assertEqual(report['reports'][0]['report']['projects'][0]['status'],
                         'unclaimed')
        unclaimed_dir = store_c / 'unclaimed' / (key + '-' + repo_a.name)
        self.assertTrue((unclaimed_dir / '_claim.json').exists())
        repo_p = self.make_repo('proj-p', REMOTE_URL)
        relay.init(store_c, repo_p)
        before = self.tree(store_c)
        preview = sync.sync_claim(store_c, key, repo_p, confirm=False)
        self.assertFalse(preview['landed'])
        self.assertEqual(self.tree(store_c), before)
        landed = sync.sync_claim(store_c, key, repo_p, confirm=True)
        self.assertTrue(landed['landed'])
        self.assertFalse(unclaimed_dir.exists())
        self.assertEqual(relay.goal_status(store_c, repo_p)[0], 'imported')

    def test_rollback_restores_and_notes_merge(self):
        store_a, repo_a = self.device('proj-a')
        self.goal(store_a, repo_a, '# 目标\n\n演示。\n')
        self.setup(store_a, device_name='device-a')
        pack_a = sync.pack(store_a, repo_a)
        store_b, repo_b = self.device('proj-b')
        self.setup(store_b, device_name='device-b')
        sync.unpack(store_b, confirm=True)
        state_path = self.store_dir(store_a, repo_a) / 'archive' / 'state.md'
        packed_state = state_path.read_bytes()
        state_path.write_bytes((state_path.read_text(encoding='utf-8')
                              + '\n<!-- 本地手改一行 -->\n').encode('utf-8'))
        rolled = sync.sync_rollback(store_a, repo_a, ref=pack_a['commit'],
                                    reason='手改验证回退')
        self.assertEqual(state_path.read_bytes(), packed_state)
        self.assertTrue(Path(rolled['backup_dir']).is_dir())
        note = json.loads((self.store_dir(store_a, repo_a) / 'state'
                           / 'rollback-note.json').read_text(encoding='utf-8'))
        self.assertEqual(note['ref'], pack_a['commit'])
        self.change_code(repo_b, 'value = 2\n')
        self.capture(store_b, repo_b, 'B 补充了缓存策略说明', session='s2')
        sync.pack(store_b, repo_b)
        sync.unpack(store_a, confirm=True)
        logs = sorted((store_a / 'sync-receipts').glob('merge-*.json'))
        latest = json.loads(logs[-1].read_text(encoding='utf-8'))
        self.assertIn('rollback_note', latest)
        self.assertEqual(latest['rollback_note']['ref'], pack_a['commit'])

    def test_health_partial_transaction_blocks_pack(self):
        store_a, repo_a = self.device('proj-a')
        self.goal(store_a, repo_a, '# 目标\n\n演示。\n')
        self.setup(store_a, device_name='device-a')
        store_dir = self.store_dir(store_a, repo_a)
        goal_bytes = (store_dir / 'archive' / 'goal.md').read_bytes()
        state_after = '# 当前状态\n\n已应用的改动。\n'.encode('utf-8')
        (store_dir / 'archive' / 'state.md').write_bytes(state_after)
        tx = {'files': {
            'goal.md': {'before_b64': base64.b64encode(goal_bytes).decode('ascii'),
                        'after_sha': sync.digest(b'other goal')},
            'state.md': {'before_b64': base64.b64encode(b'old state').decode('ascii'),
                         'after_sha': sync.digest(state_after)},
        }}
        (store_dir / 'tx').mkdir(parents=True, exist_ok=True)
        (store_dir / 'tx' / '0001.json').write_bytes(json.dumps(tx).encode('utf-8'))
        problems, _warnings = sync.health_check(store_dir)
        self.assertTrue(any('未恢复事务' in p for p in problems))
        with self.assertRaises(SyncError) as ctx:
            sync.pack(store_a, repo_a)
        self.assertIn('健康检查', str(ctx.exception))
        store_b, repo_b = self.device('proj-b')
        self.capture(store_b, repo_b, '实现了登录接口')
        flow.summarize(store_b, repo_b, source='test', semantic_text=None)
        problems, _warnings = sync.health_check(self.store_dir(store_b, repo_b))
        self.assertEqual(problems, [])

    def test_resume_has_no_sync_words(self):
        store_a, repo_a = self.device('proj-a')
        self.goal(store_a, repo_a, '# 目标\n\n演示。\n')
        self.setup(store_a, device_name='device-a')
        sync.pack(store_a, repo_a)
        store_b, repo_b = self.device('proj-b')
        self.setup(store_b, device_name='device-b')
        sync.unpack(store_b, confirm=True)
        result = flow.resume(store_b, repo_b)
        text = flow.render_resume(result)
        self.assertIn('待本机确认', text)
        self.assertNotIn('同步', text)
        self.assertNotIn('导入', text)
        self.assertNotIn('pack', text.lower())
        self.assertNotIn('unpack', text.lower())

    def test_cli_pack_unpack(self):
        store_a, repo_a = self.device('proj-a')
        draft = self.root / 'goal-cli.md'
        draft.write_bytes('# 目标\n\n演示。\n'.encode('utf-8'))
        cli = [sys.executable, str(SKILL / 'scripts' / 'relay.py'),
               '--store-root', str(store_a), '--root', str(repo_a)]
        env = dict(os.environ, PYTHONIOENCODING='utf-8')
        subprocess.run(cli + ['goal', 'draft', '--file', str(draft)],
                       check=True, capture_output=True, text=True, encoding='utf-8', env=env)
        subprocess.run(cli + ['goal', 'confirm'],
                       check=True, capture_output=True, text=True, encoding='utf-8', env=env)
        subprocess.run(cli + ['sync-setup', '--remote', str(self.bare),
                              '--device-name', 'cli-a'],
                       check=True, capture_output=True, text=True, encoding='utf-8', env=env)
        out = subprocess.run(cli + ['pack'],
                             check=True, capture_output=True, text=True, encoding='utf-8', env=env)
        self.assertIn('pack 完成', out.stdout)
        store_b, repo_b = self.device('proj-b')
        cli_b = [sys.executable, str(SKILL / 'scripts' / 'relay.py'),
                 '--store-root', str(store_b), '--root', str(repo_b)]
        subprocess.run(cli_b + ['sync-setup', '--remote', str(self.bare),
                                '--device-name', 'cli-b'],
                       check=True, capture_output=True, text=True, encoding='utf-8', env=env)
        out = subprocess.run(cli_b + ['unpack', '--confirm'],
                             check=True, capture_output=True, text=True, encoding='utf-8', env=env)
        self.assertIn('unpack 已落盘', out.stdout)
        self.assertEqual(relay.goal_status(store_b, repo_b)[0], 'imported')


    def test_marker_file_blocks_pack(self):
        store_a, repo_a = self.device('proj-a')
        self.goal(store_a, repo_a, '# 目标\n\n演示。\n')
        self.setup(store_a, device_name='device-a')
        decisions = self.store_dir(store_a, repo_a) / 'archive' / 'decisions.md'
        decisions.write_bytes((decisions.read_text(encoding='utf-8')
                             + '\n<<<<<<< HEAD\n').encode('utf-8'))
        with self.assertRaises(SyncError) as ctx:
            sync.pack(store_a, repo_a)
        self.assertIn('decisions.md', str(ctx.exception))


if __name__ == '__main__':
    unittest.main()
