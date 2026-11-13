"""v3 merge tests (batch 18): sync.merge_project unit rules, called
directly (no zip involved), plus health_check and _is_payload_file.

Fixed in this batch (2026-11-13, branch slim-v3): the missing
module-level _TEMPLATE_STATE_VALUES singleton in scripts/relay_sync.py
used to raise NameError on any agent memory.md merge, so the
managed-block + ## 事实 merge inside merge_project was unreachable.
The test below used to assert that NameError; it now asserts the
intended behaviour through merge_project (the pure helper
_merge_facts_section is still covered directly as well).

Everything else in this file runs against real behaviour: entry
union/conflict, ID-block merges, agent-goal hash choice, outer
state/index never adopted, stack template rule, changes/log/receipts/
last-summary/summaries unions, discards + quarantine, health_check,
and the payload filter.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from v3_helpers import V3Base, relay, flow, sync, RelayError

SyncError = sync.RelayError
SOURCE = {'device': 'peer', 'device_id': 'x', 'seq': None}


class MergeBase(V3Base):
    def pair(self):
        """Two repos, A (local) and B (peer): same confirmed goal, one
        capture per agent each. A and B have different git histories,
        so their entries have different fact hashes (different keys)."""
        a = self.init('proj-a')
        self.confirm_goal(a)
        self.change(a, 'app.py', 'value = 2\n')
        self.capture(a, 'codex')
        self.capture(a, 'claude-code')
        b = self.init('proj-b')
        self.confirm_goal(b)
        self.change(b, 'app.py', 'value = 3\n')
        self.capture(b, 'codex')
        self.capture(b, 'claude-code')
        return a, b

    def incoming(self, root_b, drop_memory=True):
        """B's shippable payload as merge input. memory.md is dropped
        by default to keep the other merge rules isolated; the memory
        path has its own test below."""
        payload = sync._collect_payload(self.store_dir(root_b))
        if drop_memory:
            payload = {k: v for k, v in payload.items()
                       if not k.endswith('/memory.md')}
        return payload

    def store_text(self, root, rel):
        return (self.store_dir(root) / rel).read_text(encoding='utf-8')

    def write_store(self, root, rel, text):
        p = self.store_dir(root) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding='utf-8')
        return p

    def entry_bytes(self, root, agent='codex', seq=1):
        return (self.adir(root, agent) / 'raw' / ('cap-%04d.md' % seq)).read_bytes()

    def split_entry(self, data):
        text = data.decode('utf-8')
        head, body = text.split(sync.BODY_DELIM, 1)
        return json.loads(head), body


class TestEntryUnion(MergeBase):
    def test_self_merge_skips_everything(self):
        a, _b = self.pair()
        result = sync.merge_project(self.store_dir(a),
                                    self.incoming(a), SOURCE)
        self.assertEqual(2, result['counts']['entries_skipped'])
        self.assertEqual(0, result['counts']['entries_added'])
        self.assertEqual(0, result['counts']['entries_conflicted'])
        self.assertEqual({}, result['merged_files'])
        self.assertEqual('skip', result['goal_action'])

    def test_peer_entries_land_in_own_agent_raw_with_next_seq(self):
        a, b = self.pair()
        result = sync.merge_project(self.store_dir(a),
                                    self.incoming(b), SOURCE)
        self.assertEqual(2, result['counts']['entries_added'])
        merged = result['merged_files']
        self.assertIn('agents/codex/raw/cap-0002.md', merged)
        self.assertIn('agents/claude-code/raw/cap-0002.md', merged)
        # Nothing lands in the other agent's raw/ and nothing is
        # overwritten: cap-0001 is not in the merged set at all.
        self.assertNotIn('agents/codex/raw/cap-0001.md', merged)
        self.assertNotIn('agents/claude-code/raw/cap-0001.md', merged)
        header, _body = self.split_entry(merged['agents/codex/raw/cap-0002.md'])
        self.assertEqual(2, header['seq'])
        self.assertEqual('codex', header['agent'])

    def test_same_key_different_body_keeps_both_and_conflicts(self):
        a, _b = self.pair()
        header, body = self.split_entry(self.entry_bytes(a))
        other_body = body + '\n对方补了一句正文。\n'
        incoming = {'agents/codex/raw/cap-0001.md': (
            json.dumps(header, ensure_ascii=False, sort_keys=True, indent=2)
            + sync.BODY_DELIM + other_body).encode('utf-8')}
        result = sync.merge_project(self.store_dir(a), incoming, SOURCE)
        self.assertEqual(1, result['counts']['entries_conflicted'])
        self.assertEqual(0, result['counts']['entries_added'])
        self.assertEqual(0, result['counts']['entries_skipped'])
        self.assertIn('agents/codex/raw/cap-0002.md', result['merged_files'])
        new_header, new_body = self.split_entry(
            result['merged_files']['agents/codex/raw/cap-0002.md'])
        self.assertIn('merge_note', new_header)
        self.assertEqual(other_body, new_body)
        self.assertEqual(1, len(result['conflicts']))
        self.assertEqual('entry-same-key-different-body',
                         result['conflicts'][0]['rule'])
        # The local original is untouched by the merge preview.
        _h, old_body = self.split_entry(self.entry_bytes(a))
        self.assertEqual(body, old_body)


class TestBlockMerges(MergeBase):
    def test_decisions_lessons_backlog_blocks_union(self):
        a, b = self.pair()
        self.write_store(a, 'agents/codex/decisions.md',
                         self.store_text(a, 'agents/codex/decisions.md')
                         + '\n## D-001\n- 问题：本地问题\n- 选择：本地方案\n')
        self.write_store(a, 'backlog.md',
                         self.store_text(a, 'backlog.md')
                         + '\n## T-001\n- 事项：做演示\n- Owner：codex\n- 验证方式：演示能跑。\n')
        self.write_store(b, 'agents/codex/decisions.md',
                         self.store_text(b, 'agents/codex/decisions.md')
                         + '\n## D-001\n- 问题：对方问题\n- 选择：对方方案\n')
        self.write_store(b, 'agents/codex/lessons.md',
                         self.store_text(b, 'agents/codex/lessons.md')
                         + '\n## L-001\n- 教训：对方教训\n')
        # Same backlog item, different body -> conflict, local kept.
        # A different item -> appended with a fresh local id.
        self.write_store(b, 'backlog.md',
                         self.store_text(b, 'backlog.md')
                         + '\n## T-001\n- 事项：做演示\n- Owner：claude-code\n- 验证方式：演示能跑。\n'
                         + '\n## T-002\n- 事项：写文档\n- Owner：codex\n- 验证方式：文档存在。\n')
        result = sync.merge_project(self.store_dir(a),
                                    self.incoming(b), SOURCE)
        # Actual count is 3, not 4: codex decisions +1, lessons +1,
        # backlog +1. The outer decisions.md was never given a peer
        # block in this test (both sides still hold the template), so
        # it contributes 0 -- the merge code is behaving correctly.
        self.assertEqual(3, result['counts']['blocks_added'])
        merged = result['merged_files']
        self.assertIn('对方问题', merged['agents/codex/decisions.md'].decode('utf-8'))
        self.assertIn('## D-002', merged['agents/codex/decisions.md'].decode('utf-8'))
        self.assertIn('对方教训', merged['agents/codex/lessons.md'].decode('utf-8'))
        backlog = merged['backlog.md'].decode('utf-8')
        self.assertIn('写文档', backlog)
        self.assertIn('## T-002', backlog)
        self.assertEqual(1, len([c for c in result['conflicts']
                                 if c.get('rule') == 'backlog-same-item-different-body']))
        conflict = [c for c in result['conflicts']
                    if c.get('rule') == 'backlog-same-item-different-body'][0]
        self.assertEqual('做演示', conflict['item'])
        self.assertEqual('backlog.md', conflict['file'])

    def test_memory_md_merges_managed_block_and_facts(self):
        # BUG-2 fixed: _TEMPLATE_STATE_VALUES is initialised, so
        # merge_project reaches the memory.md path -- managed-block
        # merge + ## 事实 key merge (incoming wins, noted).
        a, b = self.pair()
        self.write_store(a, 'agents/codex/memory.md',
                         self.store_text(a, 'agents/codex/memory.md')
                         .replace('## 事实', '## 事实\n- 框架：Django\n', 1))
        self.write_store(b, 'agents/codex/memory.md',
                         self.store_text(b, 'agents/codex/memory.md')
                         .replace('## 事实', '## 事实\n- 框架：Flask\n- 新键：值\n', 1))
        incoming = {'agents/codex/memory.md':
                    (self.adir(b, 'codex') / 'memory.md').read_bytes()}
        result = sync.merge_project(self.store_dir(a), incoming, SOURCE)
        merged = result['merged_files']['agents/codex/memory.md'].decode('utf-8')
        self.assertIn('- 框架：Flask', merged)
        self.assertIn('- 新键：值', merged)
        self.assertNotIn('Django', merged)
        self.assertTrue(any('事实' in (n or '') for n in result.get('notes', [])),
                        'facts win should be noted: %r' % (result.get('notes'),))

    def test_facts_section_merges_by_key_incoming_wins(self):
        # The pure helper behind the memory.md facts rule (reachable
        # even while BUG-2 blocks merge_project's memory path).
        local = ('# 记忆\n\n## 事实\n- 语言：Python\n- 框架：Django\n')
        incoming = ('# 记忆\n\n## 事实\n- 语言：Python\n- 框架：Flask\n- 新键：值\n')
        text, notes = sync._merge_facts_section(local, incoming)
        self.assertIn('- 框架：Flask', text)
        self.assertIn('- 新键：值', text)
        self.assertIn('- 语言：Python', text)
        self.assertEqual(1, len(notes))
        self.assertIn('框架', notes[0])
        self.assertIn('采纳对方值', notes[0])

    def test_changes_sections_union_by_number(self):
        a, b = self.pair()
        self.write_store(a, 'agents/codex/changes.md',
                         self.store_text(a, 'agents/codex/changes.md')
                         + '\n## 汇总 1\n本地汇总内容\n')
        self.write_store(b, 'agents/codex/changes.md',
                         self.store_text(b, 'agents/codex/changes.md')
                         + '\n## 汇总 1\n对方汇总内容\n'
                         + '\n## 汇总 2\n对方第二汇总\n')
        result = sync.merge_project(self.store_dir(a),
                                    self.incoming(b), SOURCE)
        merged = result['merged_files']['agents/codex/changes.md'].decode('utf-8')
        self.assertIn('本地汇总内容', merged)
        self.assertIn('## 汇总 2', merged)
        self.assertIn('对方第二汇总', merged)
        self.assertNotIn('对方汇总内容', merged)
        self.assertEqual(1, result['counts']['blocks_added'])
        self.assertTrue(any('汇总 1 两边正文不同' in n for n in result['notes']))

    def test_log_jsonl_union_by_line(self):
        a, b = self.pair()
        line_a = '{"at": "2026-11-13T10:00:00Z", "event": "a"}\n'
        line_b = '{"at": "2026-11-13T11:00:00Z", "event": "b"}\n'
        dup = '{"at": "2026-11-13T09:00:00Z", "event": "dup"}\n'
        for root, extra in ((a, line_a + dup), (b, line_b + dup)):
            p = self.adir(root, 'codex') / 'log.jsonl'
            p.write_text(p.read_text(encoding='utf-8') + extra, encoding='utf-8')
        result = sync.merge_project(self.store_dir(a),
                                    self.incoming(b), SOURCE)
        # Actual count is 3, not 1: log_lines_added accumulates across
        # ALL agents. codex contributes 2 (B's capture line, whose
        # timestamp differs from A's, plus line_b; dup is skipped) and
        # claude-code contributes 1 (its capture line, timestamp also
        # differs). The per-file assertions below pin the codex union.
        self.assertEqual(3, result['counts']['log_lines_added'])
        merged = result['merged_files']['agents/codex/log.jsonl'].decode('utf-8')
        self.assertIn(line_a.strip(), merged)
        self.assertIn(line_b.strip(), merged)
        self.assertEqual(1, merged.count('"event": "dup"'))
        self.assertLess(merged.index('09:00'), merged.index('10:00'))
        self.assertLess(merged.index('10:00'), merged.index('11:00'))


class TestGoalStackState(MergeBase):
    def test_agent_goal_aligned_incoming_is_adopted(self):
        a, b = self.pair()
        goal2 = '# 项目目标\n\n状态：已确认\n\n- 目标与交付物：换一个目标\n'
        # A: agent goal stamped under the old total goal, then the
        # total goal moves. B: same new total goal, agent goal stamped
        # under it. Incoming hash matches the current total -> adopt.
        relay.write_agent_goal(self.store_dir(a), 'codex', '负责后端。\n')
        self.confirm_goal(a, text=goal2)
        self.confirm_goal(b, text=goal2)
        relay.write_agent_goal(self.store_dir(b), 'codex', '负责后端，对方版。\n')
        incoming = {'agents/codex/goal.md':
                    (self.adir(b, 'codex') / 'goal.md').read_bytes(),
                    'goal.md': (self.store_dir(b) / 'goal.md').read_bytes()}
        result = sync.merge_project(self.store_dir(a), incoming, SOURCE)
        self.assertIn('agents/codex/goal.md', result['merged_files'])
        self.assertIn('负责后端，对方版。',
                      result['merged_files']['agents/codex/goal.md'].decode('utf-8'))
        self.assertTrue(any('采纳对方版' in n for n in result['notes']))

    def test_agent_goal_unaligned_incoming_is_kept_local(self):
        a, b = self.pair()
        goal2 = '# 项目目标\n\n状态：已确认\n\n- 目标与交付物：换一个目标\n'
        relay.write_agent_goal(self.store_dir(a), 'codex', '负责后端。\n')
        self.confirm_goal(a, text=goal2)
        # B still on the old total goal: its agent goal hash does not
        # match A's current total -> keep local.
        relay.write_agent_goal(self.store_dir(b), 'codex', '负责后端，对方版。\n')
        incoming = {'agents/codex/goal.md':
                    (self.adir(b, 'codex') / 'goal.md').read_bytes()}
        result = sync.merge_project(self.store_dir(a), incoming, SOURCE)
        self.assertNotIn('agents/codex/goal.md', result['merged_files'])
        self.assertTrue(any('未对齐当前总目标' in n for n in result['notes']))

    def test_outer_state_and_index_never_adopted(self):
        a, b = self.pair()
        incoming = {'state.md': '对方外层状态\n'.encode('utf-8'),
                    'index.md': '对方指路表\n'.encode('utf-8')}
        result = sync.merge_project(self.store_dir(a), incoming, SOURCE)
        self.assertNotIn('state.md', result['merged_files'])
        self.assertNotIn('index.md', result['merged_files'])
        self.assertTrue(any('忽略对方外层 state.md' in n for n in result['notes']))
        self.assertTrue(any('忽略对方指路表' in n for n in result['notes']))

    def test_stack_template_adopts_incoming(self):
        a, b = self.pair()
        relay.write_stack(self.store_dir(b), 'Python 3.12\n')
        incoming = {'stack.md': (self.store_dir(b) / 'stack.md').read_bytes()}
        result = sync.merge_project(self.store_dir(a), incoming, SOURCE)
        self.assertEqual(b'Python 3.12\n', result['merged_files']['stack.md'])

    def test_stack_non_template_keeps_local(self):
        a, b = self.pair()
        relay.write_stack(self.store_dir(a), '本地栈\n')
        relay.write_stack(self.store_dir(b), '对方栈\n')
        incoming = {'stack.md': (self.store_dir(b) / 'stack.md').read_bytes()}
        result = sync.merge_project(self.store_dir(a), incoming, SOURCE)
        self.assertNotIn('stack.md', result['merged_files'])
        self.assertTrue(any('stack.md 两边不同' in n for n in result['notes']))


class TestFilesAndSummaries(MergeBase):
    def test_receipts_union_renames_on_collision(self):
        a, _b = self.pair()
        store = self.store_dir(a)
        (store / 'receipts' / 'r1.json').write_bytes(b'{"v": 1}\n')
        (store / 'receipts' / 'r2.json').write_bytes(b'{"v": 1}\n')
        incoming = {'receipts/r1.json': b'{"v": 1}\n',
                    'receipts/r2.json': b'{"v": 2}\n'}
        result = sync.merge_project(store, incoming, SOURCE)
        self.assertEqual(1, result['counts']['files_added'])
        self.assertNotIn('receipts/r1.json', result['merged_files'])
        self.assertIn('receipts/r2-imported.json', result['merged_files'])
        self.assertEqual(b'{"v": 2}\n',
                         result['merged_files']['receipts/r2-imported.json'])

    def test_last_summary_higher_seq_wins(self):
        a, b = self.pair()
        self.write_store(a, 'agents/codex/state/last-summary.json',
                         '{"seq": 1, "x": "a"}')
        self.write_store(b, 'agents/codex/state/last-summary.json',
                         '{"seq": 3, "x": "b"}')
        result = sync.merge_project(self.store_dir(a),
                                    self.incoming(b), SOURCE)
        self.assertEqual(b'{"seq": 3, "x": "b"}',
                         result['merged_files']['agents/codex/state/last-summary.json'])

    def test_last_summary_lower_seq_kept_local(self):
        a, b = self.pair()
        self.write_store(a, 'agents/codex/state/last-summary.json',
                         '{"seq": 5, "x": "a"}')
        self.write_store(b, 'agents/codex/state/last-summary.json',
                         '{"seq": 2, "x": "b"}')
        result = sync.merge_project(self.store_dir(a),
                                    self.incoming(b), SOURCE)
        self.assertNotIn('agents/codex/state/last-summary.json',
                         result['merged_files'])

    def test_summaries_union_by_content_hash_renumbers(self):
        a, _b = self.pair()
        store = self.store_dir(a)
        (store / 'agents' / 'codex' / 'state' / 'summaries').mkdir(
            parents=True, exist_ok=True)
        (store / 'agents' / 'codex' / 'state' / 'summaries' / '0001.json'
         ).write_bytes(b'{"s": 1}')
        # Same bytes under the same name: skipped.
        result = sync.merge_project(store, {
            'agents/codex/state/summaries/0001.json': b'{"s": 1}'}, SOURCE)
        self.assertNotIn('agents/codex/state/summaries/0002.json',
                         result['merged_files'])
        # Different bytes under the same name: kept under a fresh number.
        result = sync.merge_project(store, {
            'agents/codex/state/summaries/0001.json': b'{"s": 2}'}, SOURCE)
        self.assertEqual(b'{"s": 2}',
                         result['merged_files']['agents/codex/state/summaries/0002.json'])
        self.assertEqual(1, result['counts']['files_added'])

    def test_unknown_file_is_noted_not_silently_dropped(self):
        a, _b = self.pair()
        incoming = {'agents/codex/state/unknown.txt': b'future file\n'}
        result = sync.merge_project(self.store_dir(a), incoming, SOURCE)
        self.assertNotIn('agents/codex/state/unknown.txt',
                         result['merged_files'])
        self.assertIn('忽略未纳入合并的文件：agents/codex/state/unknown.txt',
                      result['notes'])

    def test_malformed_entry_discarded_and_quarantined(self):
        a, _b = self.pair()
        incoming = {'agents/codex/raw/cap-0009.md': b'not an entry at all\n'}
        result = sync.merge_project(self.store_dir(a), incoming, SOURCE)
        self.assertEqual(1, len(result['discarded']))
        self.assertEqual('malformed', result['discarded'][0]['reason'])
        self.assertIn('agents/codex/raw/cap-0009.md', result['quarantined'])
        self.assertNotIn('agents/codex/raw/cap-0009.md',
                         result['merged_files'])

    def test_id_hash_mismatch_discarded_and_quarantined(self):
        a, _b = self.pair()
        header, body = self.split_entry(self.entry_bytes(a))
        header['fact_hash'] = '0' * 64
        incoming = {'agents/codex/raw/cap-0009.md': (
            json.dumps(header, ensure_ascii=False, sort_keys=True, indent=2)
            + sync.BODY_DELIM + body).encode('utf-8')}
        result = sync.merge_project(self.store_dir(a), incoming, SOURCE)
        self.assertEqual(1, len(result['discarded']))
        self.assertEqual('id-hash-mismatch', result['discarded'][0]['reason'])
        self.assertIn('agents/codex/raw/cap-0009.md', result['quarantined'])
        self.assertEqual(0, result['counts']['entries_added'])


class TestHealthAndPayloadFilter(MergeBase):
    def test_health_check_passes_on_good_repo(self):
        a, _b = self.pair()
        problems, _warnings = sync.health_check(self.store_dir(a))
        self.assertEqual([], problems)

    def test_health_check_reports_conflict_markers_in_memory(self):
        a, _b = self.pair()
        mem = self.adir(a, 'codex') / 'memory.md'
        mem.write_text(mem.read_text(encoding='utf-8')
                       + '\n<<<<<<< HEAD\n', encoding='utf-8')
        problems, _warnings = sync.health_check(self.store_dir(a))
        self.assertIn('档案含冲突标记：agents/codex/memory.md', problems)

    def test_is_payload_file(self):
        cases = {
            # Device-local: never shipped.
            'agents/codex/raw/cap-0001.md.lock': False,
            'checksum-manifest.json': False,
            'stats/capture.jsonl': False,
            'agents/codex/tx/0001.json': False,
            'agents/codex/backups/0001.json': False,
            'config.json': False,
            'agents/codex/state/cursor.json': False,
            'agents/codex/state/baseline.json': False,
            'state/cursor.json': False,
            # Shippable archive files.
            'goal.md': True,
            'state.md': True,
            'backlog.md': True,
            'index.md': True,
            'agents/codex/memory.md': True,
            'agents/codex/decisions.md': True,
            'agents/codex/raw/cap-0001.md': True,
            'agents/codex/state/last-summary.json': True,
            'agents/codex/state/summaries/0001.json': True,
            'receipts/goal-1.json': True,
            'state/goal-tentative.json': True,
        }
        for rel, expected in cases.items():
            self.assertEqual(expected, sync._is_payload_file(rel), rel)

    def test_collect_payload_excludes_local_only_files(self):
        a, _b = self.pair()
        store = self.store_dir(a)
        (store / 'agents' / 'codex' / 'raw' / 'cap-0001.md.lock').write_bytes(b'x')
        (store / 'agents' / 'codex' / 'tx').mkdir(exist_ok=True)
        (store / 'agents' / 'codex' / 'tx' / 't1.json').write_bytes(b'{}')
        (store / 'checksum-manifest.json').write_bytes(b'{}')
        (store / 'config.json').write_bytes(b'{}')
        payload = sync._collect_payload(store)
        for rel in payload:
            self.assertTrue(sync._is_payload_file(rel), rel)
        self.assertNotIn('agents/codex/raw/cap-0001.md.lock', payload)
        self.assertNotIn('agents/codex/tx/t1.json', payload)
        self.assertNotIn('checksum-manifest.json', payload)
        self.assertNotIn('config.json', payload)
        self.assertIn('goal.md', payload)
        self.assertIn('agents/codex/raw/cap-0001.md', payload)


if __name__ == '__main__':
    import unittest
    unittest.main()
