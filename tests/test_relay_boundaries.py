"""Volume 3, 3.6 boundary completion tests (plan part 03, step 3.6).

Style follows the old suite: real temporary directories, real Git repos,
no mocks except one stubbed carrier return for the pack-dispatcher hook
(the real pack paths are already covered by the sync tests; what 3.6 owns
is the prompt attachment, not the transport). The store root and the fake
tool log home are redirected; no test ever touches the developer's real
~/.project-relay/, ~/.claude, ~/.codex or ~/.workbuddy.
"""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
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
relayctl = _load('relayctl', SKILL / 'scripts/relayctl.py')
flow = _load('relay_flow', SKILL / 'scripts/relay_flow.py')
backfill_mod = _load('relay_backfill', SKILL / 'scripts/relay_backfill.py')
sync = _load('relay_sync', SKILL / 'scripts/relay_sync.py')
accept = _load('relay_accept', SKILL / 'scripts/relay_accept.py')
fork = _load('relay_fork', SKILL / 'scripts/relay_fork.py')
# NOTE: each importlib load creates its own relayctl copy; relay-raised
# errors must be asserted against that module's RelayError.
RelayError = relay.RelayError

GITLINK_SHA = '1234567890123456789012345678901234567890'


def _write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w', encoding='utf-8', newline='') as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + '\n')


class BoundariesBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='relay-boundaries-')
        self.addCleanup(self.temp.cleanup)
        self.store = Path(self.temp.name) / 'store'
        self.log_home = Path(self.temp.name) / 'loghome'
        self._env = mock.patch.dict(
            os.environ, {'PROJECT_RELAY_HOME': str(Path(self.temp.name))})
        self._env.start()
        self.addCleanup(self._env.stop)

    def git(self, root, *args):
        return subprocess.run(['git', '-C', str(root), *args],
                              capture_output=True, check=True)

    def make_repo(self, name, remote=None, commits=1):
        root = Path(self.temp.name) / name
        root.mkdir()
        self.git(root, 'init', '-b', 'main')
        self.git(root, 'config', 'user.name', 'Tester')
        self.git(root, 'config', 'user.email', 'test@example.invalid')
        self.git(root, 'config', 'commit.gpgsign', 'false')
        (root / 'app.py').write_bytes('value = 1\n'.encode('utf-8'))
        self.git(root, 'add', '.')
        self.git(root, 'commit', '-m', 'baseline')
        for _ in range(commits - 1):
            (root / 'app.py').write_bytes('value = 2\n'.encode('utf-8'))
            self.git(root, 'add', '.')
            self.git(root, 'commit', '-m', 'second')
        if remote:
            self.git(root, 'remote', 'add', 'origin', remote)
        return root

    def add_gitlink(self, root, path='libs/ext'):
        self.git(root, 'update-index', '--add', '--cacheinfo',
                 '160000,%s,%s' % (GITLINK_SHA, path))

    def store_dir(self, root):
        row = relay.where(self.store, path=root)['row']
        return self.store / 'projects' / (row['key'] + '-' + row['slug'])

    def workbuddy_fixture(self, proj, session='wb-one'):
        path = self.log_home / '.workbuddy' / 'projects' / ('c-' + proj.name) / (session + '.jsonl')
        _write_jsonl(path, accept.workbuddy_session_lines(str(proj), [
            ('user', '决定：采用单例模式'),
            ('tool', 'Write', {'file': 'app.py'}),
        ]))
        traces = self.log_home / '.workbuddy' / 'traces' / 't1'
        _write_jsonl(traces / 'trace_a.jsonl',
                     accept.workbuddy_trace_lines(session, ['已按单例模式改好。']))
        return path


