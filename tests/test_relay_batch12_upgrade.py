"""Batch 12 fix (part 2): recipe install/uninstall upgrade path.

The Codex PR #5 rounds 7-12 item-10 guard refuses to overwrite an
existing SKILL.md whose content differs from the current
RELAY_SKILL_TEXT, because it cannot tell "a file the user wrote
themselves" from "a file relay itself installed under an older skill
text". The first symptom: after the batch-12 skill-text rewrite, a
reinstall was refused with "已有用户自写 SKILL.md，拒绝覆盖" even
though the file on disk was relay's own previous version.

The fix records the SHA256 of the installed skill text in the recipe
state at install time. An existing file is relay's own when its bytes
equal the current text (idempotent reinstall) or its SHA256 equals the
recorded hash (upgrade from an older relay-installed version).
Anything else is still refused with the original message. A pre-fix
install has no recorded hash, so it keeps the old behaviour: a
differing file is refused, never guessed.

Fail-before note: tests 1, 2 and 4 fail on the pre-fix code (no hash
is recorded, so the upgrade and the uninstall-after-upgrade are
refused). Tests 3 and 5 are regression guards: they pass on the
pre-fix code too, and must keep passing after the fix.
"""
import contextlib
import hashlib
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
# NOTE: each importlib load creates its own relayctl copy, so flow's
# RelayError is a different class object from this module's; flow-raised
# errors must be asserted against flow.relay.RelayError.
FlowRelayError = flow.relay.RelayError

# A stand-in for "the skill text relay installed last time": short,
# valid-looking, and deliberately different from RELAY_SKILL_TEXT.
OLD_SKILL_TEXT = '---\nname: relay\n---\n\nOld relay skill body (pre batch 12).\n'


def _sha256_hex(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


class UpgradeBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='relay-upgrade-')
        self.addCleanup(self.temp.cleanup)
        self.store = Path(self.temp.name) / 'store'
        self._env = mock.patch.dict(os.environ, {'PROJECT_RELAY_HOME': str(Path(self.temp.name))})
        self._env.start()
        self.addCleanup(self._env.stop)

    def make_repo(self, name):
        root = Path(self.temp.name) / name
        root.mkdir()
        self.git(root, 'init', '-b', 'main')
        self.git(root, 'config', 'user.name', 'Tester')
        self.git(root, 'config', 'user.email', 'test@example.invalid')
        self.git(root, 'config', 'commit.gpgsign', 'false')
        (root / 'app.py').write_bytes('value = 1\n'.encode('utf-8'))
        self.git(root, 'add', '.')
        self.git(root, 'commit', '-m', 'baseline')
        return root

    def git(self, root, *args, check=True):
        return subprocess.run(['git', '-C', str(root), *args],
                              capture_output=True, check=check)

    def init(self, name='proj'):
        root = self.make_repo(name)
        with contextlib.redirect_stdout(io.StringIO()):
            relay.init(self.store, root)
        return root

    def key(self, root):
        return relay.where(self.store, path=root)['row']['key']

    def store_dir(self, root):
        row = relay.where(self.store, path=root)['row']
        return self.store / 'projects' / (row['key'] + '-' + row['slug'])

    def recipe_state(self, root, flavor='codebuddy'):
        return flow._load_recipe_state(self.store_dir(root), flavor, self.key(root))

    def state_path(self, root, flavor='codebuddy'):
        return flow._recipe_state_path(self.store_dir(root), flavor, self.key(root))

    def target(self):
        return Path(self.temp.name) / 'targethome'


