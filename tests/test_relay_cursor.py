"""Cursor parser tests (plan part 03, step 3.1).

Style follows the old suite: real temporary directories, real SQLite
databases built from the synthetic fixture JSONs in tests/fixtures/cursor/.
The fixture is SYNTHETIC (shaped after community documentation, never a
real Cursor session) -- see tests/fixtures/cursor/README.md. No test
touches a real ~/.config/Cursor, ~/Library/Application Support/Cursor or
%APPDATA%/Cursor directory.

The builder deliberately inserts global rows in REVERSED header order so
the ordering test proves events follow fullConversationHeadersOnly, not
SQLite rowid.
"""
import hashlib
import importlib.util
import json
import sqlite3
from pathlib import Path
import tempfile
import unittest

SKILL = Path(__file__).resolve().parents[1]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cursor_mod = _load('relay_cursor', SKILL / 'scripts/relay_cursor.py')
registry_mod = _load('relay_parser_registry',
                     SKILL / 'scripts/relay_parser_registry.py')
CursorRelayError = registry_mod.RelayError

FIXTURE = SKILL / 'tests' / 'fixtures' / 'cursor'


def _fixture_json(name):
    with open(FIXTURE / name, encoding='utf-8') as fh:
        return json.load(fh)


def _write_db(db_path, tables):
    """tables: {table: {key: value}}; dict values are JSON-encoded,
    str values are stored verbatim. Returns the path."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute('CREATE TABLE cursorDiskKV (key TEXT, value BLOB)')
        conn.execute('CREATE TABLE ItemTable (key TEXT, value TEXT)')
        for table, rows in tables.items():
            for key, value in rows.items():
                if isinstance(value, dict):
                    raw = json.dumps(value, ensure_ascii=False).encode('utf-8')
                elif isinstance(value, str):
                    raw = value.encode('utf-8')
                else:
                    raw = json.dumps(value, ensure_ascii=False).encode('utf-8')
                conn.execute('INSERT INTO "%s" (key, value) VALUES (?, ?)'
                             % table, (key, raw))
        conn.commit()
    finally:
        conn.close()
    return db_path


def _hash_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class CursorBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='relay-cursor-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.proj = self.base / 'proj-a'
        self.proj.mkdir()
        self.user_dir = self.base / 'CursorUser'
        self._build_user_dir(self.user_dir, self.proj)

    def _build_user_dir(self, user_dir, proj_root, ws_fixture=None,
                        global_fixture=None, ws_json=None, ws_hash='ws-hash-1'):
        """Build a Cursor User dir from fixture JSONs. ws_fixture /
        global_fixture default to the shipped fixture; pass {} tables to
        omit a database. ws_json defaults to the fixture workspace.json
        with __PROJECT_ROOT__ replaced by proj_root."""
        ws_dir = user_dir / 'workspaceStorage' / ws_hash
        ws_dir.mkdir(parents=True, exist_ok=True)
        if ws_fixture is None:
            ws_fixture = {k: v for k, v in _fixture_json('workspace-db.json').items()
                          if not k.startswith('_')}
        if ws_fixture:
            _write_db(ws_dir / 'state.vscdb', ws_fixture)
        if ws_json is None:
            ws_json = _fixture_json('workspace.json')
            ws_json = {k: (str(proj_root) if v == '__PROJECT_ROOT__' else v)
                       for k, v in ws_json.items() if not k.startswith('_')}
        if ws_json is not None:
            with open(ws_dir / 'workspace.json', 'w', encoding='utf-8') as fh:
                json.dump(ws_json, fh, ensure_ascii=False)
        if global_fixture is None:
            global_fixture = {k: v for k, v in _fixture_json('global-db.json').items()
                              if not k.startswith('_')}
        if global_fixture:
            # Reversed insertion order: proves header order, not rowid.
            reversed_rows = {table: dict(reversed(list(rows.items())))
                             for table, rows in global_fixture.items()}
            _write_db(user_dir / 'globalStorage' / 'state.vscdb', reversed_rows)
        return user_dir

    def _expected_events(self, global_db):
        expected = _fixture_json('expected-events.json')['events']
        out = []
        for ev in expected:
            ev = dict(ev)
            ev['raw_ref'] = ev['raw_ref'].replace('__GLOBAL_DB__', str(global_db))
            out.append(ev)
        return out


class TestCursorParser(CursorBase):
    def test_discovery_finds_fixture_session(self):
        parser = cursor_mod.CursorParser()
        sessions = parser.sessions(self.user_dir)
        self.assertEqual(len(sessions), 1)
        sess = sessions[0]
        self.assertEqual(sess['session_id'], 'comp-001')
        self.assertEqual(sess['cwd'], str(self.proj))
        self.assertEqual(sess['bubbles'], 7)

    def test_events_match_expected_fixture_in_header_order(self):
        parser = cursor_mod.CursorParser()
        sess = parser.sessions(self.user_dir)[0]
        events = parser.events(sess)
        expected = self._expected_events(sess['global_db'])
        self.assertEqual(len(events), len(expected))
        for got, want in zip(events, expected):
            for key in ('session_id', 'role', 'kind', 'text', 'tool', 'file',
                        'files_touched', 'raw_ref', 'synthetic'):
                self.assertEqual(got[key], want[key], 'event field %s' % key)

    def test_counts_missing_invalid_thinking(self):
        parser = cursor_mod.CursorParser()
        sess = parser.sessions(self.user_dir)[0]
        parser.events(sess)
        stats = _fixture_json('expected-events.json')['stats']
        self.assertEqual(sess['missing_bubbles'], stats['missing_bubbles'])
        self.assertEqual(sess['invalid_bubbles'], stats['invalid_bubbles'])
        self.assertEqual(sess['thinking_skipped'], stats['thinking_skipped'])

    def test_cwd_mismatch_is_surfaced_not_matched(self):
        other = self.base / 'proj-other'
        other.mkdir()
        self._build_user_dir(self.base / 'CursorUser2', other,
                             ws_hash='ws-hash-2')
        parser = cursor_mod.CursorParser()
        sessions = parser.sessions(self.base / 'CursorUser2')
        self.assertEqual(len(sessions), 1)
        # The parser surfaces cwd; the caller's strong check (v1 rule)
        # skips anything whose cwd is not the target project.
        self.assertNotEqual(sessions[0]['cwd'], str(self.proj))

    def test_no_workspace_json_means_cwd_none(self):
        # Build without workspace.json at all: cwd must be None, never
        # inferred from the storage hash.
        ws_dir = self.base / 'CursorUser3' / 'workspaceStorage' / 'ws-hash-3'
        ws_dir.mkdir(parents=True, exist_ok=True)
        ws_fixture = {k: v for k, v in _fixture_json('workspace-db.json').items()
                      if not k.startswith('_')}
        _write_db(ws_dir / 'state.vscdb', ws_fixture)
        parser = cursor_mod.CursorParser()
        sessions = parser.sessions(self.base / 'CursorUser3')
        self.assertEqual(len(sessions), 1)
        self.assertIsNone(sessions[0]['cwd'])

    def test_missing_global_db_yields_no_events(self):
        parser = cursor_mod.CursorParser()
        sess = parser.sessions(self.user_dir)[0]
        (self.user_dir / 'globalStorage' / 'state.vscdb').unlink()
        sess['global_missing'] = True
        self.assertEqual(parser.events(sess), [])

    def test_parse_is_read_only(self):
        dbs = [self.user_dir / 'workspaceStorage' / 'ws-hash-1' / 'state.vscdb',
               self.user_dir / 'globalStorage' / 'state.vscdb']
        before = {str(p): _hash_file(p) for p in dbs}
        parser = cursor_mod.CursorParser()
        sess = parser.sessions(self.user_dir)[0]
        parser.events(sess)
        for p in dbs:
            self.assertEqual(_hash_file(p), before[str(p)])

    def test_empty_home_returns_empty(self):
        parser = cursor_mod.CursorParser()
        self.assertEqual(parser.sessions(self.base / 'no-such-user'), [])
        self.assertEqual(parser.discover(self.base / 'no-such-user'), [])

    def test_legacy_itemtable_fallback(self):
        composer = _fixture_json('workspace-db.json')['cursorDiskKV'][
            'composerData:comp-001']
        ws_fixture = {'ItemTable': {'composer.composerData':
                                    {'allComposers': [composer]}},
                      'cursorDiskKV': {}}
        self._build_user_dir(self.base / 'CursorUser4', self.proj,
                             ws_fixture=ws_fixture, ws_hash='ws-hash-4')
        parser = cursor_mod.CursorParser()
        sessions = parser.sessions(self.base / 'CursorUser4')
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]['session_id'], 'comp-001')


class TestCursorS5Metadata(CursorBase):
    def test_metadata_attributes(self):
        cls = cursor_mod.CursorParser
        self.assertEqual(cls.tool_id, 'cursor')
        self.assertEqual(cls.display_name, 'Cursor')
        self.assertEqual(cls.evidence_tier, '未验证')
        self.assertTrue(cls.retention_note)
        self.assertIn('{cursor_user}/globalStorage/state.vscdb',
                      cls.log_locations)

    def test_fidelity_warnings_include_unverified(self):
        warnings = cursor_mod.fidelity_warnings()
        texts = [w['text'] for w in warnings]
        self.assertTrue(any(cursor_mod.UNVERIFIED_WARNING in t for t in texts))
        self.assertTrue(all(w.get('scope') for w in warnings))

    def test_capabilities_flags(self):
        caps = cursor_mod.CursorParser.capabilities
        self.assertTrue(caps['has_tool_calls'])
        self.assertTrue(caps['has_reasoning'])
        # File fields are context hints, not edit proof: change-file sets
        # must come from git.
        self.assertFalse(caps['has_file_attribution'])


class TestCursorRegistry(CursorBase):
    def test_registry_discovers_cursor(self):
        # NOTE: importlib loads create separate class objects (same
        # caveat as the old suite), so identity is asserted on behaviour,
        # not on class object identity.
        self.assertIn('cursor', registry_mod.EXTENDED_PARSERS)
        cls = registry_mod.get_parser('cursor')
        self.assertEqual(cls.tool_id, cursor_mod.CursorParser.tool_id)
        self.assertEqual(cls().tool, 'cursor')
        self.assertTrue(registry_mod.check_extended_registry())

    def test_core_registry_untouched(self):
        # The extension registry copies the core dict; importing it must
        # not mutate the core registry body.
        core = registry_mod.backfill_mod.PARSERS
        self.assertEqual(set(core), {'claude-code', 'codex', 'workbuddy'})
        self.assertNotIn('cursor', core)
        self.assertIsNot(core, registry_mod.EXTENDED_PARSERS)
        # The parser module itself never imports core code (docstring
        # mentions do not count -- only real import statements do).
        import re
        source = (SKILL / 'scripts' / 'relay_cursor.py').read_text(encoding='utf-8')
        self.assertIsNone(re.search(
            r'^\s*(import|from)\s+relay_(backfill|flow|sync|ctl|accept)\b',
            source, re.M))

    def test_registry_rejects_broken_parser(self):
        broken = type('Broken', (), {})
        with self.assertRaises(CursorRelayError):
            registry_mod.check_extended_registry({'broken': broken})


if __name__ == '__main__':
    unittest.main()
