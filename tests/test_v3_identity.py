"""Project identity and machine-local locator cache."""
import json
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from v3_helpers import V3Base, relay, sync


class TestProjectIdentity(V3Base):
    def test_init_creates_project_json_and_registry(self):
        root = self.init()
        store = self.store_dir(root)
        project = json.loads((store / 'project.json').read_text(encoding='utf-8'))
        self.assertEqual(32, len(project['project_id']))
        self.assertEqual('proj', project['name'])
        self.assertTrue(project['created_at'])
        self.assertTrue((self.home / '.project-relay' / 'registry.json').is_file())
        rows = relay.locate_projects('proj')
        self.assertEqual(1, len(rows))
        self.assertEqual(str(root.resolve()), rows[0]['path'])

    def test_init_is_idempotent_and_keeps_project_id(self):
        root = self.init()
        first = json.loads((self.store_dir(root) / 'project.json').read_text(encoding='utf-8'))
        relay.init(root)
        second = json.loads((self.store_dir(root) / 'project.json').read_text(encoding='utf-8'))
        self.assertEqual(first['project_id'], second['project_id'])

    def test_locate_and_open_cli(self):
        root = self.init()
        code, out = self.cli('locate', '--name', 'proj', '--json')
        self.assertEqual(0, code, out)
        rows = json.loads(out)
        self.assertEqual(1, len(rows))
        self.assertEqual(str(root.resolve()), rows[0]['path'])
        code, out = self.cli('open', 'proj')
        self.assertEqual(0, code, out)
        self.assertIn('cd "', out)
        self.assertIn(str(root.resolve()), out)

    def test_project_json_is_payload(self):
        root = self.init()
        self.assertTrue(sync._is_payload_file('project.json'))
        payload = sync._collect_payload(self.store_dir(root))
        self.assertIn('project.json', payload)

    def test_import_keeps_local_project_id(self):
        root = self.init()
        other = self.init('other')
        local_id = json.loads((self.store_dir(root) / 'project.json').read_text(encoding='utf-8'))['project_id']
        other_id = json.loads((self.store_dir(other) / 'project.json').read_text(encoding='utf-8'))['project_id']
        incoming = (self.store_dir(other) / 'project.json').read_bytes()
        result = sync.merge_project(self.store_dir(root), {'project.json': incoming},
                                    {'device': 'other'})
        self.assertIn('项目身份证 ID 与本地不同', ' '.join(result['notes']))
        self.assertNotIn('project.json', result['merged_files'])
        self.assertEqual(local_id, json.loads((self.store_dir(root) / 'project.json').read_text(encoding='utf-8'))['project_id'])
        self.assertNotEqual(local_id, other_id)

    def test_private_root_warning_has_migration_hint(self):
        base = Path(self.temp.name) / '.dsh' / 'proj'
        base.mkdir(parents=True)
        with mock.patch('relay.Path.home', return_value=Path(self.temp.name)):
            warning = relay.project_root_warning(base)
        self.assertIn('Agent 私有区', warning)
        self.assertIn('Projects', warning)
