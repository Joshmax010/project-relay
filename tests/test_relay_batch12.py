"""Batch 12: conversational skill triggers + semantic install (skill v3).

The skill text (RELAY_SKILL_TEXT, installed by `recipe install`) and the
repo root SKILL.md are the same body, except the root file uses
`name: project-relay` (the folder name) while the installed copy uses
`name: relay`. The description is written in the user's own words: it
lists what users actually say when they want a handoff, so the agent
can route a plain chat message to this skill without the user typing
any command.

The trigger evals in tests/skill-trigger-evals.json are a KEYWORD
coverage check only, not an LLM judgement: they verify that the
description contains the trigger words real users say, and that
near-miss phrases do not contain the relay-specific trigger words.
Whether an agent actually routes a free-form prompt to this skill is
model behaviour and cannot be proved by a unit test.
"""

import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SKILL = Path(__file__).resolve().parents[1]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


relay = _load('relay', SKILL / 'scripts/relay.py')
flow = _load('relay_flow', SKILL / 'scripts/relay_flow.py')

# Words the skill description must contain, taken from how users
# actually phrase a handoff. "继续" is generic; the rest are relay
# specific.
DESCRIPTION_TRIGGERS = [
    '接力', '接上', '继续', '交接', '换电脑', '换设备', '另一台电脑',
    '装上接力',
]

# Words that, on their own, mean "invoke relay". Used for the
# near-miss side of the evals: a phrase that should NOT trigger must
# not contain one of these as an action.
EXCLUSIVE_TRIGGERS = ['接力', '交接', '换电脑', '换设备', '装上接力']

# A phrase that merely asks what a trigger word means is a mention,
# not an invocation ("给我讲讲接力是什么意思"). Keyword matching
# cannot tell the two apart, so eval items that only mention a word
# carry a "note" and are checked through that exception.
META_QUESTION_MARKERS = ['是什么意思', '什么是', '讲讲']


def _description(text):
    lines = text.splitlines()
    assert lines[0] == '---', 'skill text must start with frontmatter'
    for line in lines[1:]:
        if line == '---':
            break
        if line.startswith('description:'):
            return line[len('description:'):].strip()
    raise AssertionError('no description in skill frontmatter')


class TestSkillDescriptionTriggers(unittest.TestCase):
    """The description must speak the user's language, not the agent's."""

    def test_description_contains_trigger_words(self):
        desc = _description(flow.RELAY_SKILL_TEXT)
        for word in DESCRIPTION_TRIGGERS:
            self.assertIn(word, desc,
                          'description lacks trigger word: ' + word)

    def test_root_skill_description_matches_installed(self):
        root = (SKILL / 'SKILL.md').read_text(encoding='utf-8')
        self.assertEqual(_description(root),
                         _description(flow.RELAY_SKILL_TEXT),
                         'root and installed descriptions differ')


class TestSkillBodySync(unittest.TestCase):
    """Root SKILL.md and RELAY_SKILL_TEXT are the same text (batch 13:
    the name is unified to project-relay, so there is no longer a
    one-name-apart difference between the two)."""

    def test_root_matches_installed_text(self):
        root = (SKILL / 'SKILL.md').read_text(encoding='utf-8')
        self.assertTrue(root.startswith('---\nname: project-relay\n'),
                        'root SKILL.md frontmatter name changed')
        self.assertEqual(root, flow.RELAY_SKILL_TEXT,
                         'root SKILL.md differs from installed text')

    def test_installed_text_is_conversational_not_command_list(self):
        text = flow.RELAY_SKILL_TEXT
        self.assertIn('用户这么说，你这么做', text,
                      'installed skill lacks the trigger table')
        self.assertIn('装上接力', text,
                      'installed skill lacks the semantic install trigger')


