"""v3 manifest/config integrity tests.

Covers the checksum-manifest boundary between formal archive files and
machine-local state, plus config validation and lock-handle cleanup.
Run: python -m unittest tests.test_v3_manifest_config -v
"""
import json
import os
import sys
import time
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from v3_helpers import V3Base, relay, flow, stats

SEMANTIC = (
    '## 已验证\n- 演示能跑起来，验证：跑了演示。\n'
    '## 决定\n- 继续做演示，来源：用户。\n'
    '## 待办\n- 把演示跑通，Owner：用户，验证：演示能跑。\n'
)


class TestManifestLocalState(V3Base):
    def test_local_state_changes_do_not_break_checksum(self):
        root = self.init()
        store = self.store_dir(root)
        adir = self.adir(root, 'deepseek-harness')
        (store / 'state' / 'config.json').write_text(
            json.dumps({'pending_cap': 25}), encoding='utf-8')
        (store / 'state' / 'fingerprint-cache.json').write_text(
            '{"cached": 1}', encoding='utf-8')
        (adir / 'state' / 'cursor.json').write_text('{"entry_seq": 1}', encoding='utf-8')
        (adir / 'state' / 'backlog-notice.json').write_text(
            '{"pending": 2}', encoding='utf-8')
        relay.update_checksum_manifest(store)

        present, mismatches = relay.verify_checksum_manifest(store)
        self.assertTrue(present)
        self.assertEqual([], mismatches)
        manifest = json.loads(
            (store / relay.CHECKSUM_MANIFEST).read_text(encoding='utf-8'))
        for rel in ('state/config.json', 'state/fingerprint-cache.json',
                    'agents/deepseek-harness/state/cursor.json',
                    'agents/deepseek-harness/state/backlog-notice.json'):
            self.assertNotIn(rel, manifest['files'])

        # Normal machine-local churn must not look like archive damage.
        (store / 'state' / 'config.json').write_text(
            json.dumps({'pending_cap': 30}), encoding='utf-8')
        (store / 'state' / 'fingerprint-cache.json').write_text(
            '{"cached": 2}', encoding='utf-8')
        (adir / 'state' / 'cursor.json').write_text('{"entry_seq": 2}', encoding='utf-8')
        present, mismatches = relay.verify_checksum_manifest(store)
        self.assertTrue(present)
        self.assertEqual([], mismatches)

    def test_verify_ignores_legacy_local_entries(self):
        root = self.init()
        store = self.store_dir(root)
        (store / 'state' / 'config.json').write_text('{"pending_cap": 25}', encoding='utf-8')
        (store / relay.CHECKSUM_MANIFEST).write_text(json.dumps({
            'updated_at': '2026-01-01T00:00:00Z',
            'files': {'state/config.json': '0' * 64},
        }), encoding='utf-8')
        present, mismatches = relay.verify_checksum_manifest(store)
        self.assertTrue(present)
        self.assertEqual([], mismatches)

    def test_capture_after_summarize_keeps_manifest_clean(self):
        root = self.init()
        first = self.capture(root, 'deepseek-harness')
        self.assertFalse(first['deduped'])
        self.summarize(root, 'deepseek-harness', semantic_text=SEMANTIC)
        present, mismatches = relay.verify_checksum_manifest(self.store_dir(root))
        self.assertTrue(present)
        self.assertEqual([], mismatches)

        self.change(root, 'app.py', 'value = 2\n')
        second = self.capture(root, 'deepseek-harness')
        self.assertFalse(second['deduped'])
        present, mismatches = relay.verify_checksum_manifest(self.store_dir(root))
        self.assertTrue(present)
        self.assertEqual([], mismatches)

    def test_log_append_refreshes_manifest(self):
        root = self.init()
        self.capture(root, 'deepseek-harness')
        self.summarize(root, 'deepseek-harness', semantic_text=SEMANTIC)
        code, _text = self.cli('--root', root, 'log', '--agent',
                               'deepseek-harness', '--append', '手工记录')
        self.assertEqual(0, code)
        present, mismatches = relay.verify_checksum_manifest(self.store_dir(root))
        self.assertTrue(present)
        self.assertEqual([], mismatches)

    def test_stack_and_outlook_writes_refresh_manifest(self):
        root = self.init()
        store = self.store_dir(root)
        relay.write_stack(store, 'Python 3.12\n')
        relay.write_outlook(store, '下一步：验收\n')
        present, mismatches = relay.verify_checksum_manifest(store)
        self.assertTrue(present)
        self.assertEqual([], mismatches)


