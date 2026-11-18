"""Batch 16: DeepSeek Harness is the only first-class product agent.

Pins the DSH-only product surface: init creates only agents/deepseek-harness,
recipe install writes the DSH-scanned <repo>/.dsh/skills/project-relay path,
--home writes ~/.agents/skills/project-relay, and README/SKILL describe DSH.
"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / 'tests'))


def _repo(testcase):
    tmp = tempfile.TemporaryDirectory(prefix='relay-b16-')
    testcase.addCleanup(tmp.cleanup)
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


class TestDshReadmeAndSkill(unittest.TestCase):
    def setUp(self):
        self.readme = (SKILL / 'README.md').read_text(encoding='utf-8')
        self.skill = (SKILL / 'SKILL.md').read_text(encoding='utf-8')

    def test_readme_is_dsh_only(self):
        self.assertIn('DeepSeek Harness', self.readme)
        self.assertIn('recipe install --tool deepseek-harness', self.readme)
        self.assertIn('.dsh/skills/project-relay', self.readme)

    def test_skill_is_dsh_only(self):
        self.assertIn('DeepSeek Harness', self.skill)
        self.assertIn('agents/deepseek-harness', self.skill)
        self.assertIn('relay work', self.skill)
        self.assertNotIn('WorkBuddy', self.skill)
        self.assertNotIn('Claude Code', self.skill)
        self.assertNotIn('Codex', self.skill)


class TestDshRecipe(unittest.TestCase):
    def test_install_writes_dsh_skill_and_uninstall_removes(self):
        from v3_helpers import relay, flow
        repo = _repo(self)
        relay.init(repo)
        info = flow.install_deepseek_harness(repo)
        skill = repo / '.dsh' / 'skills' / 'project-relay' / 'SKILL.md'
        self.assertTrue(skill.is_file(), 'installed DSH SKILL.md missing')
        self.assertEqual(info['tool'], 'deepseek-harness')
        installed = skill.read_bytes()
        self.assertIn(b'name: project-relay', installed)
        self.assertIn(b'agent_created: true', installed)
        out = flow.uninstall_deepseek_harness(repo)
        self.assertFalse(skill.exists())
        self.assertEqual(str(repo / '.dsh' / 'skills' / 'project-relay'), out['removed'])

    def test_install_copies_engine_and_templates(self):
        from v3_helpers import relay, flow
        repo = _repo(self)
        relay.init(repo)
        info = flow.install_deepseek_harness(repo)
        skill_dir = Path(info['skill_dir'])
        self.assertTrue((skill_dir / 'scripts' / 'relay.py').is_file())
        self.assertTrue((skill_dir / 'scripts' / 'relay_work.py').is_file())
        self.assertTrue((skill_dir / 'assets' / 'template-v2' / 'memory.md').is_file())
        self.assertTrue((skill_dir / 'references' / 'v2-flow.md').is_file())

    def test_home_install_writes_user_agents_skills(self):
        from v3_helpers import relay, flow
        repo = _repo(self)
        relay.init(repo)
        home = Path(tempfile.mkdtemp(prefix='relay-b16-home-'))
        self.addCleanup(lambda: __import__('shutil').rmtree(home, ignore_errors=True))
        info = flow.install_deepseek_harness(repo, home_dir=home)
        skill = home / '.agents' / 'skills' / 'project-relay' / 'SKILL.md'
        self.assertTrue(skill.is_file())
        self.assertEqual(info['skill_dir'], str(home / '.agents' / 'skills' / 'project-relay'))

    def test_uninstall_rejects_tampered_skill_dir(self):
        from v3_helpers import relay, flow
        repo = _repo(self)
        relay.init(repo)
        flow.install_deepseek_harness(repo)
        store = relay.store_dir_for(repo)
        state_path = store / 'agents' / 'deepseek-harness' / 'recipe.json'
        state = relay.read_json(state_path)
        bad_tmp = tempfile.TemporaryDirectory(prefix='relay-b16-bad-')
        self.addCleanup(bad_tmp.cleanup)
        precious = Path(bad_tmp.name) / 'precious'
        precious.mkdir()
        state['skill_dir'] = str(precious)
        relay.save(state_path, state)
        with self.assertRaises(relay.RelayError) as ctx:
            flow.uninstall_deepseek_harness(repo)
        self.assertIn('不是合法的 DSH 技能目录', str(ctx.exception))
        self.assertTrue(precious.is_dir(), 'tampered uninstall deleted an unrelated directory')

    def test_init_creates_only_dsh_agent(self):
        from v3_helpers import relay
        repo = _repo(self)
        relay.init(repo)
        store = relay.store_dir_for(repo)
        self.assertTrue((store / 'agents' / 'deepseek-harness' / 'memory.md').is_file())
        for old in ('codex', 'claude-code', 'workbuddy'):
            self.assertFalse((store / 'agents' / old).exists(), old)

    def test_version_and_packaged_templates_are_consistent(self):
        from v3_helpers import relay
        self.assertEqual('0.2.0', relay.VERSION)
        pyproject = (SKILL / 'pyproject.toml').read_text(encoding='utf-8')
        self.assertIn('version = "0.2.0"', pyproject)
        for name in relay.AGENT_FILES:
            template = 'agent-goal.md' if name == 'goal.md' else name
            self.assertIn('assets/template-v2/' + template, pyproject,
                          'pyproject data-files missing ' + template)
            self.assertTrue((relay.TEMPLATES_DIR / template).is_file(),
                            'template missing at runtime: ' + template)

    def test_cli_choices_accept_deepseek_harness(self):
        from v3_helpers import relay
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            with self.assertRaises(SystemExit) as ctx:
                relay.main(['recipe', 'install', '--help'])
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn('deepseek-harness', buf.getvalue())


if __name__ == '__main__':
    unittest.main()
