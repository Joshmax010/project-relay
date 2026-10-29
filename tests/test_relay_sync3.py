"""v2 sync slice 3 tests (plan part 03, steps 2.6-2.7 + acceptance).

Style follows test_relay_sync2.py: real temporary directories, real Git
repos, a local bare repo stands in for the memory remote -- no network,
no real server, no real ~/.project-relay/ ever touched
(PROJECT_RELAY_HOME is patched). Sync-raised errors are asserted
against relay_sync.RelayError (importlib double-load).
"""

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

REMOTE_URL = 'https://example.com/demo/proj3.git'


class Sync3Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='relay-sync3-')
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
        self.bare = self.root / 'mem.git'
        subprocess.run(['git', 'init', '--bare', str(self.bare)],
                       check=True, capture_output=True)

    def git(self, root, *args):
        return subprocess.run(['git', '-C', str(root), *args],
                              check=True, capture_output=True)

    def make_seed(self, name):
        root = self.root / name
        root.mkdir()
        self.git(root, 'init', '-b', 'main')
        self.git(root, 'config', 'user.name', 'Tester')
        self.git(root, 'config', 'user.email', 'test@example.invalid')
        self.git(root, 'config', 'commit.gpgsign', 'false')
        (root / 'app.py').write_text('value = 1\n')
        self.git(root, 'add', '.')
        self.git(root, 'commit', '-m', 'baseline')
        return root

    def clone(self, seed, name):
        dest = self.root / name
        subprocess.run(['git', 'clone', '--quiet', str(seed), str(dest)],
                       check=True, capture_output=True)
        self.git(dest, 'config', 'user.name', 'Tester')
        self.git(dest, 'config', 'user.email', 'test@example.invalid')
        self.git(dest, 'config', 'commit.gpgsign', 'false')
        self.git(dest, 'remote', 'set-url', 'origin', REMOTE_URL)
        return dest

    def setup(self, name, device):
        store = self.root / name
        store.mkdir(parents=True, exist_ok=True)
        sync.sync_setup(store, str(self.bare), device_name=device)
        return store

    def store_dir(self, store, repo):
        return relay.resolve_store_dir(store, repo)[1]

    def status(self, repo):
        out = subprocess.run(['git', '-C', str(repo), 'status', '--porcelain'],
                             check=True, capture_output=True)
        return out.stdout.decode('utf-8').splitlines()

    def dirty_repo(self, repo, value='value = 99\n'):
        (repo / '.gitignore').write_text('local.conf\n')
        (repo / 'local.conf').write_text('token=local\n')
        (repo / 'app.py').write_text(value)
        (repo / 'new_feature.py').write_text('x = 1\n')


