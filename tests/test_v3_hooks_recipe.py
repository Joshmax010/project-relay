"""v3 hooks + recipe tests (batch 18).

Hooks gate on the cwd: they should only act inside a project whose git
toplevel contains .project-relay/. Recipes install the relay skill
into each tool (Codex: managed AGENTS.md section, Claude Code:
user-level settings.json hooks + CLAUDE.md section, WorkBuddy:
project-level skill directory), plus the WorkBuddy upload zip.

Fixed in this batch (2026-11-13, branch slim-v3): two bugs the
tests below used to assert as actual behaviour --
- B1: relay_flow._hook_active_toplevel read relay.ARCHIVE_DIR_NAME,
  which does not exist (the constant is STORE_DIRNAME), so the hook
  gate raised AttributeError inside any git repo and hook_cwd_allowed
  was always False. The gate now reads STORE_DIRNAME and the hook
  entry points are tested through the hooks themselves.
- B2: flow.install_workbuddy passed a str to relay.atomic, which
  writes bytes only, so every install raised TypeError and wrote
  nothing. The call now encodes to UTF-8 and the install is tested
  end to end. The skipped branch of hook_stop also carries an
  'instruction' key now, matching the live branch.
"""
import json
import shutil
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from v3_helpers import V3Base, relay, flow

FlowRelayError = flow.RelayError

REPO_ROOT = Path(__file__).resolve().parents[1]


class TestHookGateConstant(V3Base):
    """B1 fixed: the gate reads STORE_DIRNAME, the constant relay.py
    actually defines (ARCHIVE_DIR_NAME never existed)."""

    def test_gate_constant_is_store_dirname(self):
        self.assertFalse(hasattr(relay, 'ARCHIVE_DIR_NAME'))
        self.assertEqual('.project-relay', relay.STORE_DIRNAME)

    def test_hook_active_toplevel_returns_project_root(self):
        root = self.init()
        self.assertEqual(str(root), str(flow._hook_active_toplevel(root)))
        plain = self.make_repo('plain')
        self.assertIsNone(flow._hook_active_toplevel(plain))
        self.assertIsNone(flow._hook_active_toplevel(self.temp.name))


class TestHookCwdAllowed(V3Base):
    def test_inside_project_allowed(self):
        root = self.init()
        self.assertTrue(flow.hook_cwd_allowed(root))
        sub = root / 'pkg'
        sub.mkdir()
        self.assertTrue(flow.hook_cwd_allowed(sub))

    def test_uninited_repo_and_outside_repo_denied(self):
        plain = self.make_repo('plain')
        self.assertFalse(flow.hook_cwd_allowed(plain))
        self.assertFalse(flow.hook_cwd_allowed(self.temp.name))
        self.assertFalse(flow.hook_cwd_allowed(self.home))


class TestHookCapture(V3Base):
    def test_capture_in_project_writes_entry(self):
        root = self.init()
        result = flow.hook_capture(root, agent='claude-code', session='s1')
        self.assertEqual(1, result['seq'])
        self.assertFalse(result['deduped'])
        self.assertEqual(1, len(self.entries(root, 'claude-code')))

    def test_other_project_cwd_skips_and_non_repo_skips(self):
        root = self.init()
        plain = self.make_repo('plain')
        skipped = flow.hook_capture(plain, agent='claude-code')
        self.assertEqual('skipped', skipped['action'])
        self.assertTrue(skipped['skipped'])
        self.assertFalse((plain / '.project-relay').exists())
        self.assertEqual([], self.entries(root, 'claude-code'))
        # Outside any repo rev-parse fails first, so the skip path runs.
        outside = flow.hook_capture(self.temp.name, agent='claude-code')
        self.assertEqual('skipped', outside['action'])
        self.assertTrue(outside['skipped'])

    def test_direct_capture_still_works(self):
        # The capture itself is fine; only the hook gate is broken.
        root = self.init()
        first = flow.capture(root, 'claude-code', session='s1')
        self.assertEqual(1, first['seq'])
        self.assertFalse(first['deduped'])
        second = flow.capture(root, 'claude-code', session='s2')
        self.assertEqual(2, second['seq'])
        self.assertEqual(2, len(self.entries(root, 'claude-code')))


