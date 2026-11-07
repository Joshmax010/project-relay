"""Batch 14: every way to install the skill -- skills CLI runners (npx /
bun x / pnpm dlx / deno / npm -g), pip/uv/pipx from git, the curl
installer (scripts/install.sh), and the Claude Code plugin marketplace
(.claude-plugin/marketplace.json + plugin.json + hooks/hooks.json).

Fail-before: pyproject.toml does not exist (pip install from git errors
"neither setup.py nor pyproject.toml found"); there is no install.sh, no
.claude-plugin/ directory, no skills/ copy, and no `hook` subcommand, so
every test below fails on the old code.
"""
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest

SKILL = Path(__file__).resolve().parents[1]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


flow = _load('relay_flow', SKILL / 'scripts' / 'relay_flow.py')

PLUGIN_FIELDS = {
    '$schema', 'name', 'displayName', 'version', 'description', 'author',
    'homepage', 'repository', 'license', 'keywords', 'metadata', 'icon',
    'documentationUrl', 'supportUrl', 'privacyPolicyUrl',
    'termsOfServiceUrl', 'defaultEnabled', 'dependencies', 'settings',
    'userConfig', 'types', 'channels', 'skills', 'commands', 'agents',
    'hooks', 'mcpServers', 'lspServers', 'outputStyles', 'workflows',
    'experimental',
}
MKT_FIELDS = {
    'name', 'owner', 'plugins', '$schema', 'description', 'version',
    'metadata', 'forceRemoveDeletedPlugins',
    'allowCrossMarketplaceDependenciesOn', 'renames',
}
# Reserved marketplace names, code.claude.com/docs/en/plugins/
# marketplace-reference (official names, community names, plugin
# directory names, package-manager names, JS object member names).
RESERVED_MKT = {
    'claude-code-marketplace', 'claude-code-plugins',
    'claude-plugins-official', 'anthropic-marketplace',
    'anthropic-plugins', 'agent-skills', 'anthropic-agent-skills',
    'life-sciences', 'knowledge-work-plugins', 'claude-for-legal',
    'claude-for-financial-services', 'financial-services-plugins',
    'first-party-plugins', 'claude-tag-plugins', 'claude-community',
    'claude-plugins-community', 'healthcare',
    'anthropic-plugin-directory', 'claude-plugin-directory',
    'npm', 'pip', 'uv', 'cargo', 'github', 'gh',
}


def _read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


