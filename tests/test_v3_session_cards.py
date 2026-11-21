"""Session-end/milestone cards, bounded resume injection, and cost visibility."""
import json
import sys
import time
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from v3_helpers import V3Base, relay, flow, stats, sync, RelayError


CARD = """## 已验证
- 会话卡可从 resume 读取，验证：自动化测试。
## 决定
- 只保存短卡，来源：用户确认。
## 待办
- 接入 Agent Stop 钩子，Owner：DSH，验证：钩子结束时产出短卡。
## 事实
- 接口：v2
"""


class TestSessionCards(V3Base):
    def test_write_card_persists_header_cost_log_and_manifest(self):
        root = self.init()
        info = flow.write_session_card(
            root, 'codex', 's1', 'session-end', CARD,
            llm_usage={'calls': 1, 'input_tokens': 100, 'output_tokens': 40})
        path = Path(info['path'])
        self.assertEqual(self.adir(root, 'codex') / 'sessions' / 'card-0001.md', path)
        card = flow._parse_session_card(path)
        self.assertEqual('codex', card['header']['agent'])
        self.assertEqual('s1', card['header']['session'])
        self.assertEqual('session-end', card['header']['stage'])
        self.assertTrue(card['header']['anchor']['branch'])
        self.assertIn('只保存短卡', card['body'])
        self.assertEqual(140, info['month_llm_tokens'])
        rows = stats.session_card_rows(self.store_dir(root))
        self.assertEqual(140, rows[0]['llm_tokens'])
        self.assertTrue(rows[0]['llm_measured'])
        log = relay.read_agent_log(self.store_dir(root), 'codex')
        self.assertEqual('session-card', log[-1]['event'])
        present, mismatches = relay.verify_checksum_manifest(self.store_dir(root))
        self.assertTrue(present)
        self.assertEqual([], mismatches)

    def test_card_write_never_reads_raw_entries(self):
        root = self.init()
        with mock.patch('relay_flow.list_entries', side_effect=AssertionError('raw read')):
            info = flow.write_session_card(root, 'codex', 's1', 'milestone', CARD)
        self.assertEqual(1, info['seq'])

    def test_resume_reads_latest_card_per_agent(self):
        root = self.init()
        flow.write_session_card(root, 'codex', 's1', 'milestone', '## 决定\n- 旧卡')
        flow.write_session_card(root, 'codex', 's1', 'session-end', CARD)
        result = flow.resume(root)
        self.assertIn('只保存短卡', result['cards_section'])
        self.assertNotIn('旧卡', result['cards_section'])
        rendered = flow.render_resume(result)
        self.assertIn('会话卡', rendered)
        self.assertIn('本月 LLM', rendered)
        self.assertIn('未设置数值上限', rendered)

    def test_resume_card_injection_has_a_total_bound(self):
        root = self.init()
        relay.ensure_agent_dir(self.store_dir(root), 'claude-code')
        body = '## 决定\n- ' + ('甲' * 2500)
        flow.write_session_card(root, 'codex', 's1', 'session-end', body)
        flow.write_session_card(root, 'claude-code', 's2', 'session-end', body)
        result = flow.resume(root)
        self.assertIn('另有 1 张最新会话卡未注入', result['cards_section'])
        self.assertLess(len(result['cards_section']), 5000)

    def test_length_cap_rejects_without_writing(self):
        root = self.init()
        path = self.store_dir(root) / 'state' / 'config.json'
        path.write_text(json.dumps({'session_card_char_cap': 20}), encoding='utf-8')
        with self.assertRaisesRegex(RelayError, '会话卡超长'):
            flow.write_session_card(root, 'codex', 's1', 'session-end', 'x' * 21)
        self.assertFalse((self.adir(root, 'codex') / 'sessions').exists())
        self.assertEqual([], stats.session_card_rows(self.store_dir(root)))

    def test_redaction_applies_to_card_body_and_rejects_secret_session(self):
        root = self.init()
        token = 'ghp_' + ('A' * 24)
        with self.assertRaisesRegex(RelayError, 'session'):
            flow.write_session_card(
                root, 'codex', token, 'session-end', '## 决定\n- token=' + token)
        self.assertFalse((self.adir(root, 'codex') / 'sessions').exists())
        info = flow.write_session_card(
            root, 'codex', 's1', 'session-end', '## 决定\n- token=' + token)
        text = Path(info['path']).read_text(encoding='utf-8')
        self.assertNotIn(token, text)
        self.assertGreater(info['redacted_count'], 0)

    def test_weekly_fuse_blocks_card_but_monthly_cap_only_alerts(self):
        root = self.init()
        store = self.store_dir(root)
        now = time.time()
        stats.record_summarize(store, 'manual', 'manual', llm_in_tokens=2000,
                               now_epoch=now - 10 * 86400)
        stats.record_summarize(store, 'manual', 'manual', llm_in_tokens=7000,
                               now_epoch=now - 86400)
        self.assertTrue(stats.compute_fuse(store, now_epoch=now)['tripped'])
        with self.assertRaisesRegex(RelayError, '成本保险丝已触发'):
            flow.write_session_card(root, 'codex', 's1', 'session-end', CARD)

        other = self.init('monthly-soft')
        store = self.store_dir(other)
        (store / 'state' / 'config.json').write_text(
            json.dumps({'monthly_llm_token_cap': 1}), encoding='utf-8')
        info = flow.write_session_card(other, 'codex', 's1', 'session-end', CARD)
        self.assertTrue(info['month_llm_tokens'] > 1)
        result = flow.resume(other)
        self.assertIn('已超过软上限', result['cost_section'])
        self.assertIn('【月度软上限】', result['banner'])

    def test_conflicting_card_fact_is_marked_not_autoselected(self):
        root = self.init()
        relay.ensure_agent_dir(self.store_dir(root), 'claude-code')
        flow.write_session_card(root, 'codex', 's1', 'session-end', CARD)
        flow.write_session_card(root, 'claude-code', 's2', 'session-end',
                                '## 事实\n- 接口：v1')
        result = flow.resume(root)
        conflict = [c for c in result['cross']['conflicts'] if c['key'] == '接口']
        self.assertEqual(1, len(conflict))
        self.assertIn('[待用户确认] 接口', result['cross_section'])
        self.assertIn('v1', result['cross_section'])
        self.assertIn('v2', result['cross_section'])

    def test_latest_milestone_only_ignores_older_milestones(self):
        root = self.init()
        flow.write_session_card(root, 'codex', 's1', 'milestone', '## 事实\n- 接口：v1')
        flow.write_session_card(root, 'codex', 's1', 'milestone', '## 事实\n- 接口：v2')
        cards = flow.latest_session_cards(self.store_dir(root), ['codex'])
        self.assertEqual(1, len(cards))
        self.assertIn('v2', cards[0]['body'])
        result = flow.resume(root)
        self.assertNotIn('v1', result['cards_section'])

    def test_fork_copies_of_same_event_are_both_kept(self):
        root = self.init()
        flow.write_session_card(root, 'codex', 's1', 'session-end', CARD)
        rel = 'agents/codex/sessions/card-0001.md'
        payload = sync._collect_payload(self.store_dir(root))
        incoming = payload[rel].replace(b'- \xe6\x8e\xa5\xe5\x8f\xa3\xef\xbc\x9av2',
                                         b'- \xe6\x8e\xa5\xe5\x8f\xa3\xef\xbc\x9av3')
        result = sync.merge_project(self.store_dir(root), {rel: incoming},
                                    {'device': 'other'})
        self.assertTrue(any(c.get('rule') == 'session-card-same-event-different-body'
                            for c in result['conflicts']))
        for merged_rel, data in result['merged_files'].items():
            dest = self.store_dir(root) / merged_rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        relay.update_checksum_manifest(self.store_dir(root))
        cards = flow.latest_session_cards(self.store_dir(root), ['codex'])
        self.assertEqual(2, len(cards))
        self.assertEqual(1, len({c['header'].get('event_id') for c in cards}))

    def test_inject_budget_includes_cross_section(self):
        root = self.init()
        relay.ensure_agent_dir(self.store_dir(root), 'claude-code')
        flow.write_session_card(root, 'codex', 's1', 'session-end', CARD)
        flow.write_session_card(root, 'claude-code', 's2', 'session-end',
                                '## 事实\n- 接口：v1')
        result = flow.inject(root, budget=80)
        self.assertLessEqual(result['tokens_estimate'], 80)

    def test_partial_llm_usage_is_rejected_not_measured(self):
        root = self.init()
        with self.assertRaisesRegex(RelayError, 'llm_usage'):
            flow.write_session_card(root, 'codex', 's1', 'session-end', CARD,
                                    llm_usage={'calls': 1})
        self.assertFalse((self.adir(root, 'codex') / 'sessions').exists())

    def test_skill_copies_are_byte_identical(self):
        repo = Path(__file__).resolve().parents[1]
        self.assertEqual((repo / 'SKILL.md').read_bytes(),
                         (repo / 'skills' / 'project-relay' / 'SKILL.md').read_bytes())

    def test_cli_card_and_usage(self):
        root = self.init()
        semantic = Path(self.temp.name) / 'card.md'
        semantic.write_text(CARD, encoding='utf-8')
        code, text = self.cli(
            '--root', root, 'card', '--agent', 'codex', '--session', 'cli-1',
            '--stage', 'session-end', '--file', semantic,
            '--llm-usage', '{"calls":1,"input_tokens":12,"output_tokens":8}')
        self.assertEqual(0, code, text)
        self.assertIn('会话卡：card-0001', text)
        self.assertIn('本月 LLM 已记录：20 tokens', text)


