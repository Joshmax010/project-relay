"""Codex PR #5 第二批评审 · 第一批修复回归测试（A1、A3、A4、A5、A6、A7）。

Style follows the existing suites: real temporary directories, real Git
repos, PROJECT_RELAY_HOME redirected, no network, no real ~/.project-relay/.
A2 (recover) belongs to the second batch and is deliberately NOT covered
here; relay_sync.py is untouched by this batch.
"""
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SKILL = Path(__file__).resolve().parents[1]
SECRET_AWS = 'AKIAIOSFODNN7EXAMPLE'  # AWS documentation example key, not real.
SECRET_GH = 'ghp_abcdefghij1234567890'  # Example-shaped token, not real.


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


relay = _load('relay', SKILL / 'scripts/relay.py')
flow = _load('relay_flow', SKILL / 'scripts/relay_flow.py')
backfill_mod = _load('relay_backfill', SKILL / 'scripts/relay_backfill.py')
FlowRelayError = flow.relay.RelayError


def _write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w', encoding='utf-8', newline='') as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + '\n')


class Batch1Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='relay-batch1-')
        self.addCleanup(self.temp.cleanup)
        self.store = Path(self.temp.name) / 'store'
        self.log_home = Path(self.temp.name) / 'loghome'
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
        root = self.make_repo(name, remote=remote or f'https://github.com/example/{name}.git')
        relay.init(self.store, root)
        return root

    def store_dir(self, root):
        row = relay.where(self.store, path=root)['row']
        return self.store / 'projects' / (row['key'] + '-' + row['slug'])

    def entries(self, root):
        return flow.list_entries(self.store_dir(root))


class TestA1HookCwdGate(Batch1Base):
    def test_hook_capture_allows_own_project_and_subdir(self):
        root_a = self.init('proj-a')
        sub = root_a / 'pkg'
        sub.mkdir()
        info = flow.hook_capture(self.store, root_a, tool='claude-code', cwd=sub)
        self.assertNotIn('skipped', info)
        self.assertEqual(1, info['seq'])
        self.assertEqual(1, len(self.entries(root_a)))

    def test_hook_capture_and_enforce_skip_other_project(self):
        root_a = self.init('proj-a')
        root_b = self.init('proj-b')
        # Own-project capture first, so enforce has a placeholder to block on.
        flow.hook_capture(self.store, root_a, tool='claude-code', cwd=root_a)
        skipped = flow.hook_capture(self.store, root_a, tool='claude-code', cwd=root_b)
        self.assertTrue(skipped.get('skipped'))
        self.assertEqual(1, len(self.entries(root_a)))
        # Enforce from the other project's cwd must PASS, never block it.
        result = flow.hook_enforce(self.store, root_a, tool='claude-code', cwd=root_b)
        self.assertEqual('pass', result['action'])
        # Same call from the own cwd blocks on the placeholder entry.
        result = flow.hook_enforce(self.store, root_a, tool='claude-code', cwd=root_a)
        self.assertEqual('block', result['action'])
        # A cwd outside any repo fails closed as well.
        outside = Path(self.temp.name) / 'not-a-repo'
        outside.mkdir()
        self.assertFalse(flow.hook_cwd_allowed(self.store, root_a, cwd=outside))
        self.assertTrue(flow.hook_capture(self.store, root_a, cwd=outside).get('skipped'))

    def test_hook_resume_skips_other_project(self):
        root_a = self.init('proj-a')
        root_b = self.init('proj-b')
        result = flow.hook_resume(self.store, root_a, tool='claude-code', cwd=root_b)
        self.assertTrue(result.get('skipped'))

    def test_installed_hook_commands_respect_cwd(self):
        root_a = self.init('proj-a')
        root_b = self.init('proj-b')
        sub = root_a / 'pkg'
        sub.mkdir()
        home = Path(self.temp.name) / 'fakehome'
        flow.install_claude(self.store, root_a, home_dir=home)
        settings = json.loads((home / '.claude' / 'settings.json').read_text(encoding='utf-8'))
        stop_cmds = [h['hooks'][0]['command'] for h in settings['hooks']['Stop']]
        # Batch 3, C2: exactly one Stop handler, sequencing capture then
        # enforce in a single process.
        self.assertEqual(1, len(stop_cmds))
        stop_cmd = stop_cmds[0]
        self.assertIn('hook_capture', stop_cmd)
        self.assertIn('hook_enforce', stop_cmd)
        if os.name == 'nt':
            # The installed command is a POSIX-shell-quoted `python -c`
            # line; cmd.exe cannot run it. Shell execution is covered on
            # POSIX; the cwd gate itself is covered in-process above.
            return
        # From project B: capture does nothing, enforce passes.
        out = subprocess.run(stop_cmd, shell=True, cwd=str(root_b), capture_output=True,
                             input=b'{"session_id": "s-x"}')
        self.assertEqual(0, out.returncode)
        self.assertIn('不属于本项目', out.stdout.decode('utf-8'))
        self.assertEqual(0, len(self.entries(root_a)))
        # From project A (even a subdir): capture lands first, then
        # enforce blocks on the placeholder the capture just left --
        # the ordering C2 guarantees (the old parallel handlers raced).
        out = subprocess.run(stop_cmd, shell=True, cwd=str(sub), capture_output=True,
                             input=b'{"session_id": "s-x"}')
        self.assertEqual(2, out.returncode)
        self.assertEqual(1, len(self.entries(root_a)))
        self.assertIn('补填采集条目', out.stdout.decode('utf-8'))