class WipSnapshotTests(Sync3Base):
    def test_snapshot_roundtrip(self):
        seed = self.make_seed('seed')
        repo_a = self.clone(seed, 'repo-a')
        repo_b = self.clone(seed, 'repo-b')
        store_a = self.setup('store-a', 'devA')
        store_b = self.setup('store-b', 'devB')
        relay.init(store_a, repo_a)
        relay.init(store_b, repo_b)
        self.dirty_repo(repo_a)
        before = self.status(repo_a)
        snap = sync.create_wip_snapshot(repo_a, 'devA', 'idA', 1)
        self.assertIsNotNone(snap)
        self.assertEqual(snap['branch'], 'main')
        self.assertIn('app.py', snap['changed_files'])
        self.assertIn('new_feature.py', snap['changed_files'])
        self.assertIn('local.conf', snap['ignored_recent'])
        # The user's real index is untouched by the temp-index build.
        self.assertEqual(self.status(repo_a), before)
        staged = subprocess.run(['git', '-C', str(repo_a), 'diff', '--cached',
                                 '--name-only'], check=True, capture_output=True)
        self.assertEqual(staged.stdout.decode('utf-8').strip(), '')
        message = subprocess.run(['git', '-C', str(repo_a), 'log', '-1',
                                  '--format=%B', snap['sha']],
                                 check=True, capture_output=True)
        body = message.stdout.decode('utf-8')
        self.assertIn('Relay-Snapshot: true', body)
        self.assertIn('Relay-Pack-Seq: 1', body)
        pushed = sync.push_wip_snapshot(repo_a, str(self.bare), snap)
        self.assertEqual(pushed['ref'], 'refs/relay/wip/main')
        restored = sync.restore_wip_snapshot(store_b, repo_b,
                                             self.store_dir(store_b, repo_b),
                                             str(self.bare))
        self.assertEqual(restored['status'], 'restored')
        self.assertEqual((repo_b / 'app.py').read_text(), 'value = 99\n')
        self.assertTrue((repo_b / 'new_feature.py').exists())
        self.assertFalse((repo_b / 'local.conf').exists())
        st = self.status(repo_b)
        self.assertTrue(any(line.startswith(' M ') for line in st))
        self.assertTrue(any(line.startswith('?? ') for line in st))

    def test_restore_refuses_dirty_worktree(self):
        seed = self.make_seed('seed')
        repo_a = self.clone(seed, 'repo-a')
        repo_b = self.clone(seed, 'repo-b')
        store_a = self.setup('store-a', 'devA')
        store_b = self.setup('store-b', 'devB')
        relay.init(store_a, repo_a)
        relay.init(store_b, repo_b)
        self.dirty_repo(repo_a)
        snap = sync.create_wip_snapshot(repo_a, 'devA', 'idA', 1)
        sync.push_wip_snapshot(repo_a, str(self.bare), snap)
        (repo_b / 'app.py').write_text('value = bw-own\n')
        refused = sync.restore_wip_snapshot(store_b, repo_b,
                                            self.store_dir(store_b, repo_b),
                                            str(self.bare))
        self.assertEqual(refused['status'], 'refused-dirty')
        self.assertEqual((repo_b / 'app.py').read_text(), 'value = bw-own\n')

    def test_overwrite_guard_needs_flag(self):
        seed = self.make_seed('seed')
        repo_a = self.clone(seed, 'repo-a')
        self.dirty_repo(repo_a)
        snap1 = sync.create_wip_snapshot(repo_a, 'devA', 'idA', 1)
        sync.push_wip_snapshot(repo_a, str(self.bare), snap1)
        (repo_a / 'app.py').write_text('value = 100\n')
        snap2 = sync.create_wip_snapshot(repo_a, 'devA', 'idA', 2)
        with self.assertRaises(SyncError) as ctx:
            sync.push_wip_snapshot(repo_a, str(self.bare), snap2)
        self.assertIn('尚未在任何设备还原', str(ctx.exception))
        pushed = sync.push_wip_snapshot(repo_a, str(self.bare), snap2,
                                        overwrite_unconsumed=True)
        self.assertTrue(pushed['overwritten_unconsumed'])

    def test_size_limit_refuses_with_biggest_files(self):
        seed = self.make_seed('seed')
        repo_a = self.clone(seed, 'repo-a')
        (repo_a / 'big.bin').write_bytes(b'x' * (2 * 1024 * 1024))
        with self.assertRaises(SyncError) as ctx:
            sync.create_wip_snapshot(repo_a, 'devA', 'idA', 1,
                                     size_limit=1024 * 1024)
        self.assertIn('big.bin', str(ctx.exception))

    def test_clean_worktree_returns_none(self):
        seed = self.make_seed('seed')
        repo_a = self.clone(seed, 'repo-a')
        self.assertIsNone(sync.create_wip_snapshot(repo_a, 'devA', 'idA', 1))


class PackWipReceiptTests(Sync3Base):
    def _device(self, name):
        seed = self.make_seed('seed-' + name)
        repo = self.clone(seed, 'repo-' + name)
        store = self.setup('store-' + name, 'dev' + name)
        relay.init(store, repo)
        relay.goal_draft(store, repo, '做一个小工具')
        relay.goal_confirm(store, repo)
        return store, repo

    def test_pack_receipt_wip_clean(self):
        store, repo = self._device('a')
        result = sync.pack(store, repo)
        self.assertEqual(result['receipt']['wip']['status'], 'clean')

    def test_pack_receipt_wip_created(self):
        store, repo = self._device('b')
        self.dirty_repo(repo)
        result = sync.pack(store, repo)
        wip = result['receipt']['wip']
        self.assertEqual(wip['status'], 'created')
        self.assertIn('local.conf', wip['ignored_recent'])
        self.assertIn('app.py', wip['changed_files'])


class MergeFormatTests(unittest.TestCase):
    def test_parse_accepts_hyphenated_headers(self):
        blocks, _pre = sync._parse_blocks('## D-001\n- 选择：甲\n', 'D')
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]['id'], 1)
        blocks, _pre = sync._parse_blocks('## D1\n- 选择：甲\n', 'D')
        self.assertEqual(len(blocks), 1)

    def test_merge_emits_canonical_hyphenated_headers(self):
        merged, added, _skipped = sync._merge_id_blocks(
            '模板\n', '## D-001\n- 选择：甲\n', 'D')
        self.assertEqual(added, 1)
        self.assertIn('## D-001', merged)
        self.assertNotIn('## D1\n', merged)
        merged, added, _skipped, _conf = sync._merge_backlog(
            '模板\n', '## T-001\n- 事项：甲\n', )
        self.assertEqual(added, 1)
        self.assertIn('## T-001', merged)