class TestPluginManifests(unittest.TestCase):
    def setUp(self):
        self.plug = _read_json(SKILL / '.claude-plugin' / 'plugin.json')
        self.mkt = _read_json(SKILL / '.claude-plugin' / 'marketplace.json')
        self.hooks = _read_json(SKILL / 'hooks' / 'hooks.json')

    def test_plugin_json_fields(self):
        self.assertEqual(set(self.plug) - PLUGIN_FIELDS, set(),
                         'plugin.json has an unknown top-level key '
                         '(validate would strip it with a warning)')
        self.assertEqual(self.plug['name'], 'project-relay')
        self.assertRegex(self.plug['name'], r'^[a-z0-9][a-z0-9-]*$',
                         'plugin name must be kebab-case')
        self.assertFalse(self.plug['name'].startswith(
            ('claude-', 'anthropic-', 'anthropics-', 'cc-plugin-')),
            'plugin name must not pass as one of Anthropic\'s own')
        for key in ('version', 'description'):
            self.assertTrue(self.plug.get(key),
                            'plugin.json %s missing (validate warns)' % key)
        self.assertTrue(self.plug.get('author', {}).get('name'),
                        'plugin.json author.name missing (validate warns)')
        self.assertTrue(self.plug['homepage'].startswith('https://'),
                        'homepage must parse as a URL or the plugin fails')

    def test_marketplace_json_fields(self):
        self.assertEqual(set(self.mkt) - MKT_FIELDS, set(),
                         'marketplace.json has an unknown top-level key')
        self.assertRegex(self.mkt['name'], r'^[A-Za-z0-9][A-Za-z0-9._-]*$',
                         'marketplace name charset')
        self.assertNotIn('..', self.mkt['name'])
        self.assertNotIn(self.mkt['name'].lower(), RESERVED_MKT,
                         'marketplace name is reserved')
        self.assertFalse(self.mkt['name'].startswith('claudeai-'),
                         'claudeai- names are reserved for claude.ai hosting')
        self.assertTrue(self.mkt['owner'].get('name'),
                        'marketplace owner.name is required')
        self.assertTrue(self.mkt.get('description'),
                        'marketplace description missing (validate warns)')
        self.assertEqual(len(self.mkt['plugins']), 1)
        entry = self.mkt['plugins'][0]
        self.assertEqual(entry['name'], self.plug['name'],
                         'entry name must match plugin.json name')
        self.assertEqual(entry['source'], './',
                         'plugin\'s own repo publishes with source "./"')
        self.assertTrue(entry.get('description'),
                        'entry description shown in /plugin listings')

    def test_versions_match_across_manifests_and_pyproject(self):
        py = tomllib.loads((SKILL / 'pyproject.toml').read_text())
        entry = self.mkt['plugins'][0]
        self.assertEqual(self.plug['version'], entry['version'])
        self.assertEqual(self.plug['version'],
                         py['project']['version'],
                         'plugin.json, marketplace entry and pyproject '
                         'must ship the same version')

    def test_plugin_skill_copy_is_byte_identical_to_root(self):
        copy = SKILL / 'skills' / 'project-relay' / 'SKILL.md'
        self.assertTrue(copy.is_file(), 'plugin skill copy missing')
        self.assertEqual(copy.read_bytes(),
                         (SKILL / 'SKILL.md').read_bytes(),
                         'skills/project-relay/SKILL.md must stay '
                         'byte-identical to the root SKILL.md (and hence '
                         'to RELAY_SKILL_TEXT); copy it, do not edit it')

    def test_hooks_json_shape(self):
        self.assertEqual(set(self.hooks), {'description', 'hooks'},
                         'hooks/hooks.json wraps the event map in "hooks"')
        for event, sub in (('SessionStart', 'resume'), ('Stop', 'stop')):
            entries = self.hooks['hooks'].get(event)
            self.assertEqual(len(entries), 1, '%s must have one entry' % event)
            self.assertIsInstance(entries[0].get('hooks'), list)
            cmd = entries[0]['hooks'][0]
            self.assertEqual(cmd.get('type'), 'command')
            self.assertIn('${CLAUDE_PLUGIN_ROOT}/scripts/relay.py',
                          cmd['command'],
                          'hook commands must use ${CLAUDE_PLUGIN_ROOT}')
            self.assertIn('hook ' + sub, cmd['command'])
        self.assertNotIn('matcher', self.hooks['hooks']['SessionStart'][0],
                         'SessionStart has no matcher, like the recipe '
                         'install: it fires on every session start')


class TestPyproject(unittest.TestCase):
    def setUp(self):
        self.py = tomllib.loads(
            (SKILL / 'pyproject.toml').read_text(encoding='utf-8'))

    def test_project_fields(self):
        proj = self.py['project']
        self.assertEqual(proj['name'], 'project-relay')
        self.assertEqual(proj['version'], '0.1.0')
        self.assertEqual(proj['requires-python'], '>=3.8')
        self.assertEqual(proj['license'], 'MIT')
        self.assertEqual(proj['dependencies'], [])
        self.assertEqual(proj['scripts'], {'relay': 'relay:main'})

    def test_py_modules_cover_every_script(self):
        on_disk = {p.stem for p in (SKILL / 'scripts').glob('*.py')}
        declared = set(self.py['tool']['setuptools']['py-modules'])
        self.assertEqual(declared, on_disk,
                         'py-modules must list every script module, or '
                         'the installed relay cannot import its own parts')
        self.assertEqual(
            self.py['tool']['setuptools']['package-dir'], {'': 'scripts'})

    def test_template_data_files_exist(self):
        files = self.py['tool']['setuptools']['data-files']
        self.assertIn('share/project-relay/template-v2', files)
        for name in files['share/project-relay/template-v2']:
            self.assertTrue((SKILL / name).is_file(),
                            'data file listed but missing: %s' % name)
        for name in ('goal.md', 'state.md', 'backlog.md', 'decisions.md',
                     'lessons.md'):
            self.assertIn('assets/template-v2/' + name,
                          files['share/project-relay/template-v2'],
                          'init copies %s, so pip must ship it' % name)

    def test_installed_layout_template_fallback(self):
        # Simulate a pip install: copy scripts/ somewhere with no assets/
        # next to it, put the templates under <prefix>/share, point
        # sys.prefix at it, and import the copied relay.py. init's
        # template check must pass through the fallback, and must fail
        # when the templates are nowhere at all.
        temp = tempfile.TemporaryDirectory(prefix='relay-batch14-pip-')
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        scripts = base / 'scripts'
        shutil.copytree(SKILL / 'scripts', scripts)
        share = base / 'share' / 'project-relay' / 'template-v2'
        shutil.copytree(SKILL / 'assets' / 'template-v2', share)
        old_prefix = sys.prefix
        sys.prefix = str(base)
        try:
            mod = _load('relay_installed', scripts / 'relay.py')
            self.assertEqual(mod.TEMPLATES_DIR.resolve(), share.resolve())
            mod._check_skill_package()  # must not raise
        finally:
            sys.prefix = old_prefix
        (share / 'goal.md').unlink()
        sys.prefix = str(base)
        try:
            mod = _load('relay_installed_broken', scripts / 'relay.py')
            with self.assertRaises(mod.RelayError):
                mod._check_skill_package()
        finally:
            sys.prefix = old_prefix


class RelayCliBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='relay-batch14-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / 'home'
        self.home.mkdir()
        self.env = dict(os.environ)
        self.env['PROJECT_RELAY_HOME'] = str(self.root / 'store')

    def _relay(self, *argv, cwd=None, input=None):
        return subprocess.run(
            [sys.executable, str(SKILL / 'scripts' / 'relay.py')] + list(argv),
            cwd=str(cwd or self.root), env=self.env, input=input,
            capture_output=True, text=True, timeout=60)

    def _init_repo(self, name='proj'):
        repo = self.root / name
        repo.mkdir()
        subprocess.run(['git', 'init', '-q', '-b', 'main', str(repo)],
                       check=True)
        out = self._relay('init', cwd=repo)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        return repo


class TestHookCli(RelayCliBase):
    def test_hook_resume_in_registered_project(self):
        repo = self._init_repo()
        out = self._relay('hook', 'resume', cwd=repo)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn('目标状态', out.stdout)

    def test_hook_resume_in_unregistered_repo_skips(self):
        repo = self.root / 'foreign'
        repo.mkdir()
        subprocess.run(['git', 'init', '-q', '-b', 'main', str(repo)],
                       check=True)
        out = self._relay('hook', 'resume', cwd=repo)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn('钩子不对账', out.stdout)

    def test_hook_resume_outside_any_repo_skips(self):
        plain = self.root / 'plain'
        plain.mkdir()
        out = self._relay('hook', 'resume', cwd=plain)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn('钩子不对账', out.stdout)

    def test_hook_stop_blocks_on_unfilled_semantic_section(self):
        repo = self._init_repo()
        out = self._relay('capture', '--message', 'hook stop 验证', cwd=repo)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        out = self._relay('hook', 'stop', cwd=repo,
                          input='{"session_id": "t1"}')
        self.assertEqual(out.returncode, 2, out.stdout + out.stderr)
        self.assertIn('采集条目', out.stdout,
                      'Stop must capture first, then enforce')
        self.assertIn('补填', out.stderr,
                      'the block reason goes to stderr, like the recipe '
                      'Stop handler (batch 3, C2)')

    def test_hook_stop_in_foreign_dir_skips_cleanly(self):
        plain = self.root / 'plain'
        plain.mkdir()
        out = self._relay('hook', 'stop', cwd=plain,
                          input='{"session_id": "t1"}')
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn('钩子不对账', out.stdout)

    def test_hook_stop_sequence_function(self):
        repo = self._init_repo()
        # The store is $PROJECT_RELAY_HOME/.project-relay, not the env
        # value itself (relay.py default_store appends STORE_DIRNAME).
        store = (Path(self.env['PROJECT_RELAY_HOME'])
                 / flow.relay.STORE_DIRNAME)
        row = flow.relay.resolve_store_dir(store, repo)[0]
        self.assertIsNotNone(row)
        out = self._relay('capture', '--message', 'hook_stop 函数验证',
                          cwd=repo)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        result = flow.hook_stop(store, repo, tool='claude-code',
                                session='t2', cwd=repo)
        self.assertEqual(result['action'], 'block')
        self.assertIn('capture', result)
        self.assertIn('enforce', result)


