"""Volume-3 slice 4 (plan part 03, step 3.4): cost measurement, tuning
procedure, and the cost fuse.

Style follows the existing suites: real temporary directories, real Git
repos, PROJECT_RELAY_HOME redirected, no network, no real
~/.project-relay/. Time-dependent rules (two-week tuning windows, weekly
fuse windows) are tested with explicit at_epoch injection, never by
waiting: the task's real two-week dogfooding run is a later step, and no
test here fabricates it.
"""

import importlib.util
import json
import os
from pathlib import Path
import time
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SKILL = Path(__file__).resolve().parents[1]
BASE_EPOCH = 1800000000  # fixed anchor for synthetic rows
DAY = 86400


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SKILL / 'scripts' / (name + '.py'))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


relay = _load('relay')
flow = _load('relay_flow')
stats = _load('relay_stats')
sync = _load('relay_sync')
StatsRelayError = stats.RelayError


def _append_row(store_dir, filename, row):
    # Stats live outside the store tree (see relay_stats docstring);
    # tests write through the module's own location rule, never a
    # hardcoded path.
    path = stats._stats_dir(store_dir) / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'a', encoding='utf-8', newline='') as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + '\n')


def _sum_row(epoch, llm, measured=True, trigger='volume', source='manual'):
    return {'source': source, 'trigger': trigger, 'entry_count': 1,
            'entry_range': [1, 1], 'new_raw_tokens_est': 10,
            'old_summary_tokens_est': 10, 'llm_calls': 1 if llm else 0,
            'llm_in_tokens': llm, 'llm_out_tokens': 0, 'llm_tokens': llm,
            'llm_measured': measured, 'assembled_chars': 10,
            'semantic_chars': 10 if llm else 0, 'archive_delta_tokens': 5,
            'noop': False, 'degraded': False, 'branched': False,
            'fused': False, 'at': '2026-01-01T00:00:00+00:00',
            'at_epoch': epoch}


def _hand_row(epoch, total, prev_llm, injection):
    return {'injection_index_tokens': injection // 2,
            'injection_timeline_tokens': injection // 2,
            'injection_full_tokens': 0, 'injection_total_tokens': injection,
            'injection_estimated': True, 'reconcile': {},
            'reconcile_total': 0, 'unrecorded_proxy': 0.0,
            'catchup_summaries': 0, 'prev_summarize_llm_tokens': prev_llm,
            'total_cost_tokens': total,
            'at': '2026-01-01T00:00:00+00:00', 'at_epoch': epoch}


class StatsBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='relay-stats-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = self.root / 'store'
        self._env = mock.patch.dict(os.environ, {
            'PROJECT_RELAY_HOME': str(self.root),
            'GIT_AUTHOR_NAME': 'Tester',
            'GIT_AUTHOR_EMAIL': 'test@example.invalid',
            'GIT_COMMITTER_NAME': 'Tester',
            'GIT_COMMITTER_EMAIL': 'test@example.invalid',
        })
        self._env.start()
        self.addCleanup(self._env.stop)

    def git(self, root, *args):
        return subprocess.run(['git', '-C', str(root), *args],
                              check=True, capture_output=True)

    def init(self, name='proj'):
        root = self.root / name
        root.mkdir(parents=True, exist_ok=True)
        self.git(root, 'init', '-b', 'main')
        self.git(root, 'config', 'user.name', 'Tester')
        self.git(root, 'config', 'user.email', 'test@example.invalid')
        self.git(root, 'config', 'commit.gpgsign', 'false')
        (root / 'app.py').write_bytes(b'value = 1\n')
        self.git(root, 'add', '.')
        self.git(root, 'commit', '-m', 'baseline')
        relay.init(self.store, root)
        return root

    def store_dir(self, root):
        return relay.resolve_store_dir(self.store, root)[1]

    def capture(self, repo, message, session='s1'):
        return flow.capture(self.store, repo, tool='codex', session=session,
                            message=message)


class TestCaptureRows(StatsBase):
    def test_capture_row_records_entries_changed_duration(self):
        repo = self.init()
        self.capture(repo, 'first')
        rows = stats.capture_rows(self.store_dir(repo))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row['entries'], 1)
        self.assertEqual(row['changed_files'], 1)
        self.assertEqual(row['tool'], 'codex')
        self.assertEqual(row['session'], 's1')
        self.assertGreaterEqual(row['duration_s'], 0)
        self.assertFalse(row['deduped'])

    def test_dedup_capture_row_records_zero_entries(self):
        repo = self.init()
        self.capture(repo, 'same')
        self.capture(repo, 'same')
        rows = stats.capture_rows(self.store_dir(repo))
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[1]['deduped'])
        self.assertEqual(rows[1]['entries'], 0)


