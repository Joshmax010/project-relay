"""P2 cleanup review regressions."""
import json
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from v3_helpers import V3Base, relay, flow, sync


CARD = """## 已验证
- P2 回归测试卡。
## 决定
- 坏卡不能被新卡覆盖。
## 待办
- 无。
## 事实
- 接口：v2
"""


class TestP2Cleanup(V3Base):
    def test_corrupt_high_card_number_is_not_reused(self):
        root = self.init()
        flow.write_session_card(root, 'codex', 's1', 'session-end', CARD)
        bad = self.adir(root, 'codex') / 'sessions' / 'card-0002.md'
        bad.write_text('truncated', encoding='utf-8')
        result = flow.write_session_card(root, 'codex', 's2', 'session-end', CARD)
        self.assertEqual(3, result['seq'])
        self.assertEqual('truncated', bad.read_text(encoding='utf-8'))
        self.assertTrue((self.adir(root, 'codex') / 'sessions' / 'card-0003.md').is_file())

    def test_default_export_zip_is_git_ignored(self):
        root = self.init()
        self.confirm_goal(root)
        self.git(root, 'add', '.gitignore')
        self.git(root, 'commit', '-m', 'gitignore')
        result = sync.export_zip(root)
        status = self.git(root, 'status', '--porcelain').stdout.decode('utf-8')
        self.assertNotIn(Path(result['zip']).name, status)

    def test_import_registration_failure_keeps_landed_result(self):
        source = self.init('source')
        self.confirm_goal(source)
        zip_path = Path(self.temp.name) / 'p2.zip'
        sync.export_zip(source, zip_path)
        dest = self.init('dest')
        relay.unregister_project_path(dest)
        with mock.patch.object(relay, 'register_project', side_effect=relay.RelayError('registry unavailable')):
            result = sync.import_zip(dest, zip_path, confirm=True)
        self.assertTrue(result['landed'])
        self.assertTrue(any('定位缓存' in warning for warning in result['report']['warnings']))

    def test_import_repairs_identity_and_refreshes_manifest(self):
        source = self.init('source-manifest')
        self.confirm_goal(source)
        zip_path = Path(self.temp.name) / 'manifest.zip'
        sync.export_zip(source, zip_path)
        dest = self.init('dest-manifest')
        (self.store_dir(dest) / 'project.json').write_text('not-json', encoding='utf-8')
        sync.import_zip(dest, zip_path, confirm=True)
        _present, mismatches = relay.verify_checksum_manifest(self.store_dir(dest))
        self.assertEqual([], mismatches)

    def test_import_from_linked_worktree_registers_main_worktree(self):
        source = self.init('source-linked')
        self.confirm_goal(source)
        zip_path = Path(self.temp.name) / 'linked.zip'
        sync.export_zip(source, zip_path)
        dest = self.init('dest-linked')
        linked = Path(self.temp.name) / 'linked-worktree'
        self.git(dest, 'worktree', 'add', '-b', 'agent/test', str(linked), 'HEAD')
        sync.import_zip(linked, zip_path, confirm=True)
        rows = relay.locate_projects('dest-linked')
        self.assertTrue(any(Path(row['path']).resolve() == dest.resolve() for row in rows))
        self.assertFalse(any(Path(row['path']).resolve() == linked.resolve() for row in rows))


class TestSkillTextUserScheme(V3Base):
    def test_pip_user_scheme_is_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            user_data = root / 'user-data'
            packaged = user_data / 'share' / 'project-relay' / 'SKILL.md'
            packaged.parent.mkdir(parents=True)
            packaged.write_text('# User Skill\n', encoding='utf-8')
            fake_flow = root / 'site-packages' / 'scripts' / 'relay_flow.py'
            fake_flow.parent.mkdir(parents=True)

            def fake_get_path(key, scheme=None):
                if scheme:
                    return str(user_data)
                return str(root / 'prefix')

            with mock.patch.object(flow, '__file__', str(fake_flow)), \
                    mock.patch.object(flow.sysconfig, 'get_path', side_effect=fake_get_path):
                self.assertEqual('# User Skill\n', flow._load_skill_text())


class TestPartitionEdgeCases(V3Base):
    def test_detached_head_is_not_called_single_agent(self):
        root = self.init()
        self.git(root, 'checkout', '--detach', 'HEAD')
        result = flow.resume(root)
        section = result['partition_section']
        self.assertIn('游离状态', section)
        self.assertNotIn('单 Agent 模式', section)

    def test_multi_agent_main_worktree_is_not_called_single_agent(self):
        root = self.init()
        relay.ensure_agent_dir(self.store_dir(root), 'codex')
        result = flow.resume(root)
        section = result['partition_section']
        self.assertIn('多 Agent 主工作树', section)
        self.assertNotIn('单 Agent 模式', section)