class TestA3BackfillRedaction(Batch1Base):
    def test_secret_never_reaches_formal_archive(self):
        proj = self.make_repo('proj-secret')
        path = (self.log_home / '.codex' / 'sessions' / '2026' / '10' / '28'
                / 'rollout-secret.jsonl')
        _write_jsonl(path, [
            {'type': 'event_msg', 'payload': {'type': 'session_meta', 'cwd': str(proj)}},
            {'type': 'event_msg', 'payload': {'type': 'message', 'role': 'user',
                                              'text': '决定：采用 %s 方案' % SECRET_AWS}},
            {'type': 'event_msg', 'payload': {'type': 'message', 'role': 'user',
                                              'text': '教训：密钥 %s 曾外泄' % SECRET_GH}},
            {'type': 'event_msg', 'payload': {'type': 'message', 'role': 'user',
                                              'text': '待办：轮换 %s' % SECRET_AWS}},
        ])
        report = backfill_mod.backfill(self.store, proj, home=self.log_home,
                                       tools=['codex'], tier='excerpt',
                                       summarize_after=True)
        self.assertIsNotNone(report['summary_seq'])
        store_dir = self.store_dir(proj)
        leaked = [p for p in store_dir.rglob('*')
                  if p.is_file()
                  and (SECRET_AWS.encode() in p.read_bytes()
                       or SECRET_GH.encode() in p.read_bytes())]
        self.assertEqual(leaked, [])
        decisions = (store_dir / 'archive' / 'decisions.md').read_text(encoding='utf-8')
        self.assertIn('采用', decisions)
        lessons = (store_dir / 'archive' / 'lessons.md').read_text(encoding='utf-8')
        self.assertIn('曾外泄', lessons)
        backlog = (store_dir / 'archive' / 'backlog.md').read_text(encoding='utf-8')
        self.assertIn('轮换', backlog)