class TestSummarizeRows(StatsBase):
    def test_assembled_summarize_row_is_measured_zero_llm(self):
        repo = self.init()
        self.capture(repo, 'work')
        flow.summarize(self.store, repo, source='manual', trigger='volume')
        rows = stats.summarize_rows(self.store_dir(repo))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row['trigger'], 'volume')
        self.assertEqual(row['llm_calls'], 0)
        self.assertEqual(row['llm_tokens'], 0)
        self.assertTrue(row['llm_measured'])
        self.assertEqual(row['semantic_chars'], 0)
        self.assertGreater(row['assembled_chars'], 0)
        self.assertEqual(row['entry_count'], 1)

    def test_semantic_summarize_row_is_estimated(self):
        repo = self.init()
        self.capture(repo, 'work')
        flow.summarize(self.store, repo, source='manual',
                       semantic_text='## 已验证\n- 做了 X（验证：cap-0001）\n')
        row = stats.summarize_rows(self.store_dir(repo))[0]
        self.assertEqual(row['llm_calls'], 1)
        self.assertFalse(row['llm_measured'])
        self.assertGreater(row['llm_tokens'], 0)
        self.assertGreater(row['semantic_chars'], 0)

    def test_llm_usage_summarize_row_is_measured(self):
        repo = self.init()
        self.capture(repo, 'work')
        flow.summarize(self.store, repo, source='manual',
                       semantic_text='## 已验证\n- 做了 X（验证：cap-0001）\n',
                       llm_usage={'calls': 2, 'input_tokens': 100,
                                  'output_tokens': 50})
        row = stats.summarize_rows(self.store_dir(repo))[0]
        self.assertEqual(row['llm_calls'], 2)
        self.assertEqual(row['llm_in_tokens'], 100)
        self.assertEqual(row['llm_out_tokens'], 50)
        self.assertTrue(row['llm_measured'])

    def test_noop_summarize_row_flagged(self):
        repo = self.init()
        self.capture(repo, 'work')
        flow.summarize(self.store, repo, source='manual')
        flow.summarize(self.store, repo, source='manual')
        rows = stats.summarize_rows(self.store_dir(repo))
        self.assertTrue(rows[-1]['noop'])


class TestHandoffRows(StatsBase):
    def test_resume_row_layers_and_total(self):
        repo = self.init()
        self.capture(repo, 'work')
        flow.summarize(self.store, repo, source='manual')
        result = flow.resume(self.store, repo, tool='codex')
        self.assertFalse(result['no_archive'])
        rows = stats.handoff_rows(self.store_dir(repo))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertGreater(row['injection_index_tokens'], 0)
        self.assertGreater(row['injection_total_tokens'], 0)
        self.assertEqual(row['catchup_summaries'], 0)
        self.assertEqual(row['total_cost_tokens'],
                         row['prev_summarize_llm_tokens']
                         + row['injection_total_tokens'])
        self.assertTrue(row['injection_estimated'])

    def test_resume_catchup_counts(self):
        repo = self.init()
        self.capture(repo, 'work', session='s1')
        flow.summarize(self.store, repo, source='manual')
        (repo / 'app.py').write_bytes(b'value = 2\n')
        self.capture(repo, 'more work', session='s2')
        flow.resume(self.store, repo, tool='codex')
        row = stats.handoff_rows(self.store_dir(repo))[-1]
        self.assertEqual(row['catchup_summaries'], 1)

    def test_resume_reconcile_counts_changed_files(self):
        repo = self.init()
        self.capture(repo, 'work')
        flow.summarize(self.store, repo, source='manual')
        (repo / 'app.py').write_bytes(b'value = 2\n')
        flow.resume(self.store, repo, tool='codex')
        row = stats.handoff_rows(self.store_dir(repo))[-1]
        self.assertGreaterEqual(row['reconcile']['changed_files'], 1)
        self.assertGreater(row['unrecorded_proxy'], 0)


