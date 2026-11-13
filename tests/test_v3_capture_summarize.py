"""v3 capture / summarize / resume core (batch 18).

Covers flow.capture (dedup, seq, entry file + header), flow.enforce
(block before the semantic section is filled, pass after),
flow.summarize (record shape, queue transition, managed memory block,
changes.md section, agent log, noop), per-agent isolation,
flow.queue_status shape, and flow.resume (five sections, cross-agent
fact check, goal alignment, outer state.md recompute).

Real temporary Git repos, relay.init(root) only, no registry, no
global store. Run: python -m unittest tests.test_v3_capture_summarize -v
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from v3_helpers import V3Base, relay, flow, RelayError

SEMANTIC = (
    '## 已验证\n- 演示能跑起来，验证：跑了演示。\n'
    '## 决定\n- 继续做演示，来源：用户。\n'
    '## 待办\n- 把演示跑通，Owner：用户，验证：演示能跑。\n'
)


class TestCapture(V3Base):
    def test_first_capture_returns_seq_path_and_header(self):
        root = self.init()
        info = self.capture(root, 'codex')
        self.assertEqual(1, info['seq'])
        self.assertFalse(info['deduped'])
        path = Path(info['path'])
        self.assertEqual('cap-0001.md', path.name)
        self.assertEqual(self.adir(root, 'codex') / 'raw' / 'cap-0001.md', path)
        head, _body = flow._parse_entry(path)
        self.assertEqual(1, head['version'])
        self.assertEqual(1, head['seq'])
        self.assertEqual('codex', head['agent'])
        self.assertEqual('pending', head['queue'])
        self.assertTrue(head['fact_hash'])
        self.assertEqual(1, len(self.entries(root, 'codex')))

    def test_second_identical_capture_dedups_same_seq(self):
        root = self.init()
        first = self.capture(root, 'codex')
        second = self.capture(root, 'codex')
        self.assertEqual(first['seq'], second['seq'])
        self.assertTrue(second['deduped'])
        self.assertEqual(first['path'], second['path'])
        self.assertEqual(1, len(self.entries(root, 'codex')))
        # A deduped capture writes no second log line.
        rows = relay.read_agent_log(self.store_dir(root), 'codex')
        self.assertEqual(1, len(rows))
        self.assertEqual('capture', rows[0]['event'])

    def test_change_then_capture_gets_next_seq(self):
        root = self.init()
        first = self.capture(root, 'codex')
        self.change(root, 'app.py', 'value = 2\n')
        second = self.capture(root, 'codex')
        self.assertEqual(first['seq'] + 1, second['seq'])
        self.assertFalse(second['deduped'])
        self.assertEqual(2, len(self.entries(root, 'codex')))
        self.assertEqual(
            [1, 2], [e['header']['seq'] for e in self.entries(root, 'codex')])
        # Same session + same facts still dedups after the change.
        third = self.capture(root, 'codex')
        self.assertEqual(2, third['seq'])
        self.assertTrue(third['deduped'])


class TestEnforce(V3Base):
    def test_enforce_passes_with_no_entries(self):
        root = self.init()
        self.assertEqual('pass', flow.enforce(root, 'codex')['action'])

    def test_enforce_blocks_until_semantic_filled(self):
        root = self.init()
        self.capture(root, 'codex')
        blocked = flow.enforce(root, 'codex')
        self.assertEqual('block', blocked['action'])
        self.assertEqual(1, blocked['entry_seq'])
        self.assertIn('cap-0001', blocked['instruction'])
        self.fill_semantic(root, 'codex')
        passed = flow.enforce(root, 'codex')
        self.assertEqual('pass', passed['action'])


class TestSummarize(V3Base):
    def _summarized(self, root, agent='codex'):
        self.capture(root, agent)
        return self.summarize(root, agent, semantic_text=SEMANTIC)

    def test_summarize_record_shape_and_queue_transition(self):
        root = self.init()
        record = self._summarized(root)
        self.assertEqual(1, record['seq'])
        self.assertEqual(1, record['entry_count'])
        self.assertEqual([1, 1], record['entry_range'])
        self.assertEqual('manual', record['source'])
        entries = self.entries(root, 'codex')
        self.assertEqual(1, len(entries))
        self.assertEqual('summarized', entries[0]['header']['queue'])

    def test_summarize_writes_managed_block_changes_and_log(self):
        root = self.init()
        before_log = relay.read_agent_log(self.store_dir(root), 'codex')
        record = self._summarized(root)
        adir = self.adir(root, 'codex')
        memory = (adir / 'memory.md').read_text(encoding='utf-8')
        self.assertIn(flow.STATE_BEGIN, memory)
        self.assertIn(flow.STATE_END, memory)
        self.assertIn('汇总 %d' % record['seq'], memory)
        changes = (adir / 'changes.md').read_text(encoding='utf-8')
        self.assertIn('## 汇总 %d' % record['seq'], changes)
        self.assertIn('覆盖条目：[1, 1]', changes)
        rows = relay.read_agent_log(self.store_dir(root), 'codex')
        # capture 写一行，summarize 再写一行。
        self.assertEqual(len(before_log) + 2, len(rows))
        self.assertEqual('capture', rows[-2]['event'])
        self.assertEqual('summarize', rows[-1]['event'])
        self.assertEqual('s%04d' % record['seq'], rows[-1]['detail'])

    def test_summarize_with_no_pending_is_noop(self):
        root = self.init()
        record = self._summarized(root)
        self.assertFalse(record.get('noop'))
        again = self.summarize(root, 'codex', semantic_text=SEMANTIC)
        self.assertTrue(again.get('noop'))
        # A noop writes no changes.md section and no log line. The
        # changes.md template prose itself mentions '## 汇总 N', so count
        # the real section header, not the bare phrase.
        changes = (self.adir(root, 'codex') / 'changes.md').read_text(
            encoding='utf-8')
        self.assertEqual(1, changes.count('## 汇总 1'))
        rows = relay.read_agent_log(self.store_dir(root), 'codex')
        self.assertEqual('summarize', rows[-1]['event'])

    def test_summarize_without_semantic_text_assembles(self):
        root = self.init()
        self.capture(root, 'codex')
        record = self.summarize(root, 'codex')
        self.assertEqual(1, record['entry_count'])
        self.assertEqual(0, record['model_calls'])
        memory = (self.adir(root, 'codex') / 'memory.md').read_text(
            encoding='utf-8')
        self.assertIn(flow.STATE_BEGIN, memory)


class TestIsolation(V3Base):
    def test_codex_capture_and_summarize_do_not_touch_claude_code(self):
        root = self.init()
        self.capture(root, 'codex')
        self.summarize(root, 'codex', semantic_text=SEMANTIC)
        self.assertEqual([], self.entries(root, 'claude-code'))
        other_memory = (self.adir(root, 'claude-code') / 'memory.md').read_text(
            encoding='utf-8')
        self.assertIn('正在做什么：未知', other_memory)
        self.assertNotIn('汇总 1', other_memory)
        other_changes = (self.adir(root, 'claude-code') / 'changes.md').read_text(
            encoding='utf-8')
        # Template prose mentions '## 汇总 N'; the real section is '## 汇总 1'.
        self.assertNotIn('## 汇总 1', other_changes)
        self.assertEqual([], relay.read_agent_log(
            self.store_dir(root), 'claude-code'))
        # And the other direction: a claude-code capture is its own seq 1.
        info = self.capture(root, 'claude-code')
        self.assertEqual(1, info['seq'])
        self.assertEqual(1, len(self.entries(root, 'claude-code')))
        self.assertEqual(1, len(self.entries(root, 'codex')))


class TestQueueStatus(V3Base):
    def test_queue_status_shape_for_one_agent(self):
        root = self.init()
        store = self.store_dir(root)
        empty = flow.queue_status(store, agent='codex')
        self.assertEqual({'pending': 0, 'summarizing': 0, 'summarized': 0,
                          'raw_mb': 0, 'oldest_pending': None,
                          'redacted_total': 0, 'stale_summarizing': [],
                          'rolled_back': [], 'over_retries': []}, empty)
        self.capture(root, 'codex')
        status = flow.queue_status(store, agent='codex')
        self.assertEqual(1, status['pending'])
        self.assertEqual(0, status['summarized'])
        self.assertEqual(0, status['summarizing'])
        self.assertIsInstance(status['raw_mb'], float)
        self.assertGreaterEqual(status['raw_mb'], 0)
        self.assertIsNotNone(status['oldest_pending'])
        self.assertIsInstance(status['redacted_total'], int)
        self.assertEqual([], status['stale_summarizing'])
        self.assertEqual([], status['rolled_back'])
        self.assertEqual([], status['over_retries'])
        self.summarize(root, 'codex', semantic_text=SEMANTIC)
        after = flow.queue_status(store, agent='codex')
        self.assertEqual(0, after['pending'])
        self.assertEqual(1, after['summarized'])

    def test_queue_status_all_agents_aggregates(self):
        root = self.init()
        store = self.store_dir(root)
        self.capture(root, 'codex')
        self.capture(root, 'workbuddy')
        total = flow.queue_status(store)
        self.assertEqual(2, total['pending'])
        self.assertEqual(0, total['summarized'])
        self.assertIsNotNone(total['oldest_pending'])


class TestResume(V3Base):
    def _set_fact(self, root, agent, key, value):
        path = self.adir(root, agent) / 'memory.md'
        lines = path.read_text(encoding='utf-8').splitlines()
        idx = lines.index('## 事实')
        lines[idx + 1:idx + 1] = ['- %s：%s' % (key, value)]
        path.write_text('\n'.join(lines) + '\n', encoding='utf-8')

    def test_resume_renders_five_sections(self):
        root = self.init()
        self.confirm_goal(root)
        self.capture(root, 'codex')
        self.summarize(root, 'codex', semantic_text=SEMANTIC)
        result = self.resume(root)
        self.assertFalse(result.get('no_archive'))
        self.assertEqual(5, len(result['sections']))
        text = self.resume_text(root)
        for marker in ('一段', '二段', '三段', '四段', '五段'):
            self.assertIn(marker, text)

    def test_resume_cross_agent_conflict_shows_in_text(self):
        root = self.init()
        self.capture(root, 'codex')
        self.summarize(root, 'codex', semantic_text=SEMANTIC)
        self.capture(root, 'claude-code')
        self.summarize(root, 'claude-code', semantic_text=SEMANTIC)
        self._set_fact(root, 'codex', '语言', 'Python')
        self._set_fact(root, 'claude-code', '语言', 'Go')
        check = flow.cross_agent_check(self.store_dir(root))
        self.assertEqual(1, len(check['conflicts']))
        self.assertEqual('语言', check['conflicts'][0]['key'])
        text = self.resume_text(root)
        self.assertIn('跨 Agent 核对', text)
        self.assertIn('语言', text)
        self.assertIn('Python', text)
        self.assertIn('Go', text)
        self.assertIn('矛盾', text)

    def test_resume_cross_agent_agreement_reports_no_conflict(self):
        root = self.init()
        self._set_fact(root, 'codex', '语言', 'Python')
        self._set_fact(root, 'claude-code', '语言', 'Python')
        text = self.resume_text(root)
        self.assertIn('各家记忆无矛盾', text)

    def test_resume_goal_alignment_when_hashes_match(self):
        root = self.init()
        self.confirm_goal(root)
        store = self.store_dir(root)
        for agent in ('codex', 'claude-code', 'workbuddy'):
            relay.write_agent_goal(store, agent, '负责自己那份。\n')
        text = self.resume_text(root)
        self.assertIn('目标对齐', text)
        self.assertIn('各家目标都与总目标一致', text)

    def test_resume_recomputes_outer_state_md(self):
        root = self.init()
        self.confirm_goal(root)
        self.capture(root, 'codex')
        self.summarize(root, 'codex', semantic_text=SEMANTIC)
        store = self.store_dir(root)
        before = (store / 'state.md').read_text(encoding='utf-8')
        self.resume(root)
        after = (store / 'state.md').read_text(encoding='utf-8')
        self.assertNotEqual(before, after)
        self.assertIn('由 resume 从各家记忆现算', after)
        self.assertIn(flow.STATE_BEGIN, after)
        self.assertIn('codex', after)


class TestFactsEndToEnd(V3Base):
    """Facts travel entry -> semantic file -> summarize -> memory.md ->
    resume cross-check. This is the path the v3 cross-agent check
    actually reads; bare summarize (no semantic file) only assembles
    counts and writes no facts, so without this test the check could
    go vacuous without anyone noticing (caught by the batch-18
    hand-run e2e, 2026-11-13)."""

    def _semantic(self, fact):
        return ('## 已验证\n- 本轮改动已验证。\n'
                '## 事实\n- 接口版本：%s\n' % fact)

    def test_facts_flow_to_memory_and_resume_flags_conflict(self):
        root = self.init()
        self.confirm_goal(root)
        self.change(root, 'app.py', 'value = 2\n')
        self.capture(root, 'codex')
        self.capture(root, 'claude-code')
        self.fill_semantic(root, 'codex')
        self.fill_semantic(root, 'claude-code')
        self.summarize(root, 'codex',
                       semantic_text=self._semantic('v2'))
        self.summarize(root, 'claude-code',
                       semantic_text=self._semantic('v1'))
        codex_mem = (self.adir(root, 'codex') / 'memory.md').read_text(
            encoding='utf-8')
        claude_mem = (self.adir(root, 'claude-code') / 'memory.md').read_text(
            encoding='utf-8')
        self.assertIn('- 接口版本：v2', codex_mem)
        self.assertIn('- 接口版本：v1', claude_mem)
        text = self.resume_text(root)
        self.assertIn('跨 Agent 核对', text)
        self.assertIn('接口版本', text)
        self.assertIn('v1', text)
        self.assertIn('v2', text)

    def test_bare_summarize_writes_no_facts(self):
        # The auto default only assembles counts: no semantic file,
        # no facts in memory.md, and the cross-check stays quiet.
        root = self.init()
        self.confirm_goal(root)
        self.change(root, 'app.py', 'value = 2\n')
        self.capture(root, 'codex')
        self.fill_semantic(root, 'codex')
        self.summarize(root, 'codex')
        mem = (self.adir(root, 'codex') / 'memory.md').read_text(
            encoding='utf-8')
        facts = flow.parse_memory_facts(mem)
        self.assertEqual({}, facts)
        self.assertIn('各家记忆无矛盾', self.resume_text(root))


if __name__ == '__main__':
    import unittest
    unittest.main()
