"""Distribution and archive schema upgrade tests."""
import importlib.util
import json
import sys
import tempfile
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from v3_helpers import V3Base, relay, flow


ROOT = Path(__file__).resolve().parents[1]


def load_package_builder():
    spec = importlib.util.spec_from_file_location(
        'package_dsh_skill', ROOT / 'scripts' / 'package_dsh_skill.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestSkillDistribution(V3Base):
    def test_recipe_install_includes_plugin_and_self_check(self):
        root = self.init()
        info = flow.install_deepseek_harness(root)
        self.assertTrue(info['self_check']['ok'])
        skill_dir = Path(info['skill_dir'])
        self.assertTrue((skill_dir / 'dsh-plugin' / 'project-relay-hooks' / 'index.js').is_file())
        self.assertTrue((skill_dir / 'dsh-plugin' / 'project-relay-hooks' / 'package.json').is_file())

    def test_release_zip_includes_plugin(self):
        builder = load_package_builder()
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / 'skill.zip'
            builder.build(out=dest)
            with zipfile.ZipFile(dest) as zf:
                names = set(zf.namelist())
        self.assertIn('project-relay/SKILL.md', names)
        self.assertIn('project-relay/dsh-plugin/project-relay-hooks/index.js', names)
        self.assertIn('project-relay/dsh-plugin/project-relay-hooks/cordis.patch.yml', names)

    def test_plugin_source_uses_pip_user_data_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_scripts = root / 'site-packages' / 'scripts'
            fake_scripts.mkdir(parents=True)
            user_data = root / 'user-data'
            plugin = user_data / 'share' / 'project-relay' / 'dsh-plugin' / 'project-relay-hooks'
            plugin.mkdir(parents=True)

            def fake_get_path(key, scheme=None):
                return str(user_data) if scheme else str(root / 'prefix')

            with mock.patch.object(flow, '_HERE', fake_scripts), \
                    mock.patch.object(flow.sysconfig, 'get_path', side_effect=fake_get_path):
                self.assertEqual(plugin.resolve(), flow._plugin_hook_source().resolve())

    def test_init_repair_refreshes_manifest_after_schema_upgrade(self):
        root = self.init()
        store = self.store_dir(root)
        path = store / 'project.json'
        project = json.loads(path.read_text(encoding='utf-8'))
        project.pop('schema_version', None)
        path.write_text(json.dumps(project), encoding='utf-8')
        relay.update_checksum_manifest(store)
        relay.init(root)
        _present, mismatches = relay.verify_checksum_manifest(store)
        self.assertEqual([], mismatches)

    def test_project_upgrade_stamps_schema_version(self):
        root = self.init()
        path = self.store_dir(root) / 'project.json'
        project = json.loads(path.read_text(encoding='utf-8'))
        project.pop('schema_version', None)
        path.write_text(json.dumps(project), encoding='utf-8')
        result = relay.upgrade_project(root)
        self.assertEqual(relay.ARCHIVE_SCHEMA_VERSION, result['schema_version'])
        self.assertEqual(relay.ARCHIVE_SCHEMA_VERSION,
                         json.loads(path.read_text(encoding='utf-8'))['schema_version'])

    def test_doctor_warns_when_schema_version_missing(self):
        root = self.init()
        path = self.store_dir(root) / 'project.json'
        project = json.loads(path.read_text(encoding='utf-8'))
        project.pop('schema_version', None)
        path.write_text(json.dumps(project), encoding='utf-8')
        relay.update_checksum_manifest(self.store_dir(root))
        result = flow.doctor(root)
        self.assertIn('archive_version_missing',
                      [check['code'] for check in result['checks']])
        self.assertEqual(0, result['errors'])
