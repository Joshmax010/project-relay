"""Exercise real Git staging and human-review state transitions in temp repos."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SKILL = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('relayctl', SKILL / 'scripts/relayctl.py')
relay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(relay)


class Workflow(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='relay-测试-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'code project'
        self.root.mkdir()
        self.git('init', '-b', 'main')
        self.git('config', 'user.name', 'Test reviewer')
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('config', 'commit.gpgsign', 'false')
        self.git('config', 'core.autocrlf', 'false')
        subprocess.run([sys.executable, str(SKILL / 'scripts/init_project.py'), str(self.root)],
                       check=True, capture_output=True)
        self.agent = self.root / '.agent'
        (self.root / 'app.py').write_text('value = 1\n')
        self.git('add', '.')
        self.git('commit', '-m', 'Previously reviewed baseline fixture')
        (self.root / 'app.py').write_text('value = 2\n')

    def git(self, *args):
        r = subprocess.run(['git', '-C', str(self.root), *args], capture_output=True, check=True)
        return r.stdout

    def call(self, fn, *args, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return fn(self.root, self.agent, *args, **kwargs)

    def mapping(self, label='task'):
        return {'state.md': '# 当前状态\n来源：用户任务；结果：测试用样例。\n最近会话：[执行](sessions/' + label + '.md)\n',
                'sessions/' + label + '.md': '# 执行记录\n改动 app.py，样例测试通过，来源：测试夹具。\n',
                'sessions/index.md': '# 会话索引\n- [执行](' + label + '.md)\n'}

    def prepare(self, mapping=None):
        mapping = mapping or self.mapping()
        bid = self.call(relay.draft, mapping, 'test task')
        self.call(relay.review, bid, approve=list(mapping), reviewer='human fixture')
        self.call(relay.apply, bid)
        return bid

    def test_draft_does_not_touch_formal_docs(self):
        before = relay.snapshot(self.root)
        bid = self.call(relay.draft, self.mapping(), 'unfinished')
        self.assertEqual(before, relay.snapshot(self.root))
        self.assertTrue((self.agent / 'inbox' / bid / 'batch.json').exists())
        with self.assertRaises(relay.RelayError):
            self.call(relay.commit_check)

    def test_pending_or_refused_sync_blocks_apply(self):
        bid = self.call(relay.draft, self.mapping(), 'pending')
        with self.assertRaises(relay.RelayError):
            self.call(relay.apply, bid)
        self.call(relay.review, bid, reject=list(self.mapping()), reason='no sync')
        with self.assertRaises(relay.RelayError):
            self.call(relay.apply, bid)

    def test_partial_approval_preserves_rejected_doc(self):
        before = (self.agent / 'lessons.md').read_bytes()
        mapping = dict(self.mapping(), **{'lessons.md': '# Incorrect hypothesis\n'})
        bid = self.call(relay.draft, mapping, 'partial')
        self.call(relay.review, bid, approve=list(self.mapping()), reviewer='human fixture')
        with self.assertRaises(relay.RelayError):
            self.call(relay.apply, bid)
        self.call(relay.review, bid, reject=['lessons.md'], reason='unverified')
        self.call(relay.apply, bid)
        self.assertEqual(before, (self.agent / 'lessons.md').read_bytes())
        self.git('add', '.')
        self.call(relay.commit_check)

    def test_required_state_rejected_blocks_apply(self):
        bid = self.call(relay.draft, self.mapping(), 'partial')
        self.call(relay.review, bid, approve=['sessions/task.md', 'sessions/index.md'], reviewer='human')
        self.call(relay.review, bid, reject=['state.md'])
        with self.assertRaises(relay.RelayError):
            self.call(relay.apply, bid)

    def test_approval_needs_reviewer(self):
        bid = self.call(relay.draft, self.mapping(), 'test')
        with self.assertRaises(relay.RelayError):
            self.call(relay.review, bid, approve=list(self.mapping()))

    def test_old_draft_cannot_overwrite_new_state(self):
        bid = self.call(relay.draft, self.mapping(), 'old')
        (self.agent / 'state.md').write_text('# New externally verified state\n')
        with self.assertRaises(relay.RelayError):
            self.call(relay.review, bid, approve=list(self.mapping()), reviewer='human')
        self.assertIn('externally verified', (self.agent / 'state.md').read_text())

    def test_code_changes_before_apply_invalidate_approval(self):
        bid = self.call(relay.draft, self.mapping(), 'test')
        self.call(relay.review, bid, approve=list(self.mapping()), reviewer='human')
        (self.root / 'app.py').write_text('value = 3\n')
        with self.assertRaises(relay.RelayError):
            self.call(relay.apply, bid)

    def test_staging_all_approved_files_passes(self):
        self.prepare()
        self.git('add', '.')
        self.call(relay.commit_check)
        tracked = self.git('ls-files').decode()
        self.assertNotIn('.agent/inbox', tracked)
        self.assertNotIn('.agent/.local', tracked)

    def test_unstaged_or_partially_staged_code_blocks(self):
        self.prepare()
        self.git('add', '.agent')
        with self.assertRaises(relay.RelayError):
            self.call(relay.commit_check)
        self.git('add', 'app.py')
        (self.root / 'app.py').write_text('value = 4\n')
        with self.assertRaises(relay.RelayError):
            self.call(relay.commit_check)

    def test_assume_unchanged_cannot_hide_stale_index_content(self):
        self.git('update-index', '--assume-unchanged', 'app.py')
        self.prepare()
        self.git('add', '.agent')
        self.assertEqual(b'', self.git('diff', '--', 'app.py'))
        with self.assertRaises(relay.RelayError):
            self.call(relay.commit_check)

    def test_autocrlf_staging_matches_approved_raw_working_files(self):
        self.git('config', 'core.autocrlf', 'true')
        (self.root / 'app.py').write_bytes(b'value = 2\r\n')
        self.prepare()
        self.git('add', '.')
        self.call(relay.commit_check)

    def test_new_file_after_approval_blocks(self):
        self.prepare()
        (self.root / 'new.py').write_text('print(1)\n')
        self.git('add', '.')
        with self.assertRaises(relay.RelayError):
            self.call(relay.commit_check)

    def test_formal_edit_after_approval_blocks(self):
        self.prepare()
        (self.agent / 'decisions.md').write_text('# Unreviewed decision\n')
        self.git('add', '.')
        with self.assertRaises(relay.RelayError):
            self.call(relay.commit_check)

    def test_unreviewed_formal_change_before_draft_is_not_silently_committed(self):
        (self.agent / 'decisions.md').write_text('# Unreviewed decision\n')
        self.prepare()
        self.git('add', '.')
        with self.assertRaises(relay.RelayError):
            self.call(relay.commit_check)

    def test_approval_cannot_be_reused_after_commit(self):
        self.prepare()
        self.git('add', '.')
        self.call(relay.commit_check)
        self.git('commit', '-m', 'Approved task')
        with self.assertRaises(relay.RelayError):
            self.call(relay.commit_check)

    def test_branch_switch_invalidates(self):
        self.prepare()
        self.git('switch', '-c', 'another-branch')
        self.git('add', '.')
        with self.assertRaises(relay.RelayError):
            self.call(relay.commit_check)

    def test_deletion_is_supported_and_staged_with_docs(self):
        (self.root / 'app.py').unlink()
        self.prepare()
        self.git('add', '.')
        self.call(relay.commit_check)

    @unittest.skipIf(os.name == 'nt', 'Windows executable mode differs')
    def test_executable_bit_change_after_approval_blocks(self):
        self.git('config', 'core.filemode', 'true')
        self.prepare()
        (self.root / 'app.py').chmod(0o755)
        self.git('add', '.')
        with self.assertRaises(relay.RelayError):
            self.call(relay.commit_check)

    def test_staged_mode_change_when_filemode_disabled_blocks(self):
        self.git('config', 'core.filemode', 'false')
        self.prepare()
        self.git('add', '.')
        self.call(relay.commit_check)
        self.git('update-index', '--chmod=+x', 'app.py')
        with self.assertRaises(relay.RelayError):
            self.call(relay.commit_check)

    @unittest.skipIf(os.name == 'nt', 'Symlink creation requires platform privileges')
    def test_code_symlink_parent_cannot_change_read_scope(self):
        directory = self.root / 'src'
        directory.mkdir()
        (directory / 'module.py').write_text('value = 1\n')
        self.git('add', 'src')
        (directory / 'module.py').unlink()
        directory.rmdir()
        outside = Path(self.temp.name) / 'external-src'
        outside.mkdir()
        (outside / 'module.py').write_text('external data\n')
        directory.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(relay.RelayError):
            self.call(relay.draft, self.mapping(), 'unsafe code tree')

    def test_ignored_original_chat_does_not_invalidate_code_review(self):
        self.prepare()
        p = self.agent / 'conversations/raw/example.txt'
        p.parent.mkdir(parents=True)
        p.write_text('Exact exported visible messages, fixture only.')
        self.git('add', '.')
        self.call(relay.commit_check)
        self.assertNotIn('conversations/raw', self.git('ls-files').decode())

    def test_force_staging_private_files_blocks(self):
        bid = self.prepare()
        self.git('add', '.')
        self.git('add', '-f', '.agent/inbox/' + bid + '/batch.json')
        with self.assertRaises(relay.RelayError):
            self.call(relay.commit_check)

    def test_apply_interruption_recovery_and_retry(self):
        before = relay.snapshot(self.root)
        bid = self.call(relay.draft, self.mapping(), 'interruption')
        self.call(relay.review, bid, approve=list(self.mapping()), reviewer='human')
        original = relay.atomic
        def fail_on_state(path, data):
            original(path, data)
            if path == self.agent / 'state.md':
                raise OSError('simulated interruption after first formal write')
        with patch.object(relay, 'atomic', side_effect=fail_on_state):
            with self.assertRaises(OSError):
                self.call(relay.apply, bid)
        with self.assertRaises(relay.RelayError):
            self.call(relay.commit_check)
        self.call(relay.recover)
        self.assertEqual(before, relay.snapshot(self.root))
        self.call(relay.apply, bid)
        self.git('add', '.')
        self.call(relay.commit_check)

    def test_recovery_conflict_leaves_external_edit_and_journal(self):
        bid = self.call(relay.draft, self.mapping(), 'interruption')
        self.call(relay.review, bid, approve=list(self.mapping()), reviewer='human')
        original = relay.atomic
        def fail(path, data):
            original(path, data)
            if path == self.agent / 'state.md':
                raise OSError('interruption')
        with patch.object(relay, 'atomic', side_effect=fail):
            with self.assertRaises(OSError):
                self.call(relay.apply, bid)
        (self.agent / 'state.md').write_text('# External change\n')
        with self.assertRaises(relay.RelayError):
            self.call(relay.recover)
        self.assertEqual('# External change\n', (self.agent / 'state.md').read_text())
        self.assertTrue((self.agent / '.local/transaction.json').exists())

    def test_repeated_init_preserves_docs_runtime_and_pending(self):
        self.prepare()
        before = {p.relative_to(self.agent): p.read_bytes() for p in self.agent.rglob('*') if p.is_file()}
        subprocess.run([sys.executable, str(SKILL / 'scripts/init_project.py'), str(self.root)],
                       check=True, capture_output=True)
        after = {p.relative_to(self.agent): p.read_bytes() for p in self.agent.rglob('*') if p.is_file()}
        self.assertEqual(before, after)

    def test_init_rejects_nonrepo_and_subdirectory_without_writes(self):
        outside = Path(self.temp.name) / 'not a repo'
        outside.mkdir()
        subdirectory = self.root / 'subdir'
        subdirectory.mkdir()
        for target in (outside, subdirectory):
            with self.subTest(target=target):
                result = subprocess.run([sys.executable, str(SKILL / 'scripts/init_project.py'), str(target)],
                                        capture_output=True)
                self.assertEqual(1, result.returncode)
                self.assertFalse((target / '.agent').exists())

    def test_incomplete_skill_package_does_not_create_partial_memory(self):
        package = Path(self.temp.name) / 'incomplete-skill'
        (package / 'scripts').mkdir(parents=True)
        initializer = package / 'scripts/init_project.py'
        initializer.write_bytes((SKILL / 'scripts/init_project.py').read_bytes())
        target = Path(self.temp.name) / 'new-code-repo'
        target.mkdir()
        subprocess.run(['git', '-C', str(target), 'init'], check=True, capture_output=True)
        result = subprocess.run([sys.executable, str(initializer), str(target)], capture_output=True)
        self.assertEqual(1, result.returncode)
        self.assertFalse((target / '.agent').exists())

    def test_rejects_target_traversal_and_symlink(self):
        for target in ('../outside.md', '/tmp/outside.md', 'C:/outside.md', 'inbox/x.md', '.local/x.md'):
            with self.subTest(target=target), self.assertRaises(relay.RelayError):
                self.call(relay.draft, {target: 'bad'}, 'bad path')
        if os.name != 'nt':
            out = Path(self.temp.name) / 'outside.md'
            out.write_text('original')
            (self.agent / 'linked.md').symlink_to(out)
            with self.assertRaises(relay.RelayError):
                self.call(relay.draft, {'linked.md': 'changed'}, 'bad link')
            self.assertEqual('original', out.read_text())

    def test_resume_separates_pending_and_formal_without_mutation(self):
        bid = self.call(relay.draft, self.mapping(), 'pending line of inquiry')
        before = relay.snapshot(self.root)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            relay.resume(self.root, self.agent)
        self.assertIn('未经审核', out.getvalue())
        self.assertIn(bid, out.getvalue())
        self.assertEqual(before, relay.snapshot(self.root))

    def test_real_cli_and_lint_exit_codes(self):
        runtime = self.agent / 'tools/relayctl.py'
        r = subprocess.run([sys.executable, str(runtime), '--root', str(self.root), 'commit-check'], capture_output=True)
        self.assertEqual(1, r.returncode)
        mapping = Path(self.temp.name) / 'proposal.json'
        mapping.write_text(json.dumps(self.mapping(), ensure_ascii=False), encoding='utf-8')
        r = subprocess.run([sys.executable, str(runtime), '--root', str(self.root), 'draft', '--file', str(mapping), '--title', 'CLI测试'], capture_output=True)
        self.assertEqual(0, r.returncode, r.stderr)
        self.call(relay.lint)


    def test_initial_unborn_head_requires_approval_for_all_formal_docs(self):
        self.git('update-ref', '-d', 'refs/heads/main')
        self.git('read-tree', '--empty')
        mapping = {p.relative_to(self.agent).as_posix(): p.read_text(encoding='utf-8')
                   for p in self.agent.rglob('*.md')}
        mapping.update(self.mapping())
        self.prepare(mapping)
        self.git('add', '.')
        self.call(relay.commit_check)


if __name__ == '__main__':
    unittest.main()