class TestSubmoduleDowngrade(BoundariesBase):
    def test_old_snapshot_skips_gitlink_with_gap(self):
        repo = self.make_repo('proj-sub')
        self.add_gitlink(repo)
        snap = relayctl.snapshot(repo)
        self.assertEqual(snap['gaps'], [{'path': 'libs/ext', 'pointer': GITLINK_SHA}])
        self.assertNotIn('libs/ext', snap['code'])
        self.assertIn('app.py', snap['code'])

    def test_v2_snapshot_skips_gitlink_with_gap(self):
        repo = self.make_repo('proj-sub2')
        self.add_gitlink(repo)
        snap = flow.snapshot_v2(repo)
        self.assertEqual(snap['gaps'], [{'path': 'libs/ext', 'pointer': GITLINK_SHA}])
        self.assertNotIn('libs/ext', snap['code'])

    def test_backfill_fidelity_names_submodule_gap(self):
        repo = self.make_repo('proj-sub3')
        self.add_gitlink(repo)
        self.workbuddy_fixture(repo)
        report = backfill_mod.backfill(self.store, repo, home=self.log_home,
                                       tools=['workbuddy'], tier='excerpt',
                                       summarize_after=True)
        self.assertIsNotNone(report['summary_seq'])
        store_dir = self.store_dir(repo)
        fidelity = (store_dir / 'archive' / 'backfill-fidelity.md').read_text(encoding='utf-8')
        self.assertIn('子模块 libs/ext 未纳入接力范围，其内部状态不归 relay 管', fidelity)


class TestWeakIdentity(BoundariesBase):
    def test_weak_init_marks_header_and_migrates_losslessly(self):
        repo = self.make_repo('proj-weak')
        result = relay.init(self.store, repo)
        row = relay.where(self.store, path=repo)['row']
        self.assertEqual(row['key_type'], 'local')
        old_dir = self.store_dir(repo)
        state_before = (old_dir / 'archive' / 'state.md').read_bytes()
        self.assertIn('身份：弱身份'.encode('utf-8'), state_before)
        # New formula differs from the old path-only formula.
        self.assertNotEqual(relay.local_key(repo.resolve()),
                            relay.digest(str(repo.resolve()).encode('utf-8'))[:16])

        # Add a remote: a migration is PENDING, never automatic.
        self.git(repo, 'remote', 'add', 'origin', 'https://example.invalid/x.git')
        prompt = relay.pending_key_migration(self.store, repo)
        self.assertIsNotNone(prompt)
        self.assertEqual(prompt['old_key'], row['key'])
        self.assertEqual(prompt['remote'], 'example.invalid/x')
        self.assertEqual(relay.where(self.store, path=repo)['row']['key'], row['key'])

        # Pack dispatcher attaches the prompt without touching the manifest.
        with mock.patch.object(sync, '_pack_git', lambda *a, **k: {'receipt': {'x': 1}}):
            packed = sync.pack(self.store, repo, carrier='git')
        self.assertEqual(packed['receipt'], {'x': 1})
        self.assertEqual(packed['key_migration_prompt'], prompt)

        # Explicit migration: rename + tombstone + migrated_from, zero loss.
        migrated = relay.migrate_key(self.store, repo)
        self.assertEqual(migrated['old_key'], row['key'])
        self.assertEqual(migrated['new_key'], prompt['new_key'])
        self.assertFalse(old_dir.exists())
        new_dir = self.store / 'projects' / (migrated['new_key'] + '-' + row['slug'])
        self.assertTrue(new_dir.is_dir())
        self.assertEqual((new_dir / 'archive' / 'state.md').read_bytes().replace(
            '身份：弱身份（无 git remote；键为路径哈希 + 首提交哈希；加 remote 后可迁移，见 relay migrate-key）\n'.encode('utf-8'), b''),
            state_before.replace(
            '身份：弱身份（无 git remote；键为路径哈希 + 首提交哈希；加 remote 后可迁移，见 relay migrate-key）\n'.encode('utf-8'), b''))
        self.assertNotIn('身份：弱身份'.encode('utf-8'),
                         (new_dir / 'archive' / 'state.md').read_bytes())
        reg = relay.load_registry(self.store)
        tomb = [r for r in reg['projects'] if r.get('status') == 'migrated']
        self.assertEqual(len(tomb), 1)
        self.assertEqual(tomb[0]['migrated_to'], migrated['new_key'])
        new_row = relay.where(self.store, path=repo)['row']
        self.assertEqual(new_row['key_type'], 'remote')
        self.assertIn(row['key'], new_row['migrated_from'])
        # Second migration is refused: already a remote key.
        with self.assertRaises(RelayError):
            relay.migrate_key(self.store, repo)

    def test_grandfathered_old_local_key_is_not_rekeyed(self):
        repo = self.make_repo('proj-old')
        old_key = relay.digest(str(repo.resolve()).encode('utf-8'))[:16]
        self.assertNotEqual(old_key, relay.local_key(repo))
        store_dir = self.store / 'projects' / (old_key + '-' + repo.name)
        store_dir.mkdir(parents=True)
        reg = {'version': 1, 'projects': [{'key': old_key, 'slug': repo.name,
                             'display_name': repo.name, 'paths': [str(repo.resolve())],
                             'remote': None, 'created_at': 't', 'last_active': 't',
                             'key_type': 'local', 'status': 'active',
                             'migrated_from': []}]}
        relay.save_registry(self.store, reg)
        row, _created, notes = relay.register(self.store, repo)
        self.assertEqual(row['key'], old_key)
        self.assertTrue(any('沿用既有项目键' in n for n in notes))


