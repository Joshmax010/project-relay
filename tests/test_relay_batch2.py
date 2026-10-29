"""Codex PR #5 第二批评审 · 第二批修复回归测试（A2、B1–B6）。

Style follows the existing suites: real temporary directories, real Git
repos, PROJECT_RELAY_HOME redirected, no network, no real
~/.project-relay/. Batch 1 (A1, A3-A7) lives in test_relay_batch1.py and
is untouched by this batch; this file only adds coverage for the
second batch. Multi-device B1 tests redirect PROJECT_RELAY_HOME per
device in sequence, because the signing key and TOFU pins are
home-scoped (that scoping is the point of the design).
"""
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
    spec.loader.exec_module(mod)
    return mod


relay = _load('relay')
flow = _load('relay_flow')
sync = _load('relay_sync')
FlowError = flow.relay.RelayError
SyncError = sync.RelayError


class Batch2Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='relay-batch2-')
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
        (root / 'app.py').write_text('value = 1\n')
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

    def change_code(self, repo, text):
        (repo / 'app.py').write_text(text)
        self.git(repo, 'add', '.')
        self.git(repo, 'commit', '-m', 'change')

    def capture(self, store, repo, message, session='s1'):
        return flow.capture(store, repo, tool='codex', session=session,
                            message=message)

    def summarize_two(self, store, repo):
        """Two summarize rounds; returns (store_dir, first_baseline_bytes)."""
        sd = self.store_dir(repo, store)
        self.capture(store, repo, 'first')
        self.change_code(repo, 'value = 2\n')
        self.capture(store, repo, 'second')
        flow.summarize(store, repo, source='test', semantic_text=None)
        first_baseline = (sd / 'state' / 'baseline.json').read_bytes()
        self.change_code(repo, 'value = 3\n')
        self.capture(store, repo, 'third')
        flow.summarize(store, repo, source='test', semantic_text=None)
        return sd, first_baseline

    @contextlib.contextmanager
    def home(self, name):
        """Run a block with PROJECT_RELAY_HOME pointed at a per-device home."""
        home = self.root / name
        home.mkdir(parents=True, exist_ok=True)
        with mock.patch.dict(os.environ, {'PROJECT_RELAY_HOME': str(home)}):
            yield home


