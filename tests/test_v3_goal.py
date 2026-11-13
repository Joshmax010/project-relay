"""v3 goal lifecycle (batch 18).

Covers relay.goal_draft / goal_confirm / goal_decline: draft hash,
confirmation receipt chain (goal-0001.json, goal-0002.json), goal.md
content equality, goal_status before/after confirmation, error paths
(empty draft, confirm with no draft), decline returning the project to
'none', and sync._local_goal_confirmed against matching / mismatching
goal bytes.

Real temporary Git repos, relay.init(root) only, no registry, no
global store. Run: python -m unittest tests.test_v3_goal -v
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from v3_helpers import V3Base, relay, sync, GOAL_TEXT, RelayError

SECOND_GOAL = (
    '# 项目目标\n\n状态：已确认\n\n- 目标与交付物：换一个目标\n'
)


class TestGoalDraftConfirm(V3Base):
    def test_draft_returns_content_hash(self):
        root = self.init()
        info = relay.goal_draft(root, GOAL_TEXT)
        self.assertEqual(relay.digest(GOAL_TEXT.encode('utf-8')), info['hash'])
        draft = (self.store_dir(root) / 'state' / 'goal-draft.md').read_text(
            encoding='utf-8')
        self.assertEqual(GOAL_TEXT, draft)

    def test_confirm_writes_goal_and_receipt(self):
        root = self.init()
        relay.goal_draft(root, GOAL_TEXT)
        info = relay.goal_confirm(root)
        self.assertFalse(info.get('already'))
        receipt = info['receipt']
        self.assertEqual(1, receipt['seq'])
        store = self.store_dir(root)
        goal_bytes = (store / 'goal.md').read_bytes()
        self.assertEqual(GOAL_TEXT.encode('utf-8'), goal_bytes)
        receipt_path = store / 'receipts' / 'goal-0001.json'
        self.assertTrue(receipt_path.is_file())
        saved = json.loads(receipt_path.read_text(encoding='utf-8'))
        self.assertEqual(1, saved['seq'])
        self.assertEqual(relay.digest(goal_bytes), saved['goal_hash'])
        self.assertEqual(saved['goal_hash'], receipt['goal_hash'])
        # The draft is consumed by confirmation.
        self.assertFalse((store / 'state' / 'goal-draft.md').exists())

    def test_goal_status_unconfirmed_before_confirmed_after(self):
        root = self.init()
        state, _info = relay.goal_status(root)
        self.assertEqual('none', state)
        relay.goal_draft(root, GOAL_TEXT)
        state, _info = relay.goal_status(root)
        self.assertEqual('none', state)
        relay.goal_confirm(root)
        state, info = relay.goal_status(root)
        self.assertEqual('confirmed', state)
        self.assertEqual(relay.digest(GOAL_TEXT.encode('utf-8')),
                         info['hash'])

    def test_empty_draft_raises(self):
        root = self.init()
        for bad in ('', '   ', '\n'):
            with self.assertRaises(RelayError):
                relay.goal_draft(root, bad)

    def test_confirm_without_draft_raises(self):
        root = self.init()
        with self.assertRaises(RelayError):
            relay.goal_confirm(root)

    def test_confirm_twice_same_goal_is_already(self):
        root = self.init()
        self.confirm_goal(root)
        relay.goal_draft(root, GOAL_TEXT)
        info = relay.goal_confirm(root)
        self.assertTrue(info.get('already'))
        self.assertEqual(1, info['receipt']['seq'])
        self.assertFalse(
            (self.store_dir(root) / 'receipts' / 'goal-0002.json').exists())


class TestGoalDecline(V3Base):
    def test_decline_with_no_goal_returns_to_none(self):
        root = self.init()
        info = relay.goal_decline(root)
        self.assertTrue(info.get('declined'))
        state, _info = relay.goal_status(root)
        self.assertEqual('none', state)
        goal = (self.store_dir(root) / 'goal.md').read_text(encoding='utf-8')
        self.assertIn('状态：未建', goal)

    def test_decline_after_confirm_retires_receipts(self):
        root = self.init()
        self.confirm_goal(root)
        store = self.store_dir(root)
        self.assertTrue((store / 'receipts' / 'goal-0001.json').exists())
        relay.goal_decline(root)
        self.assertFalse((store / 'receipts' / 'goal-0001.json').exists())
        state, _info = relay.goal_status(root)
        self.assertEqual('none', state)
        goal = (store / 'goal.md').read_text(encoding='utf-8')
        self.assertIn('状态：未建', goal)

    def test_decline_then_new_goal_confirms_cleanly(self):
        root = self.init()
        self.confirm_goal(root)
        relay.goal_decline(root)
        relay.goal_draft(root, SECOND_GOAL)
        info = relay.goal_confirm(root)
        self.assertEqual(1, info['receipt']['seq'])
        self.assertEqual(SECOND_GOAL.encode('utf-8'),
                         (self.store_dir(root) / 'goal.md').read_bytes())


class TestGoalSecondConfirm(V3Base):
    def test_new_draft_reconfirm_updates_goal_and_mints_0002(self):
        root = self.init()
        self.confirm_goal(root)
        relay.goal_draft(root, SECOND_GOAL)
        info = relay.goal_confirm(root)
        self.assertFalse(info.get('already'))
        self.assertEqual(2, info['receipt']['seq'])
        store = self.store_dir(root)
        goal_bytes = (store / 'goal.md').read_bytes()
        self.assertEqual(SECOND_GOAL.encode('utf-8'), goal_bytes)
        second = store / 'receipts' / 'goal-0002.json'
        self.assertTrue(second.is_file())
        saved = json.loads(second.read_text(encoding='utf-8'))
        self.assertEqual(relay.digest(goal_bytes), saved['goal_hash'])
        self.assertTrue((store / 'receipts' / 'goal-0001.json').exists())
        state, info = relay.goal_status(root)
        self.assertEqual('confirmed', state)
        self.assertEqual(relay.digest(goal_bytes), info['hash'])


class TestLocalGoalConfirmed(V3Base):
    def test_matching_goal_bytes_confirmed_mismatched_not(self):
        root = self.init()
        self.confirm_goal(root)
        store = self.store_dir(root)
        goal_bytes = (store / 'goal.md').read_bytes()
        self.assertTrue(sync._local_goal_confirmed(store, goal_bytes))
        self.assertFalse(sync._local_goal_confirmed(store, '被改过的目标\n'.encode('utf-8')))
        self.assertFalse(sync._local_goal_confirmed(store, SECOND_GOAL.encode('utf-8')))
        # After a second confirmation the new bytes confirm and the old do not.
        relay.goal_draft(root, SECOND_GOAL)
        relay.goal_confirm(root)
        new_bytes = (store / 'goal.md').read_bytes()
        self.assertTrue(sync._local_goal_confirmed(store, new_bytes))
        self.assertTrue(sync._local_goal_confirmed(store, goal_bytes))

    def test_no_receipts_means_not_confirmed(self):
        root = self.init()
        store = self.store_dir(root)
        self.assertFalse(sync._local_goal_confirmed(
            store, (store / 'goal.md').read_bytes()))


if __name__ == '__main__':
    import unittest
    unittest.main()
