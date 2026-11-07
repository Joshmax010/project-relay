"""Batch 16: DSH (DeepSeek Harness) install via the kimi-code-cli borrow,
--skill pinning on every skills CLI command, and the non-TTY prompt warning.

Fail-before: README has no DSH section (no 'kimi-code-cli', no
'~/.agents/skills', no '不在注册表'); the six skills CLI commands lack
'--skill project-relay'; nothing warns that a non-interactive shell pops
the install confirmation and cancels. SKILL.md has no DSH line either.
"""
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]


class TestBatch16Readme(unittest.TestCase):
    def setUp(self):
        self.readme = (SKILL / 'README.md').read_text(encoding='utf-8')
        self.skill = (SKILL / 'SKILL.md').read_text(encoding='utf-8')

    def test_dsh_section(self):
        self.assertIn('DeepSeek Harness', self.readme)
        self.assertIn('--agent kimi-code-cli', self.readme)
        self.assertIn('~/.agents/skills', self.readme)
        self.assertIn('不在 skills CLI 的 agent 注册表里', self.readme)
        self.assertIn('DSH 的技能列表里就能看到 project-relay', self.readme)

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

    def test_multi_agent_command(self):
        self.assertIn('--agent codex cursor antigravity', self.readme)

    def test_skill_text_has_dsh_line(self):
        self.assertIn('DeepSeek Harness', self.skill)
        self.assertIn('--agent kimi-code-cli', self.skill)
        self.assertIn('--skill project-relay', self.skill)

    def test_pinned_phrases_survive(self):
        self.assertIn('装到当前这个 agent 里', self.readme)
        self.assertIn('看不到就直说', self.readme)
        self.assertNotIn('想装到哪个工具也可以一起说', self.readme)


if __name__ == '__main__':
    unittest.main()
