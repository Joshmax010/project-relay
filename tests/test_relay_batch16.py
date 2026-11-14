"""Batch 16: DSH (DeepSeek Harness) install via the kimi-code-cli borrow,
--skill pinning on every skills CLI command, and the non-TTY prompt warning.

Fail-before: README has no DSH section (no 'kimi-code-cli', no
'~/.agents/skills', no '不在注册表'); the six skills CLI commands lack
'--skill project-relay'; nothing warns that a non-interactive shell pops
the install confirmation and cancels. SKILL.md has no DSH line either.
"""
import sys
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / 'tests'))


class TestBatch16Readme(unittest.TestCase):
    def setUp(self):
        self.readme = (SKILL / 'README.md').read_text(encoding='utf-8')
        self.skill = (SKILL / 'SKILL.md').read_text(encoding='utf-8')

    def test_skill_flag_on_all_runners(self):
        for runner in ('npx skills add', 'bun x skills add',
                       'pnpm dlx skills add',
                       'deno run -A npm:skills add',
                       'skills add Joshmax010/project-relay',
                       'yarn dlx skills add'):
            line = next(l for l in self.readme.splitlines()
                        if runner in l)
            self.assertIn('--skill project-relay', line, line)

    def test_non_tty_warning(self):
        self.assertIn('终端不接受输入时没它会弹确认然后取消', self.readme)

    def test_pinned_phrases_survive(self):
        self.assertIn('装到当前这个 agent 里', self.readme)
        self.assertIn('看不到就直说', self.readme)
        self.assertNotIn('想装到哪个工具也可以一起说', self.readme)


class TestBatch16DshRecipe(unittest.TestCase):
    """DSH is a first-class weak-tier tool now: recipe install/uninstall
    write/remove a project-local .deepseek-harness/skills/project-relay/SKILL.md
    carrying the same injected skill text, and CLI choices accept it."""

    def _repo(self):
        import subprocess, tempfile
        from pathlib import Path
        tmp = tempfile.TemporaryDirectory(prefix='relay-b16-')
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name) / 'proj'
        repo.mkdir(parents=True, exist_ok=True)
        subprocess.run(['git', 'init', '-b', 'main', str(repo)],
                       capture_output=True, check=True)
        subprocess.run(['git', '-C', str(repo), 'config', 'user.name', 'T'],
                       capture_output=True, check=True)
        subprocess.run(['git', '-C', str(repo), 'config', 'user.email',
                        't@e.invalid'], capture_output=True, check=True)
        subprocess.run(['git', '-C', str(repo), 'config', 'commit.gpgsign',
                        'false'], capture_output=True, check=True)
        (repo / 'app.py').write_text('value = 1\n', encoding='utf-8')
        subprocess.run(['git', '-C', str(repo), 'add', '.'], capture_output=True,
                       check=True)
        subprocess.run(['git', '-C', str(repo), 'commit', '-m', 'baseline'],
                       capture_output=True, check=True)
        return repo

    def test_install_writes_skill_and_uninstall_removes(self):
        from v3_helpers import relay, flow
        repo = self._repo()
        relay.init(repo)
        info = flow.install_deepseek_harness(repo)
        skill = repo / '.deepseek-harness' / 'skills' / 'project-relay' / 'SKILL.md'
        self.assertTrue(skill.is_file(), 'installed DSH SKILL.md missing')
        self.assertEqual(info['tool'], 'deepseek-harness')
        installed = skill.read_bytes()
        self.assertIn(b'name: project-relay', installed)
        self.assertIn(b'agent_created: true', installed)
        self.assertIn(flow.RELAY_SKILL_TEXT.splitlines()[0].encode(), installed)
        out = flow.uninstall_deepseek_harness(repo)
        self.assertFalse(skill.exists())
        self.assertIn('.deepseek-harness', out['removed'])

    def test_cli_choices_accept_deepseek_harness(self):
        from v3_helpers import relay
        import contextlib, io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            with self.assertRaises(SystemExit) as ctx:
                relay.main(['recipe', 'install', '--help'])
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn('deepseek-harness', buf.getvalue())

    def test_all_four_agents_get_full_skeleton(self):
        from v3_helpers import relay
        repo = self._repo()
        relay.init(repo)
        store = relay.store_dir_for(repo)
        for agent in ('codex', 'claude-code', 'workbuddy', 'deepseek-harness'):
            adir = store / 'agents' / agent
            self.assertTrue((adir / 'memory.md').is_file(), agent)
            self.assertTrue((adir / 'goal.md').is_file(), agent)
            self.assertTrue((adir / 'log.jsonl').is_file(), agent)


if __name__ == '__main__':
    unittest.main()