class TestA2RecoverRollback(Batch2Base):
    def test_recover_after_success_rolls_back_everything(self):
        root = self.init()
        sd = self.store_dir(root)
        before_state = (sd / 'archive' / 'state.md').read_bytes()
        self.capture(self.store, root, 'first')
        self.change_code(root, 'value = 2\n')
        self.capture(self.store, root, 'second')
        flow.summarize(self.store, root, source='test', semantic_text=None)
        # Premise check (A2): the tx log is still there after success.
        self.assertTrue(list((sd / 'tx').glob('*.json')))
        self.assertEqual(json.loads((sd / 'state' / 'cursor.json').read_text())['entry_seq'], 2)
        self.assertTrue((sd / 'state' / 'summaries' / '0001.json').exists())
        info = flow.recover(self.store, root)
        # Archive, cursor, baseline, last-summary, summary record, queues
        # must all agree with the pre-transaction state -- no cursor-new /
        # archive-old mismatch left behind.
        self.assertEqual((sd / 'archive' / 'state.md').read_bytes(), before_state)
        self.assertEqual(json.loads((sd / 'state' / 'cursor.json').read_text()),
                         {'entry_seq': 0, 'anchor_message': None})
        self.assertFalse((sd / 'state' / 'baseline.json').exists())
        self.assertFalse((sd / 'state' / 'last-summary.json').exists())
        # Tombstone: the record stays, annotated, so sequence numbers
        # stay monotonic (an older test pins that across recover).
        self.assertTrue(json.loads(
            (sd / 'state' / 'summaries' / '0001.json').read_text())['rolled_back'])
        self.assertEqual(sorted(info['entries_reverted_to_pending']), [1, 2])
        queue = flow.queue_status(sd, repair=False)
        self.assertEqual((queue['pending'], queue['summarized']), (2, 0))
        _present, mismatches = relay.verify_checksum_manifest(sd)
        self.assertEqual(mismatches, [])
        # Self-consistency proof: a fresh summarize re-consumes the batch
        # with the NEXT sequence number (tombstone keeps seq monotonic).
        rec = flow.summarize(self.store, root, source='test', semantic_text=None)
        self.assertEqual(rec['seq'], 2)
        self.assertEqual(rec['entry_range'], [1, 2])
        # The recover receipt itself must never be re-picked as a log.
        flow.recover(self.store, root)
        with self.assertRaises(FlowError) as ctx:
            flow.recover(self.store, root)
        self.assertIn('无事务日志', str(ctx.exception))

    def test_recover_latest_only_after_two_summaries(self):
        root = self.init()
        sd, first_baseline = self.summarize_two(self.store, root)
        info = flow.recover(self.store, root)
        self.assertEqual(info['baseline_summary_seq'], 1)
        self.assertEqual((sd / 'state' / 'baseline.json').read_bytes(), first_baseline)
        self.assertEqual(json.loads((sd / 'state' / 'last-summary.json').read_text())['seq'], 1)
        self.assertFalse(json.loads(
            (sd / 'state' / 'summaries' / '0001.json').read_text()).get('rolled_back', False))
        self.assertTrue(json.loads(
            (sd / 'state' / 'summaries' / '0002.json').read_text())['rolled_back'])
        self.assertEqual(json.loads((sd / 'state' / 'cursor.json').read_text())['entry_seq'], 2)
        queue = flow.queue_status(sd, repair=False)
        self.assertEqual((queue['pending'], queue['summarized']), (1, 1))
        # The first batch's oldest entry was archived by the second
        # summarize's lifecycle move; it stays summarized, just not in
        # raw/ (queue_status only scans raw/).
        archived = [e for e in flow.list_entries(sd, include_archived=True)
                    if e['header'].get('queue') == 'summarized']
        self.assertEqual(len(archived), 2)
        _present, mismatches = relay.verify_checksum_manifest(sd)
        self.assertEqual(mismatches, [])

    def test_recover_legacy_log_derives_cursor_and_drops_baseline(self):
        root = self.init()
        sd, _first = self.summarize_two(self.store, root)
        tx_path = sd / 'tx' / '0002.json'
        tx = json.loads(tx_path.read_text())
        tx.pop('before_state', None)
        tx_path.write_text(json.dumps(tx, ensure_ascii=False, indent=2))
        flow.recover(self.store, root)
        self.assertEqual(json.loads((sd / 'state' / 'cursor.json').read_text())['entry_seq'], 2)
        # Legacy baseline snapshot is unrecoverable: dropped, not faked.
        self.assertFalse((sd / 'state' / 'baseline.json').exists())
        self.assertEqual(json.loads((sd / 'state' / 'last-summary.json').read_text())['seq'], 1)
        queue = flow.queue_status(sd, repair=False)
        self.assertEqual((queue['pending'], queue['summarized']), (1, 1))

    def test_recover_legacy_log_without_summary_record_fails_closed(self):
        root = self.init()
        sd, _first = self.summarize_two(self.store, root)
        tx_path = sd / 'tx' / '0002.json'
        tx = json.loads(tx_path.read_text())
        tx.pop('before_state', None)
        tx_path.write_text(json.dumps(tx, ensure_ascii=False, indent=2))
        (sd / 'state' / 'summaries' / '0002.json').unlink()
        before_state = (sd / 'archive' / 'state.md').read_bytes()
        with self.assertRaises(FlowError):
            flow.recover(self.store, root)
        self.assertTrue(tx_path.exists())
        self.assertEqual((sd / 'archive' / 'state.md').read_bytes(), before_state)


