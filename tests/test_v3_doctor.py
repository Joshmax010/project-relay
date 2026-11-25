"""relay doctor diagnostics."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from v3_helpers import V3Base, relay, flow


CARD = """## 已验证
- 体检测试卡。
## 决定
- 无。
## 待办
- 无。
## 事实
- 接口：v2
"""


class TestDoctor(V3Base):
    def test_healthy_project(self):
        root = self.init()
        self.confirm_goal(root)
        result = flow.doctor(root)
        self.assertTrue(result['ok'])
        self.assertEqual(0, result['errors'])
        codes = [check['code'] for check in result['checks']]
        self.assertIn('project', codes)
        self.assertIn('manifest', codes)

    def test_missing_archive_is_error(self):
        root = self.make_repo('plain')
        result = flow.doctor(root)
        self.assertFalse(result['ok'])
        self.assertEqual(1, result['errors'])
        self.assertIn('no_archive', [check['code'] for check in result['checks']])

    def test_bad_card_is_reported_without_error(self):
        root = self.init()
        flow.write_session_card(root, 'codex', 's1', 'session-end', CARD)
        bad = self.adir(root, 'codex') / 'sessions' / 'card-9999.md'
        bad.write_text('truncated', encoding='utf-8')
        result = flow.doctor(root)
        self.assertEqual(0, result['errors'])
        self.assertIn('bad_card', [check['code'] for check in result['checks']])
        self.assertIn('card-9999.md', flow.render_doctor(result))

    def test_cli_json(self):
        root = self.init()
        self.confirm_goal(root)
        code, text = self.cli('--root', root, 'doctor', '--json')
        self.assertEqual(0, code, text)
        data = json.loads(text)
        self.assertTrue(data['ok'])
        self.assertIn('checks', data)
