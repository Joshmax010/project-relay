"""v3 CLI tests (batch 18): relay.main end to end.

Covers scripts/relay.py _parser/main only through the public CLI
(V3Base.cli). Subcommand names and flags were read off _parser
(lines ~899-1007) before writing; deleted commands (pack, sync-setup,
sync-claim, sync-rollback, sync-status, stats, fork, backfill,
promote, accept) are not tested.

V3Base.cli captures stdout only, so this file adds its own cli_err()
helper with contextlib.redirect_stderr for the error paths.

Notes on behaviour the brief did not pin down, asserted as
designed (fixed in this batch where the old text said otherwise):
- resume on an unbuilt repo returns 0 with a no-archive note, not
  1/ERROR: no archive is a normal state for a new project, and the
  note tells the user to run relay init;
- capture on an unbuilt repo used to raise relay_flow's RelayError
  out of main() (double-loaded RelayError class escaped main()'s
  except clause). Fixed: the sibling modules share one process-wide
  relay copy, so main() catches it and returns 1 + ERROR;
- export-zip used to fail with NameError: _FLOW is not defined
  (scripts/relay_sync.py never initialised the module global).
  Fixed: the zip round-trip below now runs end to end.
"""
import contextlib
import io
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from v3_helpers import V3Base, relay, flow, sync, stats, fork, RelayError, GOAL_TEXT


class CLIBase(V3Base):
    def cli_err(self, *args):
        """Run relay.main(argv) capturing stdout AND stderr.

        Returns (code, stdout, stderr); exceptions (argparse
        SystemExit, uncaught RelayError) propagate to the caller.
        """
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = relay.main([str(a) for a in args])
        return code, out.getvalue(), err.getvalue()

    def draft_goal(self, root):
        draft = Path(self.temp.name) / 'draft-goal.md'
        draft.write_text(GOAL_TEXT, encoding='utf-8')
        code, out, err = self.cli_err('--root', root, 'goal', 'draft',
                                      '--file', draft)
        self.assertEqual(0, code, err)
        return out


class TestInitAgents(CLIBase):
    def test_init_prints_store_path_and_gitignore_hint(self):
        root = self.make_repo('proj')
        code, out, err = self.cli_err('--root', root, 'init')
        self.assertEqual(0, code, err)
        self.assertIn('记忆仓：' + str(root / '.project-relay'), out)
        self.assertIn('gitignore', out)
        self.assertIn('.project-relay/',
                      (root / '.gitignore').read_text(encoding='utf-8'))

    def test_agents_prints_routing_table(self):
        root = self.init()
        code, out, err = self.cli_err('--root', root, 'agents')
        self.assertEqual(0, code, err)
        self.assertIn('指路表', out)
        for agent in ('codex', 'claude-code', 'workbuddy'):
            self.assertIn(agent, out)


class TestGoalFlow(CLIBase):
    def test_goal_draft_show_confirm_full_flow(self):
        root = self.init()
        out = self.draft_goal(root)
        self.assertIn('目标草案已暂存，内容 SHA256：', out)
        code, out, err = self.cli_err('--root', root, 'goal', 'show')
        self.assertEqual(0, code, err)
        self.assertIn('目标草案 diff', out)
        self.assertIn('做一个能跑的演示', out)
        code, out, err = self.cli_err('--root', root, 'goal', 'confirm')
        self.assertEqual(0, code, err)
        self.assertIn('目标已确认，收据：goal-0001.json', out)
        code, out, err = self.cli_err('--root', root, 'goal', 'status')
        self.assertEqual(0, code, err)
        self.assertIn('目标状态：confirmed', out)
        self.assertIn('goal-0001.json', out)

    def test_goal_confirm_without_draft_is_error_1(self):
        root = self.init()
        code, out, err = self.cli_err('--root', root, 'goal', 'confirm')
        self.assertEqual(1, code)
        self.assertIn('ERROR', err)
        self.assertIn('没有待确认的目标草案', err)

    def test_goal_confirm_twice_second_fails(self):
        root = self.init()
        self.draft_goal(root)
        code, _, err = self.cli_err('--root', root, 'goal', 'confirm')
        self.assertEqual(0, code, err)
        code, out, err = self.cli_err('--root', root, 'goal', 'confirm')
        self.assertEqual(1, code)
        self.assertIn('ERROR', err)


class TestStackOutlookAgentGoal(CLIBase):
    def test_stack_set_show_and_file(self):
        root = self.init()
        code, out, err = self.cli_err('--root', root, 'stack',
                                      '--set', 'Python 3.12')
        self.assertEqual(0, code, err)
        self.assertIn('技术栈已写入', out)
        code, out, _ = self.cli_err('--root', root, 'stack')
        self.assertEqual('Python 3.12\n', out)
        stack_file = Path(self.temp.name) / 'stack.txt'
        stack_file.write_text('Go 1.22\n', encoding='utf-8')
        code, out, err = self.cli_err('--root', root, 'stack',
                                      '--file', stack_file)
        self.assertEqual(0, code, err)
        self.assertEqual('Go 1.22\n',
                         relay.read_stack(self.store_dir(root)))

    def test_outlook_set_and_show(self):
        root = self.init()
        code, out, err = self.cli_err('--root', root, 'outlook',
                                      '--set', '下一步：跑通演示')
        self.assertEqual(0, code, err)
        self.assertIn('未来预期已写入', out)
        code, out, _ = self.cli_err('--root', root, 'outlook')
        self.assertEqual('下一步：跑通演示\n', out)

    def test_agent_goal_set_and_show(self):
        root = self.init()
        self.confirm_goal(root)
        code, out, err = self.cli_err('--root', root, 'agent-goal',
                                      '--agent', 'codex', '--set', '负责后端。')
        self.assertEqual(0, code, err)
        self.assertIn('codex 的目标已写入', out)
        code, out, _ = self.cli_err('--root', root, 'agent-goal',
                                    '--agent', 'codex')
        self.assertIn('总目标哈希：', out)
        self.assertIn('负责后端。', out)