class ResolveTests(Sync3Base):
    def _two_devices(self):
        seed = self.make_seed('seed')
        repo_a = self.clone(seed, 'repo-a')
        repo_b = self.clone(seed, 'repo-b')
        store_a = self.setup('store-a', 'devA')
        store_b = self.setup('store-b', 'devB')
        relay.init(store_a, repo_a)
        relay.init(store_b, repo_b)
        return store_a, repo_a, store_b, repo_b

    def test_resolve_goal_keep_local(self):
        store_a, repo_a, store_b, repo_b = self._two_devices()
        relay.goal_draft(store_a, repo_a, '目标甲')
        relay.goal_confirm(store_a, repo_a)
        relay.goal_draft(store_b, repo_b, '目标乙')
        relay.goal_confirm(store_b, repo_b)
        sync.pack(store_a, repo_a)
        sync.unpack(store_b, confirm=True)
        sd_b = self.store_dir(store_b, repo_b)
        self.assertTrue((sd_b / 'state' / 'goal-conflict.json').exists())
        pending = sync.list_pending_resolutions(store_b, repo_b)
        self.assertTrue(any(p['kind'] == 'goal' for p in pending['pending']))
        goal_before = (sd_b / 'archive' / 'goal.md').read_bytes()
        result = sync.sync_resolve(store_b, repo_b, goal=True,
                                   decision='keep-local')
        self.assertTrue(result['actions'])
        self.assertFalse((sd_b / 'state' / 'goal-conflict.json').exists())
        self.assertEqual((sd_b / 'archive' / 'goal.md').read_bytes(), goal_before)
        present, mismatches = relay.verify_checksum_manifest(sd_b)
        self.assertTrue(present)
        self.assertEqual(mismatches, [])

    def test_resolve_fork_adopt(self):
        _sa, _ra, store_b, repo_b = self._two_devices()
        sd_b = self.store_dir(store_b, repo_b)
        state_path = sd_b / 'archive' / 'state.md'
        text = state_path.read_text(encoding='utf-8')
        self.assertIn('<!-- relay:state-begin -->', text)
        text = text.replace('<!-- relay:state-end -->',
                            '- 分叉待裁：正在做什么（对方）：对方值\n'
                            '<!-- relay:state-end -->')
        state_path.write_text(text, encoding='utf-8')
        relay.update_checksum_manifest(sd_b)
        result = sync.sync_resolve(store_b, repo_b, fork='正在做什么',
                                   decision='adopt-incoming')
        self.assertTrue(result['actions'])
        after = state_path.read_text(encoding='utf-8')
        self.assertNotIn('分叉待裁', after)
        present, mismatches = relay.verify_checksum_manifest(sd_b)
        self.assertTrue(present)
        self.assertEqual(mismatches, [])

    def test_resolve_backlog_keep_local(self):
        store_a, repo_a, store_b, repo_b = self._two_devices()
        for store, repo, variant in ((store_a, repo_a, '甲'),
                                     (store_b, repo_b, '乙')):
            sd = self.store_dir(store, repo)
            backlog = sd / 'archive' / 'backlog.md'
            text = backlog.read_text(encoding='utf-8')
            backlog.write_text(text + '\n## T-001\n- 事项：发布检查\n'
                               '- Owner：Agent\n- 验证方式：%s\n'
                               '- 来源：汇总 9\n- 状态：待启动\n' % variant,
                               encoding='utf-8')
            relay.update_checksum_manifest(sd)
        sync.pack(store_a, repo_a)
        sync.unpack(store_b, confirm=True)
        pending = sync.list_pending_resolutions(store_b, repo_b)
        self.assertTrue(any(p.get('item') == '发布检查'
                            for p in pending['pending']))
        result = sync.sync_resolve(store_b, repo_b, backlog='发布检查',
                                   decision='keep-local')
        self.assertTrue(result['actions'])
        pending = sync.list_pending_resolutions(store_b, repo_b)
        self.assertFalse(any(p.get('item') == '发布检查'
                             for p in pending['pending']))
        sd_b = self.store_dir(store_b, repo_b)
        present, mismatches = relay.verify_checksum_manifest(sd_b)
        self.assertTrue(present)
        self.assertEqual(mismatches, [])


if __name__ == '__main__':
    unittest.main()