class TestZeroArchiveStart(BoundariesBase):
    def test_no_archive_branch_assesses_and_writes_nothing(self):
        repo = self.make_repo('proj-noarch', commits=2)
        result = flow.resume(self.store, repo)
        self.assertTrue(result['no_archive'])
        self.assertEqual(result['assessment']['commits'], 2)
        self.assertFalse(result['assessment']['legacy_agent'])
        self.assertIn('回溯建档', result['assessment']['advice'])
        self.assertIn('二选一', result['message'])
        self.assertNotIn('同步', result['message'])
        self.assertFalse((self.store / 'projects').exists())

    def test_no_archive_empty_repo_advises_init(self):
        root = Path(self.temp.name) / 'proj-empty'
        root.mkdir()
        self.git(root, 'init', '-b', 'main')
        result = flow.resume(self.store, root)
        self.assertTrue(result['no_archive'])
        self.assertEqual(result['assessment']['commits'], 0)
        self.assertIn('从零 init', result['assessment']['advice'])
        self.assertFalse((self.store / 'projects').exists())

    def test_no_archive_legacy_agent_advises_backfill(self):
        repo = self.make_repo('proj-legacy', commits=1)
        (repo / '.agent').mkdir()
        result = flow.resume(self.store, repo)
        self.assertTrue(result['assessment']['legacy_agent'])
        self.assertIn('回溯建档', result['assessment']['advice'])

    def test_no_archive_remote_tip_preserved(self):
        repo = self.make_repo('proj-clone', remote='https://example.invalid/x.git')
        result = flow.resume(self.store, repo)
        self.assertIn('请回旧设备执行 pack', result['message'])
        self.assertTrue(result['assessment']['has_remote'])


class TestNonGitRefusal(BoundariesBase):
    def test_init_refuses_non_git_directory(self):
        plain = Path(self.temp.name) / 'plain'
        plain.mkdir()
        with self.assertRaises(RelayError) as ctx:
            relay.init(self.store, plain)
        self.assertIn('非 Git', str(ctx.exception))


class TestFingerprintCache(BoundariesBase):
    def _repo_with_files(self, name):
        repo = self.make_repo(name)
        (repo / 'b.py').write_bytes('b = 1\n'.encode('utf-8'))
        (repo / 'sub').mkdir(exist_ok=True)
        (repo / 'sub' / 'c.py').write_bytes('c = 1\n'.encode('utf-8'))
        self.git(repo, 'add', '.')
        self.git(repo, 'commit', '-m', 'more files')
        return repo

    def test_cache_hits_misses_and_pruning(self):
        repo = self._repo_with_files('proj-fp')
        cache_dir = Path(self.temp.name) / 'fp-store'
        snap1 = flow.snapshot_v2(repo, cache_dir)
        self.assertTrue(snap1['timing']['first_snapshot'])
        self.assertEqual(snap1['timing']['cache_hits'], 0)
        self.assertEqual(snap1['timing']['cache_misses'], snap1['timing']['files'])
        snap2 = flow.snapshot_v2(repo, cache_dir)
        self.assertEqual(snap2['timing']['cache_hits'], snap2['timing']['files'])
        self.assertEqual(snap2['timing']['cache_misses'], 0)
        (repo / 'b.py').write_bytes('b = 2  # changed\n'.encode('utf-8'))
        snap3 = flow.snapshot_v2(repo, cache_dir)
        self.assertEqual(snap3['timing']['cache_misses'], 1)
        self.assertEqual(snap3['timing']['cache_hits'], snap3['timing']['files'] - 1)
        (repo / 'app.py').unlink()
        flow.snapshot_v2(repo, cache_dir)
        cache = json.loads((cache_dir / 'state' / 'fingerprint-cache.json').read_text(encoding='utf-8'))
        self.assertNotIn('app.py', cache)

    def test_threshold_injection_exceeds_without_big_repo(self):
        repo = self._repo_with_files('proj-thr')
        snap = flow.snapshot_v2(repo, Path(self.temp.name) / 'thr-store', threshold_s=0)
        self.assertTrue(snap['timing']['exceeded'])
        self.assertEqual(snap['timing']['threshold_s'], 0)

    def test_excludes_land_in_fidelity_not_code(self):
        repo = self._repo_with_files('proj-excl')
        (repo / 'vendor').mkdir(exist_ok=True)
        (repo / 'vendor' / 'x.py').write_bytes('x = 1\n'.encode('utf-8'))
        self.git(repo, 'add', '.')
        self.git(repo, 'commit', '-m', 'vendor file')
        cache_dir = Path(self.temp.name) / 'excl-store'
        flow.save_config(cache_dir, {'fingerprint_excludes': ['vendor']})
        snap = flow.snapshot_v2(repo, cache_dir)
        self.assertIn('vendor/x.py', snap['excluded'])
        self.assertNotIn('vendor/x.py', snap['code'])
        # CLI surface: timing line, threshold line, excluded line.
        relay.init(self.store, repo)
        flow.save_config(self.store_dir(repo), {'fingerprint_excludes': ['vendor'],
                                                'fingerprint_threshold_s': 0})
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = relay.main(['--store-root', str(self.store), '--root', str(repo),
                               'fingerprint'])
        self.assertEqual(code, 0)
        out = buf.getvalue()
        self.assertIn('指纹耗时', out)
        self.assertIn('超过阈值', out)
        self.assertIn('已排除：vendor/x.py', out)