class TestReport(StatsBase):
    def _seed(self, store_dir):
        now = BASE_EPOCH
        for i, total in enumerate(range(100, 2100, 100)):
            _append_row(store_dir, 'handoff.jsonl',
                        _hand_row(now - i * DAY, total, 0, total))
        specs = [('volume', 100, True), ('volume', 200, True),
                 ('volume', 300, False), ('milestone', 400, True),
                 ('time', 500, False), ('manual', 600, True)]
        for trigger, llm, measured in specs:
            _append_row(store_dir, 'summarize.jsonl',
                        _sum_row(now - DAY, llm, measured, trigger))
        return now

    def test_report_answers_three_questions(self):
        repo = self.init()
        sd = self.store_dir(repo)
        now = self._seed(sd)
        rep = stats.build_report(sd, weeks=4, now_epoch=now)
        self.assertEqual(rep['three_questions']['avg_tokens_per_handoff'], 1050.0)
        # handoff llm 0, injection sum 21000 -> share 0
        self.assertEqual(rep['three_questions']['llm_summarize_share'], 0)
        self.assertEqual(rep['three_questions']['most_frequent_trigger'], 'volume')
        # measured 100+200+400+600, estimated 300+500, never blended
        self.assertEqual(rep['summarize']['llm_tokens_measured'], 1300)
        self.assertEqual(rep['summarize']['llm_tokens_estimated'], 800)
        self.assertEqual(rep['summarize']['llm_tokens_mean'], 350.0)
        self.assertEqual(rep['triggers']['volume']['count'], 3)
        text = stats.render_report(rep)
        self.assertIn('三个问题', text)
        self.assertIn('1050', text)

    def test_p95_nearest_rank(self):
        self.assertEqual(stats._p95(list(range(100, 2100, 100))), 1900)
        self.assertEqual(stats._p95([]), 0)
        self.assertEqual(stats._p95([7]), 7)

    def test_suggestions_time_never_fired(self):
        repo = self.init()
        sd = self.store_dir(repo)
        now = BASE_EPOCH
        for _ in range(3):
            _append_row(sd, 'summarize.jsonl',
                        _sum_row(now - DAY, 100, True, 'volume'))
        rep = stats.build_report(sd, weeks=4, now_epoch=now)
        self.assertTrue(any('时间阈值可放宽' in s for s in rep['suggestions']))

    def test_empty_report_says_no_data(self):
        repo = self.init()
        rep = stats.build_report(self.store_dir(repo), weeks=4,
                                 now_epoch=BASE_EPOCH)
        self.assertIn('暂无汇总数据', rep['suggestions'][0])