class TestSessionCardStats(V3Base):
    def test_estimated_card_cost_enters_week_and_month(self):
        root = self.init()
        store = self.store_dir(root)
        now = time.time()
        stats.record_session_card(store, 'codex', 's1', 'session-end',
                                  semantic_chars=150, now_epoch=now)
        result = stats.compute_fuse(store, now_epoch=now)
        self.assertEqual(100, result['week_llm_tokens'])
        self.assertEqual(100, result['month_llm_tokens'])
        row = stats.session_card_rows(store)[0]
        self.assertFalse(row['llm_measured'])
        self.assertEqual(100, row['llm_out_tokens'])

    def test_card_cost_can_trip_the_same_weekly_fuse(self):
        root = self.init()
        store = self.store_dir(root)
        now = time.time()
        stats.record_summarize(store, 'manual', 'manual', llm_in_tokens=2000,
                               now_epoch=now - 10 * 86400)
        stats.record_session_card(store, 'codex', 's1', 'session-end', 1,
                                  llm_usage={'calls': 1, 'input_tokens': 7000,
                                             'output_tokens': 0},
                                  now_epoch=now - 86400)
        self.assertTrue(stats.compute_fuse(store, now_epoch=now)['tripped'])

class TestSessionCardSync(V3Base):
    def test_card_is_payload_and_merge_keeps_both_sides(self):
        root = self.init()
        info = flow.write_session_card(root, 'codex', 's1', 'session-end', CARD)
        rel = 'agents/codex/sessions/card-0001.md'
        self.assertTrue(sync._is_payload_file(rel))
        payload = sync._collect_payload(self.store_dir(root))
        self.assertIn(rel, payload)
        incoming = payload[rel].replace(b'- \xe6\x8e\xa5\xe5\x8f\xa3\xef\xbc\x9av2',
                                         b'- \xe6\x8e\xa5\xe5\x8f\xa3\xef\xbc\x9av3')
        result = sync.merge_project(self.store_dir(root),
                                    {rel: incoming},
                                    {'device': 'other', 'exported_at': 'now'})
        self.assertTrue(any(c['rule'] == 'session-card-same-event-different-body'
                            for c in result['conflicts']))
        self.assertTrue(any(k.endswith('sessions/card-0002.md')
                            for k in result['merged_files']))

    def test_resume_cards_is_read_only_and_ignores_memory_and_raw(self):
        root = self.init()
        flow.write_session_card(root, 'codex', 's1', 'session-end', CARD)
        state_before = (self.store_dir(root) / 'state.md').read_bytes()
        with mock.patch('relay_flow.list_entries', side_effect=AssertionError('raw read')):
            result = flow.resume(root)
        self.assertEqual('cards', result['mode'])
        self.assertIn('会话卡', result['cards_section'])
        self.assertEqual(state_before, (self.store_dir(root) / 'state.md').read_bytes())

    def test_inject_cards_only_has_requested_budget(self):
        root = self.init()
        flow.write_session_card(root, 'codex', 's1', 'session-end', CARD)
        result = flow.inject(root, budget=200)
        self.assertEqual('cards', result['mode'])
        self.assertEqual(200, result['budget'])
        self.assertLessEqual(result['tokens_estimate'], 200 + 50)
        full = flow.inject(root, full=True)
        self.assertNotIn('mode', full)
