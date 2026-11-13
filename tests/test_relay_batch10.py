"""Batch 10: takeover quality -- drift, goal alignment, import noise, handoff prompt.

Pins the gaps found by the 2026-11-11 handoff audit and fixed on
2026-11-12: resume had no way to see what moved after export, no way to
see whether the archive was serving the goal, a fresh-device import
stacked meaningless state conflicts, make_handoff's next-session prompt
named no goal, and summarize truncated backlog fields (Owner / 验证) and
could duplicate the same todo. No test touches a real ~/.project-relay/.
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
flow = _load('relay_flow', SKILL / 'scripts/relay_flow.py')
sync = _load('relay_sync', SKILL / 'scripts/relay_sync.py')

REMOTE_URL = 'https://example.com/demo/batch10.git'


class Batch10Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='relay-batch10-')
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

    def call(self, fn, *args, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return fn(*args, **kwargs)

    def init(self):
        return self.call(relay.init, self.store, self.repo)

    def capture(self, message='第一条', session='s1'):
        return self.call(flow.capture, self.store, self.repo, tool='codex',
                         session=session, message=message)

    def summarize(self, text=None, source='manual'):
        return self.call(flow.summarize, self.store, self.repo, source=source,
                         semantic_text=text)

    def store_dir(self):
        return relay.resolve_store_dir(self.store, self.repo)[1]

    def goal(self, text='# 目标\n\n演示项目。\n'):
        self.call(relay.goal_draft, self.store, self.repo, text)
        self.call(relay.goal_confirm, self.store, self.repo)


class TestPackageDrift(Batch10Base):
    """resume lists files that changed after the package's fingerprint."""

    def test_drift_section_lists_changed_file(self):
        self.init()
        self.capture()
        self.summarize('## 已验证\n- 首次汇总（验证：测试）\n')
        # After the capture, change code AND commit -- the fingerprint is
        # the anchor, so this is exactly the post-export drift.
        (self.repo / 'app.py').write_bytes('value = 2\n'.encode('utf-8'))
        (self.repo / 'newfile.py').write_bytes('x = 1\n'.encode('utf-8'))
        self.git(self.repo, 'add', '.')
        self.git(self.repo, 'commit', '-m', 'bump')
        result = self.call(flow.resume, self.store, self.repo)
        self.assertIsNotNone(result['drift_section'])
        self.assertIn('app.py', result['drift_section'])
        self.assertIn('newfile.py', result['drift_section'])
        self.assertIn('app.py', result['drift']['changed'])
        self.assertIn('newfile.py', result['drift']['added'])

    def test_drift_section_clean(self):
        self.init()
        self.capture()
        self.summarize('## 已验证\n- 首次汇总（验证：测试）\n')
        result = self.call(flow.resume, self.store, self.repo)
        self.assertIn('一致', result['drift_section'])

    def test_drift_omitted_without_fingerprint(self):
        self.init()
        result = self.call(flow.resume, self.store, self.repo)
        self.assertIsNone(result['drift_section'])
        self.assertFalse(result['drift']['has_fingerprint'])


class TestGoalAlignment(Batch10Base):
    """resume names goal promises that no todo/state tracks."""

    def _goal_with_cursor(self):
        self.goal('# 目标\n\n卷三期间完成 Cursor 接入、晋升通道。\n')

    def test_missing_item_reported(self):
        self.init()
        self._goal_with_cursor()
        result = self.call(flow.resume, self.store, self.repo)
        self.assertIn('Cursor', result['goal_align_section'])
        self.assertIn('Cursor', result['goal_align']['missing_items'])

    def test_covered_item_not_reported(self):
        self.init()
        self._goal_with_cursor()
        backlog = self.store_dir() / 'archive' / 'backlog.md'
        text = backlog.read_text(encoding='utf-8')
        text += ('\n## T-001\n- 事项：Cursor 接入包。\n- Owner：Agent\n'
                 '- 验证方式：接入可用。\n- 来源：汇总 1\n- 状态：待启动\n')
        backlog.write_text(text, encoding='utf-8')
        result = self.call(flow.resume, self.store, self.repo)
        self.assertNotIn('Cursor', result['goal_align'].get('missing_items', []))
        self.assertIn('晋升', ' '.join(result['goal_align'].get('missing_items', [])))

    def test_alignment_omitted_without_goal(self):
        self.init()
        result = self.call(flow.resume, self.store, self.repo)
        self.assertIsNone(result['goal_align_section'])


