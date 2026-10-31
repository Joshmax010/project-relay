"""Acceptance harness isolation tests (plan 1.16 regression).

Regression: `relay.py accept` used to inherit the persistent default store
(~/.project-relay) while workdir/log_home were fresh temp dirs. Fixture
projects are keyed by fixed remote URLs, so the second run measured the
first run's leftovers: change scale ~0 (baseline already advanced),
backfill entries already created (created=[]), gaps unreported (gap
detection only runs for newly created sessions), summary_seq None, and
redacted_count 0. The first run on a clean store passed; every later run
failed the same five checks.

These tests pin the fix: the default accept path must use a fresh
throwaway store, so two consecutive default invocations both PASS, and
the user's real home must stay untouched.

Style follows the old suite: real temporary directories, unittest.
No test touches the developer's real ~/.project-relay/.
"""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SKILL = Path(__file__).resolve().parents[1]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


accept = _load('relay_accept', SKILL / 'scripts/relay_accept.py')


def _run_cli(env, *args):
    # Batch 3, A2: pin both ends to UTF-8 -- the child prints Chinese
    # copy, and the parent must not decode it with the locale default
    # (cp936 on a Chinese Windows install turns UTF-8 bytes to mojibake).
    env = dict(env)
    env.setdefault('PYTHONIOENCODING', 'utf-8')
    return subprocess.run(
        [sys.executable, str(SKILL / 'scripts/relay.py'), *args],
        capture_output=True, text=True, encoding='utf-8', env=env, timeout=180)


class TestAcceptIsolation(unittest.TestCase):
    def test_cli_accept_twice_default_store_passes(self):
        """Two consecutive default `relay.py accept` runs must both PASS,
        and neither may write into the (redirected) user home."""
        with tempfile.TemporaryDirectory(prefix='relay-accept-home-') as home:
            env = dict(os.environ)
            env['PROJECT_RELAY_HOME'] = home
            for i in range(2):
                proc = _run_cli(env, 'accept')
                self.assertEqual(proc.returncode, 0,
                                 'run %d failed:\n%s' % (i + 1, proc.stdout + proc.stderr))
                self.assertIn('卷一验收：**PASS**', proc.stdout)
            home_projects = Path(home) / '.project-relay' / 'projects'
            self.assertFalse(home_projects.exists(),
                             'default accept must not write into the user home: %s'
                             % home_projects)

    def test_cli_accept_explicit_store_root_honored(self):
        """An explicit --store-root is still honored (debugging path)."""
        with tempfile.TemporaryDirectory(prefix='relay-accept-store-') as store:
            proc = _run_cli(dict(os.environ), '--store-root', store, 'accept')
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn('卷一验收：**PASS**', proc.stdout)
            self.assertTrue(any(Path(store).glob('projects/*')),
                            'explicit store root must receive the fixture projects')

    def test_run_acceptance_none_store_passes(self):
        """Direct module use with no store must also isolate (fresh store)."""
        record = accept.run_acceptance()
        self.assertEqual(record['verdict'], 'PASS')
        self.assertEqual(len(record['acts']), 14)
        self.assertTrue(all(a['result'] == '通过' for a in record['acts']))


if __name__ == '__main__':
    unittest.main()
