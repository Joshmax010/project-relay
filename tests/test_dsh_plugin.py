"""Run the DSH plugin event-shape test in Node."""
import shutil
import subprocess
import unittest
from pathlib import Path


class TestDshPluginEvents(unittest.TestCase):
    def test_real_turn_stopping_payload_reaches_relay_hook_stop(self):
        if not shutil.which('node'):
            self.skipTest('node not installed')
        repo = Path(__file__).resolve().parents[1]
        proc = subprocess.run(['node', str(repo / 'tests' / 'test_dsh_plugin.mjs')],
                              capture_output=True, text=True)
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