class TestSemanticInstall(unittest.TestCase):
    """`recipe install --tool workbuddy` must install the new text."""

    def test_install_workbuddy_writes_new_skill_text(self):
        with tempfile.TemporaryDirectory(prefix='relay-batch12-') as tmp:
            root = Path(tmp)
            env = {
                'PROJECT_RELAY_HOME': str(root),
                'GIT_AUTHOR_NAME': 'Tester',
                'GIT_AUTHOR_EMAIL': 'test@example.invalid',
                'GIT_COMMITTER_NAME': 'Tester',
                'GIT_COMMITTER_EMAIL': 'test@example.invalid',
            }
            with mock.patch.dict(os.environ, env):
                repo = root / 'proj'
                repo.mkdir()
                subprocess.run(['git', '-C', str(repo), 'init', '-b', 'main'],
                               capture_output=True, check=True)
                subprocess.run(['git', '-C', str(repo), 'config',
                                'user.name', 'Tester'],
                               capture_output=True, check=True)
                subprocess.run(['git', '-C', str(repo), 'config',
                                'user.email', 'test@example.invalid'],
                               capture_output=True, check=True)
                subprocess.run(['git', '-C', str(repo), 'config',
                                'commit.gpgsign', 'false'],
                               capture_output=True, check=True)
                (repo / 'app.py').write_bytes(b'value = 1\n')
                subprocess.run(['git', '-C', str(repo), 'add', '.'],
                               capture_output=True, check=True)
                subprocess.run(['git', '-C', str(repo), 'commit', '-m',
                                'baseline'], capture_output=True, check=True)
                store = root / 'store'
                with io.StringIO() as buf:
                    with mock.patch.object(sys, 'stdout', buf):
                        relay.init(store, repo)
                home = root / 'home'
                out = subprocess.run(
                    [sys.executable, str(SKILL / 'scripts' / 'relay.py'),
                     '--root', str(repo), '--store-root', str(store),
                     'recipe', 'install', '--tool', 'workbuddy',
                     '--target-root', str(home)],
                    capture_output=True, text=True, cwd=str(SKILL))
                self.assertEqual(out.returncode, 0, out.stderr)
                skill = (home / '.workbuddy' / 'skills' / 'project-relay'
                         / 'SKILL.md')
                self.assertTrue(skill.is_file(), 'installed SKILL.md missing')
                installed = skill.read_bytes()
                self.assertEqual(installed,
                                 flow.RELAY_SKILL_TEXT.encode('utf-8'),
                                 'installed skill is not the new text')
                self.assertIn('用户这么说，你这么做'.encode('utf-8'),
                              installed,
                              'installed skill is the old command list')


class TestTriggerEvalsKeywordCoverageOnly(unittest.TestCase):
    """Keyword coverage only, NOT an LLM judgement (see module docstring).

    Should-trigger queries must hit at least one trigger word that is
    actually in the description; should-not-trigger queries must not
    contain a relay-specific trigger word as an action. A query that
    merely asks what a trigger word means ("讲讲接力是什么意思") is a
    mention, not an invocation, and is checked through its "note".
    """

    def test_eval_queries_cover_description_triggers(self):
        evals_path = SKILL / 'tests' / 'skill-trigger-evals.json'
        evals = json.loads(evals_path.read_text(encoding='utf-8'))
        self.assertEqual(len(evals['should_trigger']), 6,
                         'expected 6 should-trigger evals')
        self.assertEqual(len(evals['should_not_trigger']), 4,
                         'expected 4 should-not-trigger evals')
        desc = _description(flow.RELAY_SKILL_TEXT)
        for item in evals['should_trigger']:
            query = item['query']
            hits = [w for w in DESCRIPTION_TRIGGERS
                    if w in desc and w in query]
            self.assertTrue(hits,
                            'should-trigger query hits no description '
                            'trigger word: ' + query)
        for item in evals['should_not_trigger']:
            query = item['query']
            if any(m in query for m in META_QUESTION_MARKERS):
                self.assertIn('note', item,
                              'meta-question eval lacks its note: ' + query)
                continue
            hits = [w for w in EXCLUSIVE_TRIGGERS if w in query]
            self.assertEqual(hits, [],
                             'should-not-trigger query hits exclusive '
                             'trigger words %r: %s' % (hits, query))


if __name__ == '__main__':
    unittest.main()


class TestBatch12FixCurrentAgentInstall(unittest.TestCase):
    """Batch 12 fix: install targets the current agent, no nagging.

    The user corrected three things after reviewing batch 12: (1) the
    "don't make the user type commands" line was repeated all over the
    skill text -- a skill should say it once; (2) the install target is
    always the agent the user is already talking to, never ask which
    tool; (3) semantic install only works when the agent can see the
    project-relay repo, otherwise say so plainly instead of forcing it.
    """

    def test_install_targets_current_agent(self):
        text = flow.RELAY_SKILL_TEXT
        self.assertIn('装到你当前这个工具里', text,
                      'install must target the current agent')
        self.assertIn('别问用户装到哪', text,
                      'skill must not ask which tool to install to')

    def test_install_requires_visible_repo(self):
        text = flow.RELAY_SKILL_TEXT
        self.assertIn('看不到就直说', text,
                      'skill must say plainly when the repo is not visible')

    def test_install_init_prerequisite(self):
        text = flow.RELAY_SKILL_TEXT
        self.assertIn('如果提示项目未建仓，先跑 relay init 再装', text,
                      'fresh clone needs init before install')

    def test_no_repeated_no_commands_nagging(self):
        text = flow.RELAY_SKILL_TEXT
        self.assertNotIn('不要让用户敲', text,
                         'repeated no-commands nagging is still in the text')

    def test_readme_install_targets_current_agent(self):
        readme = (SKILL / 'README.md').read_text(encoding='utf-8')
        self.assertNotIn('想装到哪个工具也可以一起说', readme,
                         'README still asks which tool to install to')
        self.assertIn('装到当前这个 agent 里', readme,
                      'README install must target the current agent')
        self.assertIn('看不到就直说', readme,
                      'README install must state the visible-repo '
                      'prerequisite')