class TestStoreLock(V3Base):
    def test_exit_without_enter_is_noop(self):
        lock = relay._StoreLock(Path(self.temp.name) / 'x.lock')
        self.assertFalse(lock.__exit__(None, None, None))

    def test_successful_enter_sets_and_clears_handle(self):
        lock = relay._StoreLock(Path(self.temp.name) / 'x.lock')
        with lock:
            self.assertIsNotNone(lock._fh)
        self.assertIsNone(lock._fh)

    def test_open_failure_closes_handle(self):
        lock = relay._StoreLock(Path(self.temp.name) / 'x.lock')
        with mock.patch('builtins.open', side_effect=OSError('boom')):
            with self.assertRaises(OSError):
                lock.__enter__()
        self.assertIsNone(lock._fh)

    def test_lock_failure_closes_handle(self):
        lock = relay._StoreLock(Path(self.temp.name) / 'x.lock')
        target = 'relay.msvcrt.locking' if os.name == 'nt' else 'relay.fcntl.flock'
        with mock.patch(target, side_effect=OSError('lockfail')):
            with self.assertRaises(OSError):
                lock.__enter__()
        self.assertIsNone(lock._fh)


class TestConfigValidation(V3Base):
    def test_bad_known_values_raise_relay_error(self):
        root = self.init()
        path = self.store_dir(root) / 'state' / 'config.json'
        bad_values = [
            ('pending_cap', 'many'),
            ('pending_cap', -1),
            ('exclude_globs', '*.tmp'),
            ('smoke_command', ['echo', 'x']),
            ('fingerprint_threshold_s', 'fast'),
        ]
        for key, value in bad_values:
            path.write_text(json.dumps({key: value}), encoding='utf-8')
            with self.assertRaises(relay.RelayError, msg=key):
                flow.load_config(self.store_dir(root))

    def test_invalid_json_raises_relay_error(self):
        root = self.init()
        path = self.store_dir(root) / 'state' / 'config.json'
        path.write_text('{not json', encoding='utf-8')
        with self.assertRaises(relay.RelayError):
            flow.load_config(self.store_dir(root))

    def test_valid_config_is_merged(self):
        root = self.init()
        path = self.store_dir(root) / 'state' / 'config.json'
        path.write_text(json.dumps({
            'pending_cap': 3,
            'exclude_globs': ['*.tmp'],
            'smoke_command': 'python app.py',
        }), encoding='utf-8')
        cfg = flow.load_config(self.store_dir(root))
        self.assertEqual(3, cfg['pending_cap'])
        self.assertEqual(['*.tmp'], cfg['exclude_globs'])
        self.assertEqual('python app.py', cfg['smoke_command'])


class TestStatsMonthlyCap(V3Base):
    def test_monthly_cap_reads_state_config(self):
        root = self.init()
        store = self.store_dir(root)
        now = time.time()
        (store / 'state' / 'config.json').write_text(
            json.dumps({'monthly_llm_token_cap': 100}), encoding='utf-8')
        stats.record_summarize(store, 'manual', 'manual',
                               llm_in_tokens=120, now_epoch=now)
        result = stats.compute_fuse(store, now_epoch=now)
        self.assertEqual(100, result['monthly_cap'])
        self.assertTrue(result['cap_alert'])