class TestRecipeUpgrade(UpgradeBase):
    def test_fresh_install_records_skill_hash(self):
        # Fail-before: pre-fix install writes no skill_sha256, so the
        # state assertion below fails even though the install itself
        # succeeds.
        root = self.init()
        info = flow.install_codebuddy(self.store, root, self.target(), flavor='codebuddy')
        skill = Path(info['skill_dir']) / 'SKILL.md'
        self.assertEqual(flow.RELAY_SKILL_TEXT.encode('utf-8'), skill.read_bytes())
        state = self.recipe_state(root)
        self.assertIsNotNone(state)
        self.assertEqual(_sha256_hex(flow.RELAY_SKILL_TEXT), state.get('skill_sha256'))

    def test_relay_installed_old_version_upgrades(self):
        # Fail-before: pre-fix install refuses the second install
        # ("已有用户自写 SKILL.md") because the old file differs from
        # the current text and there is no recorded hash to recognise
        # it as relay's own.
        root = self.init()
        with mock.patch.object(flow, 'RELAY_SKILL_TEXT', OLD_SKILL_TEXT):
            info = flow.install_codebuddy(self.store, root, self.target(), flavor='codebuddy')
        skill = Path(info['skill_dir']) / 'SKILL.md'
        self.assertEqual(OLD_SKILL_TEXT.encode('utf-8'), skill.read_bytes())
        # Skill text changed (batch 12 rewrite): reinstall upgrades.
        info = flow.install_codebuddy(self.store, root, self.target(), flavor='codebuddy')
        self.assertEqual(flow.RELAY_SKILL_TEXT.encode('utf-8'), skill.read_bytes())
        state = self.recipe_state(root)
        self.assertEqual(_sha256_hex(flow.RELAY_SKILL_TEXT), state.get('skill_sha256'))

    def test_user_authored_file_still_refused(self):
        # Regression guard (passes before and after the fix): a file
        # the user wrote themselves -- content equal to neither the
        # current text nor any recorded hash -- must still be refused,
        # byte-for-byte untouched.
        root = self.init()
        skill_dir = self.target() / '.codebuddy' / 'skills' / 'project-relay'
        skill_dir.mkdir(parents=True, exist_ok=True)
        user_text = '---\nname: relay\n---\n\nMy own skill notes, do not touch.\n'
        (skill_dir / 'SKILL.md').write_bytes(user_text.encode('utf-8'))
        with self.assertRaises(FlowRelayError) as ctx:
            flow.install_codebuddy(self.store, root, self.target(), flavor='codebuddy')
        self.assertIn('拒绝覆盖', str(ctx.exception))
        self.assertEqual(user_text.encode('utf-8'), (skill_dir / 'SKILL.md').read_bytes())

    def test_uninstall_after_upgrade_succeeds(self):
        # Fail-before: pre-fix uninstall compares the on-disk file
        # (old relay version) against the current text, calls it an
        # external change and refuses, so relay can neither upgrade
        # nor uninstall after a skill-text change.
        root = self.init()
        with mock.patch.object(flow, 'RELAY_SKILL_TEXT', OLD_SKILL_TEXT):
            info = flow.install_codebuddy(self.store, root, self.target(), flavor='codebuddy')
        # Relay itself shipped a new skill text; the installed file is
        # still the old relay version. Uninstall must recognise it as
        # relay's own via the recorded hash.
        flow.uninstall_codebuddy(self.store, root, flavor='codebuddy')
        self.assertFalse(Path(info['skill_dir']).exists())
        self.assertFalse(self.state_path(root).exists())

    def test_legacy_state_without_hash_still_refuses(self):
        # Regression guard (passes before and after the fix): a
        # pre-fix install recorded no hash. A differing file must
        # still be refused -- the fix never guesses.
        root = self.init()
        with mock.patch.object(flow, 'RELAY_SKILL_TEXT', OLD_SKILL_TEXT):
            flow.install_codebuddy(self.store, root, self.target(), flavor='codebuddy')
        state = self.recipe_state(root)
        state.pop('skill_sha256', None)
        flow.save(self.state_path(root), state)
        with self.assertRaises(FlowRelayError) as ctx:
            flow.install_codebuddy(self.store, root, self.target(), flavor='codebuddy')
        self.assertIn('拒绝覆盖', str(ctx.exception))


if __name__ == '__main__':
    unittest.main()