class TestTuning(StatsBase):
    def test_guardrail_rejects_over_50_percent(self):
        repo = self.init()
        sd = self.store_dir(repo)
        with self.assertRaises(StatsRelayError):
            stats.apply_tuning(sd, 'volume_files', 16)
        row = stats.apply_tuning(sd, 'volume_files', 15)
        self.assertEqual((row['old'], row['new']), (10, 15))
        with self.assertRaises(StatsRelayError):
            stats.apply_tuning(sd, 'volume_files', 23)
        row = stats.apply_tuning(sd, 'volume_files', 22)
        self.assertEqual(row['new'], 22)

    def test_one_knob_per_call_and_ranges(self):
        repo = self.init()
        sd = self.store_dir(repo)
        r1 = stats.apply_tuning(sd, 'volume_files', 12)
        r2 = stats.apply_tuning(sd, 'time_days', 10)
        self.assertEqual(r1['knob'], 'volume_files')
        self.assertEqual(r2['knob'], 'time_days')
        self.assertEqual(len(stats.tuning_rows(sd)), 2)
        with self.assertRaises(StatsRelayError):
            stats.apply_tuning(sd, 'inject_budget', 400)
        with self.assertRaises(StatsRelayError):
            stats.apply_tuning(sd, 'inject_budget', 9000)
        row = stats.apply_tuning(sd, 'inject_budget', 3000)
        self.assertEqual(row['new'], 3000)

    def test_precompress_cap_first_set_exempt_then_guarded(self):
        repo = self.init()
        sd = self.store_dir(repo)
        row = stats.apply_tuning(sd, 'precompress_sample_cap', 5)
        self.assertIsNone(row['old'])
        self.assertEqual(row['new'], 5)
        with self.assertRaises(StatsRelayError):
            stats.apply_tuning(sd, 'precompress_sample_cap', 8)
        row = stats.apply_tuning(sd, 'precompress_sample_cap', 7)
        self.assertEqual(row['new'], 7)

    def test_evaluate_gate_and_improvement(self):
        repo = self.init()
        sd = self.store_dir(repo)
        _append_row(sd, 'handoff.jsonl',
                    _hand_row(BASE_EPOCH - 7 * DAY, 1000, 0, 1000))
        stats.apply_tuning(sd, 'volume_files', 12, now_epoch=BASE_EPOCH)
        res = stats.evaluate_tuning(sd, now_epoch=BASE_EPOCH)
        self.assertFalse(res['evaluated'])
        _append_row(sd, 'handoff.jsonl',
                    _hand_row(BASE_EPOCH + 7 * DAY, 500, 0, 500))
        res = stats.evaluate_tuning(sd, now_epoch=BASE_EPOCH, force=True)
        self.assertTrue(res['evaluated'])
        self.assertTrue(res['improved'])
        self.assertEqual(res['after_mean'], 500.0)

    def test_two_no_improvements_roll_back_and_write_lesson(self):
        repo = self.init()
        sd = self.store_dir(repo)
        _append_row(sd, 'handoff.jsonl',
                    _hand_row(BASE_EPOCH - 7 * DAY, 1000, 0, 1000))
        stats.apply_tuning(sd, 'volume_files', 12, now_epoch=BASE_EPOCH)
        _append_row(sd, 'handoff.jsonl',
                    _hand_row(BASE_EPOCH + 7 * DAY, 1000, 0, 1000))
        res = stats.evaluate_tuning(sd, now_epoch=BASE_EPOCH, force=True)
        self.assertTrue(res['evaluated'])
        self.assertFalse(res['improved'])
        self.assertFalse(res['rolled_back'])
        stats.apply_tuning(sd, 'volume_files', 14,
                           now_epoch=BASE_EPOCH + 15 * DAY)
        _append_row(sd, 'handoff.jsonl',
                    _hand_row(BASE_EPOCH + 22 * DAY, 1000, 0, 1000))
        res = stats.evaluate_tuning(sd, now_epoch=BASE_EPOCH + 15 * DAY,
                                    force=True)
        self.assertTrue(res['rolled_back'])
        self.assertEqual(res['stable'], 10)
        cfg, _ = stats._read_config(sd)
        self.assertEqual(cfg['threshold_files'], 10)
        lessons = (sd / 'archive' / 'lessons.md').read_text(encoding='utf-8')
        self.assertIn('调优回退', lessons)
        self.assertIn(res['lesson'], lessons)
        present, mismatches = relay.verify_checksum_manifest(sd)
        self.assertTrue(present)
        self.assertEqual(mismatches, [])


class TestFuse(StatsBase):
    def _spike(self, sd, prev_total, week_total):
        # Fuse evaluation in production uses real time, so spike rows
        # must sit in real rolling weeks, not at the fixed BASE_EPOCH.
        now = time.time()
        _append_row(sd, 'summarize.jsonl',
                    _sum_row(now - 10 * DAY, prev_total, True, 'volume'))
        _append_row(sd, 'summarize.jsonl',
                    _sum_row(now - DAY, week_total, True, 'volume'))
        return now

    def test_floor_blocks_small_spike(self):
        repo = self.init()
        sd = self.store_dir(repo)
        now = self._spike(sd, 1000, 4000)
        self.assertFalse(stats.evaluate_fuse(sd, now_epoch=now)['tripped'])

    def test_spike_trips(self):
        repo = self.init()
        sd = self.store_dir(repo)
        now = self._spike(sd, 1000, 6000)
        state = stats.evaluate_fuse(sd, now_epoch=now)
        self.assertTrue(state['tripped'])
        self.assertIn('3 倍', state['reason'])

    def test_fused_summarize_drops_llm_text_keeps_auto(self):
        repo = self.init()
        sd = self.store_dir(repo)
        self._spike(sd, 1000, 6000)
        self.capture(repo, 'work')
        flow.summarize(self.store, repo, source='manual',
                       semantic_text='## 已验证\n- 做了 X（验证：cap-0001）\n')
        row = stats.summarize_rows(sd)[-1]
        self.assertTrue(row['fused'])
        self.assertEqual(row['semantic_chars'], 0)
        self.assertGreater(row['assembled_chars'], 0)
        self.capture(repo, 'more', session='s2')
        (repo / 'app.py').write_bytes(b'value = 2\n')
        flow.summarize(self.store, repo, source='manual',
                       semantic_text='## 已验证\n- 做了 Y（验证：cap-0002）\n',
                       semantic_auto=True)
        row = stats.summarize_rows(sd)[-1]
        # Auto text is tool-generated (zero model calls), so it counts
        # as assembled, not LLM -- but it must still reach the archive,
        # proving the fuse did not drop it.
        self.assertEqual(row['semantic_chars'], 0)
        state = (sd / 'archive' / 'state.md').read_text(encoding='utf-8')
        self.assertIn('做了 Y', state)

    def test_resume_banner_shows_fuse_alert(self):
        repo = self.init()
        sd = self.store_dir(repo)
        self._spike(sd, 1000, 6000)
        result = flow.resume(self.store, repo, tool='codex')
        self.assertIn('保险丝', result['banner'])

    def test_monthly_cap_alerts_without_degrading(self):
        repo = self.init()
        sd = self.store_dir(repo)
        cfg, _ = stats._read_config(sd)
        cfg['monthly_llm_token_cap'] = 100
        stats._write_config(sd, cfg)
        # Same instant as now: the month bucket matches by construction
        # even across a month boundary.
        now = time.time()
        _append_row(sd, 'summarize.jsonl',
                    _sum_row(now, 200, True, 'volume'))
        state = stats.evaluate_fuse(sd, now_epoch=now)
        self.assertTrue(state['cap_alert'])
        self.assertFalse(state['tripped'])
        self.capture(repo, 'work')
        flow.summarize(self.store, repo, source='manual',
                       semantic_text='## 已验证\n- 做了 X（验证：cap-0001）\n')
        row = stats.summarize_rows(sd)[-1]
        self.assertFalse(row['fused'])
        self.assertGreater(row['semantic_chars'], 0)


