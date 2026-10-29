"""v2 foundation tests (plan part 02, steps 1.2-1.5).

Style follows the old suite: real temporary directories, real Git repos,
no mocks except where a missing-skill-package case needs a patched constant.
The store root is redirected with PROJECT_RELAY_HOME so no test ever touches
the developer's real ~/.project-relay/.
"""
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
from unittest import mock

SKILL = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('relay', SKILL / 'scripts/relay.py')
relay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(relay)


def tree_bytes(root):
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in root.rglob('*') if p.is_file()}


class V2Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='relay-v2-')
        self.addCleanup(self.temp.cleanup)
        self.store = Path(self.temp.name) / 'store'
        self._env = mock.patch.dict(os.environ, {'PROJECT_RELAY_HOME': str(Path(self.temp.name))})
        self._env.start()
        self.addCleanup(self._env.stop)

    def make_repo(self, name, remote=None):
        root = Path(self.temp.name) / name
        root.mkdir()
        self.git(root, 'init', '-b', 'main')
        self.git(root, 'config', 'user.name', 'Tester')
        self.git(root, 'config', 'user.email', 'test@example.invalid')
        self.git(root, 'config', 'commit.gpgsign', 'false')
        (root / 'app.py').write_text('value = 1\n')
        self.git(root, 'add', '.')
        self.git(root, 'commit', '-m', 'baseline')
        if remote:
            self.git(root, 'remote', 'add', 'origin', remote)
        return root

    def git(self, root, *args):
        r = subprocess.run(['git', '-C', str(root), *args], capture_output=True, check=True)
        return r.stdout

    def call(self, fn, *args, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return fn(*args, **kwargs)


class TestStoreLayout(V2Base):
    def test_init_builds_spec_layout(self):
        root = self.make_repo('demo', remote='https://github.com/example/demo.git')
        result = self.call(relay.init, self.store, root)
        store_dir = result['store_dir']
        for zone in relay.ZONES:
            self.assertTrue((store_dir / zone).is_dir(), zone)
        for name in relay.ARCHIVE_FILES:
            self.assertTrue((store_dir / 'archive' / name).is_file(), name)
        self.assertTrue((self.store / 'registry.json').is_file())
        for top in ('projects', 'global', 'trash'):
            self.assertTrue((self.store / top).is_dir(), top)
        # No relay file inside the project repo itself (grill decision).
        self.assertFalse((root / '.project-relay').exists())
        self.assertFalse((root / 'relay.json').exists())
        # Store dir name: 16-hex key + readable slug.
        self.assertRegex(store_dir.name, r'^[0-9a-f]{16}-demo$')

    def test_substrate_reused(self):
        # atomic(): mode preservation + no partial file (old substrate, verbatim).
        target = Path(self.temp.name) / 'doc.md'
        relay.atomic(target, b'v1\n')
        target.chmod(0o640)
        relay.atomic(target, b'v2\n')
        self.assertEqual(b'v2\n', target.read_bytes())
        self.assertEqual(0o640, target.stat().st_mode & 0o777)
        # save(): canonical JSON bytes (sort_keys, trailing newline).
        data = {'b': 1, 'a': [2]}
        relay.save(Path(self.temp.name) / 'x.json', data)
        self.assertEqual(b'{\n  "a": [\n    2\n  ],\n  "b": 1\n}\n',
                         (Path(self.temp.name) / 'x.json').read_bytes())
        # safe(): six illegal paths refused with readable Chinese errors.
        for bad in ('../outside.md', '/tmp/outside.md', 'C:/outside.md',
                    'a\\b.md', '', 'x/../y.md'):
            with self.subTest(bad=bad), self.assertRaises(relay.RelayError):
                relay.safe(Path(self.temp.name), bad)
        if os.name != 'nt':
            outside = Path(self.temp.name) / 'outside.md'
            outside.write_text('orig')
            (Path(self.temp.name) / 'linked.md').symlink_to(outside)
            with self.assertRaises(relay.RelayError):
                relay.safe(Path(self.temp.name), 'linked.md')

    def test_init_preflight_zero_write(self):
        # Non-repo directory.
        plain = Path(self.temp.name) / 'plain'
        plain.mkdir()
        with self.assertRaises(relay.RelayError):
            self.call(relay.init, self.store, plain)
        self.assertFalse(self.store.exists())
        # Missing skill package (patched constant; real templates untouched).
        root = self.make_repo('pkg', remote='https://github.com/example/pkg.git')
        real_templates = relay.TEMPLATES_DIR
        try:
            relay.TEMPLATES_DIR = Path(self.temp.name) / 'no-such-templates'
            with self.assertRaises(relay.RelayError):
                self.call(relay.init, self.store, root)
        finally:
            relay.TEMPLATES_DIR = real_templates
        self.assertFalse(self.store.exists())

    def test_init_idempotent_byte_identical(self):
        root = self.make_repo('idem', remote='https://github.com/example/idem.git')
        self.call(relay.init, self.store, root)
        before = tree_bytes(self.store)
        self.call(relay.init, self.store, root)
        self.assertEqual(before, tree_bytes(self.store))

    def test_init_from_subdirectory_resolves_toplevel(self):
        root = self.make_repo('sub', remote='https://github.com/example/sub.git')
        (root / 'src').mkdir()
        result = self.call(relay.init, self.store, root / 'src')
        self.assertEqual(root.name, result['row']['display_name'])

    def test_nested_project_rejected(self):
        outer = self.make_repo('outer', remote='https://github.com/example/outer.git')
        self.call(relay.init, self.store, outer)
        inner = outer / 'inner'
        inner.mkdir()
        self.git(inner, 'init', '-b', 'main')
        self.git(inner, 'config', 'user.name', 'Tester')
        self.git(inner, 'config', 'user.email', 'test@example.invalid')
        (inner / 'x.py').write_text('x = 1\n')
        self.git(inner, 'add', '.')
        self.git(inner, 'commit', '-m', 'inner baseline')
        with self.assertRaises(relay.RelayError):
            self.call(relay.init, self.store, inner)


class TestIdentity(V2Base):
    def test_remote_normalization(self):
        variants = ['https://github.com/Example/Demo.git',
                    'https://github.com/example/demo',
                    'git@github.com:example/demo.git',
                    'ssh://git@github.com/example/demo.git',
                    'HTTPS://GITHUB.COM/EXAMPLE/DEMO/']
        keys = {relay.remote_key(v) for v in variants}
        self.assertEqual(1, len(keys))
        self.assertRegex(keys.pop(), r'^[0-9a-f]{16}$')
        self.assertEqual('github.com/example/demo', relay.normalize_remote(variants[0]))
        self.assertIsNone(relay.normalize_remote('/tmp/local-path'))

    def test_where_two_clones_same_key_readonly(self):
        origin = self.make_repo('origin', remote='https://github.com/example/shared.git')
        self.call(relay.init, self.store, origin)
        first = relay.where(self.store, path=origin)['row']
        clone = Path(self.temp.name) / 'clone'
        subprocess.run(['git', 'clone', '-q', str(origin), str(clone)], check=True)
        # A local-path clone records the local path as origin; point it back
        # at the shared remote so the clone really is "the same project".
        self.git(clone, 'remote', 'set-url', 'origin', 'https://github.com/example/shared.git')
        before = tree_bytes(self.store)
        found = relay.where(self.store, path=clone)
        self.assertTrue(found['found'])
        self.assertEqual(first['key'], found['row']['key'])
        self.assertNotIn(str(clone), found['row']['paths'])
        # where is read-only: registry untouched.
        self.assertEqual(before, tree_bytes(self.store))
        # init on the clone self-heals: path joins the set, store dir unchanged.
        result = self.call(relay.init, self.store, clone)
        self.assertEqual(first['key'], result['key'])
        expected_dir = self.store / 'projects' / (first['key'] + '-' + first['slug'])
        self.assertEqual(expected_dir, result['store_dir'])
        row = relay.where(self.store, path=clone)['row']
        self.assertIn(str(clone), row['paths'])
        self.assertIn(str(origin), row['paths'])

    def test_move_self_heals(self):
        root = self.make_repo('movable', remote='https://github.com/example/movable.git')
        first = self.call(relay.init, self.store, root)
        moved = Path(self.temp.name) / 'moved-here'
        root.rename(moved)
        result = self.call(relay.init, self.store, moved)
        self.assertEqual(first['store_dir'], result['store_dir'])
        self.assertIn(str(moved), result['row']['paths'])

    def test_local_key_migrates_to_remote_key(self):
        root = self.make_repo('localonly')
        first = self.call(relay.init, self.store, root)
        self.assertEqual('local', first['row']['key_type'])
        old_dir = first['store_dir']
        self.git(root, 'remote', 'add', 'origin', 'https://github.com/example/migrated.git')
        result = self.call(relay.init, self.store, root)
        self.assertEqual('remote', result['row']['key_type'])
        self.assertNotEqual(first['key'], result['key'])
        self.assertFalse(old_dir.exists())
        self.assertTrue(result['store_dir'].exists())
        registry = relay.load_registry(self.store)
        active = [r for r in registry['projects'] if r.get('status', 'active') == 'active']
        self.assertEqual(1, len(active))
        tombstones = [r for r in registry['projects'] if r.get('status') == 'migrated']
        self.assertEqual(1, len(tombstones))
        self.assertEqual(result['key'], tombstones[0]['migrated_to'])

    def test_where_by_key_and_unknown(self):
        root = self.make_repo('keyed', remote='https://github.com/example/keyed.git')
        result = self.call(relay.init, self.store, root)
        found = relay.where(self.store, key=result['key'])
        self.assertTrue(found['found'])
        missing = relay.where(self.store, key='0' * 16)
        self.assertFalse(missing['found'])


class TestLegacyPreview(V2Base):
    def test_preview_changes_nothing(self):
        root = self.make_repo('legacy', remote='https://github.com/example/legacy.git')
        subprocess.run([sys.executable, str(SKILL / 'scripts/init_project.py'), str(root)],
                       check=True, capture_output=True)
        before_agent = tree_bytes(root / '.agent')
        result = self.call(relay.init, self.store, root)
        self.assertTrue(any('迁移预览' in line for line in result['preview']))
        self.assertEqual(before_agent, tree_bytes(root / '.agent'))
        store_dir = relay.where(self.store, path=root)['row']
        store_path = self.store / 'projects' / (store_dir['key'] + '-' + store_dir['slug'])
        archived = tree_bytes(store_path / 'archive')
        # Only skeletons: no .agent content migrated.
        self.assertEqual(set(relay.ARCHIVE_FILES), {Path(p).name for p in archived})


class TestGoals(V2Base):
    DRAFT = ('# 项目目标\n\n状态：待确认\n\n- 目标与交付物：做一个 MVP（来源：用户原话）\n'
             '- 范围及不做事项：不做登录（来源：对话推断）\n'
             '- 约束与偏好（附来源）：Python 标准库 only（来源：档案既有）\n'
             '- 验收标准：测试全过\n- 最近核对时间：未核对\n')

    def _init(self, name='goalproj'):
        root = self.make_repo(name, remote=f'https://github.com/example/{name}.git')
        self.call(relay.init, self.store, root)
        return root

    def test_full_chain_confirm_and_receipt(self):
        root = self._init()
        relay.goal_draft(self.store, root, self.DRAFT)
        diff = relay.goal_show(self.store, root)
        self.assertIn('+', diff)
        self.assertIn('做一个 MVP', diff)
        info = relay.goal_confirm(self.store, root, channel='test-session')
        self.assertFalse(info.get('already'))
        receipt = info['receipt']
        self.assertEqual(1, receipt['seq'])
        self.assertIsNone(receipt['prev_receipt_hash'])
        store_dir = relay.where(self.store, path=root)['row']
        store_path = self.store / 'projects' / (store_dir['key'] + '-' + store_dir['slug'])
        goal_bytes = (store_path / 'archive' / 'goal.md').read_bytes()
        self.assertEqual(receipt['goal_hash'], relay.digest(goal_bytes))
        self.assertEqual('confirmed', relay.goal_status(self.store, root)[0])
        injected = relay.goal_inject(self.store, root)
        self.assertIn('做一个 MVP', injected)
        self.assertNotIn('暂定', injected)
        state, _ = relay.goal_check(self.store, root)
        self.assertEqual('confirmed', state)

    def test_tamper_blocks_inject_and_shows_stale(self):
        root = self._init()
        relay.goal_draft(self.store, root, self.DRAFT)
        relay.goal_confirm(self.store, root)
        store_dir = relay.where(self.store, path=root)['row']
        store_path = self.store / 'projects' / (store_dir['key'] + '-' + store_dir['slug'])
        goal = store_path / 'archive' / 'goal.md'
        goal.write_text(goal.read_text(encoding='utf-8') + '被篡改的一行\n', encoding='utf-8')
        self.assertEqual('tampered', relay.goal_status(self.store, root)[0])
        with self.assertRaises(relay.RelayError):
            relay.goal_check(self.store, root)
        injected = relay.goal_inject(self.store, root)
        self.assertIn('目标待确认', injected)
        self.assertIn('可能已过期', injected)
        self.assertIn('做一个 MVP', injected)  # stale confirmed copy
        self.assertNotIn('被篡改的一行', injected)

    def test_tentative_watermark(self):
        root = self._init()
        relay.goal_draft(self.store, root, self.DRAFT)
        info = relay.goal_confirm(self.store, root, tentative=True)
        self.assertTrue(info.get('tentative'))
        self.assertEqual('tentative', relay.goal_status(self.store, root)[0])
        injected = relay.goal_inject(self.store, root)
        self.assertTrue(injected.startswith(relay.TENTATIVE_WATERMARK))
        store_dir = relay.where(self.store, path=root)['row']
        store_path = self.store / 'projects' / (store_dir['key'] + '-' + store_dir['slug'])
        self.assertFalse(list((store_path / 'receipts').glob('goal-*.json')))

    def test_decline_then_no_nag_and_rebuild(self):
        root = self._init()
        relay.goal_decline(self.store, root)
        self.assertEqual('none', relay.goal_status(self.store, root)[0])
        for _ in range(3):
            self.assertIn('无目标档案', relay.goal_inject(self.store, root))
        # User brings it up again: draft + confirm works after decline.
        relay.goal_draft(self.store, root, self.DRAFT)
        relay.goal_confirm(self.store, root)
        self.assertEqual('confirmed', relay.goal_status(self.store, root)[0])

    def test_decline_after_confirm_retires_receipt(self):
        # Codex PR #5 finding 2: declining after a confirmed goal must
        # return the project to 'none', not to 'tampered' with the old
        # confirmed goal still surfacing on inject.
        root = self._init()
        relay.goal_draft(self.store, root, self.DRAFT)
        relay.goal_confirm(self.store, root)
        self.assertEqual('confirmed', relay.goal_status(self.store, root)[0])
        relay.goal_decline(self.store, root)
        self.assertEqual('none', relay.goal_status(self.store, root)[0])
        self.assertIn('无目标档案', relay.goal_inject(self.store, root))
        state, _info = relay.goal_check(self.store, root)
        self.assertEqual('none', state)
        store_dir = relay.where(self.store, path=root)['row']
        store_path = self.store / 'projects' / (store_dir['key'] + '-' + store_dir['slug'])
        self.assertFalse(list((store_path / 'receipts').glob('goal-*.json')))
        self.assertTrue((store_path / 'archive' / 'goal-history' / '0001-declined.md').exists())
        # Rebuild still works after a decline that retired a receipt.
        relay.goal_draft(self.store, root, self.DRAFT)
        relay.goal_confirm(self.store, root)
        self.assertEqual('confirmed', relay.goal_status(self.store, root)[0])

    def test_decline_after_tentative_clears_tentative(self):
        root = self._init()
        relay.goal_draft(self.store, root, self.DRAFT)
        relay.goal_confirm(self.store, root, tentative=True)
        self.assertEqual('tentative', relay.goal_status(self.store, root)[0])
        relay.goal_decline(self.store, root)
        self.assertEqual('none', relay.goal_status(self.store, root)[0])
        self.assertIn('无目标档案', relay.goal_inject(self.store, root))

    def test_history_and_receipt_chain(self):
        root = self._init()
        relay.goal_draft(self.store, root, self.DRAFT)
        first = relay.goal_confirm(self.store, root)['receipt']
        second_draft = self.DRAFT.replace('做一个 MVP', '做一个 MVP v2')
        relay.goal_draft(self.store, root, second_draft)
        second = relay.goal_confirm(self.store, root)['receipt']
        self.assertEqual(2, second['seq'])
        store_dir = relay.where(self.store, path=root)['row']
        store_path = self.store / 'projects' / (store_dir['key'] + '-' + store_dir['slug'])
        first_receipt_bytes = (store_path / 'receipts' / 'goal-0001.json').read_bytes()
        self.assertEqual(relay.digest(first_receipt_bytes), second['prev_receipt_hash'])
        self.assertTrue((store_path / 'archive' / 'goal-history' / '0002.md').exists())
        history_text = (store_path / 'archive' / 'goal-history' / '0002.md').read_text()
        self.assertIn('做一个 MVP（来源：用户原话）', history_text)
        self.assertNotIn('MVP v2', history_text)

    def test_confirm_idempotent_when_unchanged(self):
        root = self._init()
        relay.goal_draft(self.store, root, self.DRAFT)
        relay.goal_confirm(self.store, root)
        relay.goal_draft(self.store, root, self.DRAFT)
        info = relay.goal_confirm(self.store, root)
        self.assertTrue(info.get('already'))

    def test_goal_commands_require_store(self):
        root = self.make_repo('nostore', remote='https://github.com/example/nostore.git')
        with self.assertRaises(relay.RelayError):
            relay.goal_draft(self.store, root, self.DRAFT)
        with self.assertRaises(relay.RelayError):
            relay.goal_status(self.store, root)


class TestCLI(V2Base):
    def test_cli_init_where_goal_roundtrip(self):
        root = self.make_repo('cli', remote='https://github.com/example/cli.git')
        runtime = str(SKILL / 'scripts/relay.py')
        env = dict(os.environ, PROJECT_RELAY_HOME=str(Path(self.temp.name)))
        r = subprocess.run([sys.executable, runtime, '--root', str(root),
                            '--store-root', str(self.store), 'init'],
                           capture_output=True, text=True, env=env)
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertIn('记忆仓：', r.stdout)
        self.assertIn('项目键：', r.stdout)
        draft = Path(self.temp.name) / 'draft.md'
        draft.write_text(TestGoals.DRAFT, encoding='utf-8')
        r = subprocess.run([sys.executable, runtime, '--root', str(root),
                            '--store-root', str(self.store),
                            'goal', 'draft', '--file', str(draft)],
                           capture_output=True, text=True, env=env)
        self.assertEqual(0, r.returncode, r.stderr)
        r = subprocess.run([sys.executable, runtime, '--root', str(root),
                            '--store-root', str(self.store), 'goal', 'confirm'],
                           capture_output=True, text=True, env=env)
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertIn('已确认', r.stdout)
        r = subprocess.run([sys.executable, runtime, '--root', str(root),
                            '--store-root', str(self.store), 'where'],
                           capture_output=True, text=True, env=env)
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertIn('项目键：', r.stdout)
        r = subprocess.run([sys.executable, runtime, '--root', str(root),
                            '--store-root', str(self.store), 'goal', 'check'],
                           capture_output=True, text=True, env=env)
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertIn('PASS', r.stdout)


if __name__ == '__main__':
    unittest.main()