class TestLog(CLIBase):
    def test_log_append_and_read(self):
        root = self.init()
        code, out, err = self.cli_err('--root', root, 'log',
                                      '--agent', 'codex',
                                      '--append', 'hello log')
        self.assertEqual(0, code, err)
        self.assertIn('日志已追加到 agents/codex/log.jsonl', out)
        code, out, _ = self.cli_err('--root', root, 'log',
                                    '--agent', 'codex')
        self.assertIn('note', out)
        self.assertIn('hello log', out)
        rows = relay.read_agent_log(self.store_dir(root), 'codex')
        self.assertEqual(1, len(rows))
        self.assertEqual('hello log', rows[0]['detail'])


class TestSyncResolve(CLIBase):
    def test_sync_resolve_list_empty(self):
        root = self.init()
        code, out, err = self.cli_err('--root', root, 'sync-resolve',
                                      '--list')
        self.assertEqual(0, code, err)
        self.assertIn('无待裁项', out)

    def test_sync_resolve_fork_with_agent_flag_reaches_adjudication(self):
        # --agent is plumbed through: with no pending fork for the key
        # the command fails closed naming the key, instead of dying
        # on an unknown flag or ignoring which agent was meant.
        root = self.init()
        code, out, err = self.cli_err('--root', root, 'sync-resolve',
                                      '--fork', 'no-such-key',
                                      '--decision', 'keep-local',
                                      '--agent', 'codex')
        self.assertEqual(1, code)
        self.assertIn('ERROR', err)
        self.assertIn('no-such-key', err)


class TestEndToEnd(CLIBase):
    def test_init_confirm_capture_summarize_resume_five_sections(self):
        root = self.init()
        self.draft_goal(root)
        code, out, err = self.cli_err('--root', root, 'goal', 'confirm')
        self.assertEqual(0, code, err)
        code, out, err = self.cli_err('--root', root, 'capture',
                                      '--agent', 'codex',
                                      '--message', 'did demo work')
        self.assertEqual(0, code, err)
        self.assertIn('采集条目：cap-0001', out)
        code, out, err = self.cli_err('--root', root, 'summarize',
                                      '--agent', 'codex')
        self.assertEqual(0, code, err)
        self.assertIn('汇总序号：1', out)
        code, out, err = self.cli_err('--root', root, 'resume')
        self.assertEqual(0, code, err)
        for section in ('一段', '二段', '三段', '四段', '五段'):
            self.assertIn(section, out, 'missing ' + section)
        self.assertIn('confirmed', out)

    def test_export_clone_import_resume_round_trip(self):
        # BUG fixed (_FLOW initialised): the full handoff runs end to
        # end through the CLI -- export, clean clone (no archive, it
        # is gitignored), import, resume reads the real brief.
        root = self.init()
        self.confirm_goal(root)
        self.capture(root, 'codex', message='did demo work')
        zip_path = Path(self.temp.name) / 'bundle.zip'
        code, out, err = self.cli_err('--root', root, 'export-zip',
                                      '--zip', zip_path)
        self.assertEqual(0, code, err)
        self.assertTrue(zip_path.is_file())
        clone = Path(self.temp.name) / 'clone'
        subprocess.run(['git', 'clone', str(root), str(clone)],
                       capture_output=True, check=True)
        self.assertFalse((clone / '.project-relay').exists())
        code, out, err = self.cli_err('--root', clone, 'import-zip',
                                      '--zip', zip_path, '--confirm')
        self.assertEqual(0, code, err)
        self.assertTrue((clone / '.project-relay' / 'agents' / 'codex'
                         / 'raw' / 'cap-0001.md').is_file())
        code, out, err = self.cli_err('--root', clone, 'resume')
        self.assertEqual(0, code, err)
        for section in ('一段', '二段', '三段', '四段', '五段'):
            self.assertIn(section, out, 'missing ' + section)
        self.assertIn('confirmed', out)
        # import-zip with no bundle file at all still fails closed.
        code, out, err = self.cli_err(
            '--root', clone, 'import-zip',
            '--zip', Path(self.temp.name) / 'no-such.zip', '--confirm')
        self.assertEqual(1, code)
        self.assertIn('ERROR', err)


class TestErrorPaths(CLIBase):
    def test_resume_without_archive_returns_0_with_note(self):
        # Deliberate (see module docstring): no archive is a normal
        # state for a new project, so resume reports it and returns 0.
        root = self.make_repo('proj')
        code, out, err = self.cli_err('--root', root, 'resume')
        self.assertEqual(0, code, err)
        self.assertIn('无档案', out)
        self.assertIn('relay init', out)

    def test_capture_without_archive_returns_1_with_error(self):
        # BUG fixed (one process-wide relay copy): main()'s except
        # clause now catches the RelayError flow raises, so the CLI
        # returns 1 + stderr ERROR instead of leaking a traceback.
        root = self.make_repo('proj')
        code, out, err = self.cli_err('--root', root, 'capture',
                                      '--agent', 'codex', '--message', 'hi')
        self.assertEqual(1, code)
        self.assertIn('ERROR', err)
        self.assertIn('尚未建仓', err)

    def test_unknown_subcommand_exits_2(self):
        root = self.init()
        with self.assertRaises(SystemExit) as ctx:
            self.cli_err('--root', root, 'nosuchcmd')
        self.assertEqual(2, ctx.exception.code)


if __name__ == '__main__':
    import unittest
    unittest.main()
