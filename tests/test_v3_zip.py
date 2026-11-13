"""v3 zip handoff tests (batch 18): export-zip / import-zip / verify_bundle.

Fixed in this batch (2026-11-13, branch slim-v3): two missing
module-level singletons in scripts/relay_sync.py -- _FLOW and
_TEMPLATE_STATE_VALUES were never initialised, so every redact path
raised NameError and no zip was ever produced or landed. The four
round-trip tests below used to assert that NameError; they now assert
the intended behaviour (export writes a zip, strict export refuses a
semantic-pending latest entry, dry-run import writes nothing, clean
clone import + resume reads the real handoff).

Also covered here against real behaviour: the refusal gates that run
before any redaction (existing target zip, missing archive, missing
zip), the bundle file helpers (write/read round-trip, the zip layout
manifest.json + projects/<slug>/), verify_bundle (accept / tamper /
format-major-1 / missing manifest / minor-version warning), and the
export quality gate _export_quality_gaps (semantic-pending is a
block-severity gap on the latest entry).
"""
import hashlib
import json
import subprocess
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from v3_helpers import V3Base, relay, flow, sync, RelayError

SyncError = sync.RelayError


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


class TestZipHandoff(V3Base):
    def _setup_a(self, name='proj'):
        """Project A: init + confirmed goal + one capture per agent.

        The entries' semantic sections are filled in, so the only
        thing standing between this repo and a shippable bundle is
        BUG-1 (see the module docstring).
        """
        root = self.init(name)
        self.confirm_goal(root)
        self.change(root, 'app.py', 'value = 2\n')
        self.capture(root, 'codex')
        self.capture(root, 'claude-code')
        self.fill_semantic(root, 'codex')
        self.fill_semantic(root, 'claude-code')
        return root

    def _bundle_dir(self, slug='proj', files=None, major=2, minor=0):
        """Hand-build a bundle directory the way export_bundle would.

        files maps project-relative paths (inside .project-relay/) to
        bytes; the manifest lists them under projects/<slug>/ with
        sha256 hashes, exactly as export_bundle writes them.
        """
        files = files or {'goal.md': b'# goal\n'}
        dest = Path(self.temp.name) / ('bundle-%s-%s' % (slug, major))
        proj_dir = dest / 'projects' / slug
        manifest_files = {}
        for rel in sorted(files):
            target = proj_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(files[rel])
            manifest_files['projects/%s/%s' % (slug, rel)] = _sha256(files[rel])
        manifest = {'format': sync.BUNDLE_FORMAT, 'format_major': major,
                    'format_minor': minor, 'exported_at': '2026-11-13T00:00:00Z',
                    'source_device': 'peer', 'source_device_id': 'x',
                    'tool_version': sync.TOOL_VERSION, 'entry_count': 0,
                    'redacted_count': 0, 'files': manifest_files,
                    'projects': [{'slug': slug, 'display_name': slug,
                                  'files': sorted(manifest_files)}],
                    'quality_gaps': []}
        (dest / 'manifest.json').write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding='utf-8')
        return dest

    def _build_zip(self, zip_name='handoff.zip', **kw):
        bundle = self._bundle_dir(**kw)
        zip_path = Path(self.temp.name) / zip_name
        sync.write_bundle_zip(bundle, zip_path)
        return zip_path

    # ---- the intended flow (BUG-1 fixed: _FLOW is initialised) ----

    def test_export_zip_writes_zip_with_manifest_and_payload(self):
        root = self._setup_a()
        zip_path = Path(self.temp.name) / 'out.zip'
        result = sync.export_zip(root, zip_path)
        self.assertTrue(zip_path.is_file())
        self.assertEqual(2, result['entry_count'])
        self.assertGreater(result['file_count'], 0)
        self.assertEqual([], result['quality_gaps'])
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            manifest = json.loads(zf.read('manifest.json'))
        self.assertIn('manifest.json', names)
        self.assertTrue(any(n.startswith('projects/proj/agents/codex/raw/cap-0001.md')
                            for n in names))
        self.assertEqual(2, manifest['format_major'])
        self.assertEqual([], manifest['quality_gaps'])

    def test_strict_export_refuses_semantic_pending_then_passes_when_filled(self):
        root = self.init()
        self.confirm_goal(root)
        self.capture(root, 'codex')  # semantic section left pending
        zip_path = Path(self.temp.name) / 'strict.zip'
        with self.assertRaises(SyncError):
            sync.export_zip(root, zip_path, strict=True)
        self.assertFalse(zip_path.exists())
        self.fill_semantic(root, 'codex')
        result = sync.export_zip(root, zip_path, strict=True)
        self.assertTrue(zip_path.is_file())
        self.assertEqual([], result['quality_gaps'])

    def test_import_zip_dry_run_writes_nothing(self):
        root = self._setup_a()
        zip_path = self._build_zip(files={
            'goal.md': (self.store_dir(root) / 'goal.md').read_bytes(),
            'agents/codex/raw/cap-0001.md':
                (self.adir(root, 'codex') / 'raw' / 'cap-0001.md').read_bytes(),
        })
        before = sorted(p.relative_to(self.store_dir(root)).as_posix()
                        for p in self.store_dir(root).rglob('*')
                        if p.is_file())
        result = sync.import_zip(root, zip_path, confirm=False)
        self.assertFalse(result['landed'])
        after = sorted(p.relative_to(self.store_dir(root)).as_posix()
                       for p in self.store_dir(root).rglob('*')
                       if p.is_file())
        self.assertEqual(before, after)

    def test_export_clone_import_resume_round_trip(self):
        # The full handoff: export from A, clone A (no .project-relay/ --
        # it is gitignored), import the zip, resume reads the real brief.
        src = self._setup_a()
        zip_path = Path(self.temp.name) / 'handover.zip'
        sync.export_zip(src, zip_path)
        clone = Path(self.temp.name) / 'clone'
        subprocess.run(['git', 'clone', str(src), str(clone)],
                       check=True, capture_output=True)
        self.assertFalse((clone / '.project-relay').exists())
        result = sync.import_zip(clone, zip_path, confirm=True)
        self.assertTrue(result['landed'])
        raw = sorted((clone / '.project-relay' / 'agents' / 'codex' / 'raw').glob('cap-*.md'))
        self.assertEqual(1, len(raw))
        self.assertIn('做一个能跑的演示',
                      (clone / '.project-relay' / 'goal.md').read_text(encoding='utf-8'))
        text = self.resume_text(clone)
        self.assertIn('一段', text)
        self.assertIn('五段', text)

    # ---- refusal gates that run before any redaction (these work) ----

    def test_export_zip_refuses_existing_target(self):
        root = self._setup_a()
        zip_path = Path(self.temp.name) / 'out.zip'
        zip_path.write_bytes(b'old bundle\n')
        with self.assertRaises(SyncError):
            sync.export_zip(root, zip_path)
        self.assertEqual(b'old bundle\n', zip_path.read_bytes())

    def test_export_zip_requires_an_archive(self):
        root = self.make_repo('proj')  # never relay.init'd
        with self.assertRaises(SyncError):
            sync.export_zip(root, Path(self.temp.name) / 'out.zip')

    def test_import_zip_requires_existing_zip(self):
        root = self._setup_a()
        with self.assertRaises(SyncError):
            sync.import_zip(root, Path(self.temp.name) / 'nope.zip',
                            confirm=False)

    # ---- bundle file helpers: write/read round-trip + zip layout ----

    def test_write_read_bundle_zip_roundtrip(self):
        files = {'goal.md': b'# goal\n',
                 'agents/codex/memory.md': b'# memory\n'}
        bundle = self._bundle_dir(files=files)
        zip_path = Path(self.temp.name) / 'round.zip'
        sync.write_bundle_zip(bundle, zip_path)
        self.assertTrue(zip_path.is_file())
        with zipfile.ZipFile(zip_path) as zf:
            names = sorted(zf.namelist())
        self.assertEqual(['manifest.json', 'projects/proj/agents/codex/memory.md',
                          'projects/proj/goal.md'], names)
        dest = Path(self.temp.name) / 'unpacked'
        sync.read_bundle_zip(zip_path, dest)
        self.assertEqual(b'# goal\n',
                         (dest / 'projects' / 'proj' / 'goal.md').read_bytes())
        self.assertEqual(b'# memory\n',
                         (dest / 'projects' / 'proj' / 'agents' / 'codex' / 'memory.md').read_bytes())
        manifest = json.loads((dest / 'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(2, manifest['format_major'])
        self.assertEqual('project-relay-export', manifest['format'])

    def test_read_bundle_zip_rejects_escaping_entry(self):
        zip_path = Path(self.temp.name) / 'evil.zip'
        with zipfile.ZipFile(zip_path, 'w') as zf:
            zf.writestr('../escape.txt', b'x')
        with self.assertRaises(SyncError):
            sync.read_bundle_zip(zip_path, Path(self.temp.name) / 'evil-out')

    # ---- verify_bundle: the import gate (pure, works today) ----

    def test_verify_bundle_accepts_wellformed_bundle(self):
        bundle = self._bundle_dir()
        manifest, files, warnings = sync.verify_bundle(bundle)
        self.assertEqual([], warnings)
        self.assertEqual(b'# goal\n', files['projects/proj/goal.md'])
        self.assertEqual(2, manifest['format_major'])

    def test_verify_bundle_warns_on_minor_version_mismatch(self):
        bundle = self._bundle_dir(minor=1)
        _manifest, _files, warnings = sync.verify_bundle(bundle)
        self.assertEqual(1, len(warnings))
        self.assertIn('次版本', warnings[0])

    def test_verify_bundle_rejects_tampered_file(self):
        bundle = self._bundle_dir()
        target = bundle / 'projects' / 'proj' / 'goal.md'
        target.write_bytes(b'# goal, tampered\n')
        with self.assertRaises(SyncError):
            sync.verify_bundle(bundle)

    def test_verify_bundle_rejects_format_major_1_bundle(self):
        bundle = self._bundle_dir(major=1)
        with self.assertRaises(SyncError) as ctx:
            sync.verify_bundle(bundle)
        self.assertIn('主版本', str(ctx.exception))

    def test_verify_bundle_rejects_missing_manifest(self):
        bundle = Path(self.temp.name) / 'empty-bundle'
        (bundle / 'projects' / 'proj').mkdir(parents=True)
        with self.assertRaises(SyncError):
            sync.verify_bundle(bundle)

    def test_verify_bundle_rejects_unlisted_extra_file(self):
        bundle = self._bundle_dir()
        (bundle / 'projects' / 'proj' / 'extra.md').write_bytes(b'x')
        with self.assertRaises(SyncError):
            sync.verify_bundle(bundle)

    # ---- export quality gate (pure: runs without _flow) ----

    def test_quality_gate_flags_pending_semantic_as_block(self):
        root = self.init()
        self.confirm_goal(root)
        self.capture(root, 'codex')
        self.capture(root, 'claude-code')
        payload = sync._collect_payload(self.store_dir(root))
        gaps = sync._export_quality_gaps(root, payload)
        semantic = [g for g in gaps if g['code'] == 'semantic_pending']
        self.assertEqual(1, len(semantic))
        self.assertEqual('block', semantic[0]['severity'])
        self.assertIn('最新条目', semantic[0]['message'])
        self.assertEqual(2, len(semantic[0]['files']))

    def test_quality_gate_clear_once_semantics_filled(self):
        root = self.init()
        self.confirm_goal(root)
        self.capture(root, 'codex')
        self.capture(root, 'claude-code')
        self.fill_semantic(root, 'codex')
        self.fill_semantic(root, 'claude-code')
        payload = sync._collect_payload(self.store_dir(root))
        gaps = sync._export_quality_gaps(root, payload)
        self.assertEqual([], [g for g in gaps
                              if g['code'] == 'semantic_pending'])


if __name__ == '__main__':
    import unittest
    unittest.main()