class TestB1Signing(Batch2Base):
    def test_ed25519_rfc8032_vector(self):
        # RFC 8032 test vector 1 (empty message), verified against the
        # published vector, not just sign/verify round-trip.
        seed = bytes.fromhex('9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60')
        pub = sync._ed_public_from_seed(seed)
        self.assertEqual(pub.hex(),
                         'd75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a')
        sig = sync._ed_sign(seed, b'')
        self.assertEqual(sig.hex(),
                         'e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155'
                         '5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b')
        self.assertTrue(sync._ed_verify(pub, b'', sig))
        self.assertFalse(sync._ed_verify(pub, b'x', sig))

    def _device_a_pack(self, home_name, store_name, repo_name):
        """Pack once from device A; returns (home, store, repo, manifest_path, seed_hex, manifest_files)."""
        with self.home(home_name) as home:
            store = self.root / store_name
            repo = self.make_repo(repo_name, str(self.bare))
            relay.init(store, repo)
            sync.sync_setup(store, str(self.bare), device_name='device-a')
            relay.goal_draft(store, repo, '# 目标\n\n演示。\n')
            relay.goal_confirm(store, repo)
            self.capture(store, repo, 'hello')
            result = sync.pack(store, repo)
            manifests = sorted((store / 'sync-manifests').glob('*.json'))
            self.assertEqual(len(manifests), 1)
            key = sync.ensure_sync_key()
            return home, store, repo, manifests[0], key['private_seed'], result['manifest']['files']

    def test_pack_unpack_roundtrip_tofu_then_verified(self):
        home_a, store_a, _repo_a, manifest_path, seed_hex, manifest_files = self._device_a_pack(
            'home-a', 'store-a', 'proj-a')
        manifest = json.loads(manifest_path.read_text())
        self.assertIn('signature', manifest)
        self.assertEqual(manifest['signature']['algorithm'], 'ed25519')
        self.assertEqual(manifest['file_hashes'], manifest_files)
        # Private seed never lands in the store tree (committed or not).
        for path in store_a.rglob('*'):
            if path.is_file() and '.git' not in path.parts:
                self.assertNotIn(seed_hex, path.read_text(encoding='utf-8', errors='ignore'))
        key_path = home_a / '.project-relay-sync-key.json'
        self.assertEqual(stat.S_IMODE(key_path.stat().st_mode), 0o600)
        with self.home('home-b') as home_b:
            store_b = self.root / 'store-b'
            repo_b = self.make_repo('proj-b', str(self.bare))
            relay.init(store_b, repo_b)
            sync.sync_setup(store_b, str(self.bare), device_name='device-b')
            report = sync.unpack(store_b, confirm=True)
            verification = report['reports'][0]['report']['signature_verification']
            self.assertEqual(verification['status'], 'tofu-pinned')
            pins = json.loads((home_b / '.project-relay-sync-pins.json').read_text())
            device_a_id = json.loads((store_a / 'sync-config.json').read_text())['device_id']
            self.assertEqual(pins[device_a_id]['public_key'],
                             manifest['signature']['public_key'])
            # Second pack/unpack round verifies against the pin.
        with self.home('home-a'):
            self.change_code(_repo_a, 'value = 2\n')
            self.capture(store_a, _repo_a, 'world')
            sync.pack(store_a, _repo_a)
        with self.home('home-b'):
            report2 = sync.unpack(store_b, confirm=True)
            verification2 = report2['reports'][0]['report']['signature_verification']
            self.assertEqual(verification2['status'], 'verified')

    def _fake_clone_with(self, manifest_dict, project_files):
        """A clone-shaped dir (projects/ + sync-manifests/) for choke-point tests."""
        clone = self.root / 'fake-clone'
        if clone.exists():
            import shutil
            shutil.rmtree(clone)
        dirname = '%s-%s' % (manifest_dict['key'], manifest_dict['slug'])
        dest = clone / 'projects' / dirname
        for rel, data in project_files.items():
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        (clone / 'sync-manifests').mkdir(parents=True)
        (clone / 'sync-manifests' / ('%s-0001.json' % manifest_dict['key'])).write_text(
            json.dumps(manifest_dict, ensure_ascii=False, indent=2))
        return clone

    def test_tampered_git_bundle_refused_before_landing(self):
        home_a, store_a, _repo_a, manifest_path, _seed, _mf = self._device_a_pack(
            'home-a2', 'store-a2', 'proj-a2')
        manifest = json.loads(manifest_path.read_text())
        dirname = '%s-%s' % (manifest['key'], manifest['slug'])
        project_files = {}
        for rel in manifest['files']:
            project_files[rel[len('projects/%s/' % dirname):]] = \
                (store_a / 'projects' / dirname / rel[len('projects/%s/' % dirname):]).read_bytes()
        # Tamper one payload file's bytes; the committed manifest (paths
        # only) cannot see it, but the signature over recomputed hashes can.
        raw_rel = next(r for r in project_files if r.startswith('raw/cap-'))
        project_files[raw_rel] = project_files[raw_rel] + '\n篡改行\n'.encode('utf-8')
        clone = self._fake_clone_with(manifest, project_files)
        with self.home('home-b2'):
            store_b = self.root / 'store-b2'
            repo_b = self.make_repo('proj-b2', str(self.bare))
            relay.init(store_b, repo_b)
            before = {str(p.relative_to(store_b)): p.read_bytes()
                      for p in store_b.rglob('*') if p.is_file()}
            with self.assertRaises(SyncError) as ctx:
                sync._bundle_dir_from_clone(clone, manifest['key'], self.root / 'work-b2')
            self.assertIn('验签失败', str(ctx.exception))
            after = {str(p.relative_to(store_b)): p.read_bytes()
                     for p in store_b.rglob('*') if p.is_file()}
            self.assertEqual(before, after)

    def test_key_change_refused_then_repin_accepted(self):
        home_a, store_a, repo_a, manifest_path, _seed, _mf = self._device_a_pack(
            'home-a3', 'store-a3', 'proj-a3')
        with self.home('home-b3'):
            store_b = self.root / 'store-b3'
            repo_b = self.make_repo('proj-b3', str(self.bare))
            relay.init(store_b, repo_b)
            sync.sync_setup(store_b, str(self.bare), device_name='device-b')
            sync.unpack(store_b, confirm=True)  # pins A's key
        # Rotate A's key (new seed, same device_id/config) and re-pack.
        with self.home('home-a3'):
            (home_a / '.project-relay-sync-key.json').unlink()
            new_key = sync.ensure_sync_key()
            self.assertNotEqual(new_key['key_id'],
                                json.loads(manifest_path.read_text())['signature']['key_id'])
            self.change_code(repo_a, 'value = 9\n')
            self.capture(store_a, repo_a, 'rotated')
            sync.pack(store_a, repo_a)
        with self.home('home-b3'):
            with self.assertRaises(SyncError) as ctx:
                sync.unpack(store_b, confirm=True)
            self.assertIn('--repin', str(ctx.exception))
            report = sync.unpack(store_b, confirm=True, repin=True)
            verification = report['reports'][0]['report']['signature_verification']
            self.assertEqual(verification['status'], 'repinned')
            receipts = list((store_b / 'sync-receipts').glob('repin-*.json'))
            self.assertTrue(receipts)
            receipt = json.loads(receipts[0].read_text())
            self.assertEqual(receipt['key_id'], verification['key_id'])
            self.assertEqual(receipt['previous_key_id'], verification['previous_key_id'])

    def test_unsigned_refused_then_trust_unsigned_accepted(self):
        home_a, store_a, _repo_a, manifest_path, _seed, _mf = self._device_a_pack(
            'home-a4', 'store-a4', 'proj-a4')
        manifest = json.loads(manifest_path.read_text())
        manifest.pop('signature', None)
        manifest.pop('file_hashes', None)
        dirname = '%s-%s' % (manifest['key'], manifest['slug'])
        project_files = {}
        for rel in manifest['files']:
            project_files[rel[len('projects/%s/' % dirname):]] = \
                (store_a / 'projects' / dirname / rel[len('projects/%s/' % dirname):]).read_bytes()
        clone = self._fake_clone_with(manifest, project_files)
        with self.home('home-b4'):
            store_b = self.root / 'store-b4'
            repo_b = self.make_repo('proj-b4', str(self.bare))
            relay.init(store_b, repo_b)
            with self.assertRaises(SyncError) as ctx:
                sync._bundle_dir_from_clone(clone, manifest['key'], self.root / 'work-b4')
            self.assertIn('未签名', str(ctx.exception))
            bundle = sync._bundle_dir_from_clone(
                clone, manifest['key'], self.root / 'work-b4b', trust_unsigned=True)
            report = sync.import_bundle(bundle, store_b, confirm=True)
            self.assertEqual(report['signature_verification']['status'], 'trusted-unsigned')

    def test_cli_flags_accepted(self):
        args = relay._parser().parse_args(
            ['pack', '--trust-unsigned', '--repin'])
        self.assertTrue(args.trust_unsigned and args.repin)
        args = relay._parser().parse_args(
            ['unpack', '--confirm', '--trust-unsigned'])
        self.assertTrue(args.trust_unsigned and not args.repin)