class TestHookEnforce(V3Base):
    def test_enforce_in_project_blocks_then_passes(self):
        # Block while the latest entry is still a placeholder.
        root = self.init()
        flow.capture(root, 'claude-code', session='s1')
        blocked = flow.hook_enforce(root, agent='claude-code', session='s1')
        self.assertEqual('block', blocked['action'])
        self.assertEqual(1, blocked['entry_seq'])
        self.assertIn('cap-0001', blocked['instruction'])
        self.fill_semantic(root, 'claude-code')
        self.assertEqual('pass',
                         flow.hook_enforce(root, agent='claude-code')['action'])

    def test_direct_enforce_blocks_then_passes(self):
        # The enforcement itself is fine; only the hook gate is broken.
        root = self.init()
        flow.capture(root, 'claude-code', session='s1')
        blocked = flow.enforce(root, 'claude-code', session='s1')
        self.assertEqual('block', blocked['action'])
        self.assertEqual(1, blocked['entry_seq'])
        self.assertIn('cap-0001', blocked['instruction'])
        self.fill_semantic(root, 'claude-code')
        self.assertEqual('pass', flow.enforce(root, 'claude-code')['action'])

    def test_direct_enforce_no_entries_passes(self):
        root = self.init()
        self.assertEqual('pass', flow.enforce(root, 'claude-code')['action'])

    def test_hook_enforce_outside_repo_passes_skipped(self):
        result = flow.hook_enforce(self.temp.name, agent='claude-code')
        self.assertEqual('pass', result['action'])
        self.assertTrue(result['skipped'])

    def test_hook_enforce_in_plain_repo_skips(self):
        plain = self.make_repo('plain')
        result = flow.hook_enforce(plain, agent='claude-code')
        self.assertEqual('pass', result['action'])
        self.assertTrue(result['skipped'])


class TestHookStop(V3Base):
    def test_stop_in_project_captures_then_blocks(self):
        # Capture-then-enforce in one call, blocking on the placeholder.
        root = self.init()
        result = flow.hook_stop(root, agent='claude-code', session='s1')
        self.assertEqual('block', result['action'])
        self.assertEqual(1, result['capture']['seq'])
        self.assertEqual('block', result['enforce']['action'])
        self.assertIn('cap-0001', result['instruction'])
        self.assertEqual(1, len(self.entries(root, 'claude-code')))

    def test_direct_capture_then_enforce_sequence_blocks(self):
        # What hook_stop is supposed to do once the gate is fixed.
        root = self.init()
        cap = flow.capture(root, 'claude-code', session='s1')
        enf = flow.enforce(root, 'claude-code', session='s1')
        self.assertEqual(1, cap['seq'])
        self.assertEqual('block', enf['action'])

    def test_stop_outside_repo_skipped(self):
        result = flow.hook_stop(self.temp.name, agent='claude-code')
        for key in ('action', 'note', 'instruction', 'capture', 'enforce'):
            self.assertIn(key, result)
        self.assertEqual('pass', result['action'])
        self.assertTrue(result['capture']['skipped'])
        self.assertEqual('pass', result['enforce']['action'])

    def test_stop_in_plain_repo_skips(self):
        plain = self.make_repo('plain')
        result = flow.hook_stop(plain, agent='claude-code')
        self.assertEqual('pass', result['action'])
        self.assertTrue(result['capture']['skipped'])


class TestHookResume(V3Base):
    def test_resume_in_project_returns_five_sections(self):
        root = self.init()
        self.confirm_goal(root)
        result = flow.hook_resume(root)
        self.assertFalse(result.get('no_archive'))
        self.assertEqual(5, len(result['sections']))

    def test_direct_resume_still_works(self):
        # Resume itself is fine; only the hook gate is broken.
        root = self.init()
        self.confirm_goal(root)
        result = flow.resume(root)
        self.assertFalse(result.get('no_archive'))
        self.assertEqual(5, len(result['sections']))

    def test_resume_outside_repo_skipped(self):
        result = flow.hook_resume(self.temp.name)
        self.assertTrue(result['skipped'])
        self.assertEqual([], result['sections'])

    def test_resume_in_plain_repo_skips(self):
        plain = self.make_repo('plain')
        result = flow.hook_resume(plain)
        self.assertTrue(result['skipped'])
        self.assertEqual([], result['sections'])


class TestInstallCodex(V3Base):
    def test_install_writes_managed_section_and_recipe(self):
        root = self.init()
        result = flow.install_codex(root)
        self.assertTrue(result['changed'])
        agents_md = (root / 'AGENTS.md').read_text(encoding='utf-8')
        self.assertIn('<!-- relay:begin relay', agents_md)
        self.assertIn('project-relay 接手规程', agents_md)
        recipe = json.loads(
            (self.adir(root, 'codex') / 'recipe.json').read_text(encoding='utf-8'))
        self.assertEqual('codex', recipe['tool'])

    def test_reinstall_idempotent(self):
        root = self.init()
        flow.install_codex(root)
        result = flow.install_codex(root)
        self.assertFalse(result['changed'])
        agents_md = (root / 'AGENTS.md').read_text(encoding='utf-8')
        self.assertEqual(1, agents_md.count('<!-- relay:begin relay'))

    def test_uninstall_removes_section_and_recipe(self):
        root = self.init()
        flow.install_codex(root)
        flow.uninstall_codex(root)
        self.assertFalse((self.adir(root, 'codex') / 'recipe.json').exists())
        self.assertNotIn('<!-- relay:begin relay',
                         (root / 'AGENTS.md').read_text(encoding='utf-8'))

    def test_uninstall_without_install_raises(self):
        root = self.init()
        with self.assertRaises(FlowRelayError):
            flow.uninstall_codex(root)


