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
        code, out = self.cli('open', 'proj', '--shell', 'cmd')
        self.assertEqual(0, code, out)
        self.assertIn('cd /d "', out)
        self.assertIn(str(root.resolve()), out)
        code, out = self.cli('open', 'proj', '--shell', 'powershell')
        self.assertEqual(0, code, out)
        self.assertIn("cd '", out)

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

    def test_fresh_import_adopts_incoming_project_id(self):
        source = self.init('source')
        source_id = json.loads((self.store_dir(source) / 'project.json').read_text(encoding='utf-8'))['project_id']
        bundle = Path(self.temp.name) / 'bundle'
        sync.export_bundle(self.store_dir(source), bundle, 'dev', 'dev-1')
        clone = self.make_repo('clone')
        self.assertFalse((self.store_dir(clone)).exists())
        sync.import_bundle(bundle, clone, confirm=True)
        clone_id = json.loads((self.store_dir(clone) / 'project.json').read_text(encoding='utf-8'))['project_id']
        self.assertEqual(source_id, clone_id)
        rows = relay.locate_projects('clone')
        self.assertTrue(any(row['project_id'] == source_id for row in rows))

    def test_import_existing_store_keeps_local_project_id(self):
        source = self.init('source')
        bundle = Path(self.temp.name) / 'bundle-existing'
        sync.export_bundle(self.store_dir(source), bundle, 'dev', 'dev-1')
        local = self.init('local')
        local_id = json.loads((self.store_dir(local) / 'project.json').read_text(encoding='utf-8'))['project_id']
        sync.import_bundle(bundle, local, confirm=True)
        after = json.loads((self.store_dir(local) / 'project.json').read_text(encoding='utf-8'))['project_id']
        self.assertEqual(local_id, after)

    def test_non_object_project_json_is_ignored(self):
        root = self.init()
        before = json.loads((self.store_dir(root) / 'project.json').read_text(encoding='utf-8'))['project_id']
        result = sync.merge_project(self.store_dir(root), {'project.json': b'[]'},
                                    {'device': 'other'})
        after = json.loads((self.store_dir(root) / 'project.json').read_text(encoding='utf-8'))['project_id']
        self.assertEqual(before, after)
        self.assertTrue(any('project.json' in note for note in result['notes']))

    def test_open_quotes_shell_special_path(self):
        evil = Path(self.temp.name) / 'evil-$(touch pwned)'
        evil.mkdir()
        data = relay.read_registry()
        data['projects']['evil-id'] = {'project_id': 'evil-id', 'name': 'evil',
                                      'path': str(evil), 'last_seen': 'now'}
        relay.save_registry(data)
        code, out = self.cli('open', 'evil', '--shell', 'posix')
        self.assertEqual(0, code, out)
        self.assertIn("cd '", out)
        self.assertIn('touch pwned', out)

    def test_open_missing_path_returns_error(self):
        missing = self.temp.name + '/gone-project'
        data = relay.read_registry()
        data['projects']['gone'] = {'project_id': 'gone', 'name': 'gone',
                                    'path': missing, 'last_seen': 'now'}
        relay.save_registry(data)
        code, out = self.cli('open', 'gone', '--shell', 'posix')
        self.assertEqual(1, code, out)
        self.assertEqual('', out)

    def test_registry_bad_version_and_substring_lookup(self):
        path = self.home / '.project-relay' / 'registry.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'version': 'x', 'projects': {
            'p1': {'project_id': 'p1', 'name': 'alpha-one', 'path': self.temp.name}
        }}), encoding='utf-8')
        # locate must not crash on a non-numeric version and must substring-match.
        self.assertEqual('p1', relay.locate_projects('alp')[0]['project_id'])

    def test_move_project_keeps_project_id_and_updates_registry(self):
        root = self.init('move-src')
        project_id = json.loads((self.store_dir(root) / 'project.json').read_text(encoding='utf-8'))['project_id']
        dest = Path(self.temp.name) / 'moved-project'
        info = relay.move_project(root, dest)
        self.assertEqual(project_id, info['project']['project_id'])
        self.assertFalse(root.exists())
        self.assertTrue((dest / '.project-relay' / 'project.json').is_file())
        self.assertEqual(project_id, json.loads((dest / '.project-relay' / 'project.json').read_text(encoding='utf-8'))['project_id'])
        rows = relay.locate_projects('moved-project')
        self.assertTrue(any(row['project_id'] == project_id and Path(row['path']).resolve() == dest.resolve()
                            for row in rows))

    def test_move_project_refuses_existing_destination(self):
        root = self.init('move-guard')
        dest = Path(self.temp.name) / 'occupied'
        dest.mkdir()
        with self.assertRaises(relay.RelayError):
            relay.move_project(root, dest)

    def test_adopt_project_registers_existing_identity(self):
        root = self.init('adopt-me')
        project_id = json.loads((self.store_dir(root) / 'project.json').read_text(encoding='utf-8'))['project_id']
        relay.unregister_project_path(root)
        self.assertEqual([], [row for row in relay.locate_projects('adopt-me')
                             if row['project_id'] == project_id])
        info = relay.adopt_project(root)
        self.assertEqual(project_id, info['project']['project_id'])
        self.assertTrue(any(row['project_id'] == project_id for row in relay.locate_projects('adopt-me')))

    def test_private_root_warning_has_migration_hint(self):
        base = Path(self.temp.name) / '.dsh' / 'proj'
        base.mkdir(parents=True)
        with mock.patch('relay.Path.home', return_value=Path(self.temp.name)):
            warning = relay.project_root_warning(base)
        self.assertIn('Agent 私有区', warning)
        self.assertIn('Projects', warning)
