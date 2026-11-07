"""Codex PR #5, 2026-11-06 16:47Z: two P2 findings, batch 10 fixes.

1. hook_capture/hook_resume snapshot the install-time clone instead of
   the active session checkout: the cwd gate passes for a second clone
   of the same remote (same project key), but capture/resume still get
   the install-time toplevel, so changes that only exist in the live
   clone are missed.
2. goal_confirm() never reads goal-draft.json's base_hash, so a draft
   staged against an older goal can silently overwrite a goal another
   session confirmed/imported since.

Style follows the other suites: real temporary directories, real Git
repos, PROJECT_RELAY_HOME redirected, no network, no real ~/.project-relay/.
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

SHARED_REMOTE = 'https://github.com/example/shared.git'


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


relay = _load('relay', SKILL / 'scripts/relay.py')
flow = _load('relay_flow', SKILL / 'scripts/relay_flow.py')


class Batch10Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='relay-batch10-')
        self.addCleanup(self.temp.cleanup)
        self.store = Path(self.temp.name) / 'store'
        self._env = mock.patch.dict(
            os.environ, {'PROJECT_RELAY_HOME': str(Path(self.temp.name))})
        self._env.start()
        self.addCleanup(self._env.stop)

    def git(self, root, *args):
        return subprocess.run(['git', '-C', str(root), *args],
                              capture_output=True, check=True)

    def make_repo(self, name, remote=None):
        root = Path(self.temp.name) / name
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
        root = self.make_repo(name, remote=remote or SHARED_REMOTE)
        relay.init(self.store, root)
        return root

    def store_dir(self, root):
        row = relay.where(self.store, path=root)['row']
        return self.store / 'projects' / (row['key'] + '-' + row['slug'])

    def entries(self, root):
        return flow.list_entries(self.store_dir(root))

    def head(self, root):
        return self.git(root, 'rev-parse', 'HEAD').stdout.decode().strip()

    def two_clones(self):
        """Install-time clone A plus a second clone B of the same remote.

        B shares A's project key (same remote URL) but has its own
        working tree: a committed file that exists only in B.
        """
        root_a = self.init('clone-a')
        root_b = self.make_repo('clone-b', remote=SHARED_REMOTE)
        (root_b / 'only-b.txt').write_bytes(b'only in the second clone\n')
        self.git(root_b, 'add', '.')
        self.git(root_b, 'commit', '-m', 'b-only change')
        return root_a, root_b


class TestHookActiveCheckout(Batch10Base):
    def test_hook_capture_snapshots_active_clone(self):
        root_a, root_b = self.two_clones()
        info = flow.hook_capture(self.store, root_a, tool='claude-code', cwd=root_b)
        self.assertNotIn('skipped', info)
        self.assertEqual(1, len(self.entries(root_a)))
        entry = self.entries(root_a)[0]
        self.assertEqual(self.head(root_b), entry['header']['anchor']['head'])
        self.assertIn(self.head(root_b), entry['body'])
        self.assertNotIn(self.head(root_a), entry['body'])
        self.assertIn('only-b.txt', entry['body'])

    def test_hook_resume_runs_smoke_in_active_clone(self):
        root_a, root_b = self.two_clones()
        cfg = flow.load_config(self.store_dir(root_a))
        cfg['smoke_command'] = 'test -f only-b.txt'
        flow.save_config(self.store_dir(root_a), cfg)
        result = flow.hook_resume(self.store, root_a, tool='claude-code', cwd=root_b)
        self.assertFalse(result.get('skipped'))
        sec5 = result['sections'][4]
        self.assertIn('通过', sec5)
        self.assertIn('退出码 0', sec5)

    def test_hook_capture_skips_when_active_checkout_unresolvable(self):
        # Fail-closed: the gate passed but the cwd's top level cannot be
        # resolved -- capture nothing, raise nothing, capture the wrong
        # place never. (Simulated: gate forced open, git_toplevel broken.)
        root_a = self.init('proj-a')
        with mock.patch.object(flow, 'hook_cwd_allowed', return_value=True), \
             mock.patch.object(flow.relay, 'git_toplevel',
                               side_effect=flow.relay.RelayError('not a repo')):
            info = flow.hook_capture(self.store, root_a, tool='claude-code',
                                     cwd=root_a)
        self.assertTrue(info.get('skipped'))
        self.assertEqual(0, len(self.entries(root_a)))


class TestGoalConfirmBaseHash(Batch10Base):
    DRAFT = ('# 项目目标\n\n状态：待确认\n\n- 目标与交付物：做一个 MVP（来源：用户原话）\n'
             '- 范围及不做事项：不做登录（来源：对话推断）\n'
             '- 约束与偏好（附来源）：Python 标准库 only（来源：档案既有）\n'
             '- 验收标准：测试全过\n- 最近核对时间：未核对\n')
    DRAFT2 = DRAFT.replace('做一个 MVP', '做一个 MVP v2')
    OTHER = DRAFT.replace('做一个 MVP', '另一会话的目标')

    def _init_goal(self, name='goalproj'):
        root = self.init(name)
        relay.goal_draft(self.store, root, self.DRAFT)
        relay.goal_confirm(self.store, root)
        return root

    def _set_goal(self, store_dir, text):
        relay.atomic(store_dir / 'archive' / 'goal.md', text.encode('utf-8'))

    def test_confirm_rejects_stale_draft(self):
        root = self._init_goal()
        store_dir = self.store_dir(root)
        relay.goal_draft(self.store, root, self.DRAFT2)
        # Another session changes the goal through a path that leaves the
        # draft files alone (import-confirm / sync); the staged draft is
        # now based on a goal that no longer exists.
        self._set_goal(store_dir, self.OTHER)
        with self.assertRaisesRegex(relay.RelayError, '过期'):
            relay.goal_confirm(self.store, root)
        self.assertEqual(self.OTHER.encode('utf-8'),
                         relay._goal_bytes(store_dir))
        # The rejected draft is left in place, not deleted.
        self.assertTrue((store_dir / 'state' / 'goal-draft.md').exists())

    def test_confirm_rejects_draft_when_goal_appeared_after_no_goal_draft(self):
        root = self.init('goalproj')
        store_dir = self.store_dir(root)
        (store_dir / 'archive' / 'goal.md').unlink()
        relay.goal_draft(self.store, root, self.DRAFT)
        meta = json.loads((store_dir / 'state' / 'goal-draft.json')
                          .read_text(encoding='utf-8'))
        self.assertIsNone(meta['base_hash'])
        # A goal appears after the draft was staged: the draft is stale.
        self._set_goal(store_dir, self.OTHER)
        with self.assertRaisesRegex(relay.RelayError, '过期'):
            relay.goal_confirm(self.store, root)
        self.assertEqual(self.OTHER.encode('utf-8'),
                         relay._goal_bytes(store_dir))

    def test_reconfirm_already_confirmed_goal_not_rejected(self):
        # Ordering pin: the already-path (draft == receipt == current
        # goal) must win over the base check. The goal moved away and
        # back between drafting and confirming, so base_hash no longer
        # matches -- but this is a re-confirmation, not an overwrite.
        root = self._init_goal()
        store_dir = self.store_dir(root)
        self._set_goal(store_dir, self.OTHER)
        relay.goal_draft(self.store, root, self.DRAFT)
        self._set_goal(store_dir, self.DRAFT)
        info = relay.goal_confirm(self.store, root)
        self.assertTrue(info.get('already'))

    def test_tentative_confirm_rejects_stale_draft(self):
        root = self._init_goal()
        store_dir = self.store_dir(root)
        relay.goal_draft(self.store, root, self.DRAFT2)
        self._set_goal(store_dir, self.OTHER)
        with self.assertRaisesRegex(relay.RelayError, '过期'):
            relay.goal_confirm(self.store, root, tentative=True)
        self.assertEqual(self.OTHER.encode('utf-8'),
                         relay._goal_bytes(store_dir))
        self.assertIsNone(relay._tentative_record(store_dir))

    def test_confirm_rejects_draft_with_missing_metadata(self):
        # Conservative compat path: a draft whose goal-draft.json is gone
        # cannot prove it is still based on the current goal, so it is
        # rejected instead of confirmed on trust.
        root = self._init_goal()
        store_dir = self.store_dir(root)
        relay.goal_draft(self.store, root, self.DRAFT2)
        (store_dir / 'state' / 'goal-draft.json').unlink()
        with self.assertRaisesRegex(relay.RelayError, '元数据'):
            relay.goal_confirm(self.store, root)
        self.assertEqual(self.DRAFT.encode('utf-8'),
                         relay._goal_bytes(store_dir))

    def test_confirm_rejects_draft_with_corrupt_metadata(self):
        root = self._init_goal()
        store_dir = self.store_dir(root)
        relay.goal_draft(self.store, root, self.DRAFT2)
        (store_dir / 'state' / 'goal-draft.json').write_bytes(b'{not json')
        with self.assertRaisesRegex(relay.RelayError, '元数据'):
            relay.goal_confirm(self.store, root)
        self.assertEqual(self.DRAFT.encode('utf-8'),
                         relay._goal_bytes(store_dir))

    def test_confirm_accepts_fresh_draft(self):
        # Positive control: a draft staged against the current goal still
        # confirms normally after the check lands.
        root = self._init_goal()
        relay.goal_draft(self.store, root, self.DRAFT2)
        info = relay.goal_confirm(self.store, root)
        self.assertFalse(info.get('already'))
        self.assertEqual(2, info['receipt']['seq'])
        self.assertEqual('confirmed', relay.goal_status(self.store, root)[0])


if __name__ == '__main__':
    unittest.main()
