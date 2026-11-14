"""v3 layout exemplar (batch 18): the archive lives at the project root.

This file is the pattern for the whole v3 suite: real temporary Git
repos, relay.init(root) only, no registry, no global store. Every other
v3 test file imports the same V3Base from v3_helpers.
"""
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from v3_helpers import V3Base, relay, flow, RelayError

OUTER_FILES = ('goal.md', 'state.md', 'backlog.md', 'decisions.md',
               'stack.md', 'outlook.md', 'index.md')
AGENT_FILES = ('memory.md', 'decisions.md', 'lessons.md', 'backlog.md',
               'changes.md', 'goal.md')
AGENT_DIRS = ('raw', 'state', 'tx', 'backups')


class TestInitLayout(V3Base):
    def test_init_creates_outer_files_and_agent_dirs(self):
        root = self.init()
        store = self.store_dir(root)
        self.assertTrue(store.is_dir())
        for name in OUTER_FILES:
            self.assertTrue((store / name).is_file(), 'missing outer ' + name)
        self.assertTrue((store / 'receipts').is_dir())
        self.assertTrue((store / 'state').is_dir())
        for agent in ('codex', 'claude-code', 'workbuddy', 'deepseek-harness'):
            adir = store / 'agents' / agent
            for name in AGENT_FILES:
                self.assertTrue((adir / name).is_file(),
                                'missing agents/%s/%s' % (agent, name))
            for d in AGENT_DIRS:
                self.assertTrue((adir / d).is_dir(),
                                'missing agents/%s/%s/' % (agent, d))
            self.assertTrue((adir / 'log.jsonl').is_file())

    def test_init_is_idempotent_and_keeps_content(self):
        root = self.init()
        store = self.store_dir(root)
        relay.write_stack(store, 'Python 3.12\n')
        first = relay.init(root)
        self.assertFalse(first['gitignored'])
        self.assertEqual('Python 3.12\n', relay.read_stack(store))

    def test_init_refuses_non_git_directory(self):
        plain = Path(self.temp.name) / 'plain'
        plain.mkdir()
        with self.assertRaises(RelayError) as ctx:
            relay.init(plain)
        self.assertIn('非 Git 项目不在本版范围', str(ctx.exception))

    def test_resolve_store_dir_fails_closed_before_init(self):
        root = self.make_repo('proj')
        with self.assertRaises(RelayError):
            relay.resolve_store_dir(root)
        # store_dir_for itself does not require the archive to exist.
        self.assertEqual(root / '.project-relay', relay.store_dir_for(root))

    def test_resolve_from_subdir(self):
        root = self.init()
        sub = root / 'pkg'
        sub.mkdir()
        self.assertEqual(root / '.project-relay',
                         relay.resolve_store_dir(sub))

    def test_index_lists_agents_and_marks_tiers(self):
        root = self.init()
        text = (self.store_dir(root) / 'index.md').read_text(encoding='utf-8')
        for agent in ('codex', 'claude-code', 'workbuddy', 'deepseek-harness'):
            self.assertIn('agents/%s/' % agent, text)
        self.assertIn('deepseek-harness', text)
        self.assertIn('弱档（技能目录）', text)
        self.assertNotIn('预留', text)


class TestGitignore(V3Base):
    def test_init_writes_gitignore_entry(self):
        root = self.init()
        gi = (root / '.gitignore').read_text(encoding='utf-8')
        self.assertIn('.project-relay/', gi)
        out = self.git(root, 'status', '--porcelain').stdout.decode()
        self.assertNotIn('.project-relay', out)

    def test_init_preserves_existing_gitignore(self):
        root = self.make_repo('proj')
        (root / '.gitignore').write_text('node_modules/\n', encoding='utf-8')
        relay.init(root)
        gi = (root / '.gitignore').read_text(encoding='utf-8')
        self.assertIn('node_modules/', gi)
        self.assertIn('.project-relay/', gi)
        self.assertEqual(1, gi.count('.project-relay/'))


