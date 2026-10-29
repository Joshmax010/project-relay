"""P0 regression tests (2026-10-29 e2e): pack's redact rescan must never
damage raw entry fingerprints.

Style follows the sync suite: real temporary directories, real Git repos,
local bare repos stand in for remotes -- no network, no real remote ever
touched. Each device gets its own store root; PROJECT_RELAY_HOME is
patched as a belt and braces so no test ever touches the developer's
real ~/.project-relay/. Git identity comes from env vars (no global
config assumed).
"""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
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

# 64-hex containing an 11-digit run bounded by hex letters on both sides:
# the pre-fix mobile_cn pattern matched this run and deleted it (64 -> 53).
PHONE_LIKE_HEX = ('ab16079936577c'
                  'd2e0f9a1b3c5d7e8f0a2b4c6d8e0f1a2b3c4d5e6f'
                  '12ab34cde')
OLD_MOBILE_CN = re.compile(r'(?<!\d)1[3-9]\d{9}(?!\d)')


def _fact_hash(fp):
    return hashlib.sha256(json.dumps(
        {'anchor': fp['anchor'], 'code': fp['code'], 'gaps': fp['gaps']},
        sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()


class P0Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='relay-p0-')
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

    def make_repo(self, name, remote=None, extra_files=None):
        root = self.root / name
        root.mkdir()
        self.git(root, 'init', '-b', 'main')
        self.git(root, 'config', 'user.name', 'Tester')
        self.git(root, 'config', 'user.email', 'test@example.invalid')
        self.git(root, 'config', 'commit.gpgsign', 'false')
        (root / 'app.py').write_text('value = 1\n')
        for name_, data in (extra_files or {}).items():
            p = root / name_
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data if isinstance(data, bytes) else data.encode('utf-8'))
        self.git(root, 'add', '.')
        self.git(root, 'commit', '-m', 'baseline')
        if remote:
            self.git(root, 'remote', 'add', 'origin', remote)
        return root

    def device(self, name, remote=REMOTE_URL, extra_files=None):
        repo = self.make_repo(name, remote, extra_files)
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

    def entry_path(self, store, repo, seq):
        return self.store_dir(store, repo) / 'raw' / ('cap-%04d.md' % seq)

    def read_entry(self, store, repo, seq):
        return sync._parse_entry_text(self.entry_path(store, repo, seq)
                                      .read_text(encoding='utf-8'))

    def craft_phone_like_entry(self, store, repo, seq, phone='13800138000'):
        """Rewrite one entry's fingerprint.code to a 64-hex containing a
        phone-like digit run, recompute fact_hash so the entry stays
        verifiable, and append a real phone number to the body (body is
        not covered by fact_hash, so this is safe for verification)."""
        header, body = self.read_entry(store, repo, seq)
        fp = header['fingerprint']
        fp['code']['docs/attachment.jpg'] = {'sha256': PHONE_LIKE_HEX,
                                            'kind': 'file', 'executable': False}
        header['fact_hash'] = _fact_hash(fp)
        body = body + '\n联系方式 %s\n' % phone
        flow._write_entry(self.store_dir(store, repo), header, body)
        return header

    def roundtrip(self, store_a, repo_a, store_b, repo_b):
        bundle = self.root / 'bundle'
        sync.export_bundle(store_a, [repo_a], bundle, 'device-a', 'id-a')
        sync.import_bundle(bundle, store_b, confirm=True)
        return bundle


