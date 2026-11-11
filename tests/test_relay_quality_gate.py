"""Batch 9: export-side quality gate.

Pins the four gaps found during the 2026-11-11 DSH takeover audit of a
real zip handoff: an unresolved baseline commit, captures whose semantic
sections were still placeholders, backlog blocks whose Owner lines had
been truncated by the summarize regex, and a goal receipt that no longer
certified the goal bytes. Export records them in manifest.quality_gaps;
strict refuses a bundle with a block-severity gap. No test touches the
developer's real ~/.project-relay/.
"""

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
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
flow = _load('relay_flow', SKILL / 'scripts/relay_flow.py')
sync = _load('relay_sync', SKILL / 'scripts/relay_sync.py')
SyncRelayError = sync.relay.RelayError

REMOTE_URL = 'https://example.com/demo/quality-gate.git'


class QualityGateBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='relay-quality-')
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
        self.repo = self.make_repo('proj')
        with contextlib.redirect_stdout(io.StringIO()):
            relay.init(self.store, self.repo)

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
        self.git(root, 'remote', 'add', 'origin', REMOTE_URL)
        return root

    def capture(self, message='第一条', session='s1'):
        with contextlib.redirect_stdout(io.StringIO()):
            return flow.capture(self.store, self.repo, tool='codex',
                                session=session, message=message)

    def store_dir(self):
        return relay.resolve_store_dir(self.store, self.repo)[1]

    def bundle(self, strict=False, name='bundle'):
        dest = self.root / name
        with contextlib.redirect_stdout(io.StringIO()):
            return sync.export_bundle(self.store, [self.repo], dest, 'dev', 'id',
                                      strict=strict)

    def fill_semantic(self, rel='raw/cap-0001.md'):
        path = self.store_dir() / rel
        text = path.read_text(encoding='utf-8')
        text = text.replace(sync.SEMANTIC_PENDING_MARKER + '\n', '')
        path.write_text(text, encoding='utf-8')

    def set_baseline(self, sha):
        path = self.store_dir() / 'archive' / 'state.md'
        text = path.read_text(encoding='utf-8')
        line = '- 当前分支@基线：refs/heads/main@' + sha
        if '当前分支@基线' in text:
            text = re.sub(r'- 当前分支@基线：.*', line, text, count=1)
        else:
            text = text.replace('<!-- relay:state-begin -->',
                                '<!-- relay:state-begin -->\n' + line, 1)
        path.write_text(text, encoding='utf-8')

    def gap_codes(self, manifest):
        return [g['code'] for g in manifest.get('quality_gaps', [])]


class TestExportQualityGate(QualityGateBase):
    """Export records every gap; strict turns block-level gaps into a refusal."""

    def test_semantic_pending_latest_is_block(self):
        self.capture()
        manifest = self.bundle()
        gaps = [g for g in manifest['quality_gaps']
                if g['code'] == 'semantic_pending']
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]['severity'], 'block')
        self.assertIn('cap-0001.md', gaps[0]['files'][0])

    def test_semantic_filled_clears_gap(self):
        self.capture()
        self.fill_semantic()
        manifest = self.bundle()
        self.assertNotIn('semantic_pending', self.gap_codes(manifest))

    def test_export_zip_returns_gaps(self):
        self.capture()
        zip_path = self.root / 'out.zip'
        with contextlib.redirect_stdout(io.StringIO()):
            result = sync.export_zip(self.store, self.repo, zip_path)
        self.assertEqual(result['entry_count'], 1)
        self.assertEqual([g['code'] for g in result['quality_gaps']],
                         ['semantic_pending'])
        self.assertTrue(zip_path.exists())

    def test_strict_refuses_block_gap(self):
        self.capture()
        with self.assertRaises(SyncRelayError) as ctx:
            self.bundle(strict=True, name='strict-bundle')
        self.assertIn('阻断', str(ctx.exception))

    def test_baseline_unreachable_recorded(self):
        self.capture()
        self.set_baseline('0' * 40)
        manifest = self.bundle()
        gaps = [g for g in manifest['quality_gaps']
                if g['code'] == 'baseline_unreachable']
        self.assertEqual(len(gaps), 1)
        self.assertIn('archive/state.md', gaps[0]['files'])

    def test_baseline_reachable_not_reported(self):
        self.capture()
        head = self.git(self.repo, 'rev-parse', 'HEAD').stdout.decode().strip()
        self.set_baseline(head)
        manifest = self.bundle()
        self.assertNotIn('baseline_unreachable', self.gap_codes(manifest))

    def test_backlog_truncated_owner_recorded(self):
        self.capture()
        backlog = self.store_dir() / 'archive' / 'backlog.md'
        backlog.write_text(
            '# 待办清单\n\n## T-001\n'
            '- 事项：测试项。Owner：用户，验证：在\n'
            '- Owner：用户，验证：在\n'
            '- 验证方式：在 Windows 上跑通。\n'
            '- 来源：汇总 1\n'
            '- 状态：待启动\n',
            encoding='utf-8')
        manifest = self.bundle()
        self.assertIn('backlog_field_truncated', self.gap_codes(manifest))

    def test_goal_receipt_mismatch_recorded(self):
        with contextlib.redirect_stdout(io.StringIO()):
            relay.goal_draft(self.store, self.repo, '# 目标\n\n演示。\n')
            relay.goal_confirm(self.store, self.repo)
        goal = self.store_dir() / 'archive' / 'goal.md'
        goal.write_bytes(goal.read_bytes() + b'\nchange\n')
        manifest = self.bundle()
        self.assertIn('goal_receipt_mismatch', self.gap_codes(manifest))

    def test_clean_export_has_no_gaps(self):
        self.capture()
        self.fill_semantic()
        head = self.git(self.repo, 'rev-parse', 'HEAD').stdout.decode().strip()
        self.set_baseline(head)
        manifest = self.bundle()
        self.assertEqual(manifest['quality_gaps'], [])

    def test_old_bundle_without_gaps_field_still_verifies(self):
        self.capture()
        self.fill_semantic()
        self.bundle()
        manifest_path = self.root / 'bundle' / 'manifest.json'
        data = json.loads(manifest_path.read_text(encoding='utf-8'))
        data.pop('quality_gaps', None)
        manifest_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        manifest, files, _warnings = sync.verify_bundle(str(self.root / 'bundle'))
        self.assertNotIn('quality_gaps', manifest)
        self.assertTrue(files)


if __name__ == '__main__':
    unittest.main()