class TestB2Quarantine(Batch2Base):
    def _export_with_tampered_entry(self, tamper_seq=2):
        store_a = self.root / 'store-a'
        repo_a = self.make_repo('proj-a', 'https://example.com/x/proj.git')
        relay.init(store_a, repo_a)
        self.capture(store_a, repo_a, 'hello')
        self.change_code(repo_a, 'value = 2\n')
        self.capture(store_a, repo_a, 'world')
        flow.summarize(store_a, repo_a, source='test', semantic_text=None)
        bundle = self.root / 'bundle'
        manifest = sync.export_bundle(store_a, [repo_a], bundle, 'device-a', 'id-a',
                                      pack_seqs={})
        proj = manifest['projects'][0]
        cap = bundle / 'projects' / ('%s-%s' % (proj['key'], proj['slug'])) \
            / 'raw' / ('cap-%04d.md' % tamper_seq)
        text = cap.read_text()
        head, body = text.split('---relay-body---', 1)
        m = re.search(r'"sha256": "([0-9a-f]{64})"', head)
        self.assertIsNotNone(m, 'expected a sha in the entry header')
        head2 = head[:m.start(1)] + ('0' if m.group(1)[0] != '0' else '1') + head[m.start(1) + 1:]
        cap.write_text(head2 + '---relay-body---' + body)
        rel = 'projects/%s-%s/raw/cap-%04d.md' % (proj['key'], proj['slug'], tamper_seq)
        manifest['files'][rel] = sync._sha256(cap.read_bytes())
        (bundle / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
        return bundle, proj, cap.read_bytes()

    def test_matched_id_mismatch_quarantined_not_landed_not_counted(self):
        bundle, proj, tampered_bytes = self._export_with_tampered_entry(2)
        store_b = self.root / 'store-b'
        repo_b = self.make_repo('proj-b', 'https://example.com/x/proj.git')
        relay.init(store_b, repo_b)
        report = sync.import_bundle(bundle, store_b, confirm=True)
        entry = report['projects'][0]
        self.assertTrue(any(d['reason'] == 'id-hash-mismatch' for d in entry['discarded']))
        sd_b = self.store_dir(repo_b, store_b)
        raw_names = sorted(p.name for p in (sd_b / 'raw').glob('cap-*.md'))
        self.assertEqual(raw_names, ['cap-0001.md'])
        queue = flow.queue_status(sd_b, repair=False)
        self.assertEqual((queue['pending'], queue['summarized']), (0, 1))
        log_dir = Path(str(entry['merge_log'])[:-5])
        quarantined = list((log_dir / 'quarantined').glob('*'))
        self.assertEqual(len(quarantined), 1)
        self.assertEqual(quarantined[0].read_bytes(), tampered_bytes)
        self.assertNotIn('raw/cap-0002.md', entry['changed_files'])

    def test_unclaimed_id_mismatch_quarantined_and_claim_does_not_resurrect(self):
        bundle, proj, tampered_bytes = self._export_with_tampered_entry(1)
        store_b = self.root / 'store-b'
        repo_b = self.make_repo('proj-b', 'https://example.com/other/proj.git')
        relay.init(store_b, repo_b)
        report = sync.import_bundle(bundle, store_b, confirm=True)
        entry = report['projects'][0]
        self.assertEqual(entry['status'], 'unclaimed')
        self.assertTrue(any(d['reason'] == 'id-hash-mismatch' for d in entry['discarded']))
        unc = Path(entry['unclaimed_dir'])
        self.assertEqual(sorted(p.name for p in (unc / 'raw').glob('cap-*.md')),
                         ['cap-0002.md'])
        quarantined = list((unc / '_quarantine').glob('*'))
        self.assertEqual(len(quarantined), 1)
        self.assertEqual(quarantined[0].read_bytes(), tampered_bytes)
        result = sync.sync_claim(store_b, proj['key'], repo_b, confirm=True)
        sd_b = self.store_dir(repo_b, store_b)
        # The good entry lands (renumbered onto the empty local store);
        # the quarantined one never reaches raw/.
        self.assertEqual(sorted(p.name for p in (sd_b / 'raw').glob('cap-*.md')),
                         ['cap-0001.md'])

    def test_import_redaction_is_field_level_for_entries(self):
        header = {'version': 1, 'seq': 1, 'captured_at': '2026-10-29T00:00:00+00:00',
                  'tool': 'codex', 'session': 's1',
                  'anchor': {'branch': 'main', 'head': 'abc'},
                  'fingerprint': {'anchor': {'branch': 'main', 'head': 'abc'},
                                  'code': {'x.py': {'sha256': '13812345678'}},
                                  'gaps': []},
                  'fact_hash': 'deadbeef', 'queue': 'pending'}
        text = (json.dumps(header, ensure_ascii=False, sort_keys=True, indent=2)
                + '\n---relay-body---\n联系方式 13812345678 请勿外泄\n')
        files, _hits, _markers, discarded, _q = sync._redact_incoming(
            {'raw/cap-0001.md': text.encode('utf-8')})
        self.assertEqual(discarded, [])
        out_header, out_body = sync._parse_entry_text(files['raw/cap-0001.md'].decode('utf-8'))
        self.assertEqual(out_header['fingerprint'], header['fingerprint'])
        self.assertNotIn('13812345678', out_body)


class TestB3TemplateState(Batch2Base):
    def _template_text(self):
        return (SKILL / 'assets' / 'template-v2' / 'state.md').read_text()

    def _incoming_text(self):
        return """# 当前状态

<!-- relay:state-begin -->
- 当前分支@基线：main@abc1234
- 正在做什么：写登录接口
- 下一动作：跑测试
- 已完成且有证据：登录接口（验证：测试通过）；支付接口（验证：测试通过）
- 已改动但尚未验证：支付模块
- 阻塞待确认：等产品确认文案
- 最近会话：汇总 3，覆盖条目 1-5
<!-- relay:state-end -->
"""

    def test_template_sections_adopt_incoming(self):
        merged, conflicts, notes = sync._merge_state_md(
            self._template_text(), self._incoming_text())
        for line in ('- 正在做什么：写登录接口', '- 下一动作：跑测试',
                     '- 已改动但尚未验证：支付模块', '- 阻塞待确认：等产品确认文案',
                     '- 最近会话：汇总 3，覆盖条目 1-5'):
            self.assertIn(line, merged)
        # Union line adopts without the template placeholder polluting it.
        self.assertIn('- 已完成且有证据：登录接口（验证：测试通过）；支付接口（验证：测试通过）',
                      merged)
        self.assertNotIn('未知；', merged)
        # Branch line is local measurement: exempt from template-wins.
        self.assertIn('- 当前分支@基线：未知', merged)
        self.assertTrue(any('模板原文' in n for n in notes))
        self.assertFalse(any(c.get('key') == '正在做什么' for c in conflicts))

    def test_user_modified_section_keeps_local_with_fork_note(self):
        local = self._template_text().replace('- 正在做什么：未知',
                                              '- 正在做什么：我自己的事')
        merged, conflicts, _notes = sync._merge_state_md(local, self._incoming_text())
        self.assertIn('- 正在做什么：我自己的事', merged)
        self.assertIn('- 分叉待裁：正在做什么（对方）：写登录接口', merged)
        # The other template sections still adopt.
        self.assertIn('- 下一动作：跑测试', merged)
        self.assertTrue(any(c.get('key') == '正在做什么' for c in conflicts))

    def test_incoming_template_value_does_not_adopt(self):
        merged, _conflicts, _notes = sync._merge_state_md(
            self._template_text(), self._template_text())
        self.assertEqual(merged, self._template_text())


class TestB4CaptureDedupMessage(Batch2Base):
    def test_deduped_message_prints_drop_notice(self):
        root = self.init()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = relay.main(['--store-root', str(self.store), '--root', str(root),
                               'capture', '--tool', 'codex', '--session', 's1',
                               '--message', '第一条'])
        self.assertEqual(code, 0)
        self.assertIn('采集条目', buf.getvalue())
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = relay.main(['--store-root', str(self.store), '--root', str(root),
                               'capture', '--tool', 'codex', '--session', 's1',
                               '--message', '第二条被丢的消息'])
        self.assertEqual(code, 0)
        out = buf.getvalue()
        self.assertIn('新消息文本已被去重丢弃，未留存', out)
        self.assertIn('换 session', out)
        entries = flow.list_entries(self.store_dir(root))
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['header']['user_message'], '第一条')