class TestStackOutlook(V3Base):
    def test_stack_and_outlook_roundtrip(self):
        root = self.init()
        store = self.store_dir(root)
        relay.write_stack(store, 'Python 3.12、pytest\n')
        relay.write_outlook(store, '下一步：跑通演示\n')
        self.assertEqual('Python 3.12、pytest\n', relay.read_stack(store))
        self.assertEqual('下一步：跑通演示\n', relay.read_outlook(store))

    def test_stack_starts_as_template(self):
        root = self.init()
        self.assertIn('未知', relay.read_stack(self.store_dir(root)))


class TestAgentGoalHash(V3Base):
    def test_agent_goal_stamps_current_total_hash(self):
        root = self.init()
        store = self.store_dir(root)
        self.confirm_goal(root)
        total = relay.overall_goal_hash(store)
        self.assertIsNotNone(total)
        relay.write_agent_goal(store, 'codex', '负责后端。\n')
        self.assertEqual(total, relay.agent_goal_recorded_hash(store, 'codex'))
        text = relay.read_agent_goal(store, 'codex')
        self.assertTrue(text.startswith('总目标哈希：' + total))
        self.assertIn('负责后端。', text)

    def test_agent_goal_hash_moves_when_total_goal_moves(self):
        root = self.init()
        store = self.store_dir(root)
        self.confirm_goal(root)
        relay.write_agent_goal(store, 'codex', '负责后端。\n')
        first = relay.agent_goal_recorded_hash(store, 'codex')
        self.confirm_goal(root, text='# 项目目标\n\n状态：已确认\n\n- 目标与交付物：换一个目标\n')
        second = relay.overall_goal_hash(store)
        self.assertNotEqual(first, second)
        relay.write_agent_goal(store, 'codex', '负责后端。\n')
        self.assertEqual(second, relay.agent_goal_recorded_hash(store, 'codex'))


class TestAgentLog(V3Base):
    def test_log_append_and_read(self):
        root = self.init()
        store = self.store_dir(root)
        row = relay.append_agent_log(store, 'codex', 'capture', {'seq': 1})
        self.assertEqual('codex', row['agent'])
        rows = relay.read_agent_log(store, 'codex')
        self.assertEqual(1, len(rows))
        self.assertEqual('capture', rows[0]['event'])
        self.assertEqual({'seq': 1}, rows[0]['detail'])
        # Another agent's log is a different file.
        relay.append_agent_log(store, 'claude-code', 'capture', None)
        self.assertEqual(1, len(relay.read_agent_log(store, 'codex')))
        self.assertEqual(1, len(relay.read_agent_log(store, 'claude-code')))


class TestAgentLockQueue(V3Base):
    def test_same_agent_second_writer_queues(self):
        root = self.init()
        store = self.store_dir(root)
        acquired = []

        def _second():
            with relay.agent_lock(store, 'codex'):
                acquired.append(True)

        with relay.agent_lock(store, 'codex'):
            t = threading.Thread(target=_second)
            t.start()
            time.sleep(0.4)
            self.assertEqual([], acquired, 'second writer must queue')
        t.join(timeout=5)
        self.assertEqual([True], acquired)

    def test_different_agents_do_not_block_each_other(self):
        root = self.init()
        store = self.store_dir(root)
        with relay.agent_lock(store, 'codex'):
            with relay.agent_lock(store, 'claude-code'):
                pass  # must not block

    def test_outer_lock_is_independent_of_agent_lock(self):
        root = self.init()
        store = self.store_dir(root)
        with relay.agent_lock(store, 'codex'):
            with relay.outer_lock(store):
                pass  # must not block


class TestResumeSmoke(V3Base):
    def test_resume_without_archive_reports_no_archive(self):
        root = self.make_repo('proj')
        result = self.resume(root)
        self.assertTrue(result.get('no_archive'))
        self.assertIn('relay init', result['message'])

    def test_resume_with_archive_renders_five_sections(self):
        root = self.init()
        self.confirm_goal(root)
        self.capture(root, 'codex')
        self.summarize(root, 'codex', semantic_text='做了演示，原因是要验证，决定继续，尾巴是待验证。')
        text = self.resume_text(root)
        self.assertIn('一段', text)
        self.assertIn('五段', text)


if __name__ == '__main__':
    import unittest
    unittest.main()