class TestP0PhoneLikeHex(P0Base):
    def test_phone_like_hex_survives_roundtrip_and_body_phone_redacted(self):
        # The old pattern must match the crafted hex: otherwise this test
        # would not have caught P0.
        self.assertIsNotNone(OLD_MOBILE_CN.search(PHONE_LIKE_HEX))
        self.assertEqual(len(PHONE_LIKE_HEX), 64)
        store_a, repo_a = self.device('proj-a')
        self.goal(store_a, repo_a, '# 目标\n\n演示。\n')
        self.capture(store_a, repo_a, '实现了登录接口')
        header = self.craft_phone_like_entry(store_a, repo_a, 1)
        self.assertTrue(sync._verify_entry_id(header))
        before = self.entry_path(store_a, repo_a, 1).read_bytes()

        store_b, repo_b = self.device('proj-b')
        self.roundtrip(store_a, repo_a, store_b, repo_b)

        # Source: fingerprint untouched by the rescan write-back, body
        # phone redacted by it.
        a_text = self.entry_path(store_a, repo_a, 1).read_text(encoding='utf-8')
        a_header, _ = sync._parse_entry_text(a_text)
        self.assertEqual(a_header['fingerprint']['code']['docs/attachment.jpg']['sha256'],
                         PHONE_LIKE_HEX)
        self.assertTrue(sync._verify_entry_id(a_header))
        self.assertNotIn('13800138000', a_text)
        # Peer: entry landed (not id-hash-discarded) and still verifies.
        b_path = self.entry_path(store_b, repo_b, 1)
        self.assertTrue(b_path.exists())
        b_text = b_path.read_text(encoding='utf-8')
        b_header, _ = sync._parse_entry_text(b_text)
        self.assertEqual(b_header['fact_hash'], header['fact_hash'])
        self.assertEqual(b_header['fingerprint']['code']['docs/attachment.jpg']['sha256'],
                         PHONE_LIKE_HEX)
        self.assertTrue(sync._verify_entry_id(b_header))
        self.assertNotIn('13800138000', b_text)
        self.assertNotEqual(before, self.entry_path(store_a, repo_a, 1).read_bytes())

    def test_gate_blocks_hand_broken_fingerprint_without_mutation(self):
        store_a, repo_a = self.device('proj-a')
        self.setup(store_a)
        self.goal(store_a, repo_a, '# 目标\n\n演示。\n')
        self.capture(store_a, repo_a, '实现了登录接口')
        # Break one fingerprint char without recomputing fact_hash.
        header, body = self.read_entry(store_a, repo_a, 1)
        fp = header['fingerprint']
        code = fp['code']
        key = sorted(k for k, v in code.items() if isinstance(v, dict) and 'sha256' in v)[0]
        sha = code[key]['sha256']
        code[key]['sha256'] = ('0' if sha[0] != '0' else '1') + sha[1:]
        flow._write_entry(self.store_dir(store_a, repo_a), header, body)
        # Pack step 1 legitimately summarizes the pending entry first
        # (queue pending -> summarized); snapshot AFTER that step so the
        # assertion measures exactly what the step-4 gate must not do.
        flow.summarize(store_a, repo_a, source='pack', semantic_text=None)
        entry_file = self.entry_path(store_a, repo_a, 1)
        before = entry_file.read_bytes()
        receipts = self.store_dir(store_a, repo_a) / 'sync-receipts'
        with self.assertRaises(SyncError) as ctx:
            sync.pack(store_a, repo_a)
        self.assertIn('验门', str(ctx.exception))
        # Zero local mutation, no pack receipt, no bundle pushed.
        self.assertEqual(entry_file.read_bytes(), before)
        self.assertFalse(list(receipts.glob('pack-*.json')) if receipts.exists() else [])

    def test_gate_allows_degraded_entry(self):
        # Degraded captures carry fact_hash None by design; they must not
        # brick packing. They keep the peer's existing discard behavior.
        store_a, repo_a = self.device('proj-a')
        self.goal(store_a, repo_a, '# 目标\n\n演示。\n')
        self.capture(store_a, repo_a, '实现了登录接口')
        sd = self.store_dir(store_a, repo_a)
        flow._write_entry(sd, {'version': 1, 'seq': 2, 'captured_at': flow.utc(),
                               'tool': None, 'session': 's-deg',
                               'anchor': {'head': None, 'branch': 'main'},
                               'fingerprint': {'anchor': {'head': None, 'branch': 'main'},
                                               'code': {}, 'gaps': []},
                               'prev_seq': 1, 'fact_hash': None, 'queue': 'pending',
                               'redacted_count': 0, 'retries': 0, 'semantic': 'pending',
                               'degraded': True, 'user_message': None},
                          '# 采集条目 cap-0002（降级）\n\n采集过程出错。\n')
        bundle = self.root / 'bundle'
        manifest = sync.export_bundle(store_a, [repo_a], bundle, 'device-a', 'id-a')
        self.assertGreaterEqual(manifest['redacted_count'], 0)


class TestP0MobileBoundaries(unittest.TestCase):
    def test_real_phones_still_redacted(self):
        for text in ('请联系 13800138000 谢谢',
                     '电话：13800138000，',
                     '13800138000',
                     'call 13800138000.'):
            out, hits = flow.redact(text)
            self.assertNotIn('13800138000', out, text)
            self.assertGreaterEqual(hits, 1, text)
        # The existing id_cn sample is untouched by the boundary change.
        out, _ = flow.redact('证件 11010119900307888X 止')
        self.assertNotIn('11010119900307888X', out)

    def test_hex_embedded_runs_no_longer_match(self):
        out, hits = flow.redact('hash ' + PHONE_LIKE_HEX + ' end')
        self.assertEqual(out, 'hash ' + PHONE_LIKE_HEX + ' end')
        self.assertEqual(hits, 0)
        # A digit run glued to ASCII letters reads as part of a longer
        # token, not a standalone number, and is deliberately left alone.
        out, _ = flow.redact('user13800138000name')
        self.assertIn('13800138000', out)


class TestP0WarehouseRoundtrip(P0Base):
    def test_multi_file_warehouse_roundtrip_loses_no_entries(self):
        # True-project shape: Chinese filenames, binary attachments, and
        # several captures across sessions; every entry must land verified.
        extra = {
            'docs/05-Windows 指南.md': '# Windows\n\n客户端配置。\n',
            'docs/图片和附件/shot.png': b'\x89PNG\r\n\x1a\n' + bytes(range(256)),
        }
        store_a, repo_a = self.device('proj-a', extra_files=extra)
        self.goal(store_a, repo_a, '# 目标\n\n演示。\n')
        self.capture(store_a, repo_a, '更新了机场链接', session='s1')
        self.capture(store_a, repo_a, '核对了目标 IP', session='s2')
        self.capture(store_a, repo_a, '补了截图待办', session='s3')
        crafted = self.craft_phone_like_entry(store_a, repo_a, 2)
        self.assertTrue(sync._verify_entry_id(crafted))

        store_b, repo_b = self.device('proj-b')
        self.roundtrip(store_a, repo_a, store_b, repo_b)

        for seq in (1, 2, 3):
            b_path = self.entry_path(store_b, repo_b, seq)
            self.assertTrue(b_path.exists(), 'entry %d lost on import' % seq)
            b_header, _ = sync._parse_entry_text(
                b_path.read_text(encoding='utf-8'))
            self.assertTrue(sync._verify_entry_id(b_header), 'entry %d unverifiable' % seq)
        b_header2, _ = self.read_entry(store_b, repo_b, 2)
        self.assertEqual(b_header2['fingerprint']['code']['docs/attachment.jpg']['sha256'],
                         PHONE_LIKE_HEX)


if __name__ == '__main__':
    unittest.main()
