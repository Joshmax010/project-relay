"""DSH Stop / stage hook -> session card integration."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from v3_helpers import V3Base, relay, flow, stats


CARD = """## 已验证
- DSH Stop 钩子自动写入短卡，验证：自动化测试。
## 决定
- 只记录结论与未决项，来源：用户确认。
## 待办
- 核对 Stop 钩子的 session id，Owner：DSH，验证：卡头部有 session。
## 事实
- 接口：v2
"""


class TestDshStopHook(V3Base):
    def test_hook_stop_writes_capture_and_card(self):
        root = self.init()
        result = flow.hook_stop(root, agent='deepseek-harness', session='dsh-s1',
                                card_text=CARD)
        self.assertIn('capture', result)
        self.assertFalse(result['capture'].get('skipped'))
        self.assertEqual(1, result['capture']['seq'])
        self.assertIn('card', result)
        self.assertFalse(result['card'].get('error'))
        self.assertEqual(1, result['card']['seq'])
        card_path = self.adir(root, 'deepseek-harness') / 'sessions' / 'card-0001.md'
        self.assertEqual(card_path, Path(result['card']['path']))
        self.assertIn('DSH Stop 钩子', card_path.read_text(encoding='utf-8'))

    def test_hook_stop_without_card_does_not_write_one(self):
        root = self.init()
        result = flow.hook_stop(root, agent='deepseek-harness', session='dsh-s1')
        self.assertIsNone(result['card'])
        self.assertFalse((self.adir(root, 'deepseek-harness') / 'sessions').exists())

    def test_hook_stage_writes_milestone_card(self):
        root = self.init()
        result = flow.hook_stage(root, 'deepseek-harness', 'dsh-s1', CARD)
        self.assertEqual('milestone', result['stage'])
        self.assertEqual(1, result['seq'])
        self.assertTrue((self.adir(root, 'deepseek-harness') / 'sessions' / 'card-0001.md').exists())

    def test_cli_hook_stop_and_stage(self):
        root = self.init()
        card_file = Path(self.temp.name) / 'card.md'
        card_file.write_text(CARD, encoding='utf-8')
        code, text = self.cli('--root', root, 'hook', 'stop',
                              '--agent', 'deepseek-harness',
                              '--session', 'dsh-s1',
                              '--card-file', card_file)
        self.assertEqual(2, code, text)  # capture 需要填语义，enforce 正确拦截
        self.assertIn('会话卡：card-0001', text)
        code, text = self.cli('--root', root, 'hook', 'stage',
                              '--agent', 'deepseek-harness',
                              '--session', 'dsh-s1',
                              '--file', card_file)
        self.assertEqual(0, code, text)
        self.assertIn('阶段卡：card-0002', text)

    def test_card_failure_is_visible_but_does_not_skip_capture(self):
        root = self.init()
        path = self.store_dir(root) / 'state' / 'config.json'
        path.write_text(json.dumps({'session_card_char_cap': 10}), encoding='utf-8')
        result = flow.hook_stop(root, agent='deepseek-harness', session='dsh-s1',
                                card_text=CARD)
        self.assertFalse(result['capture'].get('skipped'))
        self.assertIn('error', result['card'])
        self.assertIn('会话卡超长', result['card']['error'])

    def test_hook_outside_project_skips_card(self):
        repo = self.make_repo('plain')
        result = flow.hook_stop(repo, agent='deepseek-harness', session='s1', card_text=CARD)
        self.assertTrue(result['card'].get('skipped'))
        self.assertTrue(result['capture'].get('skipped'))