class TestA4HookQuoting(Batch1Base):
    def test_relay_cmd_round_trips_through_shlex(self):
        weird = Path(self.temp.name) / 'a b $x `y` "q"'
        cmd = flow._relay_cmd(weird, 'capture', '--tool', 'claude-code', '--message', 'a b$c')
        parts = shlex.split(cmd)
        self.assertEqual(str(weird), parts[parts.index('--root') + 1])
        self.assertEqual('a b$c', parts[-1])

    def test_installed_hook_survives_metachar_path_without_execution(self):
        # Directory name carries space, $, backticks, quotes and $(...) --
        # under the old bare interpolation the shell would have run them.
        # Batch 3, A3: `"` is illegal in a Windows directory name, so
        # the Windows fixture drops it and keeps the other metacharacters.
        name = ('a b $(touch pwn1) `touch pwn2` $HOME' if os.name == 'nt'
                else 'a b $(touch pwn1) `touch pwn2` "q" $HOME')
        root = self.init(name)
        home = Path(self.temp.name) / 'fakehome4'
        flow.install_claude(self.store, root, home_dir=home)
        settings = json.loads((home / '.claude' / 'settings.json').read_text(encoding='utf-8'))
        stop_cmds = [h['hooks'][0]['command'] for h in settings['hooks']['Stop']]
        # Batch 3, C2: the Stop handler is now the single combined
        # capture-then-enforce command; running it from the project
        # captures one entry and then blocks on its placeholder.
        self.assertEqual(1, len(stop_cmds))
        stop_cmd = stop_cmds[0]
        if os.name == 'nt':
            # cmd.exe cannot run the POSIX-quoted command; the quoting
            # logic itself is covered by the shlex round-trip test.
            self.assertIn('hook_capture', stop_cmd)
            return
        out = subprocess.run(stop_cmd, shell=True, cwd=str(root), capture_output=True,
                             input=b'{"session_id": "s-x"}')
        self.assertEqual(2, out.returncode, out.stdout.decode('utf-8') + out.stderr.decode('utf-8'))
        self.assertEqual(1, len(self.entries(root)))
        self.assertFalse((root / 'pwn1').exists())
        self.assertFalse((root / 'pwn2').exists())


class TestA5CodebuddyUninstall(Batch1Base):
    def _install(self):
        root = self.init()
        target = Path(self.temp.name) / 'targethome'
        info = flow.install_codebuddy(self.store, root, target, flavor='codebuddy')
        return root, Path(info['skill_dir'])

    def _recipe_state(self, root):
        row = relay.where(self.store, path=root)['row']
        return self.store_dir(root) / '..' / 'global' / 'recipes' / ('codebuddy-%s.json' % row['key'])

    def test_pure_relay_dir_removed(self):
        root, skill_dir = self._install()
        flow.uninstall_codebuddy(self.store, root, flavor='codebuddy')
        self.assertFalse(skill_dir.exists())
        self.assertFalse(self._recipe_state(root).exists())

    def test_user_file_blocks_whole_dir_delete_and_stays_intact(self):
        root, skill_dir = self._install()
        user_file = skill_dir / 'notes.txt'
        user_file.write_bytes('my notes\n'.encode('utf-8'))
        nested = skill_dir / 'extra'
        nested.mkdir()
        nested_file = nested / 'keep.md'
        nested_file.write_bytes('keep me\n'.encode('utf-8'))
        with self.assertRaises(FlowRelayError) as ctx:
            flow.uninstall_codebuddy(self.store, root, flavor='codebuddy')
        self.assertIn('notes.txt', str(ctx.exception))
        self.assertIn('extra/keep.md', str(ctx.exception))
        self.assertEqual('my notes\n', user_file.read_text(encoding='utf-8'))
        self.assertEqual('keep me\n', nested_file.read_text(encoding='utf-8'))
        self.assertTrue((skill_dir / 'SKILL.md').exists())
        self.assertTrue(self._recipe_state(root).exists())

    def test_modified_skill_md_blocks_delete(self):
        root, skill_dir = self._install()
        skill = skill_dir / 'SKILL.md'
        before = skill.read_bytes()
        skill.write_bytes((skill.read_text(encoding='utf-8') + '\nmy edit\n').encode('utf-8'))
        with self.assertRaises(FlowRelayError):
            flow.uninstall_codebuddy(self.store, root, flavor='codebuddy')
        self.assertIn(b'my edit', skill.read_bytes())
        self.assertNotEqual(before, skill.read_bytes())