class TestImportNoise(Batch10Base):
    """A fresh device that init'd must not end with template-state noise."""

    def _package(self):
        self.init()
        self.goal()
        self.capture()
        self.summarize('## 已验证\n- 汇总一（验证：测试）\n'
                       '## 待办\n- 上线检查。Owner：Agent，验证：跑通冒烟。\n')
        dest = self.root / 'pkg'
        self.call(sync.export_bundle, self.store, [self.repo], dest, 'dev', 'id')
        return dest

    def test_fresh_import_adopts_and_has_no_fork_noise(self):
        dest = self._package()
        # Second device: init only, then import.
        repo_b = self.make_repo('proj-b')
        store_b = self.root / 'store-b'
        self.call(relay.init, store_b, repo_b)
        self.call(sync.import_bundle, dest, store_b, confirm=True)
        sd_b = relay.resolve_store_dir(store_b, repo_b)[1]
        state = (sd_b / 'archive' / 'state.md').read_text(encoding='utf-8')
        self.assertNotIn('分叉待裁', state)
        self.assertNotIn('- 当前分支@基线：未知', state.replace('\r\n', '\n'))
        self.assertIn('汇总 1', state)
        backlog = (sd_b / 'archive' / 'backlog.md').read_text(encoding='utf-8')
        self.assertIn('上线检查', backlog)

    def test_reimport_cleans_previous_fork_notes(self):
        dest = self._package()
        repo_b = self.make_repo('proj-c')
        store_b = self.root / 'store-c'
        self.call(relay.init, store_b, repo_b)
        self.call(sync.import_bundle, dest, store_b, confirm=True)
        sd_b = relay.resolve_store_dir(store_b, repo_b)[1]
        # Plant a stale fork note, then import again: it must be cleaned.
        state_path = sd_b / 'archive' / 'state.md'
        text = state_path.read_text(encoding='utf-8')
        state_path.write_text(text + '\n- 分叉待裁：测试（对方）：旧值\n',
                              encoding='utf-8')
        self.call(sync.import_bundle, dest, store_b, confirm=True)
        state = state_path.read_text(encoding='utf-8')
        self.assertNotIn('旧值', state)


class TestHandoffPrompt(Batch10Base):
    """make_handoff's next-session prompt must carry goal and real items."""

    def test_prompt_names_goal_and_todo_ids(self):
        self.init()
        self.goal('# 目标\n\n做一个小工具。\n')
        self.capture()
        self.summarize('## 已验证\n- 汇总一（验证：测试）\n'
                       '## 待办\n- 上线检查。Owner：Agent，验证：跑通冒烟。\n'
                       '## 待办\n- 写文档。Owner：Agent，验证：文档上线。\n')
        info = self.call(flow.make_handoff, self.store, self.repo)
        body = Path(info['package']).read_text(encoding='utf-8')
        self.assertIn('做一个小工具', body)
        self.assertIn('T-001', body)
        self.assertIn('上线检查', body)
        self.assertIn('T-002', body)
        self.assertIn('写文档', body)

    def test_prompt_without_goal_says_so(self):
        self.init()
        self.capture()
        self.summarize('## 已验证\n- 汇总一（验证：测试）\n')
        info = self.call(flow.make_handoff, self.store, self.repo)
        body = Path(info['package']).read_text(encoding='utf-8')
        self.assertIn('先与用户确认', body)


class TestBacklogParse(Batch10Base):
    """summarize must keep Owner / 验证 intact and not duplicate items."""

    def test_owner_and_verify_not_truncated(self):
        self.init()
        self.capture()
        self.summarize('## 待办\n'
                       '- yarn dlx 真机验证。Owner：用户，验证：在 Windows 上跑 yarn dlx skills add 成功。\n')
        backlog = (self.store_dir() / 'archive' / 'backlog.md').read_text(encoding='utf-8')
        self.assertIn('- Owner：用户\n', backlog)
        self.assertIn('- 验证方式：在 Windows 上跑 yarn dlx skills add 成功', backlog)
        self.assertIn('- 事项：yarn dlx 真机验证', backlog)

    def test_same_item_not_duplicated(self):
        self.init()
        self.capture('一', 's1')
        self.summarize('## 待办\n- 上线检查。Owner：Agent，验证：跑通冒烟。\n')
        self.capture('二', 's2')
        self.summarize('## 待办\n- 上线检查。Owner：Agent，验证：跑通冒烟。\n')
        backlog = (self.store_dir() / 'archive' / 'backlog.md').read_text(encoding='utf-8')
        self.assertEqual(backlog.count('- 事项：上线检查'), 1)


if __name__ == '__main__':
    unittest.main()