class TestPrecompressCap(StatsBase):
    def test_cap_samples_latest_entry_only(self):
        repo = self.init()
        self.capture(repo, 'first', session='s1')
        (repo / 'app.py').write_bytes(b'value = 2\n')
        (repo / 'new.py').write_bytes(b'x = 1\n')
        self.capture(repo, 'second', session='s2')
        sd = self.store_dir(repo)
        doc = flow.precompress(self.store, repo)
        self.assertEqual(doc['counts']['file_new'], 2)
        self.assertEqual(doc['counts']['file_edit'], 1)
        cfg = flow.load_config(sd)
        cfg['precompress_sample_cap'] = 1
        flow.save_config(sd, cfg)
        doc = flow.precompress(self.store, repo)
        self.assertEqual(doc['counts']['file_new'], 2)
        self.assertEqual(doc['counts']['file_edit'], 0)


class TestPayloadExclusion(StatsBase):
    def test_stats_never_ships_in_bundles(self):
        repo = self.init()
        self.capture(repo, 'work')
        flow.summarize(self.store, repo, source='manual')
        sd = self.store_dir(repo)
        # Stats live outside the store tree at all: no stats dir inside
        # the store, so no payload filter is even needed to keep them
        # local -- and the bundle manifest confirms it.
        self.assertFalse((sd / 'stats').exists())
        dest = self.root / 'bundle'
        manifest = sync.export_bundle(self.store, [repo], dest, 'd', 'id-d')
        self.assertFalse(any('/stats/' in f for f in manifest['files']))


class TestStatsCLI(StatsBase):
    def _cli(self, repo, *args):
        env = dict(os.environ)
        cmd = [sys.executable, str(SKILL / 'scripts' / 'relay.py'),
               '--root', str(repo), '--store-root', str(self.store), *args]
        return subprocess.run(cmd, capture_output=True, text=True,
                              encoding='utf-8', env=env)

    def test_cli_report_and_guardrail(self):
        repo = self.init()
        self.capture(repo, 'work')
        flow.summarize(self.store, repo, source='manual')
        out = self._cli(repo, 'stats')
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn('三个问题', out.stdout)
        bad = self._cli(repo, 'stats', '--tune-knob', 'volume_files',
                        '--tune-value', '16')
        self.assertEqual(bad.returncode, 1)
        self.assertIn('护栏', bad.stderr)
        good = self._cli(repo, 'stats', '--tune-knob', 'volume_files',
                         '--tune-value', '15')
        self.assertEqual(good.returncode, 0, good.stderr)
        self.assertIn('10 → 15', good.stdout)


if __name__ == '__main__':
    unittest.main()
