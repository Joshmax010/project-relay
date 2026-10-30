"""v2 fork chain tests (plan part 03, step 3.3).

Style follows the promote suite: real temporary directories, real Git
repos, store redirected with PROJECT_RELAY_HOME. The concurrent-write
guard is driven deterministically by patching the single re-read point
(relay_flow._read_archive_bytes): real bytes for the start read,
tampered bytes for the pre-write re-read -- no timing races.
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
fork = _load('relay_fork', SKILL / 'scripts/relay_fork.py')


def tree_bytes(root):
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in root.rglob('*') if p.is_file()}


class ForkBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='relay-fork-')
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

    def call(self, fn, *args, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return fn(*args, **kwargs)

    def init(self, name='proj'):
        root = self.make_repo(name)
        self.call(relay.init, self.store, root)
        return root

    def store_dir(self, root):
        row = relay.where(self.store, path=root)['row']
        return self.store / 'projects' / (row['key'] + '-' + row['slug'])

    def edit(self, root, rel, text):
        (root / rel).write_bytes(text.encode('utf-8'))

    def work(self, root, n, message):
        """One work step: edit + capture, returns the capture info."""
        self.edit(root, 'app.py', 'value = %d\n' % n)
        return self.call(flow.capture, self.store, root,
                         session='s%d' % n, message=message)

    def summarize(self, root, **kwargs):
        return self.call(flow.summarize, self.store, root, **kwargs)

    def branch_run(self, root, semantic_text):
        """A summarize whose pre-write re-read sees a concurrent writer:
        the product must branch instead of overwriting. The guard's start
        read is calls 1-4 (one per archive file, in ARCHIVE_FILES order);
        the guard re-read is calls 5-8; only the re-read is tampered."""
        orig = flow._read_archive_bytes
        calls = {'n': 0}

        def fake(archive, name):
            calls['n'] += 1
            data = orig(archive, name)
            if calls['n'] > 4 and name == 'state.md':
                return data + b'\n<!-- concurrent writer -->\n'
            return data

        with mock.patch.object(flow, '_read_archive_bytes', fake):
            return self.summarize(root, source='test',
                                  semantic_text=semantic_text,
                                  semantic_auto=True)


class TestChainPointers(ForkBase):
    def test_based_on_links_summaries_and_head_has_origin(self):
        root = self.init()
        self.work(root, 2, '做了甲')
        rec1 = self.summarize(root, source='test')
        self.assertFalse(rec1.get('branched'))
        self.assertIsNone(rec1['based_on'])
        self.assertEqual('过程式', rec1['origin'])
        self.work(root, 3, '做了乙')
        rec2 = self.summarize(root, source='test')
        self.assertEqual('sum-%04d' % rec1['seq'], rec2['based_on'])
        self.work(root, 4, '做了丙')
        rec3 = self.summarize(root, source='backfill')
        self.assertEqual('回溯式', rec3['origin'])
        self.assertEqual('sum-%04d' % rec2['seq'], rec3['based_on'])
        sd = self.store_dir(root)
        chain = fork.list_summaries(sd)
        self.assertEqual([rec1['seq'], rec2['seq'], rec3['seq']],
                         [e['seq'] for e in chain])
        self.assertEqual([], fork.detect_forks(sd)['forks'])

    def test_field_diff_lists_only_differing_fields(self):
        a = ('<!-- relay:state-begin -->\n- 正在做什么：汇总 1\n'
             '- 下一动作：见待办清单\n<!-- relay:state-end -->')
        b = ('<!-- relay:state-begin -->\n- 正在做什么：汇总 2\n'
             '- 下一动作：见待办清单\n<!-- relay:state-end -->')
        diff = fork.field_diff(a, b)
        self.assertEqual(1, len(diff))
        self.assertEqual('正在做什么', diff[0]['field'])


class TestGuardAndMerge(ForkBase):
    def _forked_store(self, root):
        """sum-1 on main, then a branched product off sum-1."""
        self.work(root, 2, '做了甲')
        self.summarize(root, source='test')
        self.work(root, 3, '做了乙')
        branched = self.branch_run(root, '## 决定\n- 支上决定内容（来源：测试）\n')
        return branched

    def test_guard_trip_branches_instead_of_overwriting(self):
        """Acceptance 3.3-3: a moved baseline becomes a branch, never an
        overwrite; cursor and entries are untouched."""
        root = self.init()
        branched = self._forked_store(root)
        self.assertTrue(branched['branched'])
        self.assertEqual('sum-0001', branched['based_on'])
        self.assertEqual(['state.md'], branched['mismatches'])
        sd = self.store_dir(root)
        state_text = (sd / 'archive' / 'state.md').read_text(encoding='utf-8')
        self.assertNotIn('concurrent writer', state_text)
        self.assertIn('汇总 1', state_text)
        branch_dir = sd / 'state' / 'branches' / branched['branch']
        self.assertTrue((branch_dir / 'state.md').exists())
        self.assertIn('汇总 2', (branch_dir / 'state.md').read_text(encoding='utf-8'))
        q = flow.queue_status(sd)
        self.assertEqual(1, q['pending'])
        cursor = json.loads((sd / 'state' / 'cursor.json').read_text(encoding='utf-8'))
        self.assertEqual(1, cursor['entry_seq'])

    def test_takeover_lists_fork_and_merges_without_loss(self):
        """Acceptance 3.3-1: two tools alternate, one concurrent write;
        takeover lists the fork, both branches' content survives, and
        the state conflict lands in the pending zone, not silently won."""
        root = self.init()
        branched = self._forked_store(root)
        branch_id = branched['branch']
        main = self.summarize(root, source='test')
        self.assertFalse(main.get('branched'))
        self.assertIn(branch_id, main['merged_branches'])
        sd = self.store_dir(root)
        decisions = (sd / 'archive' / 'decisions.md').read_text(encoding='utf-8')
        self.assertEqual(1, decisions.count('支上决定内容'))
        state_text = (sd / 'archive' / 'state.md').read_text(encoding='utf-8')
        self.assertIn('fork=%s' % branch_id, state_text)
        self.assertIn('field=正在做什么', state_text)
        self.assertIn('status=open', state_text)
        branch_rec = json.loads(
            (sd / 'state' / 'branches' / (branch_id + '.json')).read_text(encoding='utf-8'))
        self.assertEqual('merged', branch_rec['status'])
        result = flow.resume(self.store, root)
        rendered = flow.render_resume(result)
        self.assertEqual(5, len(result['sections']))
        self.assertIn('分叉清单', rendered)
        self.assertIn(branch_id, rendered)
        self.assertIn('待裁', rendered)
        detection = fork.detect_forks(sd, persist=False)
        self.assertEqual(1, len(detection['forks']))
        self.assertEqual('sum-0001', detection['forks'][0]['point'])

    def test_adjudicate_records_decision(self):
        root = self.init()
        self.work(root, 2, '做了甲')
        self.summarize(root, source='test')
        sd = self.store_dir(root)
        state_path = sd / 'archive' / 'state.md'
        text = state_path.read_text(encoding='utf-8')
        text = fork.append_pending_adjudication(
            text, {'fork': 'br-test', 'field': '正在做什么',
                   'main_value': '汇总 1', 'branch_value': '汇总 2'})
        state_path.write_bytes(text.encode('utf-8'))
        out = fork.adjudicate(sd, 'br-test', 'branch', '代码现状属实')
        self.assertEqual('branch', out['winner'])
        after = state_path.read_text(encoding='utf-8')
        self.assertIn('status=resolved', after)
        self.assertIn('winner=branch', after)
        log = json.loads((sd / 'state' / 'fork-adjudications.json').read_text(encoding='utf-8'))
        self.assertEqual('br-test', log[0]['fork'])
        self.assertEqual('代码现状属实', log[0]['note'])

    def test_resume_fork_section_writes_nothing(self):
        root = self.init()
        self._forked_store(root)
        # Consume the still-pending entry first: resume catch-up-summarizes
        # a non-empty queue (section 4 is the writing section), so a
        # zero-write assertion needs an empty queue -- same discipline as
        # the promotion resume test. The fork stays visible after merge.
        self.summarize(root, source='test')
        sd = self.store_dir(root)
        before = tree_bytes(sd)
        result = flow.resume(self.store, root)
        self.assertEqual(5, len(result['sections']))
        self.assertIn('分叉清单', result['fork_section'])
        self.assertEqual(before, tree_bytes(sd))


class TestGoalFreeze(ForkBase):
    def _confirmed_goal(self, root):
        relay.goal_draft(self.store, root, '状态：已确认\n\n目标：做一个能用的接力。\n')
        relay.goal_confirm(self.store, root)
        self.assertEqual('confirmed', relay.goal_status(self.store, root)[0])

    def test_goal_fork_freezes_inject_until_reconfirm(self):
        """Acceptance 3.3-2: a forked goal freezes injection and only the
        S4 reconfirmation flow lifts it."""
        root = self.init()
        self._confirmed_goal(root)
        sd = self.store_dir(root)
        fork.flag_goal_fork(sd, '两台设备目标版本分叉')
        frozen = relay.goal_inject(self.store, root)
        self.assertIn('已冻结', frozen)
        self.assertNotIn('做一个能用的接力', frozen)
        relay.goal_draft(self.store, root, '状态：已确认\n\n目标：做一个能用的接力，第二版。\n')
        relay.goal_confirm(self.store, root)
        self.assertFalse((sd / 'state' / 'goal-fork.json').exists())
        self.assertIn('第二版', relay.goal_inject(self.store, root))

    def test_sync_goal_conflict_marker_also_freezes(self):
        root = self.init()
        self._confirmed_goal(root)
        sd = self.store_dir(root)
        (sd / 'state' / 'goal-conflict.json').write_bytes(
            json.dumps({'reason': 'sync'}).encode('utf-8'))
        self.assertIn('已冻结', relay.goal_inject(self.store, root))
        (sd / 'state' / 'goal-conflict.json').unlink()
        self.assertIn('做一个能用的接力', relay.goal_inject(self.store, root))


class TestWildAndCompression(ForkBase):
    def test_wild_fork_labeled_unknown_origin(self):
        root = self.init()
        self.work(root, 2, '做了甲')
        rec1 = self.summarize(root, source='test')
        sd = self.store_dir(root)
        fake = dict(rec1)
        fake['seq'] = 2
        fake['based_on'] = 'sum-9999'
        flow.save(sd / 'state' / 'summaries' / '0002.json', fake)
        detection = fork.detect_forks(sd, persist=False)
        self.assertEqual(1, len(detection['wild']))
        self.assertEqual('sum-0002', detection['wild'][0]['id'])
        self.assertEqual('来源不明的一支', detection['wild'][0]['label'])
        rendered = fork.render_fork_section(sd, detection)
        self.assertIn('野分叉', rendered)
        self.assertIn('来源不明的一支', rendered)

    def test_legacy_records_are_not_wild(self):
        root = self.init()
        self.work(root, 2, '做了甲')
        self.summarize(root, source='test')
        sd = self.store_dir(root)
        record_path = sd / 'state' / 'summaries' / '0001.json'
        rec = json.loads(record_path.read_text(encoding='utf-8'))
        del rec['based_on']
        del rec['origin']
        flow.save(record_path, rec)
        detection = fork.detect_forks(sd, persist=False)
        self.assertEqual([], detection['wild'])
        self.assertEqual([], detection['forks'])

    def test_compression_folds_and_resolves(self):
        """Acceptance 3.3-4: small injected threshold (no real 200
        records); package queryable, chain resolvable, seq monotonic."""
        root = self.init()
        for i in range(2, 8):
            self.work(root, i, '做了事项 %d' % i)
            self.summarize(root, source='test')
        sd = self.store_dir(root)
        self.assertEqual(6, len(fork.list_summaries(sd)))
        package = fork.maybe_compress_chain(sd, threshold=5, keep=2)
        self.assertIsNotNone(package)
        self.assertEqual(4, package['folded_count'])
        self.assertEqual('sum-0004', package['anchor_id'])
        live = [e for e in fork.list_summaries(sd) if not e['packaged']]
        self.assertEqual([5, 6], [e['seq'] for e in live])
        self.assertEqual(6, len(fork.list_summaries(sd)))
        head = [e for e in fork.list_summaries(sd) if e['seq'] == 5][0]
        self.assertEqual('sum-0004', head['based_on'])
        anchor = fork.chain_anchor(sd)
        self.assertEqual('sum-0004', anchor['anchor_id'])
        self.assertEqual(package['total_hash'], anchor['total_hash'])
        blob = b''.join(json.dumps(rec, sort_keys=True, ensure_ascii=False).encode('utf-8')
                        for rec in package['records'])
        self.assertEqual(relay.digest(blob), package['total_hash'])
        self.assertEqual(7, flow._next_summary_seq(sd))
        self.work(root, 8, '做了事项 8')
        rec = self.summarize(root, source='test')
        self.assertEqual(7, rec['seq'])
        self.assertEqual('sum-0006', rec['based_on'])

    def test_compression_skips_legacy_tx(self):
        root = self.init()
        self.work(root, 2, '做了甲')
        self.summarize(root, source='test')
        sd = self.store_dir(root)
        tx_dir = sd / 'tx'
        logs = sorted(tx_dir.glob('*.json'))
        self.assertTrue(logs)
        tx = json.loads(logs[-1].read_text(encoding='utf-8'))
        del tx['before_state']
        flow.save(logs[-1], tx)
        self.assertIsNone(fork.maybe_compress_chain(sd, threshold=0, keep=0))


class TestForkCli(ForkBase):
    def test_fork_list_cli(self):
        root = self.init()
        self.work(root, 2, '做了甲')
        self.summarize(root, source='test')
        proc = subprocess.run(
            [sys.executable, str(SKILL / 'scripts/relay.py'),
             '--root', str(root), '--store-root', str(self.store),
             'fork', 'list'],
            capture_output=True, text=True)
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn('分叉清单', proc.stdout)


if __name__ == '__main__':
    unittest.main()
