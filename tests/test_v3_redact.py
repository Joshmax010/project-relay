"""v3 redact tests (batch 18): secrets never land in the archive.

Covers scripts/relay_flow.py REDACT_PATTERNS/redact()/capture() and
scripts/relay_sync.py _PROTECTED_HEADER_FIELDS/_redact_entry_text/
_redact_payload/_verify_redacted_entries/_fix_local_redactions/
_is_raw_entry_rel/_is_payload_file.

Secrets here are fake fixtures only (AWS's documented example key,
synthetic tokens); they exist so the patterns have something to match.

Fixed in this batch (2026-11-13, branch slim-v3): _redact_payload
used to raise NameError (relay_sync._FLOW never initialised) and a
stats/ dir under an agent used to ship in the zip. Both are fixed;
the two tests below used to assert the old behaviour and now assert
the intended behaviour.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from v3_helpers import V3Base, relay, flow, sync, stats, fork, RelayError, GOAL_TEXT

# Fake fixtures: shapes the patterns match, values that mean nothing.
AWS_KEY = 'AKIAIOSFODNN7EXAMPLE'  # AWS documented example key
GH_TOKEN = 'ghp_' + 'Ab3dEf7hIjKlMnOpQrStUvWxYz0123456789'
PHONE = '13812345678'
OPENAI_KEY = 'sk-' + 'A' * 24


def _entry_text(header, body):
    """Serialise an entry exactly the way _redact_entry_text does."""
    head = json.dumps(header, ensure_ascii=False, sort_keys=True, indent=2)
    return head + sync.BODY_DELIM + body


def _verified_header(phone_in_fingerprint=PHONE):
    """A header whose fact_hash matches its fingerprint (capture rule),
    with an 11-digit run sitting inside the protected fingerprint."""
    fp = {'anchor': {'branch': 'main', 'head': phone_in_fingerprint},
          'code': {}, 'gaps': []}
    fact_hash = relay.digest(json.dumps(
        {'anchor': fp['anchor'], 'code': fp['code'], 'gaps': fp['gaps']},
        sort_keys=True, ensure_ascii=False).encode('utf-8'))
    return {'version': 1, 'seq': 1, 'agent': 'codex',
            'fingerprint': fp, 'fact_hash': fact_hash,
            'anchor': fp['anchor'],
            'backfill': {'session_key': 'x ' + PHONE + ' y', 'events': 2},
            'user_message': 'key ' + AWS_KEY + ' done',
            'note': 'call ' + PHONE}


class TestRedact(V3Base):
    def test_aws_key_removed_text_intact(self):
        text, hits = flow.redact('key ' + AWS_KEY + ' here')
        self.assertEqual('key  here', text)
        self.assertEqual(1, hits)
        self.assertNotIn(AWS_KEY, text)

    def test_github_token_removed(self):
        text, hits = flow.redact('token ' + GH_TOKEN + ' end')
        self.assertEqual('token  end', text)
        self.assertEqual(1, hits)

    def test_mobile_phone_removed(self):
        text, hits = flow.redact('call ' + PHONE + ' end')
        self.assertEqual('call  end', text)
        self.assertEqual(1, hits)

    def test_plain_text_untouched(self):
        text, hits = flow.redact('plain hello world, no secrets here')
        self.assertEqual('plain hello world, no secrets here', text)
        self.assertEqual(0, hits)

    def test_multiple_secrets_counted(self):
        text, hits = flow.redact('key %s and call %s end'
                                 % (AWS_KEY, PHONE))
        self.assertEqual(2, hits)
        self.assertNotIn(AWS_KEY, text)
        self.assertNotIn(PHONE, text)

    def test_openai_bearer_password_patterns(self):
        text, hits = flow.redact('k %s b Bearer %s p password: hunter2'
                                 % (OPENAI_KEY, 'B' * 24))
        self.assertEqual(3, hits)
        self.assertNotIn(OPENAI_KEY, text)
        self.assertNotIn('hunter2', text)

    def test_phone_glued_to_letters_is_not_a_number(self):
        # Code comment (relay_flow.py, mobile_cn): a number glued to
        # ASCII letters reads as part of an identifier, not a number.
        text, hits = flow.redact('user' + PHONE)
        self.assertEqual('user' + PHONE, text)
        self.assertEqual(0, hits)

    def test_long_hex_hash_keeps_its_digits(self):
        # P0 (2026-10-29): an 11-digit run inside a 64-hex file hash
        # must survive -- it is not a phone number.
        hexhash = ('ab' + PHONE + 'cdef0123456789' * 5)[:64]
        self.assertEqual(64, len(hexhash))
        text, hits = flow.redact('hash ' + hexhash + ' end')
        self.assertEqual('hash ' + hexhash + ' end', text)
        self.assertEqual(0, hits)


class TestCaptureRedaction(V3Base):
    def test_capture_message_secrets_never_land(self):
        root = self.init()
        self.capture(root, 'codex',
                     message='key %s and call %s end' % (AWS_KEY, PHONE))
        entries = self.entries(root, 'codex')
        self.assertEqual(1, len(entries))
        text = entries[0]['path'].read_text(encoding='utf-8')
        self.assertNotIn(AWS_KEY, text)
        self.assertNotIn(PHONE, text)
        self.assertEqual('key  and call  end',
                         entries[0]['header']['user_message'])
        self.assertEqual(2, entries[0]['header']['redacted_count'])

    def test_capture_plain_message_survives_verbatim(self):
        root = self.init()
        self.capture(root, 'codex', message='hello world, no secrets')
        entries = self.entries(root, 'codex')
        self.assertEqual('hello world, no secrets',
                         entries[0]['header']['user_message'])
        self.assertEqual(0, entries[0]['header']['redacted_count'])


class TestRedactEntryText(V3Base):
    def test_protected_fields_pass_through_other_fields_redacted(self):
        header = _verified_header()
        body = 'body has ' + GH_TOKEN + ' and phone ' + PHONE + '\n'
        new_text, hits = sync._redact_entry_text(
            _entry_text(header, body), flow)
        new_header, new_body = sync._parse_entry_text(new_text)
        # Protected verification fields are byte-identical.
        self.assertEqual(header['fingerprint'], new_header['fingerprint'])
        self.assertEqual(header['fact_hash'], new_header['fact_hash'])
        self.assertEqual(header['anchor'], new_header['anchor'])
        self.assertEqual(header['backfill'], new_header['backfill'])
        # Every other string leaf and the whole body are redacted.
        self.assertEqual('key  done', new_header['user_message'])
        self.assertEqual('call ', new_header['note'])
        self.assertNotIn(GH_TOKEN, new_body)
        self.assertNotIn(PHONE, new_body)
        self.assertGreater(hits, 0)

    def test_clean_entry_round_trips_byte_identical(self):
        header = _verified_header(phone_in_fingerprint='plainhead')
        header['user_message'] = 'nothing secret here'
        header['note'] = 'also clean'
        header.pop('backfill')
        text = _entry_text(header, 'clean body\n')
        new_text, hits = sync._redact_entry_text(text, flow)
        self.assertEqual(0, hits)
        self.assertEqual(text, new_text)

    def test_malformed_entry_raises_value_error(self):
        with self.assertRaises(ValueError):
            sync._redact_entry_text('no header, no delimiter here', flow)


class TestVerifyRedactedEntries(V3Base):
    def _redacted_pair(self):
        header = _verified_header()
        text = _entry_text(header, 'body has ' + GH_TOKEN + '\n')
        new_text, _ = sync._redact_entry_text(text, flow)
        rel = 'agents/codex/raw/cap-0001.md'
        return rel, text.encode('utf-8'), new_text.encode('utf-8')

    def test_fingerprint_with_11_digits_still_verifies_after_redact(self):
        # P0 regression: the phone-shaped run inside the fingerprint
        # must not be touched, so verification still passes.
        rel, orig, red = self._redacted_pair()
        new_header, _ = sync._parse_entry_text(red.decode('utf-8'))
        self.assertIn(PHONE, new_header['fingerprint']['anchor']['head'])
        self.assertTrue(sync._verify_entry_id(new_header))
        sync._verify_redacted_entries({rel: orig}, {rel: red})  # no raise

    def test_tampered_protected_field_fails_closed(self):
        rel, orig, red = self._redacted_pair()
        header, body = sync._parse_entry_text(red.decode('utf-8'))
        header['fingerprint']['anchor']['head'] = 'tampered'
        bad = _entry_text(header, body).encode('utf-8')
        with self.assertRaises(sync.RelayError):
            sync._verify_redacted_entries({rel: orig}, {rel: bad})

    def test_redact_payload_redacts_entry_text(self):
        # BUG fixed (_FLOW initialised): _redact_payload now rescans
        # the payload instead of raising NameError on first use.
        rel, orig, expected = self._redacted_pair()
        out, hits = sync._redact_payload({rel: orig})
        self.assertEqual(expected, out[rel])
        self.assertGreater(hits, 0)
        self.assertNotIn(GH_TOKEN.encode(), out[rel])


class TestIsRawEntryRel(V3Base):
    def test_accepts_cap_and_archived_cap(self):
        self.assertTrue(sync._is_raw_entry_rel(
            'agents/codex/raw/cap-0001.md'))
        self.assertTrue(sync._is_raw_entry_rel(
            'agents/codex/raw/archived/cap-0001.md'))
        self.assertTrue(sync._is_raw_entry_rel(
            'agents/claude-code/raw/cap-0042.md'))

    def test_rejects_other_markdown(self):
        for rel in ('agents/codex/raw/notes.md',
                    'agents/codex/raw/cap-0001.txt',
                    'agents/codex/memory.md',
                    'agents/codex/state/cap-0001.md',
                    'agents/codex/raw/sub/cap-0001.md',
                    'agents/codex/raw/cap-0001.md.bak',
                    'goal.md'):
            self.assertFalse(sync._is_raw_entry_rel(rel), rel)


class TestIsPayloadFile(V3Base):
    def test_device_local_files_do_not_ship(self):
        for rel in ('x.lock',
                    'agents/codex/state/x.lock',
                    'checksum-manifest.json',
                    'stats/capture.jsonl',
                    'stats/fuse.json',
                    'agents/codex/tx/plan.json',
                    'agents/codex/backups/cap-0001.md',
                    'config.json',
                    'state/config.json',
                    'state/cursor.json',
                    'state/baseline.json',
                    'agents/codex/state/cursor.json',
                    'agents/codex/state/baseline.json',
                    'agents/codex/state/numbers.json',
                    'agents/codex/state/enforce.json',
                    'agents/codex/state/forks.json',
                    'agents/codex/recipe.json'):
            self.assertFalse(sync._is_payload_file(rel), rel)

    def test_archive_files_ship(self):
        for rel in ('goal.md', 'state.md', 'backlog.md', 'decisions.md',
                    'stack.md', 'outlook.md', 'index.md',
                    'agents/codex/memory.md',
                    'agents/codex/raw/cap-0001.md',
                    'agents/codex/raw/archived/cap-0001.md',
                    'agents/codex/state/last-summary.md',
                    'receipts/goal-0001.json',
                    'state/goal-draft.json'):
            self.assertTrue(sync._is_payload_file(rel), rel)

    def test_agent_stats_dir_does_not_ship(self):
        # Fixed in this batch: 'stats' joined _LOCAL_ONLY_AGENT_DIRS,
        # so a stats/ dir under an agent no longer ships either.
        self.assertFalse(sync._is_payload_file(
            'agents/codex/stats/rows.json'))


class TestFixLocalRedactions(V3Base):
    def test_repairs_local_files_and_reports_fixed(self):
        root = self.init()
        store = self.store_dir(root)
        original = {'goal.md': b'call ' + PHONE.encode() + b'\n',
                    'agents/codex/memory.md': b'clean\n'}
        redacted = {'goal.md': b'call \n',
                    'agents/codex/memory.md': b'clean\n'}
        for rel, data in original.items():
            (store / rel).write_bytes(data)
        fixed = sync._fix_local_redactions(store, original, redacted)
        self.assertEqual(['goal.md'], fixed)
        self.assertEqual(b'call \n', (store / 'goal.md').read_bytes())
        self.assertEqual(b'clean\n',
                         (store / 'agents/codex/memory.md').read_bytes())


if __name__ == '__main__':
    import unittest
    unittest.main()