@contextlib.contextmanager
def _no_git_identity_env():
    """Environment with no git identity anywhere (no global, no system,
    no author/committer env): the B5 scenario."""
    popped = {}
    for key in ('GIT_AUTHOR_NAME', 'GIT_AUTHOR_EMAIL',
                'GIT_COMMITTER_NAME', 'GIT_COMMITTER_EMAIL'):
        if key in os.environ:
            popped[key] = os.environ.pop(key)
    global_cfg = tempfile.NamedTemporaryFile(prefix='relay-gitconfig-',
                                             delete=False)
    global_cfg.close()
    empty_home = tempfile.mkdtemp(prefix='relay-empty-home-')
    with mock.patch.dict(os.environ, {
            'GIT_CONFIG_NOSYSTEM': '1',
            'GIT_CONFIG_GLOBAL': global_cfg.name,
            'HOME': empty_home}):
        try:
            yield Path(global_cfg.name)
        finally:
            os.unlink(global_cfg.name)
            os.environ.update(popped)


class TestB5GitIdentity(Batch2Base):
    def test_pack_without_global_identity_succeeds_and_leaves_global_alone(self):
        with _no_git_identity_env() as global_cfg:
            store = self.root / 'store-noid'
            repo = self.make_repo('proj-noid', str(self.bare))
            relay.init(store, repo)
            sync.sync_setup(store, str(self.bare), device_name='device-noid')
            result = sync.pack(store, repo)
            self.assertEqual(result['seq'], 1)
            name = subprocess.run(['git', '-C', str(store), 'config', '--local',
                                   '--get', 'user.name'],
                                  capture_output=True, text=True).stdout.strip()
            email = subprocess.run(['git', '-C', str(store), 'config', '--local',
                                    '--get', 'user.email'],
                                   capture_output=True, text=True).stdout.strip()
            self.assertEqual((name, email), ('relay', 'relay@project-relay.invalid'))
            self.assertEqual(global_cfg.read_bytes(), b'')
            # Global config is untouched: nothing was written to it.
            self.assertFalse(global_cfg.read_text())

    def test_existing_local_identity_is_not_overwritten(self):
        store = self.root / 'store-alice'
        repo = self.make_repo('proj-alice', str(self.bare))
        relay.init(store, repo)
        sync.sync_setup(store, str(self.bare), device_name='device-alice')
        self.git(store, 'config', 'user.name', 'Alice')
        self.git(store, 'config', 'user.email', 'alice@example.invalid')
        sync.pack(store, repo)
        name = subprocess.run(['git', '-C', str(store), 'config', '--local',
                               '--get', 'user.name'],
                              capture_output=True, text=True).stdout.strip()
        self.assertEqual(name, 'Alice')