class TestA6CodexDedup(Batch1Base):
    def _user_msgs(self, proj, records):
        path = (self.log_home / '.codex' / 'sessions' / '2026' / '10' / '28'
                / 'rollout-dedup.jsonl')
        _write_jsonl(path, records)
        parser = backfill_mod.CodexParser()
        sessions = parser.sessions(self.log_home)
        events = parser.events(sessions[0])
        return [e for e in events if e['kind'] == 'user_message']

    def _meta(self, proj):
        return {'type': 'event_msg', 'payload': {'type': 'session_meta', 'cwd': str(proj)}}

    def test_true_double_write_still_collapses(self):
        proj = self.make_repo('proj-dedup')
        msgs = self._user_msgs(proj, [
            self._meta(proj),
            {'type': 'event_msg', 'payload': {'type': 'message', 'role': 'user', 'text': '同一句话'}},
            {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'text': '同一句话'}},
        ])
        self.assertEqual(1, len(msgs))

    def test_same_stream_repeat_kept(self):
        proj = self.make_repo('proj-dedup')
        msgs = self._user_msgs(proj, [
            self._meta(proj),
            {'type': 'event_msg', 'payload': {'type': 'message', 'role': 'user', 'text': '同一句话'}},
            {'type': 'event_msg', 'payload': {'type': 'message', 'role': 'user', 'text': '同一句话'}},
        ])
        self.assertEqual(2, len(msgs))

    def test_double_written_repeat_kept_twice(self):
        proj = self.make_repo('proj-dedup')
        msgs = self._user_msgs(proj, [
            self._meta(proj),
            {'type': 'event_msg', 'payload': {'type': 'message', 'role': 'user', 'text': '同一句话'}},
            {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'text': '同一句话'}},
            {'type': 'event_msg', 'payload': {'type': 'message', 'role': 'user', 'text': '同一句话'}},
            {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'text': '同一句话'}},
        ])
        self.assertEqual(2, len(msgs))

    def test_stream_flipped_repeat_kept_twice(self):
        proj = self.make_repo('proj-dedup')
        msgs = self._user_msgs(proj, [
            self._meta(proj),
            {'type': 'event_msg', 'payload': {'type': 'message', 'role': 'user', 'text': '同一句话'}},
            {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'text': '同一句话'}},
            {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'text': '同一句话'}},
            {'type': 'event_msg', 'payload': {'type': 'message', 'role': 'user', 'text': '同一句话'}},
        ])
        self.assertEqual(2, len(msgs))


class TestA7InitAtomic(Batch1Base):
    def test_skeletons_complete_and_no_temp_leftovers(self):
        root = self.init()
        store_dir = self.store_dir(root)
        for name in relay.ARCHIVE_FILES:
            template = (relay.TEMPLATES_DIR / name).read_bytes()
            self.assertEqual(template, (store_dir / 'archive' / name).read_bytes())
        leftovers = [p for p in (store_dir / 'archive').glob('.relay-*')]
        self.assertEqual(leftovers, [])

    def test_truncated_skeleton_repaired_on_second_init(self):
        root = self.init()
        store_dir = self.store_dir(root)
        state_path = store_dir / 'archive' / 'state.md'
        goal_path = store_dir / 'archive' / 'goal.md'
        state_path.write_bytes((relay.TEMPLATES_DIR / 'state.md').read_bytes()[:10])
        goal_path.write_bytes(b'')
        result = relay.init(self.store, root)
        self.assertIn('state.md', result['repaired_skeletons'])
        self.assertIn('goal.md', result['repaired_skeletons'])
        self.assertEqual((relay.TEMPLATES_DIR / 'state.md').read_bytes(),
                         state_path.read_bytes())
        self.assertEqual((relay.TEMPLATES_DIR / 'goal.md').read_bytes(),
                         goal_path.read_bytes())

    def test_user_edited_skeleton_left_alone(self):
        root = self.init()
        store_dir = self.store_dir(root)
        custom = '我自己的决策记录\n## D-001\n- 选择：手工写\n'
        dec_path = store_dir / 'archive' / 'decisions.md'
        dec_path.write_bytes(custom.encode('utf-8'))
        result = relay.init(self.store, root)
        self.assertNotIn('decisions.md', result['repaired_skeletons'])
        self.assertEqual(custom, dec_path.read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