class TestInstallClaude(V3Base):
    def _settings(self):
        return json.loads(
            (self.home / '.claude' / 'settings.json').read_text(encoding='utf-8'))

    def test_install_registers_hooks_and_recipe(self):
        root = self.init()
        result = flow.install_claude(root, home_dir=self.home)
        self.assertTrue(result['changed'])
        settings = self._settings()
        start_cmds = [h['hooks'][0]['command']
                      for h in settings['hooks']['SessionStart']]
        self.assertTrue(any('hook resume' in c and str(root) in c
                            for c in start_cmds))
        stop_cmds = [h['hooks'][0]['command']
                     for h in settings['hooks']['Stop']]
        self.assertTrue(any('hook stop' in c and str(root) in c
                            for c in stop_cmds))
        claude_md = (self.home / '.claude' / 'CLAUDE.md').read_text(encoding='utf-8')
        self.assertIn('<!-- relay:begin relay', claude_md)
        recipe = json.loads(
            (self.adir(root, 'claude-code') / 'recipe.json').read_text(encoding='utf-8'))
        self.assertEqual('claude-code', recipe['tool'])
        self.assertEqual('strong', recipe['tier'])

    def test_reinstall_idempotent(self):
        root = self.init()
        flow.install_claude(root, home_dir=self.home)
        result = flow.install_claude(root, home_dir=self.home)
        self.assertFalse(result['changed'])
        settings = self._settings()
        self.assertEqual(1, len(settings['hooks']['SessionStart']))
        self.assertEqual(1, len(settings['hooks']['Stop']))

    def test_uninstall_removes_hooks_and_recipe(self):
        root = self.init()
        flow.install_claude(root, home_dir=self.home)
        flow.uninstall_claude(root, home_dir=self.home)
        self.assertFalse(
            (self.adir(root, 'claude-code') / 'recipe.json').exists())
        settings = self._settings()
        blob = json.dumps(settings, ensure_ascii=False)
        self.assertNotIn(str(root), blob)
        self.assertNotIn('<!-- relay:begin relay',
                         (self.home / '.claude' / 'CLAUDE.md').read_text(encoding='utf-8'))

    def test_uninstall_without_install_raises(self):
        root = self.init()
        with self.assertRaises(FlowRelayError):
            flow.uninstall_claude(root, home_dir=self.home)


class TestInstallWorkbuddy(V3Base):
    def test_install_writes_skill_and_recipe(self):
        # B2 fixed: the skill text is encoded to bytes for atomic().
        root = self.init()
        result = flow.install_workbuddy(root)
        self.assertTrue(result['installed'])
        skill = root / '.workbuddy' / 'skills' / 'project-relay' / 'SKILL.md'
        self.assertTrue(skill.is_file())
        text = skill.read_text(encoding='utf-8')
        self.assertTrue(text.startswith('---\nname: project-relay\nagent_created: true\n'))
        self.assertIn('# Relay', text)
        recipe = json.loads(
            (self.adir(root, 'workbuddy') / 'recipe.json').read_text(encoding='utf-8'))
        self.assertEqual('workbuddy', recipe['tool'])

    def test_reinstall_succeeds_again(self):
        root = self.init()
        flow.install_workbuddy(root)
        result = flow.install_workbuddy(root)
        self.assertTrue(result['installed'])
        skill = root / '.workbuddy' / 'skills' / 'project-relay' / 'SKILL.md'
        self.assertTrue(skill.is_file())

    def test_uninstall_without_install_raises(self):
        root = self.init()
        with self.assertRaises(FlowRelayError):
            flow.uninstall_workbuddy(root)


class TestPackageWorkbuddy(V3Base):
    def _packable_repo(self):
        root = self.make_repo('proj')
        shutil.copytree(REPO_ROOT / 'scripts', root / 'scripts',
                        ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copy(REPO_ROOT / 'SKILL.md', root / 'SKILL.md')
        (root / 'references').mkdir()
        return root

    def test_package_layout_and_skill_frontmatter(self):
        root = self._packable_repo()
        out = Path(self.temp.name) / 'wb.zip'
        result = flow.package_workbuddy(root, out=out)
        self.assertEqual(str(out), result['zip_path'])
        self.assertTrue(out.is_file())
        with zipfile.ZipFile(out) as zf:
            names = zf.namelist()
        tops = {n.split('/')[0] for n in names}
        self.assertEqual({'project-relay'}, tops)
        self.assertIn('project-relay/SKILL.md', names)
        self.assertIn('project-relay/scripts/relay.py', names)
        with zipfile.ZipFile(out) as zf:
            skill = zf.read('project-relay/SKILL.md').decode('utf-8')
        self.assertIn('name: project-relay', skill)
        self.assertIn('description:', skill)
        self.assertIn('agent_created: true', skill)


if __name__ == '__main__':
    import unittest
    unittest.main()