class TestB6Noise(Batch2Base):
    def test_branch_normalization(self):
        self.assertEqual(sync._normalize_branch('refs/heads/main'), 'main')
        self.assertEqual(sync._normalize_branch('main'), 'main')
        self.assertEqual(sync._normalize_branch('refs/heads/feature/x'), 'feature/x')
        root = self.init()
        sd = self.store_dir(root)
        state = (sd / 'archive' / 'state.md').read_text()
        state = state.replace('- 当前分支@基线：未知', '- 当前分支@基线：refs/heads/main@abc')
        (sd / 'archive' / 'state.md').write_text(state)
        audit = sync._consistency_audit(self.store, root, sd)
        self.assertNotIn('branch_mismatch', audit)
        state = state.replace('refs/heads/main@abc', 'other@abc')
        (sd / 'archive' / 'state.md').write_text(state)
        audit = sync._consistency_audit(self.store, root, sd)
        self.assertIn('branch_mismatch', audit)

    def test_fork_lines_each_name_their_section(self):
        local = (SKILL / 'assets' / 'template-v2' / 'state.md').read_text()
        for key in ('当前分支@基线', '正在做什么', '下一动作', '已改动但尚未验证',
                    '阻塞待确认', '最近会话'):
            local = local.replace('- %s：未知' % key, '- %s：本地值-%s' % (key, key))
        incoming = local
        for key in ('当前分支@基线', '正在做什么', '下一动作', '已改动但尚未验证',
                    '阻塞待确认', '最近会话'):
            incoming = incoming.replace('本地值-%s' % key, '对方值-%s' % key)
        merged, conflicts, _notes = sync._merge_state_md(local, incoming)
        fork_lines = [l for l in merged.splitlines() if l.startswith('- 分叉待裁：')]
        self.assertEqual(len(fork_lines), 6)
        self.assertEqual(len(set(fork_lines)), 6)
        for key in ('正在做什么', '下一动作', '已改动但尚未验证', '阻塞待确认', '最近会话'):
            self.assertTrue(any(key in l for l in fork_lines), key)
        # Report rendering must carry the section name too, or six
        # conflicts print six identical lines.
        entry = {'slug': 'proj', 'status': 'matched',
                 'counts': {'entries_added': 0, 'entries_skipped': 0},
                 'conflicts': conflicts, 'goal_action': 'skip'}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            relay._print_sync_project(entry)
        lines = [l for l in buf.getvalue().splitlines() if l.strip().startswith('冲突[')]
        self.assertEqual(len(lines), len(set(lines)))
        for key in ('正在做什么', '下一动作'):
            self.assertTrue(any(key in l for l in lines), key)


if __name__ == '__main__':
    unittest.main()
