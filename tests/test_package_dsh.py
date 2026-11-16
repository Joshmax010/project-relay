"""Packaging test: the DSH release zip is self-contained."""
import importlib.util
import tempfile
import unittest
import zipfile
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestPackageDshSkill(unittest.TestCase):
    def test_build_contains_self_contained_skill(self):
        package = _load('package_dsh_skill', SKILL / 'scripts' / 'package_dsh_skill.py')
        out = Path(tempfile.mkdtemp(prefix='relay-package-')) / 'skill.zip'
        package.build(str(out))
        self.assertTrue(out.is_file())
        with zipfile.ZipFile(out) as zf:
            names = set(zf.namelist())
        for name in ('project-relay/SKILL.md',
                     'project-relay/scripts/relay.py',
                     'project-relay/scripts/relay_work.py',
                     'project-relay/assets/template-v2/memory.md',
                     'project-relay/INSTALL.md'):
            self.assertIn(name, names)


if __name__ == '__main__':
    unittest.main()
