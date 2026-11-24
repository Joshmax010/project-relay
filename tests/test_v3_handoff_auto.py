"""Automatic project-root export and import registration."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from v3_helpers import V3Base, relay, sync


class TestAutomaticHandoff(V3Base):
    def test_export_zip_defaults_to_project_root(self):
        root = self.init()
        self.confirm_goal(root)
        result = sync.export_zip(root)
        path = Path(result['zip'])
        self.assertEqual(root.resolve(), path.parent)
        self.assertTrue(path.name.startswith('proj-project-relay-'))
        self.assertTrue(path.is_file())

    def test_import_zip_registers_fresh_project(self):
        source = self.init('source')
        self.confirm_goal(source)
        zip_path = Path(self.temp.name) / 'handoff.zip'
        sync.export_zip(source, zip_path)
        dest = self.make_repo('imported')
        self.assertFalse(self.store_dir(dest).exists())
        sync.import_zip(dest, zip_path, confirm=True)
        rows = relay.locate_projects('imported')
        self.assertTrue(any(Path(row['path']).resolve() == dest.resolve() for row in rows))

    def test_import_zip_registers_existing_project(self):
        source = self.init('source2')
        self.confirm_goal(source)
        zip_path = Path(self.temp.name) / 'handoff2.zip'
        sync.export_zip(source, zip_path)
        dest = self.init('existing-import')
        relay.unregister_project_path(dest)
        sync.import_zip(dest, zip_path, confirm=True)
        rows = relay.locate_projects('existing-import')
        self.assertTrue(any(Path(row['path']).resolve() == dest.resolve() for row in rows))