class TestInstallScript(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='relay-batch14-sh-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.script = SKILL / 'scripts' / 'install.sh'
        # A committed copy of the working tree, so install.sh can clone
        # it offline through a file:// URL.
        src = self.root / 'src'
        shutil.copytree(SKILL, src, ignore=shutil.ignore_patterns(
            '.git', '__pycache__', '*.muse-github-snapshot-sync.json'))
        (src / '.git').mkdir(exist_ok=True)
        subprocess.run(['git', 'init', '-q', '-b', 'main', str(src)],
                       check=True)
        subprocess.run(['git', '-C', str(src), 'add', '-A'], check=True)
        subprocess.run(['git', '-C', str(src), '-c', 'user.email=t@t',
                        '-c', 'user.name=t', 'commit', '-qm', 'batch14'],
                       check=True)
        self.repo_url = 'file://' + str(src)

    def _run(self, home, *argv, repo_url=None):
        env = {'HOME': str(home), 'PATH': os.environ['PATH'],
               'RELAY_REPO_URL': repo_url or self.repo_url}
        return subprocess.run(['sh', str(self.script)] + list(argv),
                              env=env, capture_output=True, text=True,
                              timeout=120)

    def test_sh_syntax(self):
        out = subprocess.run(['sh', '-n', str(self.script)],
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_install_codex(self):
        home = self.root / 'home-codex'
        home.mkdir()
        out = self._run(home, '--tool', 'codex')
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        clone = home / '.local' / 'share' / 'project-relay'
        self.assertTrue((clone / 'scripts' / 'relay.py').is_file())
        agents = (clone / 'AGENTS.md').read_text(encoding='utf-8')
        self.assertIn('relay', agents,
                      'codex managed section must land in the clone')
        self.assertIn('再跑一次', out.stdout,
                      'codex is per-project: the script must say so')

    def test_install_claude_code(self):
        home = self.root / 'home-claude'
        home.mkdir()
        out = self._run(home, '--tool', 'claude-code')
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        settings = json.loads(
            (home / '.claude' / 'settings.json').read_text())
        self.assertIn('SessionStart', settings.get('hooks', {}))
        self.assertIn('Stop', settings.get('hooks', {}))
        claude_md = (home / '.claude' / 'CLAUDE.md').read_text()
        self.assertIn('relay', claude_md)

    def test_auto_detect_single_agent_dir(self):
        home = self.root / 'home-auto'
        (home / '.codex').mkdir(parents=True)
        out = self._run(home)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn('装到 codex', out.stdout)
        clone = home / '.local' / 'share' / 'project-relay'
        self.assertIn('relay', (clone / 'AGENTS.md').read_text())

    def test_no_agent_dir_refuses(self):
        home = self.root / 'home-none'
        home.mkdir()
        out = self._run(home)
        self.assertEqual(out.returncode, 2, out.stdout + out.stderr)
        self.assertIn('--tool', out.stdout + out.stderr)

    def test_second_run_pulls(self):
        home = self.root / 'home-twice'
        home.mkdir()
        out = self._run(home, '--tool', 'codex')
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        out = self._run(home, '--tool', 'codex')
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn('快进', out.stdout)


class TestReadmeInstallSection(unittest.TestCase):
    def setUp(self):
        self.readme = (SKILL / 'README.md').read_text(encoding='utf-8')

    def test_pinned_phrases_survive(self):
        self.assertIn('装到当前这个 agent 里', self.readme)
        self.assertIn('看不到就直说', self.readme)
        self.assertNotIn('想装到哪个工具也可以一起说', self.readme)

    def test_new_routes_are_documented(self):
        for needle in ('marketplace add Joshmax010/project-relay',
                       'project-relay@joshmax010',
                       'install.sh',
                       'skills.sh/Joshmax010/project-relay/project-relay',
                       'pip install git+https://github.com/'
                       'Joshmax010/project-relay.git'):
            self.assertIn(needle, self.readme,
                          'README install section must mention: %s' % needle)
