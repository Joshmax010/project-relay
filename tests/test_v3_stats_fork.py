"""v3 stats + fork tests (batch 18).

Stats: record-only rows under <store>/stats/, the token estimator, and
the weekly-spike fuse (3x last week AND >= 5000 tokens) with its
monthly soft cap. Forks: the summary chain in an agent directory --
detection, branch write/merge, chain compression, goal-fork flag.
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from v3_helpers import V3Base, relay, flow, stats, fork

DAY = 86400


class TestStatsRows(V3Base):
    def test_record_rows_land_in_stats_dir(self):
        root = self.init()
        store = self.store_dir(root)
        stats.record_capture(store, tool='codex', entries=1, changed_files=2)
        stats.record_summarize(store, 'manual', 'manual', entry_count=1,
                               llm_in_tokens=100, llm_out_tokens=50)
        stats.record_handoff(store,
                             {'index': 10, 'timeline': 20, 'full': 30, 'total': 60},
                             {'changed_files': 2, 'missing_files': 1})
        sdir = store / 'stats'
        for name in ('capture.jsonl', 'summarize.jsonl', 'handoff.jsonl'):
            self.assertTrue((sdir / name).is_file(), 'missing ' + name)
        cap = stats.capture_rows(store)
        self.assertEqual(1, len(cap))
        self.assertEqual('codex', cap[0]['tool'])
        self.assertNotIn('llm_tokens', cap[0])  # capture never calls a model
        srow = stats.summarize_rows(store)[0]
        self.assertEqual(150, srow['llm_tokens'])
        hrow = stats.handoff_rows(store)[0]
        self.assertEqual(60, hrow['injection_total_tokens'])
        self.assertEqual(3, hrow['reconcile_total'])
        self.assertEqual(60, hrow['total_cost_tokens'])  # no prior summarize
        self.assertAlmostEqual(2 / 3, hrow['unrecorded_proxy'], places=4)

    def test_summarize_rows_and_latest(self):
        root = self.init()
        store = self.store_dir(root)
        self.assertEqual([], stats.summarize_rows(store))
        self.assertIsNone(stats.latest_summarize_row(store))
        stats.record_summarize(store, 'manual', 'manual', llm_in_tokens=10)
        stats.record_summarize(store, 'manual', 'manual', llm_in_tokens=20)
        rows = stats.summarize_rows(store)
        self.assertEqual(2, len(rows))
        self.assertEqual(20, stats.latest_summarize_row(store)['llm_in_tokens'])


class TestTokenEstimate(V3Base):
    def test_tokens_for_chars_is_chars_over_1_5_ceiling(self):
        cases = {0: 0, 1: 1, 2: 2, 3: 2, 4: 3, 150: 100, 149: 100, 151: 101}
        for chars, tokens in cases.items():
            self.assertEqual(tokens, stats.tokens_for_chars(chars),
                             'chars=%d' % chars)

    def test_estimate_tokens_matches_flow(self):
        text = 'x' * 150
        self.assertEqual(100, stats.estimate_tokens(text))
        self.assertEqual(flow.estimate_tokens(text), stats.estimate_tokens(text))
        self.assertEqual(0, stats.estimate_tokens(''))


class TestFuse(V3Base):
    def _spike(self, store, now, prev_tokens=2000, week_tokens=7000):
        stats.record_summarize(store, 'manual', 'manual',
                               llm_in_tokens=prev_tokens,
                               now_epoch=now - 10 * DAY)
        stats.record_summarize(store, 'manual', 'manual',
                               llm_in_tokens=week_tokens,
                               now_epoch=now - 1 * DAY)

    def test_steady_history_does_not_trip(self):
        root = self.init()
        store = self.store_dir(root)
        now = time.time()
        stats.record_summarize(store, 'manual', 'manual',
                               llm_in_tokens=1000, now_epoch=now - 10 * DAY)
        stats.record_summarize(store, 'manual', 'manual',
                               llm_in_tokens=1000, now_epoch=now - 1 * DAY)
        result = stats.compute_fuse(store, now_epoch=now)
        self.assertFalse(result['tripped'])
        self.assertEqual(1000, result['week_llm_tokens'])
        self.assertEqual(1000, result['prev_week_llm_tokens'])
        self.assertFalse(result['cap_alert'])
        self.assertIsNone(result['monthly_cap'])

    def test_spike_trips(self):
        root = self.init()
        store = self.store_dir(root)
        now = time.time()
        self._spike(store, now)
        result = stats.compute_fuse(store, now_epoch=now)
        self.assertTrue(result['tripped'])
        self.assertEqual(7000, result['week_llm_tokens'])
        self.assertEqual(2000, result['prev_week_llm_tokens'])
        self.assertIn('3 倍', result['reason'])

    def test_small_spike_below_floor_does_not_trip(self):
        root = self.init()
        store = self.store_dir(root)
        now = time.time()
        stats.record_summarize(store, 'manual', 'manual',
                               llm_in_tokens=1000, now_epoch=now - 10 * DAY)
        stats.record_summarize(store, 'manual', 'manual',
                               llm_in_tokens=4000, now_epoch=now - 1 * DAY)
        self.assertFalse(stats.compute_fuse(store, now_epoch=now)['tripped'])

    def test_no_previous_week_never_trips(self):
        root = self.init()
        store = self.store_dir(root)
        now = time.time()
        stats.record_summarize(store, 'manual', 'manual',
                               llm_in_tokens=99999, now_epoch=now - 1 * DAY)
        self.assertFalse(stats.compute_fuse(store, now_epoch=now)['tripped'])

    def test_evaluate_fuse_persists_trip_and_clear(self):
        root = self.init()
        store = self.store_dir(root)
        now = time.time()
        self._spike(store, now)
        tripped = stats.evaluate_fuse(store, now_epoch=now)
        self.assertTrue(tripped['tripped'])
        self.assertIsNotNone(tripped['tripped_at'])
        saved = json.loads((store / 'stats' / 'fuse.json').read_text(encoding='utf-8'))
        self.assertTrue(saved['tripped'])
        self.assertEqual(tripped['tripped_at'], saved['tripped_at'])
        # Thirty days later the spike is out of every window: it clears.
        cleared = stats.evaluate_fuse(store, now_epoch=now + 30 * DAY)
        self.assertFalse(cleared['tripped'])
        self.assertIsNotNone(cleared['cleared_at'])
        self.assertEqual(tripped['tripped_at'], cleared['tripped_at'])

    def test_fuse_tripped_quiet_by_default(self):
        root = self.init()
        self.assertFalse(stats.fuse_tripped(self.store_dir(root)))

    def test_monthly_cap_alert(self):
        root = self.init()
        store = self.store_dir(root)
        now = time.time()
        (store / 'config.json').write_text(
            json.dumps({'monthly_llm_token_cap': 100}, ensure_ascii=False),
            encoding='utf-8')
        stats.record_summarize(store, 'manual', 'manual',
                               llm_in_tokens=60, now_epoch=now)
        stats.record_summarize(store, 'manual', 'manual',
                               llm_in_tokens=60, now_epoch=now)
        result = stats.compute_fuse(store, now_epoch=now)
        self.assertEqual(100, result['monthly_cap'])
        self.assertEqual(120, result['month_llm_tokens'])
        self.assertTrue(result['cap_alert'])
        # The cap alone never trips the fuse.
        self.assertFalse(result['tripped'])


class TestFuseDropsSemantic(V3Base):
    MARKER = '熔断标记甲乙丙'
    SEMANTIC = ('## 已验证\n- %s：演示跑通，验证：手动跑过\n'
                '## 决定\n- 采用方案甲，来源：手动\n' % MARKER)

    def _archive_texts(self, root):
        adir = self.adir(root, 'codex')
        return ''.join((adir / name).read_text(encoding='utf-8')
                       for name in fork.ARCHIVE_FILES)

    def test_semantic_text_lands_when_fuse_quiet(self):
        root = self.init()
        self.capture(root, 'codex', session='s1')
        flow.summarize(root, 'codex', semantic_text=self.SEMANTIC)
        self.assertIn(self.MARKER, self._archive_texts(root))
        row = stats.latest_summarize_row(self.store_dir(root))
        self.assertFalse(row['fused'])

    def test_tripped_fuse_drops_caller_semantic_text(self):
        root = self.init()
        store = self.store_dir(root)
        now = time.time()
        stats.record_summarize(store, 'manual', 'manual',
                               llm_in_tokens=2000, now_epoch=now - 10 * DAY)
        stats.record_summarize(store, 'manual', 'manual',
                               llm_in_tokens=7000, now_epoch=now - 1 * DAY)
        self.assertTrue(stats.fuse_tripped(store, now_epoch=now))
        self.capture(root, 'codex', session='s1')
        record = flow.summarize(root, 'codex', semantic_text=self.SEMANTIC)
        self.assertFalse(record.get('noop'))
        self.assertNotIn(self.MARKER, self._archive_texts(root))
        row = stats.latest_summarize_row(store)
        self.assertTrue(row['fused'])


class TestForkDetect(V3Base):
    def test_clean_store_has_no_forks_no_wild(self):
        root = self.init()
        adir = self.adir(root, 'codex')
        detection = fork.detect_forks(adir)
        self.assertEqual([], detection['forks'])
        self.assertEqual([], detection['wild'])
        self.assertEqual([], detection['branches'])
        self.assertFalse(detection['goal_fork']['active'])
        section = fork.render_fork_section(adir, detection)
        self.assertIn('（无分叉，无待裁项）', section)

    def test_archive_files_constant(self):
        self.assertEqual(('memory.md', 'decisions.md', 'lessons.md', 'backlog.md'),
                         fork.ARCHIVE_FILES)


class TestForkBranches(V3Base):
    def _write_summary(self, adir, seq, based_on=None, with_key=True):
        rec = {'seq': seq, 'source': 'manual', 'at': '2026-11-13T00:00:00Z',
               'entry_range': [1, 1]}
        if with_key:
            rec['based_on'] = based_on
        d = adir / 'state' / 'summaries'
        d.mkdir(parents=True, exist_ok=True)
        (d / ('%04d.json' % seq)).write_text(
            json.dumps(rec, ensure_ascii=False), encoding='utf-8')
        return rec

    def _snapshot(self, progress, decision, lesson, todo):
        return {'memory.md': ('<!-- relay:state-begin -->\n- 进度：%s\n'
                              '<!-- relay:state-end -->\n' % progress),
                'decisions.md': '## D-001\n- 选择：%s\n' % decision,
                'lessons.md': '## L-001\n- 内容：%s\n' % lesson,
                'backlog.md': '## T-001\n- 事项：%s\n' % todo}

    def test_write_branch_then_merge(self):
        root = self.init()
        adir = self.adir(root, 'codex')
        self._write_summary(adir, 1, with_key=False)  # legacy base
        snap = self._snapshot('分支进度', '分支决定甲', '分支教训甲', '分支待办甲')
        rec = fork.write_branch(adir, snap, 'sum-0001', 'codex', '1-2',
                                appended=['D-001'], mismatches=[])
        self.assertTrue(rec['id'].startswith('br-'))
        self.assertEqual('open', rec['status'])
        self.assertEqual('sum-0001', rec['based_on'])
        snap_dir = adir / 'state' / 'branches' / rec['id']
        for name in fork.ARCHIVE_FILES:
            self.assertTrue((snap_dir / name).is_file(), 'missing ' + name)
        # One branch off the base is not a fork, and its based_on resolves.
        detection = fork.detect_forks(adir)
        self.assertEqual([], detection['forks'])
        self.assertEqual([], detection['wild'])
        texts = {'memory.md': ('<!-- relay:state-begin -->\n- 进度：主支进度\n'
                               '<!-- relay:state-end -->\n'),
                 'decisions.md': '## D-001\n- 选择：主支决定\n',
                 'lessons.md': '', 'backlog.md': ''}
        new_texts, merged_ids, notes = fork.merge_branches(adir, texts, summary_seq=2)
        self.assertEqual([rec['id']], merged_ids)
        # Colliding D-001 id is reassigned, content kept.
        self.assertIn('## D-002', new_texts['decisions.md'])
        self.assertIn('分支决定甲', new_texts['decisions.md'])
        self.assertIn('主支决定', new_texts['decisions.md'])
        # Fresh L/T ids merge in under their own ids.
        self.assertIn('## L-001', new_texts['lessons.md'])
        self.assertIn('分支教训甲', new_texts['lessons.md'])
        self.assertIn('## T-001', new_texts['backlog.md'])
        self.assertIn('分支待办甲', new_texts['backlog.md'])
        # The state-field conflict goes to the pending-adjudication area.
        self.assertIn('fork=%s' % rec['id'], new_texts['memory.md'])
        self.assertIn('field=进度', new_texts['memory.md'])
        self.assertTrue(any('待裁区' in n for n in notes))
        self.assertTrue(any('重编号' in n for n in notes))
        # Branch is marked merged; a second merge is a no-op.
        saved = json.loads(
            (adir / 'state' / 'branches' / (rec['id'] + '.json')).read_text(
                encoding='utf-8'))
        self.assertEqual('merged', saved['status'])
        _, merged_again, _ = fork.merge_branches(adir, new_texts, summary_seq=3)
        self.assertEqual([], merged_again)

    def test_merge_dedups_identical_item(self):
        root = self.init()
        adir = self.adir(root, 'codex')
        self._write_summary(adir, 1, with_key=False)
        snap = self._snapshot('分支进度', '主支决定', '', '')
        snap['lessons.md'] = ''
        snap['backlog.md'] = ''
        rec = fork.write_branch(adir, snap, 'sum-0001', 'codex', '1-2',
                                appended=[], mismatches=[])
        texts = {'memory.md': '', 'decisions.md': '## D-001\n- 选择：主支决定\n',
                 'lessons.md': '', 'backlog.md': ''}
        new_texts, merged_ids, notes = fork.merge_branches(adir, texts, summary_seq=2)
        self.assertEqual([rec['id']], merged_ids)
        self.assertEqual(1, new_texts['decisions.md'].count('## D-001'))
        self.assertTrue(any('已去重' in n for n in notes))

    def test_two_branches_off_one_parent_is_a_fork(self):
        root = self.init()
        adir = self.adir(root, 'codex')
        self._write_summary(adir, 1, with_key=False)
        fork.write_branch(adir, self._snapshot('分支甲', '决定甲', '教训甲', '待办甲'),
                          'sum-0001', 'codex', '1-2', appended=[], mismatches=[])
        fork.write_branch(adir, self._snapshot('分支乙', '决定乙', '教训乙', '待办乙'),
                          'sum-0001', 'codex', '1-2', appended=[], mismatches=[])
        detection = fork.detect_forks(adir)
        self.assertEqual(1, len(detection['forks']))
        self.assertEqual('sum-0001', detection['forks'][0]['point'])
        self.assertEqual(2, len(detection['forks'][0]['branches']))
        section = fork.render_fork_section(adir, detection)
        self.assertIn('分叉点 sum-0001', section)
        self.assertNotIn('（无分叉，无待裁项）', section)

    def test_branch_with_unresolvable_based_on_is_wild(self):
        root = self.init()
        adir = self.adir(root, 'codex')
        fork.write_branch(adir, self._snapshot('分支进度', '决定甲', '教训甲', '待办甲'),
                          'sum-9999', 'codex', '1-2', appended=[], mismatches=[])
        detection = fork.detect_forks(adir)
        self.assertEqual(1, len(detection['wild']))
        self.assertEqual('sum-9999', detection['wild'][0]['based_on'])
        self.assertEqual('来源不明的一支', detection['wild'][0]['label'])


class TestForkSummaries(V3Base):
    def _write_summary(self, adir, seq):
        d = adir / 'state' / 'summaries'
        d.mkdir(parents=True, exist_ok=True)
        (d / ('%04d.json' % seq)).write_text(
            json.dumps({'seq': seq, 'source': 'manual'}, ensure_ascii=False),
            encoding='utf-8')

    def test_latest_summary_and_packaged_seqs(self):
        root = self.init()
        adir = self.adir(root, 'codex')
        self.assertIsNone(fork.latest_summary(adir))
        self.assertEqual([], fork.packaged_seqs(adir))
        for seq in (1, 2, 3):
            self._write_summary(adir, seq)
        self.assertEqual(3, fork.latest_summary(adir)['seq'])
        self.assertEqual([], fork.packaged_seqs(adir))

    def test_maybe_compress_chain_folds_old_summaries(self):
        root = self.init()
        adir = self.adir(root, 'codex')
        for seq in range(1, 6):
            self._write_summary(adir, seq)
        package = fork.maybe_compress_chain(adir, threshold=3, keep=1)
        self.assertIsNotNone(package)
        self.assertEqual(4, package['folded_count'])
        self.assertEqual('sum-0004', package['anchor_id'])
        self.assertEqual([1, 2, 3, 4], fork.packaged_seqs(adir))
        self.assertEqual(5, fork.latest_summary(adir)['seq'])
        live = sorted(p.name for p in (adir / 'state' / 'summaries').glob('*.json'))
        self.assertEqual(['0005.json'], live)
        anchor = json.loads(
            (adir / 'state' / 'chain-anchor.json').read_text(encoding='utf-8'))
        self.assertEqual('sum-0004', anchor['anchor_id'])
        # Folded records stay resolvable: the chain still lists all five.
        self.assertEqual(5, len(fork.list_summaries(adir)))

    def test_maybe_compress_chain_under_threshold_returns_none(self):
        root = self.init()
        adir = self.adir(root, 'codex')
        for seq in (1, 2):
            self._write_summary(adir, seq)
        self.assertIsNone(fork.maybe_compress_chain(adir, threshold=3, keep=1))
        self.assertEqual([], fork.packaged_seqs(adir))


class TestGoalFork(V3Base):
    def test_goal_fork_flag_and_conflict_marker(self):
        root = self.init()
        adir = self.adir(root, 'codex')
        self.assertFalse(fork.goal_fork_active(adir))
        flag = adir / 'state' / 'goal-fork.json'
        flag.write_text(json.dumps({'reason': '两边目标不一致'}, ensure_ascii=False),
                        encoding='utf-8')
        self.assertTrue(fork.goal_fork_active(adir))
        self.assertEqual('两边目标不一致', fork.goal_fork_info(adir)['reason'])
        flag.unlink()
        self.assertFalse(fork.goal_fork_active(adir))
        conflict = adir / 'state' / 'goal-conflict.json'
        conflict.write_text(json.dumps({}, ensure_ascii=False), encoding='utf-8')
        self.assertTrue(fork.goal_fork_active(adir))
        conflict.unlink()
        self.assertFalse(fork.goal_fork_active(adir))

    def test_render_lists_active_goal_fork(self):
        root = self.init()
        adir = self.adir(root, 'codex')
        (adir / 'state' / 'goal-fork.json').write_text(
            json.dumps({'reason': '两边目标不一致'}, ensure_ascii=False),
            encoding='utf-8')
        detection = fork.detect_forks(adir)
        self.assertTrue(detection['goal_fork']['active'])
        section = fork.render_fork_section(adir, detection)
        self.assertIn('目标分叉已冻结注入', section)
        self.assertNotIn('（无分叉，无待裁项）', section)


if __name__ == '__main__':
    import unittest
    unittest.main()


class TestFingerprintCache(V3Base):
    """snapshot_v2 incremental cache (v2-boundaries 3.6.5, carried over).

    The cache file lives at <store>/state/fingerprint-cache.json and
    the excludes/threshold knobs come from <store>/state/config.json
    (load_config), not the outer config.json the fuse reads.
    """

    def test_second_snapshot_hits_cache_and_matches_first(self):
        root = self.init()
        store = self.store_dir(root)
        first = flow.snapshot_v2(root, store)
        self.assertTrue(first['timing']['first_snapshot'])
        self.assertGreater(first['timing']['cache_misses'], 0)
        self.assertTrue((store / 'state' / 'fingerprint-cache.json').is_file())
        second = flow.snapshot_v2(root, store)
        self.assertFalse(second['timing']['first_snapshot'])
        self.assertGreater(second['timing']['cache_hits'], 0)
        self.assertEqual(first['code'], second['code'])

    def test_excludes_skip_paths_and_report_them(self):
        root = self.init()
        store = self.store_dir(root)
        self.change(root, 'vendor/big.bin', 'x\n')
        (store / 'state' / 'config.json').write_text(
            json.dumps({'fingerprint_excludes': ['vendor/']}),
            encoding='utf-8')
        snap = flow.snapshot_v2(root, store)
        self.assertIn('vendor/big.bin', snap['excluded'])
        self.assertNotIn('vendor/big.bin', snap['code'])

    def test_threshold_zero_reports_exceeded(self):
        root = self.init()
        snap = flow.snapshot_v2(root, self.store_dir(root), threshold_s=0)
        self.assertTrue(snap['timing']['exceeded'])
        self.assertEqual(0, snap['timing']['threshold_s'])

    def test_fingerprint_cli_prints_timing_line(self):
        root = self.init()
        code, out = self.cli('--root', root, 'fingerprint',
                               '--agent', 'codex')
        self.assertEqual(0, code)
        self.assertIn('指纹耗时', out)
