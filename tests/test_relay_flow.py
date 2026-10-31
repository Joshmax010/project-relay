"""v2 flow tests (plan part 02, steps 1.6-1.14).

Style follows the old suite and the foundation tests: real temporary
directories, real Git repos, no mocks except a forced capture failure.
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


def tree_bytes(root):
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in root.rglob('*') if p.is_file()}


class FlowBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='relay-flow-')
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
        (root / 'app.py').write_bytes('value = 1\n'.encode('utf-8'))
        self.git(root, 'add', '.')
        self.git(root, 'commit', '-m', 'baseline')
        if remote:
            self.git(root, 'remote', 'add', 'origin', remote)
        return root

    def git(self, root, *args, check=True):
        return subprocess.run(['git', '-C', str(root), *args],
                              capture_output=True, check=check)

    def call(self, fn, *args, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return fn(*args, **kwargs)

    def init(self, name='proj', remote=None):
        root = self.make_repo(name, remote=remote or f'https://github.com/example/{name}.git')
        self.call(relay.init, self.store, root)
        return root

    def store_dir(self, root):
        row = relay.where(self.store, path=root)['row']
        return self.store / 'projects' / (row['key'] + '-' + row['slug'])

    def edit(self, root, rel, text):
        (root / rel).write_bytes(text.encode('utf-8'))


class TestCapture(FlowBase):
    def test_entry_matches_git_facts_and_placeholder(self):
        root = self.init()
        self.edit(root, 'app.py', 'value = 2\n')
        info = flow.capture(self.store, root, tool='claude-code', session='s1')
        self.assertEqual(1, info['seq'])
        path = Path(info['path'])
        header, body = flow._parse_entry(path)
        self.assertEqual(1, header['version'])
        self.assertEqual('pending', header['queue'])
        # Branch keeps the old engine's full-ref form (relayctl.anchor uses
        # symbolic-ref without --short); equivalence with the old engine is
        # what matters here, and resume compares snapshot_v2 values only.
        self.assertEqual('refs/heads/main', header['anchor']['branch'])
        self.assertIn('app.py', body)
        self.assertIn(flow.SEMANTIC_PLACEHOLDER, body)
        self.assertIn('做了什么', body)
        # Zero conversation reads: the body holds only git facts + placeholder.
        self.assertNotIn('value = 2', body)

    def test_triple_capture_dedupes(self):
        root = self.init()
        first = flow.capture(self.store, root, session='s1')
        second = flow.capture(self.store, root, session='s1')
        third = flow.capture(self.store, root, session='s1')
        self.assertEqual(first['seq'], second['seq'])
        self.assertTrue(second['deduped'])
        self.assertEqual(1, len(flow.list_entries(self.store_dir(root))))

    def test_degraded_entry_on_failure(self):
        root = self.init()
        with mock.patch.object(flow, 'snapshot_v2', side_effect=RuntimeError('boom')):
            with self.assertRaises(FlowRelayError):
                flow.capture(self.store, root)
        entries = flow.list_entries(self.store_dir(root))
        self.assertEqual(1, len(entries))
        self.assertTrue(entries[0]['header']['degraded'])
        self.assertIn('boom', entries[0]['header']['error'])


class TestQueue(FlowBase):
    SECRETS = ('AKIAIOSFODNN7EXAMPLE ghp_abcdefghij1234567890 '
               'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signature '
               'postgres://user:s3cret@db.internal:5432/app '
               '13800138000 11010119900307888X')

    def test_redaction_six_samples(self):
        root = self.init()
        info = flow.capture(self.store, root, message=self.SECRETS)
        header, _body = flow._parse_entry(Path(info['path']))
        self.assertEqual(6, header['redacted_count'])
        self.assertNotIn('AKIAIOSFODNN7EXAMPLE', header['user_message'])
        self.assertNotIn('13800138000', header['user_message'])
        self.assertNotIn('s3cret', header['user_message'])

    def test_cap_overflow_notices_but_never_drops(self):
        root = self.init()
        sd = self.store_dir(root)
        flow.save_config(sd, {**flow.load_config(sd), 'pending_cap': 1})
        flow.capture(self.store, root, session='a')
        self.edit(root, 'app.py', 'value = 2\n')
        flow.capture(self.store, root, session='b')
        self.assertTrue((sd / 'state' / 'backlog-notice.json').exists())
        status = flow.queue_status(sd, repair=False)
        self.assertEqual(2, status['pending'])

    def test_stale_summarizing_rolls_back(self):
        root = self.init()
        flow.capture(self.store, root)
        sd = self.store_dir(root)
        flow._set_queue(sd, 1, 'summarizing', {'claimed_at': '2000-01-01T00:00:00+00:00'})
        status = flow.queue_status(sd, repair=True)
        self.assertEqual([1], status['rolled_back'])
        self.assertEqual(1, flow.list_entries(sd)[0]['header']['retries'])
        self.assertEqual('pending', flow.list_entries(sd)[0]['header']['queue'])

    def test_relay_prefix_source_file_not_exempt(self):
        root = self.init()
        (root / '.relay-temp.py').write_bytes('x = 1\n'.encode('utf-8'))
        snap = flow.snapshot_v2(root)
        self.assertIn('.relay-temp.py', snap['code'])


class TestRecipes(FlowBase):
    def test_claude_install_idempotent_and_preserves(self):
        root = self.init()
        home = Path(self.temp.name) / 'fakehome'
        settings = home / '.claude' / 'settings.json'
        settings.parent.mkdir(parents=True)
        settings.write_bytes(json.dumps({'other': 'keep-me'}, indent=2).encode('utf-8'))
        flow.install_claude(self.store, root, home_dir=home)
        after_first_settings = settings.read_bytes()
        after_first_md = (home / '.claude' / 'CLAUDE.md').read_bytes()
        flow.install_claude(self.store, root, home_dir=home)
        self.assertEqual(after_first_settings, settings.read_bytes())
        self.assertEqual(after_first_md, (home / '.claude' / 'CLAUDE.md').read_bytes())
        self.assertIn('keep-me', settings.read_text(encoding='utf-8'))
        # Repair: tamper one char inside the managed section, reinstall.
        md = home / '.claude' / 'CLAUDE.md'
        text = md.read_text(encoding='utf-8').replace('先跑 relay resume', '先跑 relay RESUME')
        md.write_bytes(text.encode('utf-8'))
        flow.install_claude(self.store, root, home_dir=home)
        self.assertIn('先跑 relay resume', md.read_text(encoding='utf-8'))
        # Uninstall restores.
        flow.uninstall_claude(self.store, root, home_dir=home)
        self.assertNotIn('relay resume', settings.read_text(encoding='utf-8'))
        self.assertNotIn('relay:begin', md.read_text(encoding='utf-8'))

    def test_claude_uninstall_refuses_foreign_edit(self):
        root = self.init()
        home = Path(self.temp.name) / 'fakehome2'
        flow.install_claude(self.store, root, home_dir=home)
        md = home / '.claude' / 'CLAUDE.md'
        text = md.read_text(encoding='utf-8')
        # Change body without updating the recorded hash.
        md.write_bytes(text.replace('relay capture', 'relay CAPTURE').encode('utf-8'))
        with self.assertRaises(FlowRelayError):
            flow.uninstall_claude(self.store, root, home_dir=home)

    def test_recipe_state_keyed_by_project(self):
        # Codex PR #5 finding 3: two projects sharing one home must not
        # overwrite each other's recipe state, and uninstalling one must
        # leave the other's hooks installed.
        home = Path(self.temp.name) / 'fakehome3'
        root_a = self.init('proj-a')
        root_b = self.init('proj-b')
        flow.install_claude(self.store, root_a, home_dir=home)
        flow.install_claude(self.store, root_b, home_dir=home)
        settings_path = home / '.claude' / 'settings.json'
        text = settings_path.read_text(encoding='utf-8')
        self.assertIn('proj-a', text)
        self.assertIn('proj-b', text)
        key_a = relay.where(self.store, path=root_a)['row']['key']
        key_b = relay.where(self.store, path=root_b)['row']['key']
        state_dir = self.store_dir(root_a) / '..' / 'global' / 'recipes'
        self.assertTrue((state_dir / ('claude-code-%s.json' % key_a)).exists())
        self.assertTrue((state_dir / ('claude-code-%s.json' % key_b)).exists())
        flow.uninstall_claude(self.store, root_a, home_dir=home)
        text = settings_path.read_text(encoding='utf-8')
        self.assertNotIn('proj-a', text)
        self.assertIn('proj-b', text)
        self.assertFalse((state_dir / ('claude-code-%s.json' % key_a)).exists())
        self.assertTrue((state_dir / ('claude-code-%s.json' % key_b)).exists())

    def test_enforce_session_from_hook_stdin(self):
        # Codex PR #5 finding 4: a hook payload on stdin must give enforce
        # a real session key instead of the shared literal 'default'.
        root = self.init()
        flow.capture(self.store, root, session='s1')
        script = str(SKILL / 'scripts/relay.py')
        cmd = [sys.executable, script, '--store-root', str(self.store),
               '--root', str(root), 'enforce', '--tool', 'claude-code']
        out = subprocess.run(cmd, input=b'{"session_id": "hook-sess-1"}',
                             capture_output=True)
        self.assertIn('补填采集条目', out.stdout.decode('utf-8'))
        state = json.loads((self.store_dir(root) / 'state' / 'enforce.json').read_text(encoding='utf-8'))
        self.assertIn('hook-sess-1', state['session_counts'])
        self.assertNotIn('default', state['session_counts'])

    def test_session_from_stdin_fallbacks(self):
        with mock.patch.object(sys, 'stdin', io.StringIO('not json')):
            self.assertIsNone(relay._session_from_stdin())
        with mock.patch.object(sys, 'stdin', io.StringIO('{"session_id": "abc"}')):
            self.assertEqual('abc', relay._session_from_stdin())
        with mock.patch.object(sys, 'stdin', io.StringIO('')):
            self.assertIsNone(relay._session_from_stdin())

    def test_codebuddy_weak_recipe_and_codex_section(self):
        root = self.init()
        target = Path(self.temp.name) / 'targethome'
        info = flow.install_codebuddy(self.store, root, target, flavor='codebuddy')
        skill = Path(info['skill_dir']) / 'SKILL.md'
        self.assertTrue(skill.exists())
        self.assertIn('无生命周期 hooks', info['note'])
        flow.uninstall_codebuddy(self.store, root, flavor='codebuddy')
        self.assertFalse(Path(info['skill_dir']).exists())
        (root / 'AGENTS.md').write_bytes('# My agents\n'.encode('utf-8'))
        flow.install_codex(self.store, root)
        agents = (root / 'AGENTS.md').read_text(encoding='utf-8')
        self.assertIn('# My agents', agents)
        self.assertIn('relay:begin', agents)
        flow.uninstall_codex(self.store, root)
        self.assertEqual('# My agents\n', (root / 'AGENTS.md').read_text(encoding='utf-8'))


class TestPrecompress(FlowBase):
    def test_counts_claimed_and_degraded(self):
        root = self.init()
        flow.capture(self.store, root, session='a')
        self.edit(root, 'app.py', 'value = 2\n')
        (root / 'new.py').write_bytes('n = 1\n'.encode('utf-8'))
        flow.capture(self.store, root, session='b')
        sd = self.store_dir(root)
        # A crafted entry whose semantic text claims an un-evidenced file.
        flow._write_entry(sd, {'version': 1, 'seq': 99, 'captured_at': flow.utc(),
                               'tool': None, 'session': 'c', 'anchor': {'head': None, 'branch': 'main'},
                               'fingerprint': {'anchor': {'head': None, 'branch': 'main'}, 'code': {}, 'gaps': []},
                               'prev_seq': 2, 'fact_hash': 'x', 'queue': 'pending',
                               'redacted_count': 0, 'retries': 0, 'semantic': 'pending',
                               'degraded': False, 'user_message': None},
                          '# 采集条目 cap-0099\n\n## 语义（待填）\n### 做了什么\n改了 ghost_module.py\n')
        doc = flow.precompress(self.store, root)
        self.assertGreaterEqual(doc['counts']['file_edit'], 1)
        self.assertGreaterEqual(doc['counts']['file_new'], 1)
        self.assertTrue(any(c['file'] == 'ghost_module.py' and c['status'] == 'unconfirmed'
                            for c in doc['claimed_changes']))
        # Regeneration overwrites, never versions.
        flow.precompress(self.store, root)
        self.assertFalse(list((sd / 'state').glob('digest-*.json')))


class TestFingerprint(FlowBase):
    def test_snapshot_matches_old_engine_without_submodules(self):
        root = self.init()
        self.edit(root, 'app.py', 'value = 3\n')
        old = relay.relayctl.snapshot(root)
        new = flow.snapshot_v2(root)
        self.assertEqual(old['code'], new['code'])
        self.assertEqual(old['anchor'], new['anchor'])

    def test_crlf_filtered_hash(self):
        root = self.init()
        (root / '.gitattributes').write_bytes('*.txt text eol=crlf\n'.encode('utf-8'))
        (root / 'note.txt').write_bytes('line one\nline two\n'.encode('utf-8'))
        self.git(root, 'add', '.')
        self.git(root, 'commit', '-m', 'crlf attrs')
        (root / 'note.txt').write_bytes(b'line one\r\nline two\r\n')
        self.assertTrue(flow.filtered_hash_equal(root, 'note.txt'))
        head_bytes = self.git(root, 'show', 'HEAD:note.txt', check=True).stdout
        self.assertNotEqual(head_bytes, (root / 'note.txt').read_bytes())

    def test_submodule_downgraded_to_gap(self):
        root = self.init()
        sub = self.make_repo('submod', remote='https://github.com/example/submod.git')
        self.git(root, '-c', 'protocol.file.allow=always', 'submodule', 'add', str(sub), 'vend')
        snap = flow.snapshot_v2(root)
        self.assertEqual(1, len(snap['gaps']))
        self.assertEqual('vend', snap['gaps'][0]['path'])

    def test_assume_unchanged_cannot_hide(self):
        root = self.init()
        self.edit(root, 'app.py', 'value = 9\n')
        self.git(root, 'update-index', '--assume-unchanged', 'app.py')
        diff = self.git(root, 'diff', '--name-only', check=True).stdout.decode()
        self.assertNotIn('app.py', diff)
        _staged, _work, match = flow.index_truth(root, 'app.py')
        self.assertFalse(match)


class TestScales(FlowBase):
    def test_milestone_from_new_receipt(self):
        root = self.init()
        draft = Path(self.temp.name) / 'draft.md'
        draft.write_bytes('# 项目目标\n\n状态：待确认\n\n- 目标与交付物：做 MVP\n'.encode('utf-8'))
        relay.goal_draft(self.store, root, draft.read_text(encoding='utf-8'))
        relay.goal_confirm(self.store, root)
        rec = flow.scales(self.store, root)
        self.assertEqual('milestone', rec['triggered'])

    def test_change_scale(self):
        root = self.init()
        for i in range(10):
            (root / ('f%02d.py' % i)).write_bytes(('x = %d\n' % i).encode('utf-8'))
        rec = flow.scales(self.store, root)
        self.assertEqual('change', rec['triggered'])

    def test_time_scale_and_no_goal_milestone_disabled(self):
        root = self.init()
        relay.goal_decline(self.store, root)
        flow.capture(self.store, root)
        sd = self.store_dir(root)
        import datetime as dt
        old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=8)).isoformat()
        flow.save(sd / 'state' / 'last-summary.json',
                  {'seq': 0, 'at': old, 'source': 'test', 'receipt_seq': None})
        rec = flow.scales(self.store, root)
        self.assertEqual('time', rec['triggered'])
        milestone = [r for r in rec['rows'] if r['scale'] == 'milestone'][0]
        self.assertIn('无锚点', milestone['reason'])

    def test_below_all_scales_and_debounce(self):
        root = self.init()
        rec = flow.scales(self.store, root)
        self.assertIsNone(rec['triggered'])
        flow.capture(self.store, root)
        flow.summarize(self.store, root, source='test')
        rec = flow.scales(self.store, root)
        self.assertIsNone(rec['triggered'])
        time_row = [r for r in rec['rows'] if r['scale'] == 'time'][0]
        self.assertIn('防抖', time_row['reason'])


class TestSummarize(FlowBase):
    def _summarized(self, root):
        flow.capture(self.store, root, session='s')
        return flow.summarize(self.store, root, source='test')

    def test_incremental_cursor_and_baseline(self):
        root = self.init()
        first = self._summarized(root)
        self.assertEqual([1, 1], first['entry_range'])
        self.edit(root, 'app.py', 'value = 5\n')
        flow.capture(self.store, root, session='s2')
        second = flow.summarize(self.store, root, source='test')
        self.assertEqual([2, 2], second['entry_range'])
        sd = self.store_dir(root)
        self.assertEqual(2, flow._cursor(sd)['entry_seq'])
        measure = flow.measure_change(self.store, root)
        self.assertEqual(0, len(measure['counts']['changed']))

    def test_number_whitelist_and_demotion(self):
        root = self.init()
        flow.capture(self.store, root, session='s')
        semantic = ('## 已验证\n- 完成了 X\n## 已做未验证\n- 做了 Y（验证：跑测试）\n'
                    '## 已讨论\n- 改了 999 个文件\n')
        # Note: the bogus number lives in a discussed line; whitelist check
        # runs over the whole semantic text.
        rec = flow.summarize(self.store, root, source='test', semantic_text=semantic)
        self.assertTrue(rec['number_events'])
        self.assertEqual(1, len(rec['demotions']))
        state = (self.store_dir(root) / 'archive' / 'state.md').read_text(encoding='utf-8')
        self.assertIn('完成了 X', state.split('已改动但尚未验证')[1])

    def test_decisions_backlog_and_lesson_gate(self):
        root = self.init()
        flow.capture(self.store, root, session='s')
        semantic = ('## 决定\n- 选用标准库（来源：用户明确要求）\n'
                    '## 教训\n- 不要在周五上线（证据：原料 cap-0001 失败记录）\n'
                    '## 教训\n- 凭感觉的教训\n'
                    '## 待办\n- 写 README（Owner：用户，验证：文件存在）\n')
        rec = flow.summarize(self.store, root, source='test', semantic_text=semantic)
        sd = self.store_dir(root)
        self.assertEqual(['D-001'], rec['appended_decisions'])
        self.assertEqual(1, len(rec['appended_lessons']))
        self.assertEqual(['凭感觉的教训'], rec['rejected_lessons'])
        backlog = (sd / 'archive' / 'backlog.md').read_text(encoding='utf-8')
        self.assertIn('T-001', backlog)
        self.assertIn('Owner：用户', backlog)
        self.assertIn('验证方式：文件存在', backlog)

    def test_anchor_mismatch_aborts(self):
        root = self.init()
        flow.capture(self.store, root, session='s', message='hello-anchor')
        flow.summarize(self.store, root, source='test')
        sd = self.store_dir(root)
        (sd / 'raw' / 'cap-0001.md').unlink()
        flow.capture(self.store, root, session='s2')
        with self.assertRaises(FlowRelayError):
            flow.summarize(self.store, root, source='test')

    def test_recover_roundtrip_and_abort(self):
        root = self.init()
        self._summarized(root)
        sd = self.store_dir(root)
        tx = flow.read_json(sd / 'tx' / '0001.json')
        import base64
        state_path = sd / 'archive' / 'state.md'
        state_path.write_bytes(base64.b64decode(tx['files']['state.md']['before_b64']))
        info = flow.recover(self.store, root)
        self.assertEqual('0001.json', info['recovered'])
        self.assertFalse((sd / 'tx' / '0001.json').exists())
        # Abort path: third-state content keeps the log.
        flow.capture(self.store, root, session='s3')
        flow.summarize(self.store, root, source='test')
        state_path.write_bytes('被外部改写的第三态\n'.encode('utf-8'))
        with self.assertRaises(FlowRelayError):
            flow.recover(self.store, root)
        self.assertTrue((sd / 'tx' / '0002.json').exists())


class TestArchive(FlowBase):
    def test_templates_fields_and_handoff(self):
        root = self.init()
        info = flow.make_handoff(self.store, root)
        self.assertTrue(Path(info['package']).exists())
        body = Path(info['package']).read_text(encoding='utf-8')
        for section in ('状态卷顶部', '目标现状', '待办未闭环', '下一会话提示词', '原料保真度与缺口'):
            self.assertIn(section, body)
        index = (self.store_dir(root) / 'state' / 'handoff-index.md').read_text(encoding='utf-8')
        self.assertIn(info['name'], index)
        # Backlog is the only open-item list: state.md carries no T- items.
        state = (self.store_dir(root) / 'archive' / 'state.md').read_text(encoding='utf-8')
        self.assertNotIn('## T-', state)

    def test_budget_auto_split(self):
        root = self.init()
        sd = self.store_dir(root)
        state_path = sd / 'archive' / 'state.md'
        state_path.write_bytes((state_path.read_text(encoding='utf-8') + '\n'.join(
            '填充行 %d' % i for i in range(200)) + '\n').encode('utf-8'))
        moved = flow.enforce_budget(sd)
        self.assertIn('state.md', moved)
        self.assertTrue((sd / 'archive' / 'splits').exists())
        self.assertTrue((sd / 'state' / 'split-index.json').exists())
        self.assertIn(flow.STATE_BEGIN, state_path.read_text(encoding='utf-8'))


class TestResume(FlowBase):
    def test_standard_five_sections_zero_write(self):
        root = self.init()
        flow.capture(self.store, root, session='s')
        flow.summarize(self.store, root, source='test')
        sd = self.store_dir(root)
        before = tree_bytes(sd)
        result = flow.resume(self.store, root)
        self.assertFalse(result['sick'])
        self.assertEqual(5, len(result['sections']))
        self.assertTrue(result['sections'][0].startswith('一段'))
        self.assertTrue(result['sections'][4].startswith('五段'))
        self.assertEqual(before, tree_bytes(sd))

    def test_inconsistency_reports_and_correction_rides_backfill(self):
        root = self.init()
        flow.capture(self.store, root, session='s')
        flow.summarize(self.store, root, source='test')
        (root / 'app.py').unlink()
        self.git(root, 'add', '-A')
        self.git(root, 'commit', '-m', 'drop app')
        self.git(root, 'commit', '--allow-empty', '-m', 'second')
        result = flow.resume(self.store, root)
        self.assertIn('档案过期', result['sections'][1])
        self.assertIn('新增提交：2', result['sections'][1])
        state = (self.store_dir(root) / 'archive' / 'state.md').read_text(encoding='utf-8')
        self.assertIn('对账纠正', state)

    def test_sick_banner_first_on_backfill_failure(self):
        root = self.init()
        flow.capture(self.store, root, session='s', message='anchor-one')
        flow.summarize(self.store, root, source='test')
        sd = self.store_dir(root)
        cursor = flow.read_json(sd / 'state' / 'cursor.json')
        cursor['anchor_message'] = 'bogus-anchor'
        flow.save(sd / 'state' / 'cursor.json', cursor)
        flow.capture(self.store, root, session='s2')
        result = flow.resume(self.store, root)
        self.assertTrue(result['sick'])
        rendered = flow.render_resume(result)
        self.assertLess(rendered.index('带病接手'), rendered.index('一段'))

    def test_no_archive_branch_writes_nothing(self):
        root = self.make_repo('fresh', remote='https://github.com/example/fresh.git')
        result = flow.resume(self.store, root)
        self.assertTrue(result['no_archive'])
        self.assertFalse((self.store / 'projects').exists())

    def test_fork_visibility(self):
        root = self.init()
        flow.capture(self.store, root, tool='claude-code', session='s')
        result = flow.resume(self.store, root, tool='codex')
        self.assertIn('分叉可见性', result['sections'][1])


class TestInject(FlowBase):
    def test_budget_keeps_state_and_backlog(self):
        root = self.init()
        flow.capture(self.store, root, session='s')
        flow.summarize(self.store, root, source='test')
        info = flow.inject(self.store, root, budget=2000)
        self.assertLessEqual(info['tokens_estimate'], 2000)
        self.assertIn('当前分支@基线', info['text'])
        tight = flow.inject(self.store, root, budget=100)
        self.assertIn('当前分支@基线', tight['text'])
        self.assertIn('待办清单', tight['text'])


class TestCLI(FlowBase):
    def test_cli_capture_queue_resume(self):
        root = self.init()
        runtime = str(SKILL / 'scripts/relay.py')
        env = dict(os.environ, PROJECT_RELAY_HOME=str(Path(self.temp.name)),
                PYTHONIOENCODING='utf-8')
        base = [sys.executable, runtime, '--root', str(root),
                '--store-root', str(self.store)]
        for args in (['capture', '--tool', 'codex'], ['queue'], ['resume']):
            r = subprocess.run(base + args, capture_output=True, text=True, encoding='utf-8', env=env)
            self.assertEqual(0, r.returncode, r.stderr)
        r = subprocess.run(base + ['capture', '--tool', 'codex'], capture_output=True,
                           text=True, encoding='utf-8', env=env)
        self.assertIn('采集', r.stdout)


if __name__ == '__main__':
    unittest.main()