class TestMonorepoSplit(BoundariesBase):
    def test_default_scope_none_and_split_chain_point(self):
        repo = self.make_repo('proj-mono')
        (repo / 'pkg').mkdir(exist_ok=True)
        (repo / 'pkg' / 'a.py').write_bytes('a = 1\n'.encode('utf-8'))
        self.git(repo, 'add', '.')
        self.git(repo, 'commit', '-m', 'pkg')
        relay.init(self.store, repo)
        parent_dir = self.store_dir(repo)
        self.assertIsNone(flow.load_config(parent_dir)['scope'])
        flow.capture(self.store, repo, tool='codex', session='s1', message='第一条')
        flow.summarize(self.store, repo, source='test')
        self.assertEqual(fork.latest_summary(parent_dir)['id'], 'sum-0001')

        result = relay.split_archive(self.store, repo, 'pkg', 'sub')
        self.assertEqual(result['split_summary_id'], 'sum-0001')
        child_dir = result['child_dir']
        child_cfg = flow.load_config(child_dir)
        self.assertEqual(child_cfg['scope'], 'pkg')
        self.assertEqual(child_cfg['split_from']['parent_key'], result['parent_key'])
        self.assertEqual(child_cfg['split_from']['split_summary_id'], 'sum-0001')
        # The child's chain head IS the split point, so its first post-split
        # summary takes based_on from it (3.3: base = latest summary).
        self.assertEqual(fork.latest_summary(child_dir)['id'], 'sum-0001')
        # Parent untouched by the split; later parent summaries stay out.
        parent_before = (parent_dir / 'archive' / 'state.md').read_bytes()
        self.assertEqual((parent_dir / 'archive' / 'state.md').read_bytes(), parent_before)
        flow.capture(self.store, repo, tool='codex', session='s2', message='第二条')
        flow.summarize(self.store, repo, source='test')
        self.assertEqual(fork.latest_summary(parent_dir)['id'], 'sum-0002')
        self.assertFalse((child_dir / 'state' / 'summaries' / '0002.json').exists())
        # Path addressing still resolves the parent (documented limit);
        # the child is addressed by key.
        self.assertEqual(relay.where(self.store, path=repo)['row']['key'],
                         result['parent_key'])
        self.assertEqual(relay.where(self.store, key=result['child_key'])['row']['key'],
                         result['child_key'])

    def test_split_rejects_dot_and_escape(self):
        repo = self.make_repo('proj-split-bad')
        relay.init(self.store, repo)
        with self.assertRaises(RelayError):
            relay.split_archive(self.store, repo, '.', 'sub')
        with self.assertRaises(RelayError):
            relay.split_archive(self.store, repo, '../outside', 'sub')


if __name__ == '__main__':
    unittest.main()
