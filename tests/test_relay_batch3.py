"""Codex PR #5 第三批修复回归测试（Windows 环境批 + claim F1–F5 + 新 7 条 C1–C7）。

Style follows the existing suites: real temporary directories, real Git
repos, PROJECT_RELAY_HOME redirected, no network, no real
~/.project-relay/. Batch 1 (A1, A3-A7) lives in test_relay_batch1.py and
batch 2 (A2, B1-B6) in test_relay_batch2.py; this file only adds
coverage for the third batch. Windows-only behavior (backslash payload
keys, CRLF checkout conversion) cannot be produced on a POSIX runner,
so the tests pin the byte-level contracts those fixes rest on
(posix keys, byte-exact hashes, no writes on dry-run) and the Windows
harness re-run is the empirical check for the platform byte behavior.
"""

import base64
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SKILL = Path(__file__).resolve().parents[1]


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SKILL / 'scripts' / (name + '.py'))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


relay = _load('relay')
flow = _load('relay_flow')
sync = _load('relay_sync')
backfill_mod = _load('relay_backfill')
# Each _load creates its own module copy, so exception identity differs
# per copy: sync.* raises sync.relay.RelayError, not this file's relay
# copy (the same trap batch 1 avoids with FlowRelayError).
RelayError = sync.relay.RelayError


def _write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w', encoding='utf-8', newline='') as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + '\n')


class Batch3Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='relay-batch3-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = self.root / 'store'
        self._env = mock.patch.dict(os.environ, {
            'PROJECT_RELAY_HOME': str(self.root),
            'GIT_AUTHOR_NAME': 'Tester',
            'GIT_AUTHOR_EMAIL': 'test@example.invalid',
            'GIT_COMMITTER_NAME': 'Tester',
            'GIT_COMMITTER_EMAIL': 'test@example.invalid',
        })
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
        root.mkdir(parents=True, exist_ok=True)
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

    def make_repo_at(self, parent, name, remote=None):
        parent = Path(parent)
        parent.mkdir(parents=True, exist_ok=True)
        root = parent / name
        root.mkdir(parents=True, exist_ok=True)
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

    def init(self, name='proj', remote=None):
        root = self.make_repo(name, remote=remote or 'https://example.com/x/proj.git')
        relay.init(self.store, root)
        return root

    def store_dir(self, root, store=None):
        return relay.resolve_store_dir(store or self.store, root)[1]

    def goal(self, store, repo, text='# 目标\n\n演示。\n'):
        relay.goal_draft(store, repo, text)
        relay.goal_confirm(store, repo)

    def capture(self, store, repo, message, session='s1'):
        return flow.capture(store, repo, tool='codex', session=session,
                            message=message)

    def export(self, store, repo, dest, device='device-a'):
        cfg = sync.load_sync_config(store) or {}
        return sync.export_bundle(store, [repo], dest, device,
                                  cfg.get('device_id') or 'id-a')


