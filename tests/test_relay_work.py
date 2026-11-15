"""v3 worktree/branch transaction tests (DSH-only product).

Covers the two modes named in SKILL.md:
- single-agent: project root and Agent workspace overlap -> direct edit, no worktree;
- multi-agent: project root outside workspace -> one branch/worktree per Agent;
and the shared .project-relay store across linked worktrees.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from v3_helpers import V3Base, relay, flow, load, SKILL

work = load('relay_work', SKILL / 'scripts' / 'relay_work.py')


class TestWorkModes(V3Base):
    def _init_clean(self):
        root = self.init()
        # relay init creates .gitignore; commit it so the main worktree is
        # clean before a merge test checks the clean-tree precondition.
        self.git(root, 'add', '.gitignore')
        self.git(root, 'commit', '-m', 'relay init')
        return root

    def test_single_agent_overlap_is_direct(self):
        root = self.init()
        info = work.enter(root, workspace=root, agent='deepseek-harness')
        self.assertEqual('single', info['mode'])
        self.assertIsNone(info['worktree'])
        self.assertEqual(self.git(root, 'branch', '--show-current').stdout.decode().strip(),
                         info['branch'])
        self.assertFalse((Path(self.temp.name) / 'worktrees').exists())

    def test_single_agent_nested_workspace_is_direct(self):
        root = self.init()
        nested = root / 'nested'
        nested.mkdir()
        info = work.enter(root, workspace=nested, agent='deepseek-harness')
        self.assertEqual('single', info['mode'])
        self.assertIsNone(info['worktree'])

    def test_multi_agent_creates_worktree_on_agent_branch(self):
        root = self.init()
        workspace = Path(self.temp.name) / 'dsh-workspace'
        workspace.mkdir()
        info = work.enter(root, workspace=workspace, agent='deepseek-harness')
        self.assertEqual('multi', info['mode'])
        self.assertTrue(info['created'])
        tree = Path(info['worktree'])
        self.assertTrue(tree.is_dir())
        self.assertFalse((tree / '.project-relay').exists(),
                         'worktree must not get its own .project-relay')
        self.assertEqual('agent/deepseek-harness',
                         self.git(tree, 'branch', '--show-current').stdout.decode().strip())
        # Repeated enter returns the same worktree, not a second one.
        again = work.enter(root, workspace=workspace, agent='deepseek-harness')
        self.assertFalse(again['created'])
        self.assertEqual(info['worktree'], again['worktree'])

    def test_store_is_shared_across_worktree(self):
        root = self.init()
        workspace = Path(self.temp.name) / 'dsh-workspace'
        workspace.mkdir()
        info = work.enter(root, workspace=workspace, agent='deepseek-harness')
        tree = Path(info['worktree'])
        # store_dir_for must resolve back to the main project root.
        self.assertEqual(self.store_dir(root), relay.store_dir_for(tree))
        flow.capture(tree, 'deepseek-harness', message='from worktree')
        cap = self.store_dir(root) / 'agents' / 'deepseek-harness' / 'raw' / 'cap-0001.md'
        self.assertTrue(cap.is_file(), 'capture from worktree did not land in shared store')
        self.assertFalse((tree / '.project-relay').exists(),
                         'worktree must not get its own store')

    def test_review_and_merge_require_confirmation(self):
        root = self._init_clean()
        workspace = Path(self.temp.name) / 'dsh-workspace'
        workspace.mkdir()
        info = work.enter(root, workspace=workspace, agent='deepseek-harness')
        tree = Path(info['worktree'])
        (tree / 'app.py').write_text('value = 2\n', encoding='utf-8')
        self.git(tree, 'add', '.')
        self.git(tree, 'commit', '-m', 'agent change')
        review = work.review(root, agent='deepseek-harness')
        self.assertEqual(1, review['commit_count'])
        self.assertIn('app.py', review['diff_stat'])
        with self.assertRaises(relay.RelayError):
            work.merge(root, agent='deepseek-harness', confirm=False)
        merged = work.merge(root, agent='deepseek-harness', confirm=True)
        self.assertTrue(merged['merged'])
        self.assertIn('value = 2', (root / 'app.py').read_text(encoding='utf-8'))

    def test_merge_conflict_aborts_and_keeps_main_clean(self):
        root = self._init_clean()
        workspace = Path(self.temp.name) / 'dsh-workspace'
        workspace.mkdir()
        info = work.enter(root, workspace=workspace, agent='deepseek-harness')
        tree = Path(info['worktree'])
        (tree / 'app.py').write_text('value = "agent"\n', encoding='utf-8')
        self.git(tree, 'add', '.')
        self.git(tree, 'commit', '-m', 'agent change')
        (root / 'app.py').write_text('value = "main"\n', encoding='utf-8')
        self.git(root, 'add', 'app.py')
        self.git(root, 'commit', '-m', 'main change')
        with self.assertRaises(relay.RelayError) as ctx:
            work.merge(root, agent='deepseek-harness', confirm=True)
        self.assertIn('合并', str(ctx.exception))
        self.assertEqual('value = "main"\n', (root / 'app.py').read_text(encoding='utf-8'))
        self.assertEqual('', self.git(root, 'status', '--porcelain').stdout.decode().strip())

    def test_status_lists_main_and_agent_worktree(self):
        root = self.init()
        workspace = Path(self.temp.name) / 'dsh-workspace'
        workspace.mkdir()
        info = work.enter(root, workspace=workspace, agent='deepseek-harness')
        status = work.status(root, agent='deepseek-harness')
        branches = [row['branch'] for row in status['worktrees']]
        self.assertIn('agent/deepseek-harness', branches)
        self.assertTrue(status['worktrees'][0]['is_main'])


if __name__ == '__main__':
    unittest.main()