class TestA1GitPinning(Batch3Base):
    def test_git_wrapper_checkout_keeps_lf_under_autocrlf_true(self):
        # A1: the wrapper pins -c core.autocrlf=false per process, so a
        # checkout through it never converts LF->CRLF even when the repo
        # config says autocrlf=true -- and the config itself is untouched.
        root = self.root / 'pin'
        root.mkdir()
        self.git(root, 'init', '-b', 'main')
        self.git(root, 'config', 'user.name', 'Tester')
        self.git(root, 'config', 'user.email', 'test@example.invalid')
        self.git(root, 'config', 'core.autocrlf', 'true')
        (root / 'app.py').write_bytes(b'line one\nline two\n')
        relay.git(root, 'add', '.')
        relay.git(root, 'commit', '-m', 'baseline')
        (root / 'app.py').unlink()
        relay.git(root, 'checkout', '--', 'app.py')
        self.assertEqual((root / 'app.py').read_bytes(), b'line one\nline two\n')
        out = subprocess.run(['git', '-C', str(root), 'config', '--local',
                              '--get', 'core.autocrlf'],
                             capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(out.stdout.strip(), 'true')

    def test_sync_setup_commits_gitattributes_and_is_idempotent(self):
        store = self.root / 'store-attr'
        sync.sync_setup(store, str(self.bare), device_name='device-attr')
        self.assertEqual((store / '.gitattributes').read_bytes(), b'* -text\n')
        tracked = subprocess.run(['git', '-C', str(store), 'ls-files'],
                                 capture_output=True, text=True, encoding='utf-8')
        self.assertIn('.gitattributes', tracked.stdout.splitlines())
        head_before = subprocess.run(['git', '-C', str(store), 'rev-parse', 'HEAD'],
                                     capture_output=True, text=True, encoding='utf-8').stdout.strip()
        sync.sync_setup(store, str(self.bare), device_name='device-attr')
        head_after = subprocess.run(['git', '-C', str(store), 'rev-parse', 'HEAD'],
                                    capture_output=True, text=True, encoding='utf-8').stdout.strip()
        self.assertEqual(head_before, head_after)


class TestA2ByteStability(Batch3Base):
    def test_bundle_bytes_hashed_as_stored(self):
        # A2: the Windows hash failures were text-mode newline
        # translation (disk CRLF vs hashed LF). The contract is
        # byte-exactness: a CRLF file with a CRLF hash imports fine and
        # lands byte-identical, never silently normalized.
        root_a = self.init('proj-a')
        self.goal(self.store, root_a)
        self.capture(self.store, root_a, 'did a thing')
        bundle = self.root / 'bundle'
        self.export(self.store, root_a, bundle)
        key = relay.resolve_store_dir(self.store, root_a)[0]['key']
        slug = relay.resolve_store_dir(self.store, root_a)[0]['slug']
        target = bundle / 'projects' / ('%s-%s' % (key, slug)) / 'archive' / 'state.md'
        crlf = target.read_bytes().replace(b'\n', b'\r\n')
        self.assertNotEqual(crlf, target.read_bytes())
        target.write_bytes(crlf)
        manifest_path = bundle / 'manifest.json'
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        rel = 'projects/%s-%s/archive/state.md' % (key, slug)
        manifest['files'][rel] = sync._sha256(crlf)
        manifest_path.write_bytes(json.dumps(manifest).encode('utf-8'))
        store_b = self.root / 'store-b'
        report = sync.import_bundle(bundle, store_b, confirm=True)
        self.assertEqual(report['projects'][0]['status'], 'unclaimed')
        landed = store_b / 'unclaimed' / ('%s-%s' % (key, slug)) / 'archive' / 'state.md'
        self.assertEqual(landed.read_bytes(), crlf)

    def test_init_templates_are_lf(self):
        root = self.init('proj-lf')
        sd = self.store_dir(root)
        for name in ('goal.md', 'state.md', 'backlog.md', 'decisions.md', 'lessons.md'):
            self.assertNotIn(b'\r', (sd / 'archive' / name).read_bytes())


class TestA4Rollback(Batch3Base):
    def test_rollback_restores_payload_on_setup_store(self):
        # A4: the Windows "该提交不含本项目负载" was the tar-walk rel
        # comparison using OS-native separators against a posix prefix.
        # This pins the restore path on a sync_setup store end to end.
        root = self.init('proj-rb')
        self.goal(self.store, root)
        self.capture(self.store, root, 'first')
        sync.sync_setup(self.store, str(self.bare), device_name='device-rb')
        sync.pack(self.store, root)
        head = subprocess.run(['git', '-C', str(self.store), 'rev-parse', 'HEAD'],
                              capture_output=True, text=True, encoding='utf-8').stdout.strip()
        sd = self.store_dir(root)
        original = (sd / 'archive' / 'decisions.md').read_bytes()
        (sd / 'archive' / 'decisions.md').write_bytes(b'# tampered\n')
        info = sync.sync_rollback(self.store, root, ref=head, reason='test')
        self.assertGreater(info['files_restored'], 0)
        self.assertEqual((sd / 'archive' / 'decisions.md').read_bytes(), original)


class TestF1ClaimPosix(Batch3Base):
    def test_claim_merges_nested_files_with_posix_keys(self):
        # F1: payload keys must be posix, or the merge allowlist ignores
        # every file (the Windows claim bug). Nested archive/raw files
        # must land, and no changed key may contain a backslash.
        root_a = self.init('proj-a')
        self.goal(self.store, root_a)
        self.capture(self.store, root_a, 'did a thing')
        bundle = self.root / 'bundle'
        self.export(self.store, root_a, bundle)
        store_b = self.root / 'store-b'
        sync.import_bundle(bundle, store_b, confirm=True)
        key = relay.resolve_store_dir(self.store, root_a)[0]['key']
        repo_b = self.make_repo('proj-b', remote='https://example.com/x/proj.git')
        result = sync.sync_claim(store_b, key, repo_b, confirm=True)
        self.assertTrue(result['landed'])
        changed = result['report']['changed_files']
        self.assertIn('raw/cap-0001.md', changed)
        self.assertFalse(any('\\' in c for c in changed))
        self.assertFalse(any(n.startswith('忽略未纳入合并的文件：')
                            for n in result['report']['notes']))


class TestF2ClaimDryRun(Batch3Base):
    def test_claim_dry_run_writes_nothing(self):
        # F2: a dry-run claim must not init the real store -- no project
        # dir, no registry row -- even when the project has no local
        # store yet. The preview still reports the merge.
        root_a = self.init('proj-a')
        self.goal(self.store, root_a)
        self.capture(self.store, root_a, 'did a thing')
        bundle = self.root / 'bundle'
        self.export(self.store, root_a, bundle)
        store_b = self.root / 'store-b'
        sync.import_bundle(bundle, store_b, confirm=True)
        key = relay.resolve_store_dir(self.store, root_a)[0]['key']
        repo_b = self.make_repo('proj-b', remote='https://example.com/x/proj.git')
        preview = sync.sync_claim(store_b, key, repo_b, confirm=False)
        self.assertFalse(preview['landed'])
        self.assertIn('raw/cap-0001.md', preview['report']['changed_files'])
        self.assertFalse((store_b / 'projects').exists())
        self.assertEqual(relay.load_registry(store_b).get('projects'), [])
        # And the real claim afterwards still lands.
        result = sync.sync_claim(store_b, key, repo_b, confirm=True)
        self.assertTrue(result['landed'])
        self.assertTrue((store_b / 'projects').is_dir())


class TestF3ReadyGate(Batch3Base):
    def test_unclaimed_only_unpack_is_not_ready(self):
        # F3: an unpack that lands nothing (only unclaimed) must not
        # announce ready -- the old gate was vacuously true on zero rows.
        root_a = self.init('proj-a')
        self.goal(self.store, root_a)
        self.capture(self.store, root_a, 'did a thing')
        sync.sync_setup(self.store, str(self.bare), device_name='device-a')
        sync.pack(self.store, root_a)
        store_b = self.root / 'store-b'
        sync.sync_setup(store_b, str(self.bare), device_name='device-b')
        result = sync.unpack(store_b, confirm=True)
        self.assertTrue(result['landed'])
        statuses = [p['status'] for r in result['reports'] if 'report' in r
                    for p in r['report']['projects']]
        self.assertIn('unclaimed', statuses)
        self.assertNotIn('matched', statuses)
        self.assertFalse(result['ready'])
        self.assertTrue(result['ready_problems'])


class TestF4ParentOfStore(Batch3Base):
    def test_key_and_pins_live_next_to_store(self):
        # F4: parent-of-store. An isolated store must not read or write
        # the real home key file; the production store ~/.project-relay
        # keeps exactly the old home path.
        isolated = self.root / 'isolated' / 'store'
        sync.sync_setup(isolated, str(self.bare), device_name='device-iso')
        self.assertTrue((self.root / 'isolated' / '.project-relay-sync-key.json').exists())
        self.assertFalse((self.root / '.project-relay-sync-key.json').exists())
        prod = self.root / '.project-relay'
        sync.sync_setup(prod, str(self.bare), device_name='device-prod')
        self.assertTrue((self.root / '.project-relay-sync-key.json').exists())
        self.assertEqual(sync._sync_key_path(prod),
                         self.root / '.project-relay-sync-key.json')
        self.assertEqual(sync._sync_pins_path(prod),
                         self.root / '.project-relay-sync-pins.json')


class TestF5ClaimGuard(Batch3Base):
    def _manual_claim(self, store, key, slug, files):
        dest = store / 'unclaimed' / ('%s-%s' % (key, slug))
        for rel, data in files.items():
            p = dest / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        claim = {'key': key, 'slug': slug, 'display_name': slug,
                 'source_device': 'device-x', 'source_device_id': 'id-x',
                 'pack_seq': 1, 'imported_at': 'now', 'files': sorted(files)}
        sync.save(dest / '_claim.json', claim)
        return dest

    def test_all_ignored_claim_refuses_and_preserves(self):
        # F5: zero files landed + ignored files present => fail, and
        # the unclaimed copy must survive (not be consumed).
        store = self.root / 'store-f5'
        key = '0123456789abcdef'
        dest = self._manual_claim(store, key, 'proj',
                                  {'notes/extra.txt': b'hello\n'})
        repo = self.make_repo('proj', remote='https://example.com/x/proj.git')
        with self.assertRaises(RelayError):
            sync.sync_claim(store, key, repo, confirm=True)
        self.assertTrue((dest / 'notes' / 'extra.txt').exists())
        self.assertTrue((dest / '_claim.json').exists())

    def test_partial_claim_still_lands(self):
        # F5 guards only the zero-landing case; a partial landing (one
        # good file + one ignored file) still lands and reports the
        # ignored file in notes.
        root_a = self.init('proj-a')
        self.goal(self.store, root_a)
        self.capture(self.store, root_a, 'did a thing')
        bundle = self.root / 'bundle'
        self.export(self.store, root_a, bundle)
        store_b = self.root / 'store-b'
        sync.import_bundle(bundle, store_b, confirm=True)
        key = relay.resolve_store_dir(self.store, root_a)[0]['key']
        slug = relay.resolve_store_dir(self.store, root_a)[0]['slug']
        dest = store_b / 'unclaimed' / ('%s-%s' % (key, slug))
        (dest / 'notes').mkdir(exist_ok=True)
        (dest / 'notes' / 'extra.txt').write_bytes(b'hello\n')
        repo_b = self.make_repo('proj-b', remote='https://example.com/x/proj.git')
        result = sync.sync_claim(store_b, key, repo_b, confirm=True)
        self.assertTrue(result['landed'])
        self.assertIn('raw/cap-0001.md', result['report']['changed_files'])
        self.assertTrue(any(n.startswith('忽略未纳入合并的文件：notes/extra.txt')
                            for n in result['report']['notes']))
        self.assertFalse(dest.exists())


class TestC1ManifestValidation(Batch3Base):
    def test_validator_rejects_traversal_and_bad_slug(self):
        sync._validate_project_meta('0123456789abcdef', 'proj')
        for key, slug in (('../../../../escaped', 'proj'),
                          ('', 'proj'),
                          ('0123456789ABCDEF', 'proj'),
                          ('0123456789abcdef', 'a/b'),
                          ('0123456789abcdef', '..'),
                          ('0123456789abcdef', ''),
                          ('0123456789abcdef', 'a\\b')):
            with self.assertRaises(RelayError):
                sync._validate_project_meta(key, slug)

    def test_import_rejects_crafted_key_before_writing(self):
        root_a = self.init('proj-a')
        self.goal(self.store, root_a)
        self.capture(self.store, root_a, 'did a thing')
        bundle = self.root / 'bundle'
        self.export(self.store, root_a, bundle)
        manifest_path = bundle / 'manifest.json'
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        manifest['projects'][0]['key'] = '../../../../escaped'
        manifest_path.write_bytes(json.dumps(manifest).encode('utf-8'))
        store_b = self.root / 'store-b'
        with self.assertRaises(RelayError):
            sync.import_bundle(bundle, store_b, confirm=True)
        self.assertFalse((store_b / 'unclaimed').exists())


class TestC2StopHandler(Batch3Base):
    def test_install_registers_single_stop_handler(self):
        root = self.init('proj-c2')
        home = self.root / 'fakehome'
        flow.install_claude(self.store, root, home_dir=home)
        settings = json.loads((home / '.claude' / 'settings.json').read_text(encoding='utf-8'))
        stop_cmds = [h['hooks'][0]['command'] for h in settings['hooks']['Stop']]
        self.assertEqual(1, len(stop_cmds))
        self.assertIn('hook_capture', stop_cmds[0])
        self.assertIn('hook_enforce', stop_cmds[0])
        self.assertEqual(stop_cmds[0], flow._hook_command(self.store, root, 'stop'))

    def test_reinstall_prunes_legacy_separate_handlers(self):
        root = self.init('proj-c2b')
        home = self.root / 'fakehome'
        (home / '.claude').mkdir(parents=True, exist_ok=True)
        legacy = {'hooks': {
            'SessionStart': [{'hooks': [{'type': 'command',
                                         'command': flow._hook_command(self.store, root, 'resume')}]}],
            'Stop': [{'hooks': [{'type': 'command',
                                 'command': flow._hook_command(self.store, root, 'capture')}]},
                     {'hooks': [{'type': 'command',
                                 'command': flow._hook_command(self.store, root, 'enforce')}]}]}}
        (home / '.claude' / 'settings.json').write_bytes(
            json.dumps(legacy).encode('utf-8'))
        flow.install_claude(self.store, root, home_dir=home)
        settings = json.loads((home / '.claude' / 'settings.json').read_text(encoding='utf-8'))
        stop_cmds = [h['hooks'][0]['command'] for h in settings['hooks']['Stop']]
        self.assertEqual(1, len(stop_cmds))
        self.assertEqual(stop_cmds[0], flow._hook_command(self.store, root, 'stop'))


class TestC3IncompleteTx(Batch3Base):
    SEMANTIC = '决定：采用 X 方案。\n教训：Y 曾踩坑。\n待办：做 Z。\n'

    def _summarized_store(self):
        root = self.init('proj-c3')
        self.goal(self.store, root)
        self.capture(self.store, root, 'first', session='s1')
        self.capture(self.store, root, 'second', session='s2')
        flow.summarize(self.store, root, source='test', semantic_text=self.SEMANTIC)
        sd = self.store_dir(root)
        archive = {n: (sd / 'archive' / n).read_bytes()
                   for n in ('state.md', 'decisions.md', 'lessons.md', 'backlog.md')}
        return root, sd, archive

    def _simulate_crash(self, sd, before_files=()):
        tx = json.loads((sd / 'tx' / '0001.json').read_text(encoding='utf-8'))
        (sd / 'state' / 'summaries' / '0001.json').unlink()
        sync.save(sd / 'state' / 'cursor.json', tx['before_state']['cursor'])
        for seq in (1, 2):
            flow._set_queue(sd, seq, 'pending')
        for name in before_files:
            (sd / 'archive' / name).write_bytes(
                base64.b64decode(tx['files'][name]['before_b64']))

    def test_crash_after_archive_writes_recovers_without_duplicates(self):
        root, sd, archive = self._summarized_store()
        self._simulate_crash(sd)
        flow.summarize(self.store, root, source='test', semantic_text=self.SEMANTIC)
        for name, data in archive.items():
            self.assertEqual((sd / 'archive' / name).read_bytes(), data,
                             'archive file %s changed by the retry (duplicate append?)' % name)
        self.assertEqual(json.loads((sd / 'state' / 'cursor.json').read_text(encoding='utf-8'))['entry_seq'], 2)
        queue = flow.queue_status(sd, repair=False)
        self.assertEqual((queue['pending'], queue['summarized']), (0, 2))

    def test_partial_tx_recovers_without_duplicates(self):
        root, sd, archive = self._summarized_store()
        self._simulate_crash(sd, before_files=('state.md',))
        flow.summarize(self.store, root, source='test', semantic_text=self.SEMANTIC)
        for name, data in archive.items():
            self.assertEqual((sd / 'archive' / name).read_bytes(), data,
                             'archive file %s changed by the retry (duplicate append?)' % name)


class TestC4FolderAbsolute(Batch3Base):
    def test_setup_folder_stores_absolute_pointer(self):
        # C4: a relative --folder must be resolved before it is written
        # to ~/.project-relay-location, or later commands resolve it
        # against their own cwd and find a different store.
        old_cwd = os.getcwd()
        try:
            os.chdir(self.root)
            result = sync.sync_setup_folder('relfolder', device_name='device-f')
        finally:
            os.chdir(old_cwd)
        self.assertEqual(result['store'], str(self.root / 'relfolder'))
        pointer = (self.root / '.project-relay-location').read_text(encoding='utf-8')
        self.assertEqual(pointer, str(self.root / 'relfolder'))

    def test_legacy_relative_pointer_resolves_against_home(self):
        # C4 hardening: a pointer written by a pre-fix version (relative)
        # must resolve against the home dir, never the caller's cwd.
        (self.root / '.project-relay-location').write_bytes(b'relstore')
        elsewhere = self.root / 'elsewhere'
        elsewhere.mkdir()
        old_cwd = os.getcwd()
        try:
            os.chdir(elsewhere)
            with mock.patch.dict(os.environ, {'PROJECT_RELAY_HOME': ''}):
                with mock.patch.object(Path, 'home', return_value=self.root):
                    self.assertEqual(relay.default_store(), self.root / 'relstore')
        finally:
            os.chdir(old_cwd)


class TestC5HandoffSeq(Batch3Base):
    def test_two_handoffs_same_day_keep_both(self):
        # C5: two handoffs on one day without an intervening summary must
        # not share a filename (the old shared summary counter did).
        root = self.init('proj-c5')
        self.goal(self.store, root)
        first = flow.make_handoff(self.store, root)
        first_bytes = Path(first['package']).read_bytes()
        second = flow.make_handoff(self.store, root)
        self.assertNotEqual(first['name'], second['name'])
        self.assertEqual(Path(first['package']).read_bytes(), first_bytes)
        self.assertTrue(Path(second['package']).exists())
        index = (self.store_dir(root) / 'state' / 'handoff-index.md').read_text(encoding='utf-8')
        self.assertIn(first['name'], index)
        self.assertIn(second['name'], index)


class TestC6UnclaimedConflicts(Batch3Base):
    def _two_device_bundles(self):
        remote = 'https://example.com/x/same.git'
        repo_a = self.make_repo_at(self.root / 'dev-a', 'proj', remote=remote)
        relay.init(self.store, repo_a)
        self.goal(self.store, repo_a, '# 目标\n\nA 的目标。\n')
        self.capture(self.store, repo_a, 'A did a thing', session='sa')
        bundle_a = self.root / 'bundle-a'
        self.export(self.store, repo_a, bundle_a, device='device-a')
        store_b = self.root / 'store-b'
        repo_b = self.make_repo_at(self.root / 'dev-b', 'proj', remote=remote)
        relay.init(store_b, repo_b)
        self.goal(store_b, repo_b, '# 目标\n\nB 的目标。\n')
        self.capture(store_b, repo_b, 'B did another thing', session='sb')
        bundle_b = self.root / 'bundle-b'
        self.export(store_b, repo_b, bundle_b, device='device-b')
        key = relay.resolve_store_dir(self.store, repo_a)[0]['key']
        slug = relay.resolve_store_dir(self.store, repo_a)[0]['slug']
        self.assertEqual(key, relay.resolve_store_dir(store_b, repo_b)[0]['key'])
        self.assertEqual(slug, relay.resolve_store_dir(store_b, repo_b)[0]['slug'])
        return store_b, bundle_a, bundle_b, key, slug

    def test_repeat_import_preserves_first_payload(self):
        # C6: two devices exporting the same project before the receiver
        # registers it must not let the second import overwrite the first.
        store_c = self.root / 'store-c'
        _store_b, bundle_a, bundle_b, key, slug = self._two_device_bundles()
        sync.import_bundle(bundle_a, store_c, confirm=True)
        dest = store_c / 'unclaimed' / ('%s-%s' % (key, slug))
        first_cap = (dest / 'raw' / 'cap-0001.md').read_bytes()
        self.assertIn(b'A did a thing', first_cap)
        preview = sync.import_bundle(bundle_b, store_c, confirm=False)
        preview_conflicts = [c['file'] for c in preview['projects'][0]['unclaimed_conflicts']]
        self.assertIn('raw/cap-0001.md', preview_conflicts)
        self.assertTrue(all(c['preserved'] is None
                            for c in preview['projects'][0]['unclaimed_conflicts']))
        report = sync.import_bundle(bundle_b, store_c, confirm=True)
        conflicts = report['projects'][0]['unclaimed_conflicts']
        self.assertIn('raw/cap-0001.md', [c['file'] for c in conflicts])
        # First device's payload intact; second device's version kept
        # as evidence under _conflicts, never merged as payload.
        self.assertEqual((dest / 'raw' / 'cap-0001.md').read_bytes(), first_cap)
        preserved = [c for c in conflicts if c['file'] == 'raw/cap-0001.md'][0]
        preserved_path = store_c / preserved['preserved']
        self.assertIn(b'B did another thing', preserved_path.read_bytes())
        claim = json.loads((dest / '_claim.json').read_text(encoding='utf-8'))
        self.assertTrue(claim.get('unclaimed_conflicts'))

    def test_identical_reimport_is_not_a_conflict(self):
        store_c = self.root / 'store-c'
        _store_b, bundle_a, _bundle_b, key, slug = self._two_device_bundles()
        sync.import_bundle(bundle_a, store_c, confirm=True)
        report = sync.import_bundle(bundle_a, store_c, confirm=True)
        self.assertEqual(report['projects'][0]['unclaimed_conflicts'], [])
        self.assertFalse((store_c / 'unclaimed' / ('%s-%s' % (key, slug)) / '_conflicts').exists())


class TestC7ManifestRefresh(Batch3Base):
    def test_backfill_fidelity_is_manifest_listed(self):
        # C7: the fidelity file is written AFTER summarize refreshes the
        # manifest, so without a refresh here it stays unlisted and later
        # edits evade integrity checks (verify ignores unlisted files).
        proj = self.make_repo('proj-c7')
        log_home = self.root / 'loghome'
        _write_jsonl(log_home / '.codex' / 'sessions' / '2026' / '10' / '28' / 'rollout-test.jsonl', [
            {'type': 'event_msg', 'payload': {'type': 'session_meta', 'cwd': str(proj)}},
            {'type': 'event_msg', 'payload': {'type': 'message', 'role': 'user',
                                              'text': '决定：采用 X 方案'}},
        ])
        report = backfill_mod.backfill(self.store, proj, home=log_home,
                                       tools=['codex'], tier='excerpt',
                                       summarize_after=True)
        self.assertIsNotNone(report.get('summary_seq'))
        sd = self.store_dir(proj)
        manifest = json.loads((sd / 'state' / 'checksum-manifest.json').read_text(encoding='utf-8'))
        self.assertIn('archive/backfill-fidelity.md', manifest['files'])
        fidelity = sd / 'archive' / 'backfill-fidelity.md'
        fidelity.write_bytes(fidelity.read_bytes() + b'\n# tamper\n')
        _present, mismatches = relay.verify_checksum_manifest(sd)
        self.assertTrue(any(m['file'] == 'archive/backfill-fidelity.md' for m in mismatches))

    def test_handoff_is_manifest_listed(self):
        root = self.init('proj-c7h')
        self.goal(self.store, root)
        info = flow.make_handoff(self.store, root)
        sd = self.store_dir(root)
        manifest = json.loads((sd / 'state' / 'checksum-manifest.json').read_text(encoding='utf-8'))
        self.assertIn('archive/handoffs/' + info['name'], manifest['files'])
        target = Path(info['package'])
        target.write_bytes(target.read_bytes() + b'\n# tamper\n')
        _present, mismatches = relay.verify_checksum_manifest(sd)
        self.assertTrue(any(m['file'] == 'archive/handoffs/' + info['name']
                            for m in mismatches))


if __name__ == '__main__':
    unittest.main()
