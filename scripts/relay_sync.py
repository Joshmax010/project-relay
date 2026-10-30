"""project-relay v2 sync (plan part 03, steps 2.1-2.5 + S6 merge on the v1 layout).

Slice 1 (2.1-2.2): carrier-independent import/export pipeline + git carrier.
Slice 2 (2.3-2.5, this change): folder carrier (sync-folder store root,
heartbeat locks, conflict-copy isolation, checksum manifest, bundle
rollback) + server carrier (byte-path backends, atomic current swap,
(seq, device_id) anti-overwrite key) + reminder-timing landing (2.5).

Standard library only. Loaded lazily by scripts/relay.py like relay_flow and
relay_backfill (each importlib load creates its own relayctl copy, so tests
must assert sync-raised errors against relay_sync.relay.RelayError).

Mapping note (read first): S6's planned directories (goal/, state/,
sessions/, cursors/, meta/) do not exist in the v1 implementation. The merge
rules in this module are implemented on the actual v1 layout -- six zones
(raw/archive/state/tx/backups/receipts), the archive five (goal/state/
backlog/decisions/lessons), cursor.json, baseline.json, receipts/goal-*.json,
raw/cap-*.md entries. The full mapping table lives in
references/v2-sync.md; nothing here invents S6 directory names.

Decoupling (plan 2.5.6): nothing in this module is called from resume,
capture, or any same-machine flow. resume output contains no sync wording;
the only sync-adjacent state visible there is the goal state, rendered as
"待本机确认" (see relay_flow.resume).
"""

import importlib.util
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import uuid
import zipfile
from pathlib import Path, PurePosixPath

_HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location('relay', _HERE / 'relay.py')
relay = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(relay)

RelayError = relay.RelayError
require = relay.require
utc = relay.utc
digest = relay.digest
atomic = relay.atomic
save = relay.save
read_json = relay.read_json
git = relay.git

_FLOW = None


def _flow():
    """relay_flow, loaded once (single source of truth for redact patterns,
    summarize, and queue observation). Its RelayError class differs from
    this module's, so callers wrap flow calls and re-raise as RelayError."""
    global _FLOW
    if _FLOW is None:
        spec = importlib.util.spec_from_file_location('relay_flow', _HERE / 'relay_flow.py')
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _FLOW = mod
    return _FLOW


BUNDLE_FORMAT = 'project-relay-export'
FORMAT_MAJOR = 1
# Minor 1 (slice 2): manifest gains the encryption reservation fields
# (encryption / encryption_salt, plan 2.4.6). verify_bundle only warns on
# minor mismatch, so minor-0 bundles still import.
FORMAT_MINOR = 1
TOOL_VERSION = 'project-relay-v2'

# ---------------------------------------------------------------- sync signing (batch 2, B1)
# Git-carrier "push is trust" fix: a holder of push rights could rewrite
# a packed tree and the peer would merge it (the folder/server carriers
# do not have this hole -- their bundles are hash-gated by manifest, see
# verify_bundle -- but git rebuilds its bundle manifest from disk, so
# content tampering there is invisible until merge). The fix is a
# signature over the bundle manifest, travelling in the committed
# sync-manifest, verified before any landing.
#
# DEVIATION (documented in references/v2-sync.md §15): the task brief
# asked for an HMAC signature, but an HMAC cannot do what the brief
# also requires -- TOFU pinning on first sight by a peer that has never
# seen the key. A symmetric key is either shared with the peer (then a
# pusher who can read the remote can also read it, and the signature
# proves nothing) or never reaches the peer (then the peer cannot
# verify at all). Only an asymmetric signature makes "first sight pins
# the key, later change needs --repin" true: the private seed stays
# home-only (0600), the public key travels in the manifest and is
# pinned per source device_id on first sight. The implementation below
# is pure-stdlib Ed25519 (RFC 8032, hashlib.sha512 only, no third-party
# crypto), so the "standard library only" constraint still holds; the
# signed payload canonical form is identical to the folder/server
# manifest files dict (same _collect_payload + redaction pipeline), so
# the pack manifest calibre is consistent across carriers.
_ED_Q = 2 ** 255 - 19
_ED_L = 2 ** 252 + 27742317777372353535851937790883648493
_ED_D = (-121665 * pow(121666, _ED_Q - 2, _ED_Q)) % _ED_Q
_ED_I = pow(2, (_ED_Q - 1) // 4, _ED_Q)


def _ed_xrecover(y, sign):
    xx = (y * y - 1) * pow((_ED_D * y * y + 1) % _ED_Q, _ED_Q - 2, _ED_Q) % _ED_Q
    x = pow(xx, (_ED_Q + 3) // 8, _ED_Q)
    if (x * x - xx) % _ED_Q != 0:
        x = x * _ED_I % _ED_Q
    if (x * x - xx) % _ED_Q != 0:
        raise ValueError('点不在曲线上')
    if x % 2 != sign:
        x = _ED_Q - x
    return x


_ED_BY = (4 * pow(5, _ED_Q - 2, _ED_Q)) % _ED_Q
_ED_B = (_ed_xrecover(_ED_BY, 0), _ED_BY)


def _ed_add(P, Q):
    x1, y1 = P
    x2, y2 = Q
    d = _ED_D
    x3 = (x1 * y2 + x2 * y1) * pow((1 + d * x1 * x2 * y1 * y2) % _ED_Q,
                                   _ED_Q - 2, _ED_Q) % _ED_Q
    y3 = (y1 * y2 + x1 * x2) * pow((1 - d * x1 * x2 * y1 * y2) % _ED_Q,
                                   _ED_Q - 2, _ED_Q) % _ED_Q
    return (x3, y3)


def _ed_mul(P, e):
    Q = (0, 1)
    while e:
        if e & 1:
            Q = _ed_add(Q, P)
        P = _ed_add(P, P)
        e >>= 1
    return Q


def _ed_encode_point(P):
    x, y = P
    return (y | ((x & 1) << 255)).to_bytes(32, 'little')


def _ed_decode_point(data):
    if len(data) != 32:
        raise ValueError('公钥/签名点长度错误')
    y = int.from_bytes(data, 'little') & ((1 << 255) - 1)
    sign = data[31] >> 7
    x = _ed_xrecover(y, sign)
    if (-x * x + y * y - 1 - _ED_D * x * x * y * y) % _ED_Q != 0:
        raise ValueError('点不在曲线上')
    return (x, y)


def _ed_clamp(h):
    a = bytearray(h[:32])
    a[0] &= 248
    a[31] &= 127
    a[31] |= 64
    return int.from_bytes(a, 'little')


def _ed_public_from_seed(seed):
    h = hashlib.sha512(seed).digest()
    return _ed_encode_point(_ed_mul(_ED_B, _ed_clamp(h)))


def _ed_sign(seed, msg):
    h = hashlib.sha512(seed).digest()
    prefix = h[32:]
    pub = _ed_public_from_seed(seed)
    r = int.from_bytes(hashlib.sha512(prefix + msg).digest(), 'little') % _ED_L
    r_enc = _ed_encode_point(_ed_mul(_ED_B, r))
    k = int.from_bytes(hashlib.sha512(r_enc + pub + msg).digest(), 'little') % _ED_L
    s = (r + k * _ed_clamp(h)) % _ED_L
    return r_enc + s.to_bytes(32, 'little')


def _ed_verify(pub, msg, sig):
    if len(pub) != 32 or len(sig) != 64:
        return False
    try:
        a_point = _ed_decode_point(pub)
        r_point = _ed_decode_point(sig[:32])
    except ValueError:
        return False
    s = int.from_bytes(sig[32:], 'little')
    if s >= _ED_L:
        return False
    k = int.from_bytes(hashlib.sha512(sig[:32] + pub + msg).digest(),
                       'little') % _ED_L
    return _ed_mul(_ED_B, s) == _ed_add(r_point, _ed_mul(a_point, k))


SYNC_KEY_FILENAME = '.project-relay-sync-key.json'
SYNC_PINS_FILENAME = '.project-relay-sync-pins.json'


def _sync_key_path(store=None):
    """The signing private seed: next to the store in use, never in any
    store, bundle, or synced tree (a key inside the synced tree would be
    readable by exactly the pusher the signature is meant to stop).

    Batch 3, F4: the location is parent-of-store, not home. For the
    production store ~/.project-relay the parent IS the home directory,
    so the production path is unchanged; but a --store-root store (tests,
    or a store deliberately placed elsewhere) no longer reads or writes
    the user's real home key file -- the old behavior leaked test keys
    into the real home and shared one key across test devices. store=None
    keeps the legacy home path for callers without a store context."""
    if store is not None:
        return Path(store).expanduser().resolve().parent / SYNC_KEY_FILENAME
    return relay.home_dir() / SYNC_KEY_FILENAME


def _sync_pins_path(store=None):
    """TOFU pins: source device_id -> pinned public key. Public keys
    only, so no permission requirement beyond the default. Same
    parent-of-store rule as the key (batch 3, F4)."""
    if store is not None:
        return Path(store).expanduser().resolve().parent / SYNC_PINS_FILENAME
    return relay.home_dir() / SYNC_PINS_FILENAME


def ensure_sync_key(store=None):
    """Generate the device signing key on first use (git sync-setup is
    the primary path; git pack lazily ensures it for pre-existing
    setups). Returns the key record. The file is chmod 0600 after every
    write (save() alone would leave 0644) and re-tightened on load."""
    path = _sync_key_path(store)
    try:
        data = read_json(path)
    except (ValueError, OSError):
        data = None
    if isinstance(data, dict) and data.get('private_seed') and data.get('public_key'):
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return data
    seed = secrets.token_bytes(32)
    pub = _ed_public_from_seed(seed)
    data = {'key_id': digest(pub)[:16], 'private_seed': seed.hex(),
            'public_key': pub.hex(), 'created_at': utc()}
    save(path, data)
    os.chmod(path, 0o600)
    return data


def _signing_payload(device_id, exported_at, pack_seq, file_hashes):
    """Canonical signed bytes: device + time + sequence + the full
    path->hash manifest (the same dict folder/server bundles hash-gate).
    Signing paths alone would not detect content tampering; signing the
    hashes does, because the verifier recomputes them from disk."""
    return json.dumps({'device_id': device_id, 'exported_at': exported_at,
                       'pack_seq': pack_seq,
                       'files': {k: file_hashes[k] for k in sorted(file_hashes)}},
                      ensure_ascii=False, sort_keys=True,
                      separators=(',', ':')).encode('utf-8')


def sign_bundle_manifest(device_id, exported_at, pack_seq, file_hashes,
                         store=None):
    """Sign one git pack's manifest. Returns the manifest 'signature'
    dict (public key included: first sight pins it, see TOFU below)."""
    key = ensure_sync_key(store)
    sig = _ed_sign(bytes.fromhex(key['private_seed']),
                   _signing_payload(device_id, exported_at, pack_seq, file_hashes))
    return {'algorithm': 'ed25519', 'key_id': key['key_id'],
            'public_key': key['public_key'], 'signature': sig.hex()}


def _load_pins(store=None):
    try:
        pins = read_json(_sync_pins_path(store))
    except (ValueError, OSError):
        return {}
    return pins if isinstance(pins, dict) else {}


def _save_pins(pins, store=None):
    save(_sync_pins_path(store), pins)


def verify_bundle_signature(manifest, file_hashes, trust_unsigned=False,
                            repin=False, store=None):
    """Verify a git sync-manifest's signature against recomputed hashes.

    Returns a verification dict (status: verified / tofu-pinned /
    repinned / trusted-unsigned) or raises RelayError naming the cause,
    so the caller refuses landing before any write. TOFU: a source
    device_id with no pin gets its public key pinned on first sight --
    that first sight is trusted by construction (same caveat as SSH
    host keys: it must be the user's own fresh pack, not a stranger's).
    A later public-key change is refused unless --repin explicitly
    accepts it. An unsigned bundle is refused unless --trust-unsigned
    explicitly allows it. A manifest without a device_id can still be
    cryptographically verified, but there is nothing to pin it to, so
    it verifies without pinning (status verified-no-pin)."""
    sig = manifest.get('signature')
    device_id = manifest.get('source_device_id')
    if not isinstance(sig, dict) or sig.get('algorithm') != 'ed25519' \
            or not sig.get('signature') or not sig.get('public_key'):
        if trust_unsigned:
            return {'status': 'trusted-unsigned', 'device_id': device_id,
                    'key_id': None,
                    'note': '包未签名，用户以 --trust-unsigned 明示放行'}
        raise RelayError('Git 包未签名，拒绝落盘（源设备 %s）：请先在源设备重新 pack '
                         '生成签名，或以 --trust-unsigned 明示放行无签包' % device_id)
    try:
        pub = bytes.fromhex(sig['public_key'])
        sig_bytes = bytes.fromhex(sig['signature'])
    except ValueError:
        raise RelayError('Git 包签名格式损坏，拒绝落盘')
    key_id = digest(pub)[:16]
    if sig.get('key_id') and sig['key_id'] != key_id:
        raise RelayError('Git 包签名 key_id 与公钥不符，拒绝落盘')
    payload = _signing_payload(device_id, manifest.get('exported_at'),
                               manifest.get('pack_seq'), file_hashes)
    if not _ed_verify(pub, payload, sig_bytes):
        raise RelayError('Git 包验签失败：包内容与签名不符（文件、时间或序列号'
                         '可能被篡改），拒绝落盘')
    if not device_id:
        return {'status': 'verified-no-pin', 'device_id': device_id,
                'key_id': key_id, 'note': '签名有效，但包无设备 ID，无法钉死'}
    pins = _load_pins(store)
    pinned = pins.get(device_id)
    if pinned is None:
        pins[device_id] = {'key_id': key_id, 'public_key': sig['public_key'],
                           'first_seen': utc(),
                           'source_device': manifest.get('source_device')}
        _save_pins(pins, store)
        return {'status': 'tofu-pinned', 'device_id': device_id,
                'key_id': key_id, 'note': '首次见该设备签名公钥，已钉死（TOFU）'}
    if pinned.get('public_key') != sig['public_key']:
        if repin:
            prev = pinned.get('key_id')
            pins[device_id] = {'key_id': key_id, 'public_key': sig['public_key'],
                               'first_seen': pinned.get('first_seen'),
                               'repinned_at': utc(), 'previous_key_id': prev,
                               'source_device': manifest.get('source_device')}
            _save_pins(pins, store)
            return {'status': 'repinned', 'device_id': device_id,
                    'key_id': key_id, 'previous_key_id': prev,
                    'note': '签名公钥已变更，用户以 --repin 明示接受新公钥'}
        raise RelayError('Git 包签名公钥与钉死的公钥不符（设备 %s，钉死 %s，当前 %s），'
                         '拒绝落盘：密钥可能被替换，请核实后以 --repin 明示接受'
                         % (device_id, pinned.get('key_id'), key_id))
    return {'status': 'verified', 'device_id': device_id, 'key_id': key_id}
MARKER_PREFIXES = ('<<<<<<<', '=======', '>>>>>>>')

# Files in the store root that never enter a git pack commit. sync-manifests/
# MUST be committed (it is how unpack finds project bundles), so it is
# deliberately absent from this list.
GITIGNORE_TEXT = """registry.json
global/
trash/
sync-config.json
sync-receipts/
unclaimed/
*.pyc
__pycache__/
"""

# Payload inclusion, per project store dir, relative posix paths.
# Everything else in a project store is device-local and never ships:
# state/config.json (threshold prefs), enforce.json, digest/numbers.json
# (precompress working files), backlog-notice.json, pack-seq.json,
# goal-conflict.json, goal-imported.json, rollback-note.json, cursor.json
# and baseline.json (local measurement state), raw-index.json (local index),
# tx/ (recovery state), backups/ (local insurance, S6: not synced).
ARCHIVE_PREFIX = 'archive/'
STATE_INCLUDED = {
    'state/split-index.json',
    'state/goal-tentative.md', 'state/goal-tentative.json',
    'state/goal-draft.md', 'state/goal-draft.json',
    'state/last-summary.json', 'state/handoff-index.md',
}
STATE_SUMMARIES_PREFIX = 'state/summaries/'
RAW_PREFIX = 'raw/'
RECEIPTS_PREFIX = 'receipts/'


def _compact_ts(iso):
    return re.sub(r'[^0-9]', '', iso)[:14]


def _sha256(data):
    return digest(data)


def _jail_check(rel):
    """Bundle path jail: posix relative, normalized, no drive, no '..'."""
    require(isinstance(rel, str) and rel, '包内路径为空')
    require('\\' not in rel and ':' not in rel, '包内路径非法：' + rel)
    parts = PurePosixPath(rel).parts
    require(not PurePosixPath(rel).is_absolute() and '..' not in parts,
            '包内路径越界：' + rel)
    require(PurePosixPath(rel).as_posix() == rel, '包内路径未规范化：' + rel)


_PROJECT_KEY_RE = re.compile(r'^[0-9a-f]{16}$')
_PROJECT_SLUG_RE = re.compile(r'^[a-z0-9._-]+$')


def _validate_project_meta(key, slug):
    """Batch 3, C1: manifest-controlled project identity. verify_bundle
    jails listed file paths but never looked at these two fields, so a
    crafted key like '../../../../escaped' sent the later _claim.json
    write outside the store. Keys are digest[:16] hex and slugs are
    slug_for() output; anything else is a crafted manifest, refused
    before any write."""
    require(isinstance(key, str) and _PROJECT_KEY_RE.match(key),
            '包内项目键非法：%r' % (key,))
    require(isinstance(slug, str) and _PROJECT_SLUG_RE.match(slug)
            and slug not in ('.', '..'),
            '包内项目名非法：%r' % (slug,))


def _contained_unclaimed_dir(store, key, slug):
    """Unclaimed landing dir, proven inside store/unclaimed (3.8-safe:
    relative_to raises ValueError on escape; is_relative_to needs 3.9)."""
    dest = (Path(store) / 'unclaimed' / ('%s-%s' % (key, slug))).resolve()
    base = (Path(store) / 'unclaimed').resolve()
    try:
        dest.relative_to(base)
    except ValueError:
        raise RelayError('待认领目标越界：' + str(dest))
    return dest


def _no_symlink_components(base, rel):
    """Reject any symlink component under base (files and dirs alike)."""
    p = base
    for part in PurePosixPath(rel).parts:
        p = p / part
        require(not p.is_symlink(), '包内含符号链接：' + rel)


def _is_payload_file(rel):
    # Rollback trash (plan 2.3.6) is archived, not deleted -- but it must
    # never ship: it is yesterday's replaced bytes, not the live archive.
    if rel.startswith('archive/rollback-trash/'):
        return False
    if rel.startswith(ARCHIVE_PREFIX) or rel.startswith(RECEIPTS_PREFIX):
        return True
    if rel in STATE_INCLUDED or rel.startswith(STATE_SUMMARIES_PREFIX):
        return True
    if rel.startswith(RAW_PREFIX) and rel.endswith('.md'):
        name = rel[len(RAW_PREFIX):]
        return name.startswith('cap-') or name.startswith('archived/cap-')
    return False


def _collect_payload(store_dir):
    """Read a project's shippable files. Returns {relpath: bytes}."""
    out = {}
    require((store_dir / 'archive').is_dir(), '项目仓缺少 archive 区：' + str(store_dir))
    for path in sorted(store_dir.rglob('*')):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(store_dir).as_posix()
        if _is_payload_file(rel):
            out[rel] = path.read_bytes()
    for name in ('goal.md', 'state.md', 'backlog.md', 'decisions.md', 'lessons.md'):
        require(('archive/' + name) in out, '档案缺失：archive/' + name)
    return out


# Verification inputs inside a raw entry header. These are evidence, not
# prose: the peer recomputes fact_hash from them, so redacting them would
# silently rewrite what the peer verifies. P0 (2026-10-29 e2e): the
# phone-number pattern deleted 11 digits out of a 64-hex file hash inside
# fingerprint.code, every entry then failed id-hash verification on
# import, and the rescan wrote the damage back locally. These fields are
# never redacted at any level. 'anchor' duplicates fingerprint.anchor, so
# it is protected too, to keep the two copies identical.
_PROTECTED_HEADER_FIELDS = ('fingerprint', 'fact_hash', 'backfill', 'anchor')


def _is_raw_entry_rel(rel):
    """True for payload paths that are capture entry files (same predicate
    as _is_payload_file's raw branch)."""
    if not (rel.startswith(RAW_PREFIX) and rel.endswith('.md')):
        return False
    name = rel[len(RAW_PREFIX):]
    return name.startswith('cap-') or name.startswith('archived/cap-')


def _redact_header_value(value, flow):
    """Redact one header value, recursing into containers. Protected
    verification fields are passed through untouched at every level.
    Returns (value, hits)."""
    if isinstance(value, str):
        return flow.redact(value)
    if isinstance(value, dict):
        out, hits = {}, 0
        for k, v in value.items():
            if k in _PROTECTED_HEADER_FIELDS:
                out[k] = v
            else:
                out[k], n = _redact_header_value(v, flow)
                hits += n
        return out, hits
    if isinstance(value, list):
        out, hits = [], 0
        for v in value:
            nv, n = _redact_header_value(v, flow)
            out.append(nv)
            hits += n
        return out, hits
    return value, 0


def _redact_entry_text(text, flow):
    """Field-level redaction for one raw entry file. The body is redacted
    in full; in the header, verification inputs are passed through
    untouched and every other string leaf is redacted. The head is
    re-serialized with the same settings capture uses, so a zero-hit
    entry round-trips byte-identical. Raises ValueError on a malformed
    entry (caller converts to a fail-closed RelayError)."""
    head, body = text.split(BODY_DELIM, 1)
    header = json.loads(head)
    new_header, head_hits = _redact_header_value(header, flow)
    new_body, body_hits = flow.redact(body)
    new_head = json.dumps(new_header, ensure_ascii=False, sort_keys=True, indent=2)
    return new_head + BODY_DELIM + new_body, head_hits + body_hits


def _verify_redacted_entries(original, redacted):
    """Fail-closed gate for the redact rescan (P0). Runs after redaction,
    before the caller writes anything back or builds a bundle, so a
    rescan that damages verification inputs aborts the pack with nothing
    mutated: no local write-back, no bundle, no receipt.

    For every raw entry: the protected verification fields must be
    identical, and a non-degraded entry that fails verification after
    redaction aborts the pack -- whether the damage came from the rescan
    (verified before, broken after) or was already there (a hand-broken
    fingerprint must not ship as a cleanly receipted bundle either).
    Entries that were already unverifiable by design -- degraded
    captures carry fact_hash None -- keep the peer's existing discard
    behavior and must not brick packing; their protected fields are
    still tripwired above."""
    for rel in sorted(original):
        if not _is_raw_entry_rel(rel):
            continue
        try:
            old_text = original[rel].decode('utf-8')
            old_header, _ = _parse_entry_text(old_text)
        except (UnicodeDecodeError, RelayError, AttributeError, TypeError, ValueError):
            raise RelayError('脱敏复扫无法建立验算基线，原料条目 %s 格式损坏，拒绝打包' % rel)
        try:
            new_text = redacted[rel].decode('utf-8')
            new_header, _ = _parse_entry_text(new_text)
        except (UnicodeDecodeError, RelayError, AttributeError, TypeError, ValueError):
            raise RelayError('pack 已中止：脱敏复扫改动了验算字段，原料条目 %s 脱敏后无法解析；本地文件未改动，未装包' % rel)
        for field in _PROTECTED_HEADER_FIELDS:
            if old_header.get(field) != new_header.get(field):
                raise RelayError('pack 已中止：脱敏复扫改动了验算字段，原料条目 %s 的 %s 被改动；本地文件未改动，未装包' % (rel, field))
        if new_header.get('degraded'):
            continue
        if not _verify_entry_id(new_header):
            if _verify_entry_id(old_header):
                raise RelayError('pack 已中止：脱敏复扫改动了验算字段，原料条目 %s 的指纹验算失败；本地文件未改动，未装包' % rel)
            raise RelayError('pack 已中止：原料条目 %s 指纹验算失败（装包前验门）；本地文件未改动，未装包' % rel)


def _redact_payload(files):
    """Rescan every payload text as untrusted input. Returns (files, hits).

    Raw entries get field-level redaction (body in full, header minus
    verification inputs); every other payload file is redacted in full.
    All v1 payload files are UTF-8 text by construction; a non-UTF-8 file
    aborts the export instead of shipping bytes nobody can audit.
    The verification gate runs here -- after redaction, before the caller
    writes anything back or builds a bundle -- so a rescan that damages
    verification inputs aborts the pack with nothing mutated (P0)."""
    flow = _flow()
    out = {}
    hits = 0
    for rel in sorted(files):
        try:
            text = files[rel].decode('utf-8')
        except UnicodeDecodeError:
            raise RelayError('负载非 UTF-8 文本，拒绝导出：' + rel)
        if _is_raw_entry_rel(rel):
            try:
                new_text, n = _redact_entry_text(text, flow)
            except ValueError:
                raise RelayError('脱敏复扫无法解析原料条目 %s：格式损坏，拒绝打包' % rel)
            out[rel] = new_text.encode('utf-8')
            hits += n
        else:
            new_text, n = flow.redact(text)
            out[rel] = new_text.encode('utf-8')
            hits += n
    _verify_redacted_entries(files, out)
    return out, hits


def _fix_local_redactions(store_dir, original, redacted):
    """Write rescanned redactions back to the local files as well.

    Bundle-only redaction would make the next pack re-export the unredacted
    originals and manufacture merge conflicts on the peer, so a rescan hit
    repairs the local copy too (atomic write, counted in the receipt)."""
    fixed = []
    for rel in sorted(original):
        if original[rel] != redacted[rel]:
            atomic(store_dir / rel, redacted[rel])
            fixed.append(rel)
    return fixed


# ---------------------------------------------------------------- config
def load_sync_config(store):
    """Sync config, any carrier. Legacy git configs (remote, no carrier
    key) are accepted; a dict with neither carrier nor remote is corrupt."""
    path = store / 'sync-config.json'
    if not path.exists():
        return None
    cfg = read_json(path)
    require(isinstance(cfg, dict) and
            (cfg.get('carrier') in ('git', 'folder', 'server') or cfg.get('remote')),
            '同步配置损坏：' + str(path))
    return cfg


def save_sync_config(store, cfg):
    save(store / 'sync-config.json', cfg)


def _device(store, cfg):
    return cfg.get('device_name') or 'device', cfg.get('device_id') or ''


def _carrier_of(store, cfg, override=None):
    """Resolve the carrier for a command (plan 2.3/2.4 dispatch).

    Explicit --carrier wins; otherwise the config's carrier; a legacy
    config with only a remote is git. No config at all is an error, not
    a silent default: packing without setup must fail loudly."""
    if override is not None:
        require(override in ('git', 'folder', 'server'),
                '未知载体：' + str(override))
        return override
    if cfg is None:
        raise RelayError('未运行 sync-setup：请先运行 relay sync-setup 配置同步载体')
    carrier = cfg.get('carrier')
    if carrier in ('git', 'folder', 'server'):
        return carrier
    if cfg.get('remote'):
        return 'git'
    raise RelayError('同步配置损坏：' + str(Path(store) / 'sync-config.json'))


def _receipt_path(store, kind, key):
    d = store / 'sync-receipts'
    d.mkdir(parents=True, exist_ok=True)
    base = '%s-%s-%s' % (kind, key, _compact_ts(utc()))
    path = d / (base + '.json')
    n = 2
    while path.exists():
        path = d / ('%s-%d.json' % (base, n))
        n += 1
    return path


# ---------------------------------------------------------------- export
def export_bundle(store, toplevels, dest_dir, device_name, device_id, pack_seqs=None):
    """Build a 2.1 export bundle directory. Returns the manifest dict.

    dest_dir is created fresh (an existing non-empty directory is refused,
    so a stale bundle can never be mistaken for a new one). Local files
    repaired by the rescan are written back (see _fix_local_redactions).
    pack_seqs maps project key -> pack sequence number (pack supplies it;
    standalone exports leave it null)."""
    dest = Path(dest_dir)
    require(not dest.exists() or not any(dest.iterdir()), '导出目标目录非空：' + str(dest))
    dest.mkdir(parents=True, exist_ok=True)
    pack_seqs = pack_seqs or {}
    manifest_files = {}
    projects_meta = []
    entry_count = 0
    redacted_total = 0
    for toplevel in toplevels:
        row, store_dir = relay.resolve_store_dir(store, toplevel)
        payload = _collect_payload(store_dir)
        redacted, hits = _redact_payload(payload)
        _fix_local_redactions(store_dir, payload, redacted)
        redacted_total += hits
        proj_dir = dest / 'projects' / (row['key'] + '-' + row['slug'])
        for rel in sorted(redacted):
            target = proj_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(redacted[rel])
            manifest_files['projects/%s-%s/%s' % (row['key'], row['slug'], rel)] = \
                _sha256(redacted[rel])
        entries = [r for r in redacted if r.startswith(RAW_PREFIX) and r.endswith('.md')]
        for rel in entries:
            _parse_entry_text(redacted[rel].decode('utf-8'))
            entry_count += 1
        projects_meta.append({
            'key': row['key'], 'slug': row['slug'],
            'display_name': row.get('display_name', row['slug']),
            'remote': row.get('remote'), 'key_type': row.get('key_type', 'remote'),
            'pack_seq': pack_seqs.get(row['key']),
            'files': sorted('projects/%s-%s/%s' % (row['key'], row['slug'], r)
                            for r in redacted),
        })
    manifest = {
        'format': BUNDLE_FORMAT, 'format_major': FORMAT_MAJOR,
        'format_minor': FORMAT_MINOR, 'exported_at': utc(),
        'source_device': device_name, 'source_device_id': device_id,
        'tool_version': TOOL_VERSION, 'entry_count': entry_count,
        'redacted_count': redacted_total, 'files': manifest_files,
        'projects': projects_meta,
        # Encryption reservation (plan 2.4.6, slice 2): whole-bundle
        # encryption itself is deferred to volume 3, but the fields are
        # fixed now. 'none' is the only value produced today; a future
        # encrypted bundle must set encryption to its scheme name and
        # encryption_salt to the derivation salt (format reserved).
        'encryption': 'none', 'encryption_salt': None,
    }
    save(dest / 'manifest.json', manifest)
    return manifest


def write_bundle_zip(bundle_dir, zip_path):
    """Pack a bundle directory into a zip with identical internal layout."""
    bundle = Path(bundle_dir)
    require((bundle / 'manifest.json').exists(), '非导出包目录：' + str(bundle))
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(bundle.rglob('*')):
            if path.is_file():
                zf.write(path, path.relative_to(bundle).as_posix())
    return Path(zip_path)


def read_bundle_zip(zip_path, dest_dir):
    """Unpack a bundle zip, rejecting absolute/escaping/symlink entries."""
    dest = Path(dest_dir)
    require(not dest.exists() or not any(dest.iterdir()), '解包目标目录非空：' + str(dest))
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            name = info.filename
            _jail_check(name)
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise RelayError('包内含符号链接条目：' + name)
            target = dest / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(zf.read(info))
    return dest


# ---------------------------------------------------------------- verify
def verify_bundle(bundle_dir):
    """2.1 import step 1: manifest-only validation, then full hash check.

    Any failure rejects the whole bundle with zero writes. Returns
    (manifest, files, warnings) where files maps bundle-root-relative posix
    paths to bytes."""
    bundle = Path(bundle_dir)
    mpath = bundle / 'manifest.json'
    require(mpath.exists(), '包内无 manifest.json，拒绝导入')
    try:
        manifest = json.loads(mpath.read_text(encoding='utf-8'))
    except (ValueError, UnicodeDecodeError):
        raise RelayError('manifest.json 无法解析，拒绝导入')
    require(isinstance(manifest, dict), 'manifest.json 不是对象，拒绝导入')
    require(manifest.get('format') == BUNDLE_FORMAT,
            '包格式名不符（%s），拒绝导入' % manifest.get('format'))
    if manifest.get('format_major') != FORMAT_MAJOR:
        raise RelayError(
            '包格式主版本 %s 与本工具 %s 不一致，拒绝导入；'
            '需要先升级/降级工具' % (manifest.get('format_major'), FORMAT_MAJOR))
    warnings = []
    if manifest.get('format_minor') != FORMAT_MINOR:
        warnings.append('包格式次版本 %s 与本工具 %s 不同，未知字段将被忽略'
                        % (manifest.get('format_minor'), FORMAT_MINOR))
    listed = manifest.get('files')
    require(isinstance(listed, dict) and listed, 'manifest 缺少文件清单，拒绝导入')
    for rel in listed:
        _jail_check(rel)
        require(rel.startswith('projects/'), '清单含非项目文件，拒绝导入：' + rel)
    files = {}
    for path in sorted(bundle.rglob('*')):
        if not path.is_file():
            continue
        rel = path.relative_to(bundle).as_posix()
        if rel == 'manifest.json':
            continue
        _jail_check(rel)
        _no_symlink_components(bundle, rel)
        require(rel in listed, '包内有清单外文件，拒绝导入：' + rel)
        data = path.read_bytes()
        require(_sha256(data) == listed[rel], '包内文件哈希不符，拒绝导入：' + rel)
        files[rel] = data
    for rel in listed:
        require(rel in files, '清单内文件缺失，拒绝导入：' + rel)
    return manifest, files, warnings


# ---------------------------------------------------------------- entries
BODY_DELIM = '\n---relay-body---\n'


def _parse_entry_text(text):
    """Parse one cap file. Raises RelayError on any structural problem."""
    require(BODY_DELIM.strip() in text, '采集条目格式损坏')
    head, body = text.split(BODY_DELIM, 1)
    try:
        header = json.loads(head)
    except ValueError:
        raise RelayError('采集条目头不是 JSON')
    require(header.get('version') == 1 and isinstance(header.get('seq'), int),
            '采集条目头格式错误')
    return header, body


def _entry_key(header):
    """Merge identity for a v1 entry: (source, session, fact_hash).

    v1 entries are Markdown files with JSON headers, not JSONL, so the
    plan's JSONL content-hash ID maps to this triple plus a body check:
    identical key + identical body skips, identical key + different body
    keeps both versions (see merge_project)."""
    source = header.get('source') or 'capture'
    return (source, header.get('session'), header.get('fact_hash'))


def _verify_entry_id(header):
    """Recompute a v1 entry's fact_hash from its own header fields.

    Capture entries: digest of {anchor, code, gaps} from the fingerprint
    (the capture dedup key, plan D7). Backfill entries: digest of
    {backfill key, events count}. Returns True when verifiable and equal,
    False when unverifiable or mismatched (caller discards, never raises:
    incoming data is untrusted)."""
    fact = header.get('fact_hash')
    if not isinstance(fact, str) or not fact:
        return False
    try:
        if header.get('source') == 'backfill':
            bf = header.get('backfill') or {}
            expect = digest(json.dumps(
                {'backfill': bf['session_key'], 'events': bf['events']},
                sort_keys=True, ensure_ascii=False).encode('utf-8'))
        else:
            fp = header.get('fingerprint') or {}
            expect = digest(json.dumps(
                {'anchor': fp['anchor'], 'code': fp['code'], 'gaps': fp['gaps']},
                sort_keys=True, ensure_ascii=False).encode('utf-8'))
    except (KeyError, TypeError):
        return False
    return expect == fact


def _has_markers(text):
    for line in text.splitlines():
        s = line.strip()
        if s.startswith(MARKER_PREFIXES):
            return True
    return False


def _parse_local_entries_all(store_dir):
    """All local entries in file order. A damaged local entry is fatal:
    merging must never silently drop local data.

    This is the enumeration to use: after a same-key conflict merge, two
    files can share one merge key, and the dict version below collapses
    them (last wins)."""
    out = []
    raw = store_dir / 'raw'
    paths = []
    if raw.is_dir():
        paths += sorted(raw.glob('cap-*.md'))
        arch = raw / 'archived'
        if arch.is_dir():
            paths += sorted(arch.glob('cap-*.md'))
    for path in paths:
        header, body = _parse_entry_text(path.read_text(encoding='utf-8'))
        out.append({'seq': header['seq'], 'body': body,
                    'queue': header.get('queue', 'pending'),
                    'header': header, 'path': path})
    return out


def _parse_local_entries(store_dir):
    """All local entries by merge key (last wins on same-key duplicates).

    Only for callers where duplicates cannot occur or do not matter
    (counts are taken from _parse_local_entries_all instead)."""
    out = {}
    for entry in _parse_local_entries_all(store_dir):
        out[_entry_key(entry['header'])] = entry
    return out


def _parse_blocks(text, prefix):
    """Split an archive file into (id, body) blocks for ## <prefix><n>.

    Returns (blocks, rest): blocks is a list of dicts in file order, rest
    is the non-block preamble (template text) preserved verbatim."""
    lines = text.splitlines()
    blocks = []
    preamble = []
    cur = None
    for line in lines:
        # Accept both the canonical hyphenated form (## D-001, what
        # summarize writes and relay_flow counts) and legacy plain
        # numbers (## D1). Without the hyphen, summarize's blocks were
        # invisible to merge and silently dropped on unpack.
        m = re.match(r'^## %s-?(\d+)\s*$' % prefix, line)
        if m:
            if cur is not None:
                blocks.append(cur)
            cur = {'id': int(m.group(1)), 'lines': []}
        elif cur is not None:
            cur['lines'].append(line)
        else:
            preamble.append(line)
    if cur is not None:
        blocks.append(cur)
    return blocks, '\n'.join(preamble)


def _block_body(block):
    return '\n'.join(l.rstrip() for l in block['lines']).strip('\n')


def _merge_id_blocks(local_text, incoming_text, prefix):
    """Union append-only blocks by body content hash.

    Local blocks keep their IDs; incoming new blocks are appended with
    fresh local IDs in incoming order. Returns (merged_text, added, skipped).
    Same content under different IDs dedups; different content is always
    kept (S6: the merge layer never adjudicates semantic duplicates)."""
    local_blocks, local_pre = _parse_blocks(local_text or '', prefix)
    inc_blocks, _inc_pre = _parse_blocks(incoming_text or '', prefix)
    seen = {_sha256(_block_body(b).encode('utf-8')) for b in local_blocks}
    next_id = (max([b['id'] for b in local_blocks]) + 1) if local_blocks else 1
    added = []
    skipped = 0
    for b in inc_blocks:
        h = _sha256(_block_body(b).encode('utf-8'))
        if h in seen:
            skipped += 1
            continue
        seen.add(h)
        added.append({'id': next_id, 'lines': b['lines']})
        next_id += 1
    if not added:
        return local_text, 0, skipped
    out = (local_text or '').rstrip('\n')
    for b in added:
        # Canonical hyphenated form, matching summarize's writer and
        # relay_flow's ^## T-\d+ / D-001 readers (plain ## D1 would be
        # uncounted by the home device's own tooling after unpack).
        out += '\n## %s-%03d\n%s\n' % (prefix, b['id'], '\n'.join(b['lines']).rstrip('\n'))
    return out + '\n', len(added), skipped


def _merge_backlog(local_text, incoming_text):
    """Backlog union keyed by the 事项 line.

    Same item + same body skips; same item + different body keeps the
    local block and records a conflict whose merge-log entry carries the
    full incoming block (both versions retained, neither silently lost).
    Returns (merged_text, added, skipped, conflicts)."""
    local_blocks, _lpre = _parse_blocks(local_text or '', 'T')
    inc_blocks, _ipre = _parse_blocks(incoming_text or '', 'T')

    def item_key(block):
        for line in block['lines']:
            m = re.match(r'^- 事项：(.*)$', line)
            if m:
                return m.group(1).strip()
        return None

    by_item = {}
    for b in local_blocks:
        by_item.setdefault(item_key(b), []).append(b)
    next_id = (max([b['id'] for b in local_blocks]) + 1) if local_blocks else 1
    added = []
    skipped = 0
    conflicts = []
    for b in inc_blocks:
        key = item_key(b)
        local_same = [x for x in by_item.get(key, [])
                      if _block_body(x) == _block_body(b)]
        if local_same:
            skipped += 1
            continue
        if key is not None and key in by_item:
            conflicts.append({'item': key,
                              'local': _block_body(by_item[key][0]),
                              'incoming': _block_body(b)})
            continue
        added.append({'id': next_id, 'lines': b['lines']})
        next_id += 1
    if not added:
        return local_text, 0, skipped, conflicts
    out = (local_text or '').rstrip('\n')
    for b in added:
        out += '\n## T-%03d\n%s\n' % (b['id'], '\n'.join(b['lines']).rstrip('\n'))
    return out + '\n', len(added), skipped, conflicts


STATE_BEGIN = '<!-- relay:state-begin -->'
STATE_END = '<!-- relay:state-end -->'

_TEMPLATE_STATE_VALUES = None


def _template_state_values():
    """Managed-block values of the init template state.md, read from the
    template itself (never guessed). Batch 2, B3: a local section is
    'still template' iff its value byte-equals the template's value for
    that key -- no heuristic ('未知'-looking text the user typed is NOT
    template, because it is not the template's bytes for that key... in
    practice the template values are all '未知', so the distinction only
    bites if a template value ever changes, which is exactly when a
    heuristic would go wrong). Unreadable template -> {} (fail-closed:
    no template-wins without the template)."""
    global _TEMPLATE_STATE_VALUES
    if _TEMPLATE_STATE_VALUES is not None:
        return _TEMPLATE_STATE_VALUES
    values = {}
    try:
        text = (relay.TEMPLATES_DIR / 'state.md').read_text(encoding='utf-8')
        if STATE_BEGIN in text and STATE_END in text:
            block = text[text.index(STATE_BEGIN):text.index(STATE_END)]
            for line in block.splitlines():
                m = re.match(r'^- ([^：]+)：(.*)$', line)
                if m:
                    values[m.group(1)] = m.group(2)
    except (OSError, ValueError):
        values = {}
    _TEMPLATE_STATE_VALUES = values
    return values


def _merge_state_md(local_text, incoming_text):
    """S6 state.md rule on the v1 managed block.

    已完成且有证据 takes the union of ；-separated values; any other
    managed line that differs keeps the local value and gains a
    分叉待裁 note line after STATE_END (both values retained, the note
    survives later summarizes because only the block itself is rewritten).
    Outside-block text is kept local; a difference is reported, not
    auto-appended. Returns (merged_text, conflicts, notes).

    Batch 2, B3: a local section that is still the init template
    verbatim loses to substantive incoming content -- a fresh device
    that init'd and then unpacked used to end with every section
    '未知' plus six fork notes, i.e. the incoming knowledge was present
    but unreachable. The branch line ('当前分支@基线') is exempt: it is
    local measurement (D-sync-3), not content, so adopting the peer's
    branch would misdescribe this device's code state; it keeps the old
    keep-local rule until a local summarize fills it in. The union rule
    for 已完成且有证据 also drops template placeholder tokens, so a
    template '未知' never pollutes an adopted value list."""
    local_text = local_text or ''
    incoming_text = incoming_text or ''
    conflicts = []
    notes = []
    if STATE_BEGIN not in local_text or STATE_END not in local_text:
        if local_text != incoming_text:
            conflicts.append({'file': 'archive/state.md',
                              'rule': 'unmanaged-state-kept-local',
                              'note': '本地 state.md 无受管块，保留本地全文'})
        return local_text, conflicts, notes
    l_start = local_text.index(STATE_BEGIN)
    l_stop = local_text.index(STATE_END) + len(STATE_END)
    l_before, l_block, l_after = (local_text[:l_start], local_text[l_start:l_stop],
                                  local_text[l_stop:])
    if STATE_BEGIN not in incoming_text or STATE_END not in incoming_text:
        return local_text, conflicts, notes
    i_start = incoming_text.index(STATE_BEGIN)
    i_stop = incoming_text.index(STATE_END) + len(STATE_END)
    i_block = incoming_text[i_start:i_stop]
    i_before, i_after = incoming_text[:i_start], incoming_text[i_stop:]
    if (l_before, l_after) != (i_before, i_after):
        conflicts.append({'file': 'archive/state.md',
                          'rule': 'outside-block-kept-local',
                          'note': '块外文本不同，保留本地版本'})

    def parse_block(block):
        order = []
        values = {}
        for line in block.splitlines():
            m = re.match(r'^- ([^：]+)：(.*)$', line)
            if m:
                order.append(m.group(1))
                values[m.group(1)] = m.group(2)
        return order, values

    order, l_values = parse_block(l_block)
    _i_order, i_values = parse_block(i_block)
    template_values = _template_state_values()
    merged_values = dict(l_values)
    fork_notes = []
    for key in order:
        if key not in i_values:
            continue
        if i_values[key] == l_values[key]:
            continue
        # B3 template-wins: local still template-verbatim, incoming
        # substantive (differs from the template value and non-empty).
        # '无' counts as substantive here: known-none beats unknown.
        if key != '当前分支@基线' \
                and key in template_values \
                and l_values[key] == template_values[key] \
                and i_values[key].strip() \
                and i_values[key] != template_values[key]:
            merged_values[key] = i_values[key]
            notes.append('state.md 的 %s 仍为模板原文，采纳对方内容' % key)
            continue
        if key == '已完成且有证据':
            vals = []
            for v in (l_values[key] + '；' + i_values[key]).split('；'):
                v = v.strip()
                if v and v not in ('无', template_values.get(key)) \
                        and v not in vals:
                    vals.append(v)
            merged_values[key] = '；'.join(vals) if vals else '无'
        else:
            fork_notes.append('- 分叉待裁：%s（对方）：%s' % (key, i_values[key]))
            conflicts.append({'file': 'archive/state.md', 'key': key,
                              'rule': 'fork-note-kept-local',
                              'local': l_values[key], 'incoming': i_values[key]})
    for key in i_values:
        if key not in merged_values:
            order.append(key)
            merged_values[key] = i_values[key]
    block_lines = [STATE_BEGIN]
    for key in order:
        block_lines.append('- %s：%s' % (key, merged_values[key]))
    block_lines.append(STATE_END)
    merged = l_before + '\n'.join(block_lines)
    if fork_notes:
        merged += '\n' + '\n'.join(fork_notes)
    merged += l_after
    return merged, conflicts, notes


# ---------------------------------------------------------------- merge
def _local_goal_confirmed(local_dir, goal_bytes):
    receipts = local_dir / 'receipts'
    if not receipts.is_dir():
        return False
    want = digest(goal_bytes)
    for path in receipts.glob('goal-*.json'):
        try:
            if read_json(path).get('goal_hash') == want:
                return True
        except (ValueError, UnicodeDecodeError, OSError):
            continue
    return False


def _merge_goal(local_dir, incoming_goal, source_info, incoming_receipts):
    """S6 goal rule on v1 files: NEVER auto-merge.

    Returns (action, merged_bytes_or_None, pending_info_or_None, conflict).
    adopt: local has no goal (missing/skeleton/blank) -> take incoming and
      mark it imported-pending-local-confirm. skip: bytes identical and
      locally confirmed. mark-pending: bytes identical but locally
      unconfirmed -> record provenance without touching the file.
      conflict: bytes differ -> keep local, report, flag for re-confirm."""
    skeleton = relay._skeleton_bytes()
    local_path = local_dir / 'archive' / 'goal.md'
    local_goal = local_path.read_bytes() if local_path.exists() else None
    receipt_seq = None
    if incoming_receipts:
        for seq in sorted(incoming_receipts):
            if incoming_receipts[seq].get('goal_hash') == digest(incoming_goal):
                receipt_seq = seq
    pending = {'source_device': source_info.get('device'),
               'source_device_id': source_info.get('device_id'),
               'source_receipt_seq': receipt_seq,
               'source_goal_hash': digest(incoming_goal),
               'imported_at': utc()}
    if local_goal is None or local_goal == skeleton or not local_goal.strip():
        return 'adopt', incoming_goal, pending, None
    if incoming_goal == local_goal:
        if _local_goal_confirmed(local_dir, local_goal):
            return 'skip', None, None, None
        return 'mark-pending', None, pending, None
    return 'conflict', None, None, {
        'file': 'archive/goal.md', 'rule': 'goal-never-auto-merge',
        'local_hash': digest(local_goal), 'incoming_hash': digest(incoming_goal),
        'note': '两边目标不同，保留本地版本，需按 S4 重新确认'}


def _map_queue(incoming_queue):
    if incoming_queue in ('summarized', 'archived'):
        return 'summarized'
    return 'pending'


def merge_project(local_dir, incoming_files, source_info):
    """Pure merge preview AND apply input (no writes).

    local_dir is the live project store dir (read-only here). incoming_files
    maps project-relative posix paths to already-verified, already-redacted
    bytes. Returns a result dict; merged_files holds only changed/added
    files (land writes exactly these). Cursor and baseline are never in the
    merged set (see references/v2-sync.md D-sync-2/3)."""
    local_dir = Path(local_dir)
    conflicts = []
    notes = []
    discarded = []
    quarantined = {}
    merged = {}
    counts = {'entries_added': 0, 'entries_skipped': 0, 'entries_conflicted': 0,
              'blocks_added': 0, 'blocks_skipped': 0, 'files_added': 0}
    local_entries_list = _parse_local_entries_all(local_dir)
    local_by_key = {}
    for e in local_entries_list:
        local_by_key.setdefault(_entry_key(e['header']), []).append(e)
    max_seq = max([e['seq'] for e in local_entries_list], default=0)

    def next_seq():
        nonlocal max_seq
        max_seq += 1
        return max_seq

    # ---- raw entries (append-type, union by merge key)
    incoming_entries = []
    for rel in sorted(incoming_files):
        if not (rel.startswith('raw/') and rel.endswith('.md')):
            continue
        name = rel[len('raw/'):]
        if not (name.startswith('cap-') or name.startswith('archived/cap-')):
            continue
        try:
            text = incoming_files[rel].decode('utf-8')
            header, body = _parse_entry_text(text)
        except (RelayError, UnicodeDecodeError) as exc:
            discarded.append({'file': rel, 'reason': 'malformed', 'detail': str(exc)})
            quarantined[rel] = incoming_files[rel]
            continue
        if _has_markers(body):
            discarded.append({'file': rel, 'reason': 'conflict-markers'})
            quarantined[rel] = incoming_files[rel]
            continue
        if not _verify_entry_id(header):
            discarded.append({'file': rel, 'reason': 'id-hash-mismatch'})
            # Batch 2, B2: a discarded entry's bytes are quarantined,
            # never landed in raw/ and never counted -- the old code
            # reported the discard but dropped the bytes entirely, so a
            # mis-verified entry could neither be inspected nor
            # repaired, and its 'summarized' queue claim lingered in
            # summaries that did land. Quarantine lives next to the
            # merge-log (see _land_project), outside raw/ and outside
            # the payload, so queue counts can never include it.
            quarantined[rel] = incoming_files[rel]
            continue
        incoming_entries.append((rel, header, body))
    # Source seq order, NOT fact-hash order: renumbering must preserve the
    # source device's chronology, so an empty local store ends up with
    # the same filenames (a previous sort by merge key swapped two
    # entries' filenames nondeterministically across runs).
    incoming_entries.sort(key=lambda t: t[1].get('seq', 0))
    for rel, header, body in incoming_entries:
        key = _entry_key(header)
        existing_list = local_by_key.get(key)
        if existing_list is not None and \
                any(e['body'] == body for e in existing_list):
            counts['entries_skipped'] += 1
            continue
        seq = next_seq()
        new_header = dict(header)
        new_header['seq'] = seq
        new_header['queue'] = _map_queue(header.get('queue', 'pending'))
        if existing_list is not None:
            new_header['merge_note'] = (
                '与本地同键条目内容不同，双方保留（来源：%s，源序号 %s）'
                % (source_info.get('device'), header.get('seq')))
            counts['entries_conflicted'] += 1
            conflicts.append({'file': 'raw/cap-%04d.md' % seq,
                              'rule': 'entry-same-key-different-body',
                              'local_seq': existing_list[0]['seq'],
                              'incoming_seq': header.get('seq')})
        else:
            counts['entries_added'] += 1
        merged['raw/cap-%04d.md' % seq] = (
            json.dumps(new_header, ensure_ascii=False, sort_keys=True, indent=2)
            + BODY_DELIM + body).encode('utf-8')

    def local_text(rel):
        p = local_dir / rel
        return p.read_text(encoding='utf-8') if p.exists() else ''

    # ---- append-only archive blocks
    for rel, prefix in (('archive/decisions.md', 'D'), ('archive/lessons.md', 'L')):
        if rel not in incoming_files:
            continue
        new_text, added, skipped = _merge_id_blocks(local_text(rel),
                                                     incoming_files[rel].decode('utf-8'),
                                                     prefix)
        if new_text != local_text(rel):
            merged[rel] = new_text.encode('utf-8')
        counts['blocks_added'] += added
        counts['blocks_skipped'] += skipped
    if 'archive/backlog.md' in incoming_files:
        new_text, added, skipped, bl_conflicts = _merge_backlog(
            local_text('archive/backlog.md'),
            incoming_files['archive/backlog.md'].decode('utf-8'))
        if new_text != local_text('archive/backlog.md'):
            merged['archive/backlog.md'] = new_text.encode('utf-8')
        counts['blocks_added'] += added
        counts['blocks_skipped'] += skipped
        for c in bl_conflicts:
            c = dict(c, file='archive/backlog.md', rule='backlog-same-item-different-body')
            conflicts.append(c)
    # ---- state.md managed block
    if 'archive/state.md' in incoming_files:
        new_text, st_conflicts, st_notes = _merge_state_md(
            local_text('archive/state.md'),
            incoming_files['archive/state.md'].decode('utf-8'))
        if new_text != local_text('archive/state.md'):
            merged['archive/state.md'] = new_text.encode('utf-8')
        conflicts.extend(st_conflicts)
        notes.extend(st_notes)
    # ---- goal (never auto-merged) + incoming receipt info
    incoming_receipts = {}
    for rel in sorted(incoming_files):
        if rel.startswith('receipts/goal-') and rel.endswith('.json'):
            try:
                rec = json.loads(incoming_files[rel].decode('utf-8'))
                if isinstance(rec, dict) and isinstance(rec.get('seq'), int):
                    incoming_receipts[rec['seq']] = rec
            except (ValueError, UnicodeDecodeError):
                continue
    goal_action = 'absent'
    pending_info = None
    goal_conflict = None
    if 'archive/goal.md' in incoming_files:
        goal_action, goal_bytes, pending_info, goal_conflict = _merge_goal(
            local_dir, incoming_files['archive/goal.md'], source_info,
            incoming_receipts)
        if goal_action == 'adopt':
            merged['archive/goal.md'] = goal_bytes
        if goal_conflict is not None:
            conflicts.append(goal_conflict)
    # ---- tentative / draft state (adopt only when consistent / absent)
    if goal_action == 'adopt' and 'state/goal-tentative.json' in incoming_files:
        try:
            tent = json.loads(incoming_files['state/goal-tentative.json'].decode('utf-8'))
            if tent.get('content_hash') == digest(merged['archive/goal.md']):
                merged['state/goal-tentative.json'] = incoming_files['state/goal-tentative.json']
                if 'state/goal-tentative.md' in incoming_files:
                    merged['state/goal-tentative.md'] = incoming_files['state/goal-tentative.md']
        except (ValueError, UnicodeDecodeError):
            pass
    if 'state/goal-draft.md' in incoming_files or 'state/goal-draft.json' in incoming_files:
        has_local_draft = ((local_dir / 'state' / 'goal-draft.md').exists()
                           or (local_dir / 'state' / 'goal-draft.json').exists())
        if not has_local_draft:
            for rel in ('state/goal-draft.md', 'state/goal-draft.json'):
                if rel in incoming_files:
                    merged[rel] = incoming_files[rel]
        else:
            notes.append('本地已有目标草案，忽略对方草案')
    # ---- splits (content-hash union, renumber on collision)
    local_split_hashes = {}
    split_nums = {}
    splits_dir = local_dir / 'archive' / 'splits'
    if splits_dir.is_dir():
        for p in splits_dir.glob('*.md'):
            local_split_hashes[_sha256(p.read_bytes())] = p.name
            m = re.match(r'^(.+)-(\d{4})\.md$', p.name)
            if m:
                split_nums[m.group(1)] = max(split_nums.get(m.group(1), 0), int(m.group(2)))
    inc_split_index = {}
    if 'state/split-index.json' in incoming_files:
        try:
            idx = json.loads(incoming_files['state/split-index.json'].decode('utf-8'))
            for e in idx.get('splits', []):
                inc_split_index[e.get('split')] = e
        except (ValueError, UnicodeDecodeError):
            pass
    new_index_entries = []
    for rel in sorted(incoming_files):
        if not rel.startswith('archive/splits/') or not rel.endswith('.md'):
            continue
        data = incoming_files[rel]
        h = _sha256(data)
        if h in local_split_hashes:
            continue
        old_name = rel.split('/')[-1]
        m = re.match(r'^(.+)-(\d{4})\.md$', old_name)
        base = m.group(1) if m else old_name[:-3]
        split_nums[base] = split_nums.get(base, 0) + 1
        new_name = '%s-%04d.md' % (base, split_nums[base])
        merged['archive/splits/' + new_name] = data
        counts['files_added'] += 1
        old_entry = inc_split_index.get(old_name, {})
        new_index_entries.append({'file': base, 'split': new_name,
                                  'at': old_entry.get('at', utc())})
    if new_index_entries:
        try:
            local_idx = json.loads(local_text('state/split-index.json')) \
                if (local_dir / 'state' / 'split-index.json').exists() else {'splits': []}
        except (ValueError, UnicodeDecodeError):
            local_idx = {'splits': []}
        local_idx.setdefault('splits', []).extend(new_index_entries)
        merged['state/split-index.json'] = (
            json.dumps(local_idx, ensure_ascii=False, sort_keys=True, indent=2) + '\n'
        ).encode('utf-8')
    # ---- goal-history / handoffs (content-hash union, rename on collision)
    for prefix, sub in (('archive/goal-history/', 'goal-history'),
                        ('archive/handoffs/', 'handoffs')):
        taken = set()
        base_dir = local_dir / prefix
        if base_dir.is_dir():
            taken = {p.name for p in base_dir.glob('*') if p.is_file()}
        local_hashes = {}
        if base_dir.is_dir():
            for p in base_dir.glob('*'):
                if p.is_file():
                    local_hashes[_sha256(p.read_bytes())] = p.name
        for rel in sorted(incoming_files):
            if not rel.startswith(prefix):
                continue
            data = incoming_files[rel]
            if _sha256(data) in local_hashes:
                continue
            name = rel.split('/')[-1]
            stem, suffix = name.rsplit('.', 1) if '.' in name else (name, '')
            while name in taken:
                stem += '-imported'
                name = stem + ('.' + suffix if suffix else '')
            taken.add(name)
            merged[prefix + name] = data
            counts['files_added'] += 1
            if sub == 'handoffs':
                pass
    if any(r.startswith('archive/handoffs/') for r in merged):
        lines = set()
        idx_path = local_dir / 'state' / 'handoff-index.md'
        if idx_path.exists():
            lines.update(l for l in idx_path.read_text(encoding='utf-8').splitlines() if l.strip())
        for rel in sorted(merged):
            if rel.startswith('archive/handoffs/'):
                name = rel.split('/')[-1]
                lines.add('- %s: archive/handoffs/%s' % (name, name))
        merged['state/handoff-index.md'] = ('\n'.join(sorted(lines)) + '\n').encode('utf-8')
    # ---- summaries (content-hash union, renumber filename on collision)
    local_sum_hashes = {}
    sum_dir = local_dir / 'state' / 'summaries'
    used_nums = set()
    if sum_dir.is_dir():
        for p in sum_dir.glob('*.json'):
            local_sum_hashes[_sha256(p.read_bytes())] = p.name
            try:
                used_nums.add(int(p.stem))
            except ValueError:
                pass
    next_num = (max(used_nums) + 1) if used_nums else 1
    for rel in sorted(incoming_files):
        if not rel.startswith('state/summaries/') or not rel.endswith('.json'):
            continue
        data = incoming_files[rel]
        if _sha256(data) in local_sum_hashes:
            continue
        while ('%04d.json' % next_num) in used_nums:
            next_num += 1
        name = '%04d.json' % next_num
        used_nums.add(next_num)
        next_num += 1
        merged['state/summaries/' + name] = data
        counts['files_added'] += 1
    if 'state/last-summary.json' in incoming_files and \
            not (local_dir / 'state' / 'last-summary.json').exists():
        merged['state/last-summary.json'] = incoming_files['state/last-summary.json']
    # ---- anything else in the payload is local-only or unknown: ignore
    handled_prefixes = ('raw/', 'archive/', 'state/split-index.json',
                        'state/goal-tentative.', 'state/goal-draft.',
                        'state/summaries/', 'state/last-summary.json',
                        'state/handoff-index.md', 'receipts/')
    for rel in sorted(incoming_files):
        if not (rel.startswith(handled_prefixes) or rel == 'archive/goal.md'):
            notes.append('忽略未纳入合并的文件：' + rel)
    return {'merged_files': merged, 'conflicts': conflicts, 'counts': counts,
            'goal_action': goal_action, 'pending_info': pending_info,
            'discarded': discarded, 'quarantined': quarantined, 'notes': notes}


# ---------------------------------------------------------------- health
def health_check(store_dir):
    """Pre-pack / pre-merge health gate (plan 2.5 step 2).

    Problems abort the operation; warnings do not. "Unrecovered
    transaction" means the LATEST tx log shows partial application: some
    files still at before-bytes while others already left before-bytes.
    A merely-present log from a completed summarize is healthy (summarize
    never deletes its tx log, it doubles as undo), and later evolution
    away from both before and after bytes is normal (new summarizes,
    merges) -- it only means rollback is no longer clean, which recover()
    already refuses on its own."""
    store_dir = Path(store_dir)
    problems = []
    warnings = []
    archive = store_dir / 'archive'
    for name in ('goal.md', 'state.md', 'backlog.md', 'decisions.md', 'lessons.md'):
        p = archive / name
        if not p.exists():
            problems.append('档案缺失：archive/' + name)
            continue
        try:
            text = p.read_text(encoding='utf-8')
        except (UnicodeDecodeError, OSError) as exc:
            problems.append('档案不可读：archive/%s（%s）' % (name, exc))
            continue
        if _has_markers(text):
            problems.append('档案含冲突标记：archive/' + name)
    state_text = (archive / 'state.md').read_text(encoding='utf-8') \
        if (archive / 'state.md').exists() else ''
    if STATE_BEGIN in state_text and STATE_END not in state_text:
        problems.append('state.md 受管块缺结束标记')
    if STATE_BEGIN not in state_text:
        warnings.append('state.md 无受管块（下次汇总将补写）')
    try:
        entry_seqs = {e['seq'] for e in _parse_local_entries_all(store_dir)}
    except RelayError as exc:
        problems.append('原料条目损坏：' + str(exc))
        entry_seqs = set()
    # Transaction gate: only the LATEST tx log matters (vol-1 recover()
    # itself only looks at the latest: a log means "this summarize can
    # still be rolled back", not "a transaction is in progress").
    # A completed transaction followed by legitimate evolution (a later
    # summarize, a merge) moves files away from BOTH before and after
    # bytes -- that is normal and must not block pack/merge. What blocks
    # is PARTIAL application: at least one file still at before-bytes
    # while another has already left before-bytes, i.e. the transaction
    # started applying and never finished.
    tx_dir = store_dir / 'tx'
    if tx_dir.is_dir():
        import base64
        logs = sorted(p for p in tx_dir.glob('*.json')
                      if not p.name.endswith('.recovered.json'))
        if logs:
            path = logs[-1]
            try:
                tx = json.loads(path.read_text(encoding='utf-8'))
                files = tx['files']
            except (ValueError, UnicodeDecodeError, KeyError) as exc:
                problems.append('事务日志无法解析：tx/%s（%s）' % (path.name, exc))
                files = {}
            at_before = []
            left_before = []
            for name, rec in files.items():
                current = (archive / name).read_bytes() if (archive / name).exists() else b''
                try:
                    before = base64.b64decode(rec['before_b64'])
                except Exception:
                    problems.append('事务日志损坏：tx/%s' % path.name)
                    break
                # A file the transaction intentionally left unchanged has
                # after_sha == digest(before): current == before is its
                # CORRECT applied state, not an unapplied write.
                if current == before and digest(current) != rec['after_sha']:
                    at_before.append(name)
                elif current != before:
                    left_before.append(name)
            if at_before and left_before:
                problems.append('未恢复事务：tx/%s 部分应用（仍在事务前：%s；已离开事务前：%s）'
                                % (path.name, '、'.join(at_before), '、'.join(left_before)))
    cursor_path = store_dir / 'state' / 'cursor.json'
    if cursor_path.exists():
        try:
            cursor = json.loads(cursor_path.read_text(encoding='utf-8'))
            seq = cursor.get('entry_seq', 0)
            if seq and seq not in entry_seqs:
                problems.append('游标指向不存在的条目：cap-%04d' % seq)
        except (ValueError, UnicodeDecodeError) as exc:
            problems.append('游标文件无法解析：' + str(exc))
    receipts = store_dir / 'receipts'
    if receipts.is_dir():
        for path in receipts.glob('goal-*.json'):
            try:
                json.loads(path.read_text(encoding='utf-8'))
            except (ValueError, UnicodeDecodeError):
                problems.append('收据无法解析：receipts/' + path.name)
    return problems, warnings


# ---------------------------------------------------------------- shadow land
def _shadow_copy(local_dir):
    """Copy the whole project store to a scratch dir (symlinks refused)."""
    local_dir = Path(local_dir)
    shadow = Path(tempfile.mkdtemp(prefix='relay-merge-'))
    for root, dirs, files in os.walk(local_dir):
        root = Path(root)
        for d in dirs:
            if (root / d).is_symlink():
                raise RelayError('本地仓含符号链接，拒绝合并：' + str(root / d))
        for f in files:
            src = root / f
            if src.is_symlink():
                raise RelayError('本地仓含符号链接，拒绝合并：' + str(src))
            rel = src.relative_to(local_dir)
            dest = shadow / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(src.read_bytes())
    return shadow


def _land_files(local_dir, merged_files):
    """Atomic-ish landing with full rollback on failure.

    Every replaced file is first backed up to a temp dir; on any error the
    backups are written back and new files removed, so the store keeps its
    pre-merge bytes. Returns the backup dir (caller removes on success)."""
    local_dir = Path(local_dir)
    backup = Path(tempfile.mkdtemp(prefix='relay-land-'))
    backed = []
    added = []
    try:
        for rel in sorted(merged_files):
            live = local_dir / rel
            live.parent.mkdir(parents=True, exist_ok=True)
            if live.exists():
                snap = backup / rel
                snap.parent.mkdir(parents=True, exist_ok=True)
                snap.write_bytes(live.read_bytes())
                backed.append(rel)
            else:
                added.append(rel)
            atomic(live, merged_files[rel])
    except Exception:
        for rel in backed:
            atomic(local_dir / rel, (backup / rel).read_bytes())
        for rel in added:
            try:
                (local_dir / rel).unlink()
            except OSError:
                pass
        raise
    return backup


def _land_project(store, local_dir, merge_result, source_info, marker_files=None,
                  quarantined=None):
    """Shadow-check, land, then write flags + merge-log.

    marker_files: incoming files excluded for conflict markers are
    preserved under the merge-log's conflicted/ folder (both versions
    retained, never silently dropped). quarantined (batch 2, B2): bytes
    of entries merge discarded (malformed / id-hash-mismatch / non-UTF-8)
    are preserved under the merge-log's quarantined/ folder -- outside
    raw/, outside the payload, never counted -- so a discard is
    inspectable and repairable instead of silently gone."""
    local_dir = Path(local_dir)
    shadow = _shadow_copy(local_dir)
    for rel, data in merge_result['merged_files'].items():
        dest = shadow / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    problems, _warnings = health_check(shadow)
    if problems:
        raise RelayError('影子合并健康检查失败，正式仓未改动（影子目录保留供排查：%s）：%s'
                         % (shadow, '；'.join(problems)))
    backup = _land_files(local_dir, merge_result['merged_files'])
    shutil.rmtree(backup, ignore_errors=True)
    # Slice 2 (plan 2.3.5): every landing refreshes the integrity manifest,
    # so the next takeover's check runs against post-merge bytes.
    relay.update_checksum_manifest(local_dir)
    if merge_result.get('pending_info') is not None:
        save(local_dir / 'state' / 'goal-imported.json',
             merge_result['pending_info'])
    if any(c.get('file') == 'archive/goal.md' for c in merge_result['conflicts']):
        save(local_dir / 'state' / 'goal-conflict.json',
             {'at': utc(), 'source_device': source_info.get('device'),
              'source_device_id': source_info.get('device_id'),
              'source_seq': source_info.get('seq')})
    log = {'at': utc(), 'kind': 'merge',
           'source_device': source_info.get('device'),
           'source_device_id': source_info.get('device_id'),
           'source_seq': source_info.get('seq'),
           'goal_action': merge_result['goal_action'],
           'counts': merge_result['counts'],
           'conflicts': merge_result['conflicts'],
           'discarded': merge_result['discarded'],
           'quarantined': sorted(quarantined) if quarantined else [],
           'notes': merge_result['notes'],
           'changed_files': sorted(merge_result['merged_files'])}
    rollback_note = local_dir / 'state' / 'rollback-note.json'
    if rollback_note.exists():
        try:
            log['rollback_note'] = json.loads(rollback_note.read_text(encoding='utf-8'))
        except (ValueError, UnicodeDecodeError):
            log['rollback_note'] = {'raw': True}
    log_path = _receipt_path(store, 'merge', _project_key(local_dir))
    save(log_path, log)
    if marker_files:
        conflicted_dir = Path(str(log_path)[:-5])  # strip .json
        conflicted_dir.mkdir(parents=True, exist_ok=True)
        for rel, data in marker_files.items():
            (conflicted_dir / rel.replace('/', '__')).write_bytes(data)
    quarantined_files = []
    if quarantined:
        quarantine_dir = Path(str(log_path)[:-5]) / 'quarantined'
        quarantine_dir.mkdir(parents=True, exist_ok=True)
        for rel, data in sorted(quarantined.items()):
            name = rel.replace('/', '__')
            (quarantine_dir / name).write_bytes(data)
            quarantined_files.append(name)
    shutil.rmtree(shadow, ignore_errors=True)
    return {'changed_files': sorted(merge_result['merged_files']),
            'conflicts': merge_result['conflicts'],
            'counts': merge_result['counts'],
            'goal_action': merge_result['goal_action'],
            'merge_log': str(log_path),
            'quarantined_files': quarantined_files}


def _project_key(local_dir):
    """Project key from the store path name (<key>-<slug>)."""
    return Path(local_dir).name[:16]


# ---------------------------------------------------------------- import
def _redact_incoming(payload):
    """Redact + marker-scan an incoming payload (pure, no writes).

    Returns (files, hits, marker_files, discarded, quarantined): files
    is the redacted payload for merging; marker files are excluded from
    merging (the caller preserves them next to the merge-log); non-UTF-8
    files are discarded with a reason and their bytes quarantined.

    Batch 2, B2: raw entries get the same field-level redaction as the
    export rescan (_redact_entry_text) instead of whole-text redaction
    -- whole-text redaction on the import side could damage the very
    fingerprint digits the peer then verifies (the P0 failure mode,
    manufactured on arrival), turning valid entries into
    id-hash-mismatch discards. A malformed entry that field redaction
    cannot parse falls back to whole-text redaction; merge_project
    still discards and quarantines it, so the fallback cannot land it."""
    flow = _flow()
    files = {}
    marker_files = {}
    discarded = []
    quarantined = {}
    hits = 0
    for rel, data in sorted(payload.items()):
        try:
            text = data.decode('utf-8')
        except UnicodeDecodeError:
            discarded.append({'file': rel, 'reason': 'non-utf8'})
            quarantined[rel] = data
            continue
        if _has_markers(text):
            marker_files[rel] = data
            discarded.append({'file': rel, 'reason': 'conflict-markers'})
            continue
        if _is_raw_entry_rel(rel):
            try:
                cleaned, n = _redact_entry_text(text, flow)
            except ValueError:
                cleaned, n = flow.redact(text)
        else:
            cleaned, n = flow.redact(text)
        hits += n
        files[rel] = cleaned.encode('utf-8')
    return files, hits, marker_files, discarded, quarantined


def import_bundle(bundle_dir, store, confirm=False):
    """Import a verified bundle (plan 2.1 steps 3-5).

    Dry-run (confirm=False) writes NOTHING: merge_project is pure, so the
    report is a faithful preview. On confirm, matched projects are merged
    transactionally and unmatched projects land in unclaimed/; a merge-log
    receipt is written per matched project. The registry is never edited
    by import (claiming happens explicitly via sync-claim)."""
    manifest, files, warnings = verify_bundle(bundle_dir)
    # Batch 3, C1: validate every manifest project identity before any
    # landing (matched or unclaimed) uses it to build paths.
    for _proj in manifest.get('projects', []):
        _validate_project_meta(_proj.get('key'), _proj.get('slug'))
    registry = relay.load_registry(store)
    source_base = {'device': manifest['source_device'],
                   'device_id': manifest['source_device_id']}
    report = {'landed': confirm, 'exported_at': manifest['exported_at'],
              'source_device': manifest['source_device'],
              'source_device_id': manifest['source_device_id'],
              'warnings': warnings, 'projects': [], 'merge_logs': [],
              # Batch 2, B1: git bundles rebuilt by _bundle_dir_from_clone
              # carry their signature verification here; direct
              # folder/server bundles have none (their manifest hash
              # gate is the verification), so this stays None there.
              'signature_verification': manifest.get('signature_verification')}
    for proj in sorted(manifest.get('projects', []), key=lambda p: p['key']):
        prefix = 'projects/%s-%s/' % (proj['key'], proj['slug'])
        payload = {rel[len(prefix):]: data for rel, data in files.items()
                   if rel.startswith(prefix)}
        files_in, hits, marker_files, redacted_discarded, redacted_quarantined = \
            _redact_incoming(payload)
        matched = None
        for row in registry.get('projects', []):
            if row.get('status', 'active') != 'active':
                continue
            if row.get('key') == proj['key']:
                matched = row
                break
            local_remote = row.get('remote') or ''
            if proj.get('remote') and local_remote:
                # NEVER compare normalize() outputs directly: it returns
                # None for already-canonical 'host/path' strings (no
                # colon), and None == None would match every project.
                # Canonical strings compare directly; raw URLs compare
                # after normalization, only when both normalize.
                proj_norm = relay.normalize_remote(proj['remote'])
                local_norm = relay.normalize_remote(local_remote)
                if proj['remote'] == local_remote or (
                        proj_norm is not None and proj_norm == local_norm):
                    matched = row
                    break
        entry = {'key': proj['key'], 'slug': proj['slug'],
                 'display_name': proj.get('display_name'),
                 'pack_seq': proj.get('pack_seq'),
                 'redacted_count': hits,
                 'marker_files': sorted(marker_files),
                 'discarded': list(redacted_discarded),
                 'quarantined': sorted(redacted_quarantined)}
        if matched is None:
            entry['status'] = 'unclaimed'
            # Batch 2, B2: the unclaimed landing used to write the
            # payload wholesale -- including entries merge would
            # discard (id-hash-mismatch, malformed) -- with their
            # original 'summarized' queue headers intact, and the
            # report's discarded list stayed empty. Scan raw entries
            # here with the same gate merge_project uses, quarantine
            # the failures (never under raw/, never counted), and
            # report them -- in dry-run too, so the preview is faithful.
            scan_discarded = []
            scan_quarantined = dict(redacted_quarantined)
            for rel in sorted(files_in):
                if not _is_raw_entry_rel(rel):
                    continue
                try:
                    header, _body = _parse_entry_text(
                        files_in[rel].decode('utf-8'))
                except (RelayError, UnicodeDecodeError) as exc:
                    scan_discarded.append({'file': rel, 'reason': 'malformed',
                                           'detail': str(exc)})
                    scan_quarantined[rel] = files_in[rel]
                    continue
                if not _verify_entry_id(header):
                    scan_discarded.append({'file': rel,
                                           'reason': 'id-hash-mismatch'})
                    scan_quarantined[rel] = files_in[rel]
            for rel in scan_quarantined:
                files_in.pop(rel, None)
            entry['discarded'].extend(scan_discarded)
            entry['quarantined'] = sorted(scan_quarantined)
            # Batch 3, C6 (preview, read-only): a repeat import landing
            # on an existing unclaimed dir must not overwrite the first
            # device's payload -- two devices exporting the same project
            # before this receiver registers it would otherwise lose the
            # first device's entries/decisions to the second import.
            # Conflicting rels are computed here so the dry-run preview
            # is faithful; the confirm branch below preserves the
            # incoming versions as evidence instead of overwriting.
            dest_preview = store / 'unclaimed' / ('%s-%s' % (proj['key'], proj['slug']))
            conflict_rels = [rel for rel, data in files_in.items()
                             if (dest_preview / rel).is_file()
                             and (dest_preview / rel).read_bytes() != data]
            entry['unclaimed_conflicts'] = [{'file': rel, 'preserved': None}
                                            for rel in conflict_rels]
            if confirm:
                dest = _contained_unclaimed_dir(store, proj['key'], proj['slug'])
                preserved = []
                for rel, data in files_in.items():
                    p = dest / rel
                    p.parent.mkdir(parents=True, exist_ok=True)
                    if rel in conflict_rels:
                        cdir = dest / '_conflicts'
                        cdir.mkdir(parents=True, exist_ok=True)
                        base = '%s--%s-%s' % (rel.replace('/', '__'),
                                              (manifest['source_device_id'] or 'unknown')[:8],
                                              _compact_ts(utc()))
                        cpath = cdir / base
                        n = 2
                        while cpath.exists():
                            cpath = cdir / ('%s-%d' % (base, n))
                            n += 1
                        cpath.write_bytes(data)
                        preserved.append({'file': rel,
                                          'preserved': 'unclaimed/%s-%s/_conflicts/%s'
                                                       % (proj['key'], proj['slug'],
                                                          cpath.name)})
                        continue
                    if p.exists():
                        continue  # identical bytes: idempotent re-import
                    p.write_bytes(data)
                qdir = dest / '_quarantine'
                for rel, data in scan_quarantined.items():
                    p = qdir / rel.replace('/', '__')
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(data)
                claim = {'key': proj['key'], 'slug': proj['slug'],
                         'display_name': proj.get('display_name'),
                         'remote': proj.get('remote'), 'key_type': proj.get('key_type'),
                         'source_device': manifest['source_device'],
                         'source_device_id': manifest['source_device_id'],
                         'pack_seq': proj.get('pack_seq'),
                         'imported_at': utc(), 'files': sorted(files_in),
                         'marker_files': sorted(marker_files),
                         'quarantined': sorted(scan_quarantined)}
                if preserved:
                    # Batch 3, C6: the claim record names the preserved
                    # conflicting versions (claim metadata itself still
                    # follows the latest import; the payload files are
                    # what must never be overwritten).
                    claim['unclaimed_conflicts'] = preserved
                    entry['unclaimed_conflicts'] = preserved
                save(dest / '_claim.json', claim)
                index_path = store / 'unclaimed' / '_index.json'
                index = read_json(index_path) if index_path.exists() else {'claims': []}
                index.setdefault('claims', [])
                index['claims'] = [c for c in index['claims'] if c['key'] != proj['key']]
                index['claims'].append({'key': proj['key'], 'slug': proj['slug'],
                                        'display_name': proj.get('display_name')})
                save(index_path, index)
                entry['unclaimed_dir'] = str(dest)
            report['projects'].append(entry)
            continue
        local_dir = relay.project_store_dir(store, matched)
        source_info = dict(source_base, seq=proj.get('pack_seq'))
        merge_result = merge_project(local_dir, files_in, source_info)
        merge_result['discarded'].extend(redacted_discarded)
        merge_result['quarantined'].update(redacted_quarantined)
        entry['status'] = 'matched'
        entry['counts'] = merge_result['counts']
        entry['conflicts'] = merge_result['conflicts']
        entry['goal_action'] = merge_result['goal_action']
        entry['discarded'] = merge_result['discarded']
        entry['quarantined'] = sorted(merge_result['quarantined'])
        entry['notes'] = merge_result['notes']
        entry['changed_files'] = sorted(merge_result['merged_files'])
        if confirm:
            landed = _land_project(store, local_dir, merge_result, source_info,
                                   marker_files=marker_files,
                                   quarantined=merge_result['quarantined'])
            entry['merge_log'] = landed['merge_log']
            entry['quarantined_files'] = landed['quarantined_files']
            report['merge_logs'].append(landed['merge_log'])
        report['projects'].append(entry)
    return report


# ---------------------------------------------------------------- git
def _text(value):
    """relay.git returns bytes stdout/stderr; receipts need str."""
    if isinstance(value, bytes):
        return value.decode('utf-8', 'replace')
    return value or ''


def _git_ok(root, *args):
    return relay.git(root, *args, check=False).returncode == 0


def _git_branch(store):
    result = relay.git(store, 'symbolic-ref', '--short', 'HEAD', check=False)
    if result.returncode == 0 and _text(result.stdout).strip():
        return _text(result.stdout).strip()
    return 'main'


# ---------------------------------------------------------------- git identity (batch 2, B5)
# The store repo is relay's own activity repo, not the user's code repo:
# when it has no local user.name/user.email (e.g. a sandbox with no
# global git identity), the first pack used to die at commit time. The
# tool now sets a LOCAL identity on the store repo only -- never a
# global config -- and pre-checks it before the commit step so the
# failure, if setting it is impossible, names the store and the remedy
# instead of surfacing as a bare git error at step 5.
GIT_IDENTITY_NAME = 'relay'
GIT_IDENTITY_EMAIL = 'relay@project-relay.invalid'


def _ensure_git_identity(store):
    """Set a local git identity on the store repo when it has none.

    Returns (name, email, changed): changed is True only when this call
    wrote a value. An existing local identity -- including a user's own
    name -- is never overwritten. Global config is never read for
    writing and never written: `git config --local` (the default for
    `git -C <repo> config` without --global) scopes to this repo."""
    def _get(key):
        r = relay.git(store, 'config', '--local', '--get', key, check=False)
        return _text(r.stdout).strip() if r.returncode == 0 else ''
    name = _get('user.name')
    email = _get('user.email')
    changed = False
    if not name:
        r = relay.git(store, 'config', '--local', 'user.name',
                      GIT_IDENTITY_NAME, check=False)
        require(r.returncode == 0,
                '无法为记忆仓设置本地 git 身份（%s）：请手工运行 git -C %s '
                'config user.name ... 后重试；全局配置未被改动' % (store, store))
        name, changed = GIT_IDENTITY_NAME, True
    if not email:
        r = relay.git(store, 'config', '--local', 'user.email',
                      GIT_IDENTITY_EMAIL, check=False)
        require(r.returncode == 0,
                '无法为记忆仓设置本地 git 身份（%s）：请手工运行 git -C %s '
                'config user.email ... 后重试；全局配置未被改动' % (store, store))
        email, changed = GIT_IDENTITY_EMAIL, True
    return name, email, changed


def _commit_if_changes(store, paths, message):
    """Stage exactly the given paths; commit only when staged diff exists.
    Returns the new HEAD SHA or None. Identity errors surface with a hint."""
    paths = [p for p in paths if (store / p).exists()]
    if not paths:
        return None
    if not _git_ok(store, 'add', '--', *paths):
        raise RelayError('git add 失败（请检查记忆仓是否为 Git 仓）')
    result = relay.git(store, 'diff', '--cached', '--quiet', check=False)
    if result.returncode == 0:
        return None
    # B5: a store repo without any identity must not die here -- the
    # local identity is ensured (local scope only) before committing.
    _ensure_git_identity(store)
    result = relay.git(store, 'commit', '-m', message, check=False)
    if result.returncode != 0:
        raise RelayError('git commit 失败（请先设置 git user.name / user.email）：'
                         + _text(result.stderr).strip()[:300])
    return _text(relay.git(store, 'rev-parse', 'HEAD').stdout).strip()


def _converge_history(store, remote, branch):
    """Fold the remote branch into the local store history (plan 2.2:
    unpack is "pull and merge", not "overwrite").

    Every device's store repo starts from its own independent root commit,
    so a plain push after the first pack is rejected as non-fast-forward.
    The content merge already happened (import pipeline, both versions
    retained); this step only converges HISTORY: fetch the remote tip and
    merge it with -X ours, so the already-merged working tree wins every
    conflict and the resulting tree is unchanged. Returns one of
    'merged', 'up-to-date', 'no-local-commits', 'fetch-failed'."""
    fetched = relay.git(store, 'fetch', '--quiet', remote, branch, check=False)
    if fetched.returncode != 0:
        return 'fetch-failed'
    head = relay.git(store, 'rev-parse', '--verify', 'HEAD', check=False)
    if head.returncode != 0:
        return 'no-local-commits'
    anc = relay.git(store, 'merge-base', '--is-ancestor', 'FETCH_HEAD', 'HEAD',
                    check=False)
    if anc.returncode == 0:
        return 'up-to-date'
    result = relay.git(store, 'merge', '--allow-unrelated-histories', '-X', 'ours',
                       '--no-edit', 'FETCH_HEAD', check=False)
    if result.returncode != 0:
        raise RelayError('合并远端历史失败：%s；请手工处理后重试'
                         % _text(result.stderr).strip()[:300])
    return 'merged'


def _clone(remote, parent, name='clone', checkout=None):
    """Clone a remote for read-only inspection.

    A bare remote whose HEAD still points at its init-time default (e.g.
    master) while packs push main leaves git clone with NO checkout at
    all (only refs). So after cloning, explicitly check out a remote
    branch: the caller's preferred branch when present, else main, else
    master, else the first remote branch. Detached HEAD is fine: clones
    are never committed to."""
    dest = Path(parent) / name
    result = relay.git(parent, 'clone', '--quiet', remote, name, check=False)
    if result.returncode != 0:
        raise RelayError('克隆远端失败：%s' % _text(result.stderr).strip()[:300])
    refs = relay.git(dest, 'for-each-ref', '--format=%(refname:short)',
                     'refs/remotes/origin/', check=False)
    branches = []
    if refs.returncode == 0:
        for line in _text(refs.stdout).splitlines():
            line = line.strip()
            if line.startswith('origin/') and not line.endswith('/HEAD'):
                branches.append(line[len('origin/'):])
    if branches:
        pick = None
        for candidate in ([checkout] if checkout else []) + ['main', 'master']:
            if candidate in branches:
                pick = candidate
                break
        if pick is None:
            pick = sorted(branches)[0]
        out = relay.git(dest, 'checkout', '--quiet', 'origin/' + pick, check=False)
        if out.returncode != 0:
            raise RelayError('克隆后检出远端分支失败：%s'
                             % _text(out.stderr).strip()[:300])
    return dest


def _clone_is_empty(clone_dir):
    # Do NOT test HEAD: a bare remote whose HEAD still points at its
    # init-time default (e.g. master) while packs push main leaves a clone
    # with a dangling HEAD even though refs exist. Any ref means content.
    return relay.git(clone_dir, 'show-ref', check=False).returncode != 0


def _read_remote_bundle(clone_dir, key):
    """Latest sync-manifest for one project key inside a clone.

    Recency is by exported_at, NOT by pack_seq: pack_seq is a per-device
    counter, so two devices' seq=1 manifests would tie and glob order
    would pick arbitrarily. Returns (manifest_dict, project_dir_name)
    or None when the project has no manifest in this clone."""
    manifests = clone_dir / 'sync-manifests'
    if not manifests.is_dir():
        return None
    best = None
    best_rank = None
    for path in manifests.glob('%s-*.json' % key):
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (ValueError, UnicodeDecodeError):
            continue
        rank = (data.get('exported_at', ''), data.get('pack_seq', -1), path.name)
        if best_rank is None or rank > best_rank:
            best_rank = rank
            best = data
    if best is None:
        return None
    slug = best.get('slug') or ''
    # Batch 3, C1: the slug builds a directory name below; validate
    # before it touches any path.
    _validate_project_meta(key, slug)
    return best, '%s-%s' % (key, slug)


def _bundle_dir_from_clone(clone_dir, key, workdir, trust_unsigned=False,
                           repin=False, store=None):
    """Materialize a verifiable bundle dir from a remote clone.

    The import pipeline only ever reads bundles, never the clone itself,
    so clone-only state (git history, branches) cannot leak into merges.
    Batch 2, B1: this is the git carrier's signature choke point -- every
    git read (pack step 0, unpack, claim scan bundles) funnels through
    here, so a tampered or re-keyed bundle fails here, before the import
    pipeline lands anything. The verification outcome travels in the
    rebuilt bundle manifest ('signature_verification') so import reports
    and receipts carry the trace; a --repin acceptance additionally
    writes a dedicated repin receipt under the store when known."""
    manifest, dirname = _read_remote_bundle(clone_dir, key)
    if manifest is None:
        return None
    src = clone_dir / 'projects' / dirname
    if not src.is_dir():
        return None
    bundle = Path(workdir) / ('bundle-%s' % key)
    bundle.mkdir(parents=True, exist_ok=True)
    dest = bundle / 'projects' / dirname
    # The bundle manifest must match the disk content exactly, so the
    # hash dict is recomputed here (the committed sync-manifest entry
    # only carries a path list, not hashes, and no per-project counts).
    # Only listed files are copied: a later pack never deletes files
    # from git, so the directory may hold stale files from older packs
    # that are NOT part of this pack's bundle.
    file_hashes = {}
    entry_count = 0
    listed = manifest.get('files') or []
    for listed_rel in sorted(listed):
        if not listed_rel.startswith('projects/%s/' % dirname):
            continue
        rel = listed_rel[len('projects/%s/' % dirname):]
        # Batch 3, C1: the copy below runs before verify_bundle, so a
        # listed path with '..' would escape the rebuilt bundle dir
        # before any jail sees it. Jail it here, at copy time.
        _jail_check(rel)
        src_file = src / rel
        if not src_file.is_file():
            raise RelayError('远端清单文件缺失：' + listed_rel)
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        data = src_file.read_bytes()
        target.write_bytes(data)
        file_hashes[listed_rel] = _sha256(data)
        if rel.startswith('raw/') and rel.endswith('.md'):
            entry_count += 1
    # B1: verify the signature over the RECOMPUTED hashes -- a pusher
    # who rewrites a file changes its hash, which breaks the signature
    # (they hold no private seed to re-sign), and a pusher who rewrites
    # the manifest's own file_hashes field cannot help either, because
    # verification never trusts that field, only disk bytes.
    verification = verify_bundle_signature(manifest, file_hashes,
                                           trust_unsigned=trust_unsigned,
                                           repin=repin, store=store)
    if verification.get('status') == 'repinned' and store is not None:
        receipt = {'at': utc(), 'kind': 'repin',
                   'source_device': manifest.get('source_device'),
                   'source_device_id': manifest.get('source_device_id'),
                   'previous_key_id': verification.get('previous_key_id'),
                   'key_id': verification.get('key_id'),
                   'pack_seq': manifest.get('pack_seq')}
        save(Path(store) / 'sync-receipts' / ('repin-%s-%s.json'
             % ((manifest.get('source_device_id') or 'unknown')[:8],
                _compact_ts(utc()))), receipt)
        verification = dict(verification, repin_receipt='sync-receipts/repin-*.json')
    entry = dict(manifest)
    entry['files'] = sorted(file_hashes)
    project_manifest = {'format': BUNDLE_FORMAT, 'format_major': FORMAT_MAJOR,
                        'format_minor': manifest.get('format_minor', 0),
                        'exported_at': manifest.get('exported_at', utc()),
                        'source_device': manifest.get('source_device'),
                        'source_device_id': manifest.get('source_device_id'),
                        'tool_version': TOOL_VERSION,
                        'entry_count': entry_count,
                        'redacted_count': 0,
                        'files': file_hashes,
                        'projects': [entry],
                        'encryption': manifest.get('encryption', 'none'),
                        'encryption_salt': manifest.get('encryption_salt'),
                        'signature_verification': verification}
    save(bundle / 'manifest.json', project_manifest)
    return bundle


# ---------------------------------------------------------------- folder carrier (plan 2.3, slice 2)
# The store root IS the sync folder: registry.json, projects/ and
# sync-receipts/ all live under it and the sync service copies the whole
# tree. No sync-service API is ever called and no sync progress is
# observed; the tool only disciplines its own writes (atomic writes,
# heartbeat locks, conflict-copy isolation, checksum manifest) and
# refuses to build on a broken tree.
CONFLICT_PATTERNS_NOTE = '同步服务典型冲突命名'


def _is_conflict_name(name):
    """A sync-service conflict copy? Matched on the BASENAME only, so a
    conflict-named directory cannot condemn its children. Deliberately
    NOT matched: 'goal-conflict.json' (a legitimate plan-2.6 marker: it
    contains '-conflict.' but none of the patterns below, which all
    require a space/paren prefix or the sync-conflict infix)."""
    low = name.lower()
    return ('冲突副本' in name or 'conflicted copy' in low
            or '.sync-conflict-' in low or ' (conflict ' in low
            or '（conflict' in name)


def scan_folder_conflicts(store, isolate=True):
    """Scan the whole store for sync-service conflict copies (plan 2.3.4).

    trash/ (previous isolations) and sync-receipts/ (merge-log evidence)
    and .git are never scanned, so isolating a file cannot re-isolate
    its own preserved copy. With isolate=True each hit is MOVED (never
    deleted) to trash/conflicts/<ts>/ (relative path with '/' -> '__')
    and a conflicts-<ts>.json record lists the original paths; with
    isolate=False only the list is returned. Returns
    {found, isolated, report_path}."""
    store = Path(store)
    found = []
    for path in sorted(store.rglob('*')):
        if not path.is_file() or path.is_symlink():
            continue
        parts = path.relative_to(store).parts
        if parts[0] in ('.git', 'trash', 'sync-receipts'):
            continue
        if _is_conflict_name(path.name):
            found.append(path.relative_to(store).as_posix())
    isolated = []
    report_path = None
    if found and isolate:
        ts = _compact_ts(utc())
        dest_dir = store / 'trash' / 'conflicts' / ts
        dest_dir.mkdir(parents=True, exist_ok=True)
        for rel in found:
            dest = dest_dir / rel.replace('/', '__')
            n = 2
            while dest.exists():
                dest = dest_dir / ('%s-%d' % (rel.replace('/', '__'), n))
                n += 1
            shutil.move(str(store / rel), str(dest))
            isolated.append(rel)
        report_path = store / 'trash' / 'conflicts' / ('conflicts-%s.json' % ts)
        save(report_path, {'at': utc(), 'found': found, 'isolated': isolated})
    return {'found': found, 'isolated': isolated,
            'report_path': str(report_path) if report_path else None}


def sync_setup_folder(store, device_name=None):
    """Point the whole store at a sync folder (plan 2.3.1).

    Refuses when the current default store already holds projects and is
    a different directory: silently adopting a second store would strand
    the existing archive. Writes {carrier: folder} config plus the
    ~/.project-relay-location pointer (via relay.home_dir(), so tests
    stay inside PROJECT_RELAY_HOME); device identity stays in the home
    file, never inside the synced tree (two devices must never fight
    over one device_id). Runs one conflict scan immediately.

    Batch 3, C4: the store path is resolved to absolute before anything
    (including the home pointer below) is derived from it. A relative
    --folder stored verbatim would later resolve against each command's
    cwd and point at a different store, making projects appear missing
    or creating a second store."""
    store = Path(store).expanduser().resolve()
    default = relay.default_store()
    if default != store:
        reg_path = default / 'registry.json'
        if reg_path.exists():
            try:
                reg = read_json(reg_path)
            except ValueError:
                reg = {}
            if any(r.get('status', 'active') == 'active'
                   for r in reg.get('projects', [])):
                raise RelayError(
                    '本机默认记忆仓已有项目，与同步文件夹不是同一仓：'
                    '请先在默认仓执行 pack 导出，或手动把项目迁移到同步文件夹后再 setup')
    cfg = None
    if (store / 'sync-config.json').exists():
        try:
            cfg = read_json(store / 'sync-config.json')
        except ValueError:
            cfg = None
    if not isinstance(cfg, dict):
        cfg = {}
    cfg['carrier'] = 'folder'
    cfg.pop('remote', None)
    cfg.pop('server_type', None)
    cfg.pop('server_url', None)
    cfg.pop('server_user', None)
    cfg.setdefault('device_id', uuid.uuid4().hex)
    cfg['device_name'] = device_name or cfg.get('device_name') or socket.gethostname()
    cfg['updated_at'] = utc()
    save_sync_config(store, cfg)
    atomic(relay.home_dir() / '.project-relay-location',
           str(store).encode('utf-8'))
    scan = scan_folder_conflicts(store, isolate=True)
    notes = ['记忆仓已指向同步文件夹：' + str(store),
             '此后默认命令读写该文件夹；换设备时把同步文件夹同步过去即可']
    if scan['found']:
        notes.append('发现 %d 个同步冲突副本，已隔离，未自动合并（见 %s）'
                     % (len(scan['found']), scan['report_path']))
    return {'config': cfg, 'store': str(store), 'scan': scan, 'notes': notes}


# ---------------------------------------------------------------- server carrier (plan 2.4, slice 2)
# Server-side zero logic: the server is a byte store addressed by path.
# Remote layout per project key: relay/<key>/current/ holds one complete
# bundle (manifest.json + projects/... files), relay/<key>/current-index.json
# is a fast index written AFTER the swap (manifest.json inside current/ is
# the authority -- an index write crash cannot corrupt comparisons).
# Uploads go tmp dir -> read-back hash verify -> directory swap; a crash
# mid-swap leaves current/ missing and .old-<seq>-<ts>/ present, which the
# next read restores before anything else (see _read_server_current).
# Anti-overwrite key is (pack_seq, device_id): an existing bundle wins
# (upload refused) iff its seq is higher, or seq ties with a different
# device; same-device same-seq with different bytes is that device's own
# retry and is allowed (refusing it would brick the device after an
# upload-success-but-seq-not-bumped crash window); identical bundle hash
# is an idempotent retransmit and is allowed.
SERVER_TYPES = ('webdav', 'sftp', 'https', 'local')


def _server_rel(key, *parts):
    return '/'.join(['relay', key] + list(parts))


class LocalDirBackend:
    """Local-directory stand-in for a server (tests + local: URLs).

    Same method surface as the network backends: get/put/exists/list_dir/
    rename/remove_tree over posix relative paths."""

    def __init__(self, root):
        self.root = Path(root)

    def _p(self, rel):
        _jail_check(rel)
        return self.root / rel

    def exists(self, rel):
        return self._p(rel).exists()

    def get(self, rel):
        p = self._p(rel)
        return p.read_bytes() if p.is_file() else None

    def put(self, rel, data):
        p = self._p(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def list_dir(self, rel):
        p = self._p(rel)
        return sorted(x.name for x in p.iterdir()) if p.is_dir() else []

    def rename(self, src, dst):
        s, d = self._p(src), self._p(dst)
        require(s.exists(), '服务器上无此路径：' + src)
        require(not d.exists(), '服务器目标路径已存在：' + dst)
        d.parent.mkdir(parents=True, exist_ok=True)
        s.rename(d)

    def remove_tree(self, rel):
        shutil.rmtree(self._p(rel), ignore_errors=True)


class FaultBackend:
    """Wraps any backend and injects failures for tests (plan 2.4 tests).

    put_fail_after=N writes only the first N bytes of a tmp-dir upload
    then raises (simulated断线 mid-upload); get_fail makes every read
    raise. All other methods delegate untouched."""

    def __init__(self, inner, put_fail_after=None, get_fail=False):
        self.inner = inner
        self.put_fail_after = put_fail_after
        self.get_fail = get_fail

    def exists(self, rel):
        return self.inner.exists(rel)

    def get(self, rel):
        if self.get_fail:
            raise RelayError('模拟断线：读取失败')
        return self.inner.get(rel)

    def put(self, rel, data):
        if self.put_fail_after is not None and '/.tmp-' in rel:
            self.inner.put(rel, data[:self.put_fail_after])
            raise RelayError('模拟断线：写入中断（临时文件已残留，正式版本未动）')
        return self.inner.put(rel, data)

    def list_dir(self, rel):
        return self.inner.list_dir(rel)

    def rename(self, src, dst):
        return self.inner.rename(src, dst)

    def remove_tree(self, rel):
        return self.inner.remove_tree(rel)


class CurlBackend:
    """WebDAV / plain-HTTPS byte store via curl (plan 2.4.1).

    NOT exercised against a real server in this slice (tests use
    LocalDirBackend); the code path is straightforward curl and is
    marked as such in v2-sync.md §9. get() returns None on any curl
    failure (404 and network failure are indistinguishable here, and
    the download path treats None as missing -- fail-closed).
    rename() uses WebDAV MOVE; plain-HTTPS endpoints (allow_move=False)
    cannot swap atomically and are refused at swap time."""

    def __init__(self, base_url, token=None, allow_move=True):
        self.base = base_url.rstrip('/')
        self.token = token
        self.allow_move = allow_move

    def _url(self, rel):
        from urllib.parse import quote
        return self.base + '/' + '/'.join(quote(p) for p in rel.split('/'))

    def _run(self, args, data=None):
        cmd = ['curl', '-sf'] + args
        if self.token:
            cmd += ['-H', 'Authorization: Bearer ' + self.token]
        return subprocess.run(cmd, input=data, capture_output=True)

    def exists(self, rel):
        return self._run(['-I', self._url(rel)]).returncode == 0

    def get(self, rel):
        r = self._run([self._url(rel)])
        return r.stdout if r.returncode == 0 else None

    def put(self, rel, data):
        with tempfile.NamedTemporaryFile(delete=False) as tf:
            tf.write(data)
            tmp = tf.name
        try:
            r = self._run(['-X', 'PUT', '--data-binary', '@' + tmp,
                           self._url(rel)])
        finally:
            os.unlink(tmp)
        require(r.returncode == 0, '服务器上传失败：' + _text(r.stderr)[:200])

    def list_dir(self, rel):
        # WebDAV PROPFIND, Depth 1; href basenames only, best-effort.
        r = self._run(['-X', 'PROPFIND', '-H', 'Depth: 1', self._url(rel)])
        if r.returncode != 0:
            return []
        names = []
        for m in re.finditer(rb'<(?:\w+:)?href>([^<]+)</(?:\w+:)?href>',
                             r.stdout):
            href = m.group(1).decode('utf-8', 'ignore').rstrip('/')
            base = href.split('/')[-1]
            if base and base not in names:
                names.append(base)
        return names

    def rename(self, src, dst):
        require(self.allow_move, 'HTTPS 端点不支持改名，无法原子换位')
        r = self._run(['-X', 'MOVE', '-H', 'Destination: ' + self._url(dst),
                       self._url(src)])
        require(r.returncode == 0, '服务器改名失败：' + _text(r.stderr)[:200])

    def remove_tree(self, rel):
        self._run(['-X', 'DELETE', self._url(rel)])


class SftpBackend:
    """SFTP/rsync-over-SSH byte store (plan 2.4.1).

    Endpoint form: [user@]host:/base/path. Authentication is the user's
    ssh key/agent (no password handling here). Like CurlBackend, NOT
    exercised against a real server in this slice."""

    def __init__(self, endpoint, user=None):
        m = re.match(r'^(?:([^@]+)@)?([^:]+):(.+)$', endpoint or '')
        require(m, 'SFTP 端点格式错误，应为 [user@]host:/base/path：' + str(endpoint))
        self.user = user or m.group(1)
        self.host = m.group(2)
        self.base = m.group(3).rstrip('/')
        require(self.host and self.base, 'SFTP 端点缺少 host 或路径')

    def _remote(self, rel):
        prefix = ('%s@' % self.user) if self.user else ''
        return '%s%s:%s/%s' % (prefix, self.host, self.base, rel)

    def _ssh(self, script):
        r = subprocess.run(['ssh', self.host, script], capture_output=True)
        require(r.returncode == 0, 'SSH 命令失败：' + _text(r.stderr)[:200])
        return r

    def _rsync(self, src, dst):
        r = subprocess.run(['rsync', '-az', '-e', 'ssh', src, dst],
                           capture_output=True)
        require(r.returncode == 0, 'rsync 失败：' + _text(r.stderr)[:200])

    def exists(self, rel):
        r = subprocess.run(['ssh', self.host, 'test', '-e',
                            '%s/%s' % (self.base, rel)], capture_output=True)
        return r.returncode == 0

    def get(self, rel):
        with tempfile.TemporaryDirectory(prefix='relay-sftp-') as tmp:
            target = Path(tmp) / 'file'
            r = subprocess.run(['rsync', '-az', '-e', 'ssh', self._remote(rel),
                                str(target)], capture_output=True)
            return target.read_bytes() if r.returncode == 0 and target.is_file() else None

    def put(self, rel, data):
        with tempfile.TemporaryDirectory(prefix='relay-sftp-') as tmp:
            target = Path(tmp) / 'file'
            target.write_bytes(data)
            parent = '%s/%s' % (self.base, '/'.join(rel.split('/')[:-1]))
            self._ssh('mkdir -p %s' % parent)
            self._rsync(str(target), self._remote(rel))

    def list_dir(self, rel):
        r = subprocess.run(['ssh', self.host, 'ls', '-1',
                            '%s/%s' % (self.base, rel)], capture_output=True)
        return _text(r.stdout).splitlines() if r.returncode == 0 else []

    def rename(self, src, dst):
        self._ssh('mv %s/%s %s/%s' % (self.base, src, self.base, dst))

    def remove_tree(self, rel):
        self._ssh('rm -rf %s/%s' % (self.base, rel))


def _server_backend(store, cfg):
    """Backend from a server-carrier config (plan 2.4.5: address + user
    in config, token only in the separate credentials file, never in the
    bundle and never printed)."""
    stype = cfg.get('server_type')
    url = cfg.get('server_url')
    require(stype in SERVER_TYPES and url,
            '服务器配置不完整：请先运行 relay sync-setup --carrier server')
    if stype == 'local':
        root = url[6:] if url.startswith('local:') else url
        return LocalDirBackend(root)
    token = None
    cred = cfg.get('credential_file')
    if cred:
        cred_path = Path(cred) if Path(cred).is_absolute() else Path(store) / cred
        if cred_path.exists():
            try:
                token = read_json(cred_path).get('token')
            except ValueError:
                token = None
    if stype == 'sftp':
        return SftpBackend(url, user=cfg.get('server_user'))
    return CurlBackend(url, token=token, allow_move=(stype == 'webdav'))


def _read_server_current(backend, key):
    """Current bundle info for a project, restoring a crashed swap first.

    Returns None when the server holds nothing for the key. Raises when
    current/ exists but has no readable manifest (unknown state must
    block an upload, never be overwritten blind)."""
    base = _server_rel(key)
    names = backend.list_dir(base)
    if 'current' not in names:
        olds = sorted(n for n in names if n.startswith('.old-'))
        if not olds:
            return None
        backend.rename('%s/%s' % (base, olds[-1]), base + '/current')
    raw = backend.get(base + '/current/manifest.json')
    if raw is None:
        raise RelayError('服务器 current 缺少 manifest.json（可能换位中断后未恢复），'
                         '请检查服务器端 %s/current' % base)
    try:
        manifest = json.loads(raw.decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        raise RelayError('服务器 current 的 manifest.json 无法解析，拒绝覆盖')
    projects = manifest.get('projects') or []
    seq = projects[0].get('pack_seq') if projects else None
    device_id = manifest.get('source_device_id')
    if seq is None:
        # Standalone exports carry pack_seq=None in their manifest; the
        # index written by upload_server_bundle (same swap) carries the
        # seq the upload was issued with, so fall back to it rather
        # than comparing against an unknown (None would disable the
        # anti-overwrite key entirely).
        index_raw = backend.get(base + '/current-index.json')
        if index_raw is not None:
            try:
                index = json.loads(index_raw.decode('utf-8'))
                seq = index.get('seq')
                device_id = device_id or index.get('device_id')
            except (ValueError, UnicodeDecodeError):
                pass
    return {'seq': seq, 'device_id': device_id,
            'device_name': manifest.get('source_device'),
            'exported_at': manifest.get('exported_at'),
            'bundle_hash': _sha256(raw), 'manifest': manifest,
            'manifest_raw': raw}


def upload_server_bundle(backend, key, bundle_dir, seq, device_id,
                         device_name=None):
    """Atomic-ish server upload (plan 2.4.2-2.4.3).

    Verifies the bundle first (import-grade validation), refuses a
    newer existing bundle, writes a tmp dir, reads every file back and
    compares hashes, then swaps tmp into current/ (previous current/
    parked as .old-*, removed after the swap). A failure before the swap
    leaves the formal version untouched; a crash during the swap is
    recovered by _read_server_current on the next read. The sequence
    number is NOT consumed here -- the caller bumps pack-seq only after
    this returns (plan 2.4.3 / slice-1 rule)."""
    manifest, files, _warnings = verify_bundle(bundle_dir)
    bundle = Path(bundle_dir)
    new_hash = _sha256((bundle / 'manifest.json').read_bytes())
    current = _read_server_current(backend, key)
    if current is not None:
        if current['bundle_hash'] == new_hash:
            return {'status': 'unchanged', 'seq': seq,
                    'bundle_hash': new_hash}
        eseq, edev = current['seq'], current['device_id']
        if eseq is not None and seq is not None:
            if eseq > seq or (eseq == seq and edev != device_id):
                raise RelayError(
                    '服务器上已有更新的包（seq %s，设备 %s），拒绝覆盖：'
                    '先在本机拉取最新包合并，再重新导出' % (eseq, edev))
    base = _server_rel(key)
    tmp = '%s/.tmp-%s-%s' % (base, seq, (device_id or 'nodev')[:8])
    backend.remove_tree(tmp)  # stale tmp from a crashed earlier upload
    ordered = ['manifest.json'] + sorted(files)
    for rel in ordered:
        data = (bundle / 'manifest.json').read_bytes() if rel == 'manifest.json' \
            else files[rel]
        backend.put('%s/%s' % (tmp, rel), data)
    for rel in ordered:
        got = backend.get('%s/%s' % (tmp, rel))
        want = (bundle / 'manifest.json').read_bytes() if rel == 'manifest.json' \
            else files[rel]
        require(got == want, '远端字节校验失败，正式版本未动：' + rel)
    old = None
    try:
        if backend.exists(base + '/current'):
            old = '%s/.old-%s-%s' % (base, seq, _compact_ts(utc()))
            backend.rename(base + '/current', old)
        backend.rename(tmp, base + '/current')
    except Exception:
        if old is not None and not backend.exists(base + '/current'):
            try:
                backend.rename(old, base + '/current')
            except Exception:
                pass
        raise
    if old is not None:
        backend.remove_tree(old)
    index = {'seq': seq, 'device_id': device_id, 'device_name': device_name,
             'bundle_hash': new_hash, 'exported_at': manifest.get('exported_at'),
             'updated_at': utc()}
    backend.put(base + '/current-index.json',
                json.dumps(index, ensure_ascii=False).encode('utf-8'))
    return {'status': 'uploaded', 'seq': seq, 'device_id': device_id,
            'bundle_hash': new_hash, 'exported_at': manifest.get('exported_at')}


def download_server_bundle(backend, key, dest_dir):
    """Download current/ into a bundle dir, hash-checking every file
    against the server manifest (plan 2.4.4: download feeds the 2.1
    import pipeline, never a shortcut overwrite)."""
    current = _read_server_current(backend, key)
    if current is None:
        raise RelayError('服务器上无本项目包：' + _server_rel(key, 'current'))
    manifest = current['manifest']
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / 'manifest.json').write_bytes(current['manifest_raw'])
    for rel, want in (manifest.get('files') or {}).items():
        data = backend.get(_server_rel(key, 'current', rel))
        require(data is not None, '远端包文件缺失：' + rel)
        require(_sha256(data) == want, '远端包文件哈希不符：' + rel)
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return dest


def sync_setup_server(store, server_type, server_url, server_user=None,
                      token=None, device_name=None):
    """Server-carrier config (plan 2.4.5): address + user in
    sync-config.json; a token, when given, goes ONLY to the separate
    sync-credentials.json (0600) referenced by pointer, never into the
    config and never into any bundle. Setup without a token still
    succeeds (token can be added later)."""
    store = Path(store)
    require(server_type in SERVER_TYPES, '未知服务器类型：' + str(server_type))
    require(server_url, '服务器地址必填（--server-url）')
    cfg = None
    if (store / 'sync-config.json').exists():
        try:
            cfg = read_json(store / 'sync-config.json')
        except ValueError:
            cfg = None
    if not isinstance(cfg, dict):
        cfg = {}
    cfg['carrier'] = 'server'
    cfg.pop('remote', None)
    cfg['server_type'] = server_type
    cfg['server_url'] = server_url
    if server_user:
        cfg['server_user'] = server_user
    cfg.setdefault('device_id', uuid.uuid4().hex)
    cfg['device_name'] = device_name or cfg.get('device_name') or socket.gethostname()
    cfg['updated_at'] = utc()
    notes = ['服务器载体已配置：%s %s' % (server_type, server_url)]
    if token is not None:
        cred_path = store / 'sync-credentials.json'
        save(cred_path, {'token': token})
        os.chmod(cred_path, 0o600)
        cfg['credential_file'] = 'sync-credentials.json'
        notes.append('凭据已单独存入 sync-credentials.json（0600 权限），'
                     '仍属本机明文：请保护本机账户安全；凭据永不进入导出包')
    else:
        notes.append('未提供凭据：需要时可用 sync-setup --server-token 补录'
                     '（或设 RELAY_SERVER_TOKEN 环境变量）')
    save_sync_config(store, cfg)
    return {'config': cfg, 'notes': notes}


# ---------------------------------------------------------------- setup
def sync_setup(store, remote, device_name=None):
    """Bootstrap: git init + remote + gitignore + config + claim scan.

    The scan only REPORTS matches; nothing is landed (landing is unpack's
    job). An unreachable remote fails setup outright (the user must fix
    the address before any sync work)."""
    store = Path(store)
    # Batch 3, A1-adjacent: setup on a fresh path must work -- `git -C`
    # cannot chdir into a directory that does not exist yet, so the old
    # code died with a bare "Git 操作失败: init" on any fresh store.
    store.mkdir(parents=True, exist_ok=True)
    if not (store / '.git').is_dir():
        result = relay.git(store, 'init', '-b', 'main', check=False)
        if result.returncode != 0:
            relay.git(store, 'init')
    existing = relay.git(store, 'config', '--get', 'remote.origin.url', check=False)
    if existing.returncode == 0 and _text(existing.stdout).strip():
        if _text(existing.stdout).strip() != remote:
            relay.git(store, 'remote', 'set-url', 'origin', remote)
    else:
        relay.git(store, 'remote', 'add', 'origin', remote)
    atomic(store / '.gitignore', GITIGNORE_TEXT.encode('utf-8'))
    # Batch 3, A1: pin the store repo to byte-stable checkouts. The
    # per-process -c flag covers relay's own git calls, but plain git
    # run by the user inside the store (or any future call bypassing
    # the wrapper) would still convert LF->CRLF on checkout under
    # autocrlf=true and break bundle hashes and pack signatures.
    # `* -text` in a committed .gitattributes closes that path without
    # touching the user's global/system config. Written on every setup
    # so pre-fix stores heal on their next setup.
    atomic(store / '.gitattributes', b'* -text\n')
    _commit_if_changes(store, ['.gitattributes'],
                       'relay sync-setup: pin store to LF checkouts')
    config = None
    if (store / 'sync-config.json').exists():
        try:
            config = read_json(store / 'sync-config.json')
        except ValueError:
            config = None
    if not isinstance(config, dict):
        config = {}
    config.setdefault('device_id', uuid.uuid4().hex)
    config['carrier'] = 'git'
    config['remote'] = remote
    config['device_name'] = device_name or config.get('device_name') or socket.gethostname()
    config['updated_at'] = utc()
    save_sync_config(store, config)
    # Batch 2, B1: first git setup generates the device signing key
    # (home-only, 0600). The key file lives outside the store, so setup
    # on a second device generates a DIFFERENT key -- that is the point:
    # each device signs with its own seed, and peers pin each device's
    # public key separately (TOFU, see verify_bundle_signature).
    sync_key = ensure_sync_key(store)
    registry = relay.load_registry(store)
    local_by_key = {r['key']: r for r in registry.get('projects', [])
                    if r.get('status', 'active') == 'active'}
    claims = []
    notes = ['同步签收密钥已生成（仅存本机 %s，0600 权限，私钥永不进包）：%s'
             % (_sync_key_path(store), sync_key['key_id'])]
    with tempfile.TemporaryDirectory(prefix='relay-setup-') as tmp:
        clone_dir = _clone(remote, tmp, checkout=_git_branch(store))
        if _clone_is_empty(clone_dir):
            notes.append('远端为空：尚未有任何 pack，先在本机 pack 一次')
        else:
            projects_dir = clone_dir / 'projects'
            if projects_dir.is_dir():
                seen_keys = set()
                for d in sorted(projects_dir.iterdir()):
                    if not d.is_dir() or len(d.name) < 17 or d.name[16] != '-':
                        continue
                    key, slug = d.name[:16], d.name[17:]
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    row = local_by_key.get(key)
                    if row is not None:
                        claims.append({'key': key, 'slug': slug, 'status': 'matched',
                                       'local_display': row.get('display_name'),
                                       'local_paths': row.get('paths')})
                    else:
                        claims.append({'key': key, 'slug': slug, 'status': 'unclaimed'})
            else:
                notes.append('远端无 projects 目录：尚未有任何 pack')
    return {'config': config, 'claims': claims, 'notes': notes}


# ---------------------------------------------------------------- shared pack/unpack helpers (slice 2)
def _normalize_branch(branch):
    """Batch 2, B6: branch names compare after normalization -- the
    archive baseline line may record the full ref (refs/heads/main)
    while rev-parse reports the short name (main); they are the same
    branch, and reporting a mismatch for them is pure noise."""
    branch = (branch or '').strip()
    if branch.startswith('refs/heads/'):
        return branch[len('refs/heads/'):]
    return branch


def _consistency_audit(store, toplevel, store_dir):
    """Plan 2.5 step 3, report-only (never fails a pack): archive baseline
    branch vs current branch, dirty working files (capped at 20), drift
    count. Drift is normal; this only reports it."""
    flow = _flow()
    audit = {}
    try:
        branch = _text(relay.git(toplevel, 'rev-parse', '--abbrev-ref', 'HEAD').stdout).strip()
        head = _text(relay.git(toplevel, 'rev-parse', 'HEAD').stdout).strip()
        audit['branch'] = branch
        audit['head'] = head
        state_text = (store_dir / 'archive' / 'state.md').read_text(encoding='utf-8')
        m = re.search(r'^- 当前分支@基线：([^@]+)@', state_text, re.MULTILINE)
        if m and _normalize_branch(m.group(1)) != _normalize_branch(branch):
            audit['branch_mismatch'] = '档案基线分支 %s 与当前分支 %s 不一致' % (m.group(1), branch)
        dirty = _text(relay.git(toplevel, 'status', '--porcelain', check=False,
                                honor_autocrlf=True).stdout).splitlines()
        audit['dirty_files'] = dirty[:20]
        audit['dirty_truncated'] = len(dirty) > 20
        measure = flow.measure_change(store, toplevel)
        audit['drift'] = measure.get('drift_count')
    except Exception as exc:
        audit['error'] = str(exc)
    return audit


def _reconcile_note(store, row):
    """One read-only reconcile line for a project (plan 2.5.4): goal
    status, pending count, code branch/HEAD. Mirrors the slice-1 git
    unpack loop; missing code paths yield a note without branch fields
    (the caller skips such rows when the toplevel is not a local dir)."""
    store_dir = relay.project_store_dir(store, row)
    note = {'key': row['key'], 'slug': row['slug']}
    try:
        note['goal_status'] = relay.goal_status(store, row['paths'][0])[0]
    except Exception as exc:
        note['goal_status'] = 'error: %s' % exc
    try:
        note['pending'] = _flow().queue_status(store_dir, repair=False)['pending']
    except Exception as exc:
        note['pending_error'] = str(exc)
    try:
        note['branch'] = _text(relay.git(
            row['paths'][0], 'rev-parse', '--abbrev-ref', 'HEAD').stdout).strip()
        note['head'] = _text(relay.git(
            row['paths'][0], 'rev-parse', 'HEAD').stdout).strip()
    except Exception:
        pass
    return note


def _ready_gate(store, rows):
    """Plan 2.5.4 readiness gate: ready iff every project passes health,
    has no tampered goal, and has no checksum-manifest mismatch. Branch
    drift and other normal differences are listed by reconcile, not
    blockers (see v2-sync.md §9, D-sync-17).

    Batch 3, F3: an empty row set is NOT ready. Unclaimed-only unpacks
    feed zero matched rows into the gate, and the old vacuous truth
    printed "接力就绪" for a run that landed nothing."""
    if not rows:
        return False, ['本次未落盘任何已登记项目（仅待认领或无匹配项目），'
                       '接力尚未就绪：请先认领待认领项目后再 unpack']
    problems = []
    for row in rows:
        store_dir = relay.project_store_dir(store, row)
        if not store_dir.is_dir():
            continue
        probs, _warnings = health_check(store_dir)
        if probs:
            problems.append('%s 健康检查失败：%s' % (row['slug'], '；'.join(probs)))
        try:
            status = relay.goal_status(store, row['paths'][0])[0] \
                if row.get('paths') else None
        except Exception:
            status = None
        if status == 'tampered':
            problems.append('%s 目标被篡改（tampered），请先处理目标冲突' % row['slug'])
        _present, mismatches = relay.verify_checksum_manifest(store_dir)
        if mismatches:
            problems.append('%s 校验清单不符：%s'
                            % (row['slug'], '、'.join(m['file'] for m in mismatches)))
    return (not problems, problems)


# ---------------------------------------------------------------- pack
def _pack_git(store, toplevel, remote=None, device_name=None,
              overwrite_unconsumed=False, trust_unsigned=False, repin=False):
    """Fixed pack order (plan 2.5): pull-merge, summarize, health, audit,
    redact-rescan, bundle/commit/push, receipt. Any step failure stops
    the pack with an actionable message; the sequence number is consumed
    only after a successful push. Plan 2.6 rides on step 5: the WIP
    snapshot commit is built BEFORE the bundle push (so an oversize
    snapshot aborts the pack before anything is pushed) and its ref is
    pushed only AFTER the bundle push succeeds (so a failed pack never
    leaves a snapshot behind); overwrite_unconsumed is the explicit
    confirmation for covering an unconsumed snapshot. Batch 2: B1
    trust_unsigned/repin thread into the step-0 pull-merge (a tampered
    remote bundle must stop the pack, not merge silently); B5 identity
    is pre-checked after the step-2 health gate, before any commit."""
    row, store_dir = relay.resolve_store_dir(store, toplevel)
    key = row['key']
    slug = row['slug']
    cfg = load_sync_config(store)
    remote = remote or cfg.get('remote')
    device = device_name or cfg.get('device_name') or socket.gethostname()
    device_id = cfg.get('device_id')
    if not device_id:
        device_id = uuid.uuid4().hex
        cfg['device_id'] = device_id
        save_sync_config(store, cfg)
    steps = []
    flow = _flow()
    # 0: pull first, merge deterministically (anti-overwrite discipline)
    with tempfile.TemporaryDirectory(prefix='relay-pack-') as tmp:
        clone_dir = _clone(remote, tmp, checkout=_git_branch(store))
        if _clone_is_empty(clone_dir):
            steps.append({'step': 0, 'status': 'skipped', 'note': '远端为空'})
        else:
            bundle = _bundle_dir_from_clone(clone_dir, key, tmp,
                                            trust_unsigned=trust_unsigned,
                                            repin=repin, store=store)
            if bundle is None:
                steps.append({'step': 0, 'status': 'skipped',
                              'note': '远端无本项目清单'})
            else:
                merged = import_bundle(bundle, store, confirm=True)
                steps.append({'step': 0, 'status': 'merged',
                              'report': merged})
    # 1: forced summarize (pending only, deterministic minimal increment)
    summary_seq = None
    status = flow.queue_status(store_dir, repair=False)
    if status['pending'] > 0:
        try:
            summary_seq = flow.summarize(store, toplevel, source='pack',
                                         semantic_text=None)
        except Exception as exc:
            raise RelayError('pack 步骤①汇总失败：%s；请先手工运行 relay summarize 排查'
                             % exc)
        steps.append({'step': 1, 'status': 'summarized', 'seq': summary_seq})
    else:
        steps.append({'step': 1, 'status': 'noop'})
    # 2: health gate
    problems, warnings = health_check(store_dir)
    if problems:
        raise RelayError('pack 步骤②健康检查失败：%s' % '；'.join(problems))
    steps.append({'step': 2, 'status': 'ok', 'warnings': warnings})
    # Batch 2, B5: identity pre-check after the health gate and before
    # any commit step -- a store repo that cannot take a local identity
    # fails here with a named remedy, not as a bare git error at step 5.
    _ensure_git_identity(store)
    # 3: archive/code consistency audit (report only, never fails)
    audit = _consistency_audit(store, toplevel, store_dir)
    steps.append({'step': 3, 'status': 'reported', 'audit': audit})
    # 4: redact rescan (repairs local files, counts only)
    payload = _collect_payload(store_dir)
    cleaned, hits = _redact_payload(payload)
    fixed = _fix_local_redactions(store_dir, payload, cleaned)
    steps.append({'step': 4, 'status': 'ok', 'redacted_count': hits,
                  'fixed_local': fixed})
    # 5: bundle, commit, push
    seq_path = store_dir / 'state' / 'pack-seq.json'
    seq = read_json(seq_path)['next_seq'] if seq_path.exists() else 1
    # 5a (plan 2.6): build the WIP snapshot BEFORE any push, so an
    # oversize worktree aborts the pack before the bundle leaves.
    snapshot = create_wip_snapshot(toplevel, device, device_id, seq)
    with tempfile.TemporaryDirectory(prefix='relay-pack-') as tmp:
        staging = Path(tmp) / 'bundle'
        manifest = export_bundle(store, [toplevel], staging, device, device_id,
                                 pack_seqs={key: seq})
        manifest_path = store / 'sync-manifests' / ('%s-%04d.json' % (key, seq))
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        project_manifest = manifest['projects'][0]
        # Provenance for the next device: the per-project entry is what
        # unpack rebuilds bundles from, so device/time travel with it
        # (top-level manifest fields do not survive into sync-manifests).
        project_manifest['source_device'] = device
        project_manifest['source_device_id'] = device_id
        project_manifest['exported_at'] = manifest['exported_at']
        project_manifest['format_minor'] = FORMAT_MINOR
        # Batch 2, B1: the committed per-project manifest carries the
        # signature over the full path->hash manifest (the same dict
        # folder/server bundles hash-gate) plus the hash dict itself
        # for readers that want it; verification recomputes hashes
        # from disk and never trusts this stored copy.
        project_manifest['file_hashes'] = dict(manifest['files'])
        project_manifest['signature'] = sign_bundle_manifest(
            device_id, manifest['exported_at'], seq, manifest['files'],
            store=store)
        save(manifest_path, project_manifest)
        stage = ['sync-manifests/' + manifest_path.name] + [
            f for f in manifest['files'] if f.startswith('projects/%s-%s/' % (key, slug))]
        message = ('relay pack %s %s %s entries=%d files=%d seq=%d'
                   % (device, manifest['exported_at'], slug,
                      manifest['entry_count'], len(manifest['files']), seq))
        commit = _commit_if_changes(store, stage, message)
        branch = _git_branch(store)
        # Converge history before pushing: without this, a second
        # device's independent root history makes every push after the
        # first pack fail as non-fast-forward (see _converge_history).
        history_merge = _converge_history(store, remote, branch)
        head = relay.git(store, 'rev-parse', '--verify', 'HEAD', check=False)
        if head.returncode == 0:
            commit = _text(head.stdout).strip()
        origin = relay.git(store, 'config', '--get', 'remote.origin.url', check=False)
        if origin.returncode == 0 and _text(origin.stdout).strip() == remote:
            push_args = ('push', 'origin', branch)
        else:
            push_args = ('push', remote, branch)
        result = relay.git(store, *push_args, check=False)
        if result.returncode != 0:
            raise RelayError('pack 步骤⑤推送失败：%s；请检查远端地址与网络/凭证后重试'
                             % _text(result.stderr).strip()[:300])
    # 5b (plan 2.6): push the snapshot ref only after the bundle push
    # succeeded. A clean worktree yields no snapshot and no ref push.
    wip = {'status': 'clean'}
    if snapshot is not None:
        if remote is None:
            wip = {'status': 'skipped', 'reason': '未配置记忆仓远端，快照未推送'}
        else:
            pushed = push_wip_snapshot(toplevel, remote, snapshot,
                                       overwrite_unconsumed=overwrite_unconsumed)
            wip = {'status': 'created', 'sha': snapshot['sha'],
                   'branch': snapshot['branch'],
                   'changed_files': snapshot['changed_files'],
                   'size_bytes': snapshot['size_bytes'],
                   'ignored_recent': snapshot['ignored_recent'],
                   'ref': pushed['ref'],
                   'overwritten_unconsumed': pushed['overwritten_unconsumed'],
                   'previous': pushed['previous']}
    save(seq_path, {'next_seq': seq + 1})
    receipt = {'at': utc(), 'kind': 'pack', 'carrier': 'git', 'remote': remote,
               'device_name': device, 'device_id': device_id,
               'project_key': key, 'project_slug': slug, 'seq': seq,
               'commit': commit, 'history_merge': history_merge,
               'entry_count': manifest['entry_count'],
               'summary_count': len(list((store_dir / 'state' / 'summaries').glob('*.json')))
               if (store_dir / 'state' / 'summaries').is_dir() else 0,
               'file_count': len(manifest['files']),
               'redacted_count': hits, 'summary_seq': summary_seq,
               'pull_merge': steps[0], 'audit': audit, 'steps': steps,
               'wip': wip}
    receipt_path = _receipt_path(store, 'pack', key)
    save(receipt_path, receipt)
    return {'receipt': receipt, 'receipt_path': str(receipt_path), 'seq': seq,
            'commit': commit, 'manifest': manifest}


def pack_folder(store, toplevel, device_name=None):
    """Folder-carrier pack (plan 2.3 + 2.5 fixed order).

    No bundle, no commit, no sequence number: the synced folder IS the
    transport, so "packing" means: isolate conflict copies, refuse on a
    broken checksum manifest, force-summarize (a foreign heartbeat aborts
    the pack -- never pack with unsummarized raw), health gate, audit,
    redact rescan, refresh the checksum manifest, write a pack receipt.
    Any step failure stops the pack with an actionable message."""
    row, store_dir = relay.resolve_store_dir(store, toplevel)
    key, slug = row['key'], row['slug']
    cfg = load_sync_config(store)
    device = device_name or cfg.get('device_name') or socket.gethostname()
    device_id = cfg.get('device_id') or ''
    steps = []
    flow = _flow()
    scan = scan_folder_conflicts(store, isolate=True)
    steps.append({'step': 0, 'status': 'conflicts-isolated' if scan['found'] else 'clean',
                  'found': scan['found'], 'report': scan['report_path']})
    present, mismatches = relay.verify_checksum_manifest(store_dir)
    if present and mismatches:
        raise RelayError('pack 中止：校验清单不符（可能被同步截断/冲突污染）：%s；'
                         '先从其他设备或最近导出包恢复，恢复前不汇总、不写档案'
                         % '、'.join(m['file'] for m in mismatches))
    summary_seq = None
    status = flow.queue_status(store_dir, repair=False)
    if status['pending'] > 0:
        try:
            result = flow.summarize(store, toplevel, source='pack',
                                    semantic_text=None)
        except Exception as exc:
            raise RelayError('pack 步骤①汇总失败：%s；请先手工运行 relay summarize 排查'
                             % exc)
        if isinstance(result, dict) and result.get('degraded'):
            raise RelayError('pack 中止：另一台设备近期在写此项目，本次只采集不汇总；'
                             '请等对方写完后再 pack，不要带着未汇总原料打包')
        summary_seq = result
        steps.append({'step': 1, 'status': 'summarized', 'seq': summary_seq})
    else:
        steps.append({'step': 1, 'status': 'noop'})
    problems, warnings = health_check(store_dir)
    if problems:
        raise RelayError('pack 步骤②健康检查失败：%s' % '；'.join(problems))
    steps.append({'step': 2, 'status': 'ok', 'warnings': warnings})
    audit = _consistency_audit(store, toplevel, store_dir)
    steps.append({'step': 3, 'status': 'reported', 'audit': audit})
    payload = _collect_payload(store_dir)
    cleaned, hits = _redact_payload(payload)
    fixed = _fix_local_redactions(store_dir, payload, cleaned)
    steps.append({'step': 4, 'status': 'ok', 'redacted_count': hits,
                  'fixed_local': fixed})
    manifest_count = relay.update_checksum_manifest(store_dir)
    steps.append({'step': 5, 'status': 'ok', 'checksum_files': manifest_count})
    summaries_dir = store_dir / 'state' / 'summaries'
    receipt = {'at': utc(), 'kind': 'pack', 'carrier': 'folder', 'remote': None,
               'device_name': device, 'device_id': device_id,
               'project_key': key, 'project_slug': slug, 'seq': None,
               'commit': None,
               'entry_count': len([r for r in payload
                                   if r.startswith(RAW_PREFIX) and r.endswith('.md')]),
               'summary_count': len(list(summaries_dir.glob('*.json')))
               if summaries_dir.is_dir() else 0,
               'file_count': len(payload),
               'redacted_count': hits, 'summary_seq': summary_seq,
               'scan': scan, 'steps': steps}
    receipt_path = _receipt_path(store, 'pack', key)
    save(receipt_path, receipt)
    return {'receipt': receipt, 'receipt_path': str(receipt_path), 'seq': None,
            'commit': None, 'manifest': None}


def pack_server(store, toplevel, device_name=None, backend=None):
    """Server-carrier pack (plan 2.4 + 2.5 fixed order).

    Step 0 pulls the server's current bundle and merges it (server empty
    -> skip; pull failure aborts); steps 1-4 mirror the git pack; step 5
    uploads (upload failure aborts WITHOUT consuming the sequence
    number); step 6 writes a server pack receipt (server_url recorded,
    credentials never)."""
    row, store_dir = relay.resolve_store_dir(store, toplevel)
    key, slug = row['key'], row['slug']
    cfg = load_sync_config(store)
    backend = backend or _server_backend(store, cfg)
    device = device_name or cfg.get('device_name') or socket.gethostname()
    device_id = cfg.get('device_id') or ''
    steps = []
    flow = _flow()
    # 0: pull first, merge deterministically (anti-overwrite discipline)
    try:
        with tempfile.TemporaryDirectory(prefix='relay-pack-server-') as tmp:
            bundle = download_server_bundle(backend, key, Path(tmp) / 'bundle')
            merged = import_bundle(bundle, store, confirm=True)
            steps.append({'step': 0, 'status': 'merged', 'report': merged})
    except RelayError as exc:
        if '服务器上无本项目包' in str(exc):
            steps.append({'step': 0, 'status': 'skipped', 'note': '服务器为空'})
        else:
            raise RelayError('pack 步骤⓪拉取合并失败：%s；请检查服务器地址与网络后重试'
                             % exc)
    # 1: forced summarize (pending only, deterministic minimal increment)
    summary_seq = None
    status = flow.queue_status(store_dir, repair=False)
    if status['pending'] > 0:
        try:
            summary_seq = flow.summarize(store, toplevel, source='pack',
                                         semantic_text=None)
        except Exception as exc:
            raise RelayError('pack 步骤①汇总失败：%s；请先手工运行 relay summarize 排查'
                             % exc)
        steps.append({'step': 1, 'status': 'summarized', 'seq': summary_seq})
    else:
        steps.append({'step': 1, 'status': 'noop'})
    # 2: health gate
    problems, warnings = health_check(store_dir)
    if problems:
        raise RelayError('pack 步骤②健康检查失败：%s' % '；'.join(problems))
    steps.append({'step': 2, 'status': 'ok', 'warnings': warnings})
    # 3: archive/code consistency audit (report only, never fails)
    audit = _consistency_audit(store, toplevel, store_dir)
    steps.append({'step': 3, 'status': 'reported', 'audit': audit})
    # 4: redact rescan (repairs local files, counts only)
    payload = _collect_payload(store_dir)
    cleaned, hits = _redact_payload(payload)
    fixed = _fix_local_redactions(store_dir, payload, cleaned)
    steps.append({'step': 4, 'status': 'ok', 'redacted_count': hits,
                  'fixed_local': fixed})
    # 5: bundle + upload (sequence consumed only after a successful upload)
    seq_path = store_dir / 'state' / 'pack-seq.json'
    seq = read_json(seq_path)['next_seq'] if seq_path.exists() else 1
    with tempfile.TemporaryDirectory(prefix='relay-pack-server-') as tmp:
        staging = Path(tmp) / 'bundle'
        manifest = export_bundle(store, [toplevel], staging, device, device_id,
                                 pack_seqs={key: seq})
        try:
            uploaded = upload_server_bundle(backend, key, staging, seq,
                                            device_id, device_name=device)
        except RelayError as exc:
            raise RelayError('pack 步骤⑤上传失败：%s；序列号未消耗，可直接重试'
                             % exc)
    save(seq_path, {'next_seq': seq + 1})
    summaries_dir = store_dir / 'state' / 'summaries'
    receipt = {'at': utc(), 'kind': 'pack', 'carrier': 'server',
               'server_url': cfg.get('server_url'),
               'server_type': cfg.get('server_type'),
               'device_name': device, 'device_id': device_id,
               'project_key': key, 'project_slug': slug, 'seq': seq,
               'commit': None, 'upload': uploaded,
               'entry_count': manifest['entry_count'],
               'summary_count': len(list(summaries_dir.glob('*.json')))
               if summaries_dir.is_dir() else 0,
               'file_count': len(manifest['files']),
               'redacted_count': hits, 'summary_seq': summary_seq,
               'pull_merge': steps[0], 'audit': audit, 'steps': steps}
    receipt_path = _receipt_path(store, 'pack', key)
    save(receipt_path, receipt)
    return {'receipt': receipt, 'receipt_path': str(receipt_path), 'seq': seq,
            'commit': None, 'manifest': manifest}


def pack(store, toplevel, remote=None, device_name=None, carrier=None,
         overwrite_unconsumed=False, trust_unsigned=False, repin=False):
    """Carrier dispatcher (plan 2.3/2.4): git keeps the slice-1 behavior;
    folder and server packs share the 2.5 fixed order with carrier-
    specific step 0/5. The fixed order and fail-stop rule are identical
    across carriers; only the transport step differs. The WIP
    overwrite flag is git-only: folder/server carriers have no snapshot
    channel (plan 2.6), so the flag is ignored there. Batch 2, B1:
    trust_unsigned/repin are git-only (they gate the step-0 pull-merge
    signature check) and ignored by the other carriers."""
    cfg = load_sync_config(store)
    c = _carrier_of(store, cfg, carrier)
    if c == 'folder':
        result = pack_folder(store, toplevel, device_name=device_name)
    elif c == 'server':
        result = pack_server(store, toplevel, device_name=device_name)
    else:
        result = _pack_git(store, toplevel, remote=remote, device_name=device_name,
                           overwrite_unconsumed=overwrite_unconsumed,
                           trust_unsigned=trust_unsigned, repin=repin)
    # Volume 3, 3.6.3: pack-time prompt, never auto-migration. A weak-key
    # project that gained a remote is reported here; the pack itself still
    # uses the weak key until the user runs `relay migrate-key`.
    result['key_migration_prompt'] = relay.pending_key_migration(store, toplevel)
    return result


# ---------------------------------------------------------------- unpack
def _unpack_git(store, remote=None, confirm=False, project_keys=None,
                toplevel=None, trust_unsigned=False, repin=False):
    """Pull from the git carrier and merge via the import pipeline.

    Dry-run (no --confirm) prints a preview and writes NOTHING. On confirm,
    merge results are committed in the local store but NOT pushed (push is
    the next pack's job, plan 2.2.4); a read-only reconcile summary follows
    (branch/HEAD vs baseline, goal status, pending count — no resume call:
    resume's section 4 may write, so it is never invoked here). Plan 2.6:
    when `toplevel` is given (the code checkout on this device), an
    unconsumed WIP snapshot for its branch is restored into that
    worktree AFTER the archive merge; a refused/failed restore never
    bricks the already-landed archive merge (it is reported, not raised).
    Folder/server unpacks have no snapshot channel and never restore."""
    cfg = load_sync_config(store)
    remote = remote or cfg.get('remote')
    with tempfile.TemporaryDirectory(prefix='relay-unpack-') as tmp:
        clone_dir = _clone(remote, tmp, checkout=_git_branch(store))
        if _clone_is_empty(clone_dir):
            return {'landed': False, 'note': '远端无内容'}
        projects_dir = clone_dir / 'projects'
        if not projects_dir.is_dir():
            return {'landed': False, 'note': '远端无 projects 目录'}
        registry = relay.load_registry(store)
        reports = []
        changed = []
        # One import per KEY, not per directory: the git tree holds one
        # snapshot dir per device slug (same project, different checkout
        # names), and _bundle_dir_from_clone already resolves the key to
        # its latest manifest. Iterating every dir would import the same
        # bundle twice (second run all-skipped, but noisy).
        seen_keys = set()
        for d in sorted(projects_dir.iterdir()):
            if not d.is_dir() or len(d.name) < 17 or d.name[16] != '-':
                continue
            key = d.name[:16]
            if key in seen_keys:
                continue
            seen_keys.add(key)
            if project_keys and key not in project_keys:
                continue
            bundle = _bundle_dir_from_clone(clone_dir, key, tmp,
                                            trust_unsigned=trust_unsigned,
                                            repin=repin, store=store)
            if bundle is None:
                reports.append({'key': key, 'status': 'skipped',
                                'note': '远端无本项目清单'})
                continue
            report = import_bundle(bundle, store, confirm=confirm)
            reports.append({'key': key, 'report': report})
            if confirm:
                for proj in report['projects']:
                    if proj.get('status') == 'matched':
                        # Stage by the LOCAL dirname: the bundle slug is
                        # the source device's name for the project, which
                        # may differ from this device's slug for the same
                        # key (same remote, different checkout dir).
                        local_row = next(
                            (x for x in registry.get('projects', [])
                             if x.get('key') == proj['key']), None)
                        local_slug = local_row['slug'] if local_row else proj['slug']
                        prefix = 'projects/%s-%s/' % (proj['key'], local_slug)
                        changed.extend(prefix + f for f in proj.get('changed_files', []))
        if not confirm:
            return {'landed': False, 'reports': reports}
        commit = None
        if changed:
            commit = _commit_if_changes(
                store, changed,
                'relay unpack merge %s %s' % (cfg.get('device_name'), utc()))
        # Converge history so the next pack from this device fast-forwards
        # (same independent-root problem as pack; see _converge_history).
        history_merge = _converge_history(
            store, remote, _git_branch(store))
        head = relay.git(store, 'rev-parse', '--verify', 'HEAD', check=False)
        if head.returncode == 0:
            commit = _text(head.stdout).strip()
        reconcile = []
        matched_rows = []
        for proj in [r for r in reports if 'report' in r]:
            for entry in proj['report']['projects']:
                if entry.get('status') != 'matched':
                    continue
                row = next((x for x in registry.get('projects', [])
                            if x.get('key') == entry['key']), None)
                if row is None or not row.get('paths'):
                    continue
                if not Path(row['paths'][0]).is_dir():
                    continue
                matched_rows.append(row)
                reconcile.append(_reconcile_note(store, row))
        ready, ready_problems = _ready_gate(store, matched_rows)
        # Plan 2.6 restore: only with an explicit code checkout, only
        # after the archive merge landed, never raising.
        wip_restores = []
        if toplevel is not None:
            try:
                _row_u, store_dir_u = relay.resolve_store_dir(store, toplevel)
                wip_restores.append(restore_wip_snapshot(
                    store, toplevel, store_dir_u, remote))
            except RelayError as exc:
                wip_restores.append({'status': 'skipped', 'note': str(exc)})
        receipt = {'at': utc(), 'kind': 'unpack', 'carrier': 'git', 'remote': remote,
                   'device_name': cfg.get('device_name'),
                   'device_id': cfg.get('device_id'), 'commit': commit,
                   'history_merge': history_merge,
                   'reports': reports, 'reconcile': reconcile,
                   'ready': ready, 'ready_problems': ready_problems,
                   'wip_restores': wip_restores}
        receipt_path = _receipt_path(store, 'unpack', '')
        save(receipt_path, receipt)
        return {'landed': True, 'commit': commit, 'reports': reports,
                'reconcile': reconcile, 'ready': ready,
                'ready_problems': ready_problems,
                'wip_restores': wip_restores,
                'receipt_path': str(receipt_path)}


def unpack_folder(store, confirm=False, project_keys=None):
    """Folder-carrier unpack (plan 2.3 + 2.5.4): there is no bundle to
    import -- the synced folder already holds the archive. So this is a
    verify + reconcile pass: isolate conflict copies, check every
    project's checksum manifest (a mismatch blocks readiness and names
    the recovery path, it never writes on top of broken bytes), build
    read-only reconcile notes, and apply the readiness gate. The
    `confirm` flag is accepted for CLI symmetry and ignored (nothing
    here needs confirmation because nothing is merged)."""
    cfg = load_sync_config(store)
    scan = scan_folder_conflicts(store, isolate=True)
    registry = relay.load_registry(store)
    rows = [r for r in registry.get('projects', [])
            if r.get('status', 'active') == 'active']
    if project_keys:
        rows = [r for r in rows if r['key'] in project_keys]
    checksum = {}
    for row in rows:
        store_dir = relay.project_store_dir(store, row)
        if not store_dir.is_dir():
            continue
        present, mismatches = relay.verify_checksum_manifest(store_dir)
        checksum[row['key']] = {'present': present,
                                'mismatches': [m['file'] for m in mismatches]}
    reconcile = []
    for row in rows:
        if row.get('paths') and Path(row['paths'][0]).is_dir():
            reconcile.append(_reconcile_note(store, row))
    ready, ready_problems = _ready_gate(store, rows)
    receipt = {'at': utc(), 'kind': 'unpack', 'carrier': 'folder',
               'device_name': cfg.get('device_name'),
               'device_id': cfg.get('device_id'),
               'scan': scan, 'checksum': checksum, 'reconcile': reconcile,
               'ready': ready, 'ready_problems': ready_problems}
    receipt_path = _receipt_path(store, 'unpack', '')
    save(receipt_path, receipt)
    return {'landed': True, 'carrier': 'folder', 'conflicts': scan,
            'checksum': checksum, 'reconcile': reconcile, 'ready': ready,
            'ready_problems': ready_problems,
            'receipt_path': str(receipt_path)}


def unpack_server(store, confirm=False, project_keys=None, backend=None):
    """Server-carrier unpack (plan 2.4.4 + 2.5.4): download each
    project's current bundle and run it through the 2.1 import pipeline
    (dry-run without --confirm writes nothing). On confirm, landed
    results are NOT committed anywhere (the server carrier has no git
    semantics); a read-only reconcile + readiness gate follows, and
    only a passed gate is announced as ready by the caller."""
    cfg = load_sync_config(store)
    backend = backend or _server_backend(store, cfg)
    registry = relay.load_registry(store)
    rows = [r for r in registry.get('projects', [])
            if r.get('status', 'active') == 'active']
    if project_keys:
        rows = [r for r in rows if r['key'] in project_keys]
    reports = []
    with tempfile.TemporaryDirectory(prefix='relay-unpack-server-') as tmp:
        for row in rows:
            try:
                bundle = download_server_bundle(
                    backend, row['key'], Path(tmp) / ('bundle-%s' % row['key']))
            except RelayError as exc:
                reports.append({'key': row['key'], 'status': 'skipped',
                                'note': str(exc)})
                continue
            report = import_bundle(bundle, store, confirm=confirm)
            reports.append({'key': row['key'], 'report': report})
    if not confirm:
        return {'landed': False, 'carrier': 'server', 'reports': reports}
    matched_rows = []
    reconcile = []
    for rep in reports:
        if 'report' not in rep:
            continue
        for entry in rep['report']['projects']:
            if entry.get('status') != 'matched':
                continue
            row = next((x for x in registry.get('projects', [])
                        if x.get('key') == entry['key']), None)
            if row is None or not row.get('paths'):
                continue
            if not Path(row['paths'][0]).is_dir():
                continue
            matched_rows.append(row)
            reconcile.append(_reconcile_note(store, row))
    ready, ready_problems = _ready_gate(store, matched_rows)
    receipt = {'at': utc(), 'kind': 'unpack', 'carrier': 'server',
               'server_url': cfg.get('server_url'),
               'server_type': cfg.get('server_type'),
               'device_name': cfg.get('device_name'),
               'device_id': cfg.get('device_id'),
               'reports': reports, 'reconcile': reconcile,
               'ready': ready, 'ready_problems': ready_problems}
    receipt_path = _receipt_path(store, 'unpack', '')
    save(receipt_path, receipt)
    return {'landed': True, 'carrier': 'server', 'reports': reports,
            'reconcile': reconcile, 'ready': ready,
            'ready_problems': ready_problems,
            'receipt_path': str(receipt_path)}


def unpack(store, remote=None, confirm=False, project_keys=None, carrier=None,
           toplevel=None, trust_unsigned=False, repin=False):
    """Carrier dispatcher (plan 2.3/2.4): git keeps the slice-1 behavior
    (clone -> import -> commit, no push); folder verifies + reconciles
    the synced tree; server downloads -> import pipeline -> reconcile.
    All three end with the same 2.5.4 readiness gate. `toplevel` is the
    code checkout to restore a WIP snapshot into; it is git-only and
    ignored by the folder/server carriers (no snapshot channel). Batch
    2, B1: trust_unsigned/repin gate git signature verification only."""
    cfg = load_sync_config(store)
    c = _carrier_of(store, cfg, carrier)
    if c == 'folder':
        return unpack_folder(store, confirm=confirm, project_keys=project_keys)
    if c == 'server':
        return unpack_server(store, confirm=confirm, project_keys=project_keys)
    return _unpack_git(store, remote=remote, confirm=confirm,
                       project_keys=project_keys, toplevel=toplevel,
                       trust_unsigned=trust_unsigned, repin=repin)


# ---------------------------------------------------------------- claim / rollback / status
def sync_claim(store, key, toplevel, confirm=False):
    """Claim an unclaimed project: init the code repo if needed, then run
    the same merge preview/land as import (dry-run without --confirm).

    Batch 3, F2: the claim scan is read-only, so it runs first, and a
    dry-run never inits the real store: when the project has no local
    store yet, the preview merges against a throwaway store (relay.init
    writes only to the store, never to the code repo) that is deleted
    afterwards. Only --confirm creates the real local store."""
    store = Path(store)
    found = None
    unclaimed = store / 'unclaimed'
    if unclaimed.is_dir():
        for d in sorted(unclaimed.iterdir()):
            if not d.is_dir() or d.name.startswith('_'):
                continue
            claim_path = d / '_claim.json'
            if claim_path.exists():
                try:
                    claim = json.loads(claim_path.read_text(encoding='utf-8'))
                except (ValueError, UnicodeDecodeError):
                    continue
                if claim.get('key') == key:
                    found = (d, claim)
                    break
    if found is None:
        raise RelayError('待认领区无此项目：' + key)
    claim_dir, claim = found
    payload = {}
    for root, _dirs, files in os.walk(claim_dir):
        # Batch 2, B2: the import-time quarantine subtree is evidence,
        # not payload -- claiming must never resurrect quarantined
        # entries into a merge (or report them as ignored files).
        # Batch 3, C6: same for conflict-preserved versions.
        _dirs[:] = [d for d in _dirs if d not in ('_quarantine', '_conflicts')]
        for f in files:
            if f == '_claim.json':
                continue
            p = Path(root) / f
            # Batch 3, F1: payload keys are merge allowlist keys, which
            # only match posix prefixes (raw/, archive/, ...). str() of
            # a Windows relative path is backslash-joined, so every
            # file was "ignored" and the claim merged nothing.
            payload[p.relative_to(claim_dir).as_posix()] = p.read_bytes()
    files_in, _hits, marker_files, discarded, redacted_quarantined = \
        _redact_incoming(payload)
    source_info = {'device': claim.get('source_device'),
                   'device_id': claim.get('source_device_id'),
                   'seq': claim.get('pack_seq')}
    preview_tmp = None
    try:
        row, store_dir = relay.resolve_store_dir(store, toplevel)
    except RelayError:
        if confirm:
            relay.init(store, toplevel)
            row, store_dir = relay.resolve_store_dir(store, toplevel)
        else:
            preview_tmp = tempfile.TemporaryDirectory(prefix='relay-claim-preview-')
            preview_store = Path(preview_tmp.name) / 'store'
            relay.init(preview_store, toplevel)
            row, store_dir = relay.resolve_store_dir(preview_store, toplevel)
    try:
        merge_result = merge_project(store_dir, files_in, source_info)
        merge_result['discarded'].extend(discarded)
        merge_result['quarantined'].update(redacted_quarantined)
        report = {'key': key, 'slug': claim.get('slug'), 'status': 'matched',
                  'counts': merge_result['counts'], 'conflicts': merge_result['conflicts'],
                  'goal_action': merge_result['goal_action'],
                  'discarded': merge_result['discarded'],
                  'quarantined': sorted(merge_result['quarantined']),
                  'notes': merge_result['notes'],
                  'changed_files': sorted(merge_result['merged_files']),
                  'marker_files': sorted(marker_files)}
        if not confirm:
            return {'landed': False, 'report': report}
        # Batch 3, F5: an all-ignored merge must fail, not silently
        # consume the claim: landing zero files while deleting the
        # unclaimed copy destroys the only copy of content merge
        # refused to take. Partial landings (some files merged, some
        # ignored) are designed behavior and still land; only the
        # zero-landing case refuses.
        ignored = [n for n in merge_result['notes']
                   if n.startswith('忽略未纳入合并的文件：')]
        if ignored and not merge_result['merged_files']:
            raise RelayError('认领合并 0 文件且有 %d 个文件被忽略未纳入合并，'
                             '拒绝消费待认领副本（请先核对包内容）：%s'
                             % (len(ignored), '；'.join(ignored[:5])))
        landed = _land_project(store, store_dir, merge_result, source_info,
                               marker_files=marker_files,
                               quarantined=merge_result['quarantined'])
    finally:
        if preview_tmp is not None:
            preview_tmp.cleanup()
    report['quarantined_files'] = landed['quarantined_files']
    report['merge_log'] = landed['merge_log']
    shutil.rmtree(claim_dir, ignore_errors=True)
    index_path = unclaimed / '_index.json'
    if index_path.exists():
        try:
            index = read_json(index_path)
            index['claims'] = [c for c in index.get('claims', []) if c['key'] != key]
            save(index_path, index)
        except ValueError:
            pass
    return {'landed': True, 'report': report}


def _rollback_git(store, toplevel, ref, reason=''):
    """Restore one project to a git ref (plan 2.2.6: git history is the
    undo mechanism). Payload files are replaced wholesale; local-only
    files (config, enforce, cursor, ...) are untouched. The rollback note
    (state/rollback-note.json) is a local file AND a high-priority note in
    later merge-logs; real anti-overwrite protection is pack's pull-merge."""
    row, store_dir = relay.resolve_store_dir(store, toplevel)
    key, slug = row['key'], row['slug']
    store = Path(store)
    if not (store / '.git').is_dir():
        raise RelayError('记忆仓未初始化为 Git 仓，请先运行 relay sync-setup')
    if relay.git(store, 'rev-parse', '--verify', ref, check=False).returncode != 0:
        raise RelayError('无法解析回退目标：' + ref)
    prefix = 'projects/%s-%s/' % (key, slug)
    result = relay.git(store, 'archive', ref, prefix, check=False)
    if result.returncode != 0:
        raise RelayError('读取远端提交失败：' + _text(result.stderr).strip()[:300])
    with tempfile.TemporaryDirectory(prefix='relay-rollback-') as tmp:
        tmp = Path(tmp)
        with tarfile.open(fileobj=io.BytesIO(result.stdout), mode='r:*') as tf:
            tf.extractall(tmp, filter='data')
        ref_files = {}
        for root, _dirs, files in os.walk(tmp):
            for f in files:
                p = Path(root) / f
                # Batch 3, A4/F1: os.walk yields OS-native separators, so
                # str() gives backslashes on Windows and never matches
                # the posix prefix -- every rollback on Windows died
                # here with "该提交不含本项目负载". Normalize to posix.
                rel = p.relative_to(tmp).as_posix()
                if not rel.startswith(prefix):
                    continue
                proj_rel = rel[len(prefix):]
                if _is_payload_file(proj_rel):
                    ref_files[proj_rel] = p.read_bytes()
        if not ref_files:
            raise RelayError('该提交不含本项目负载：' + ref)
        backup_dir = store / 'sync-receipts' / ('rollback-%s' % _compact_ts(utc()))
        backup_dir.mkdir(parents=True, exist_ok=True)
        current = _collect_payload(store_dir)
        for rel, data in current.items():
            p = backup_dir / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        for rel in current:
            (store_dir / rel).unlink(missing_ok=True)
        for rel, data in ref_files.items():
            p = store_dir / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            atomic(p, data)
        note = {'ref': ref, 'at': utc(), 'reason': reason}
        save(store_dir / 'state' / 'rollback-note.json', note)
        relay.update_checksum_manifest(store_dir)
        stage = [prefix + r for r in ref_files]
        stage += [prefix + r for r in current if r not in ref_files]
        commit = None
        existing = [p for p in stage if (store / p).exists()]
        if existing:
            relay.git(store, 'add', '--', *existing, check=False)
        deleted = [p for p in stage if not (store / p).exists()]
        if deleted:
            relay.git(store, 'add', '-u', '--', *deleted, check=False)
        if relay.git(store, 'diff', '--cached', '--quiet', check=False).returncode != 0:
            result2 = relay.git(store, 'commit', '-m',
                                'relay rollback %s %s' % (slug, ref), check=False)
            if result2.returncode != 0:
                raise RelayError('回退提交失败：' + _text(result2.stderr).strip()[:300])
            commit = _text(relay.git(store, 'rev-parse', 'HEAD').stdout).strip()
        return {'backup_dir': str(backup_dir), 'commit': commit,
                'files_restored': len(ref_files), 'note': note}


def _collect_payload_lenient(store_dir):
    """Payload files present on disk, without the archive-five
    requirement: a rollback target may itself be the broken tree being
    rescued, and its backup must not fail on a missing file."""
    out = {}
    for path in sorted(store_dir.rglob('*')):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(store_dir).as_posix()
        if _is_payload_file(rel):
            out[rel] = path.read_bytes()
    return out


def rollback_from_bundle(store, toplevel, bundle, reason=''):
    """Folder/server-carrier rollback (plan 2.3 boundary: no git
    semantics, rollback rides on export bundles).

    The bundle is verified as untrusted input first (zero writes on
    failure). Current payload files are backed up to
    sync-receipts/rollback-<ts>/ AND moved (renamed, never deleted) to
    archive/rollback-trash/<ts>/ -- the plan-2.3.6 deletion shield, so a
    mistaken rollback can be undone from the trash. Bundle files then
    replace the payload wholesale; local-only files are untouched. No
    commit exists on these carriers (commit: None)."""
    row, store_dir = relay.resolve_store_dir(store, toplevel)
    key, slug = row['key'], row['slug']
    store = Path(store)
    bundle = Path(bundle)
    with tempfile.TemporaryDirectory(prefix='relay-rollback-bundle-') as tmp:
        if bundle.is_file():
            bundle_dir = read_bundle_zip(bundle, Path(tmp) / 'bundle')
        elif bundle.is_dir():
            bundle_dir = bundle
        else:
            raise RelayError('回退包不存在：' + str(bundle))
        manifest, files, _warnings = verify_bundle(bundle_dir)
        entry = next((p for p in manifest.get('projects', [])
                      if p.get('key') == key), None)
        require(entry is not None, '包内无本项目：' + key)
        dirname = '%s-%s' % (entry['key'], entry['slug'])
        ref_files = {}
        for rel, data in files.items():
            if not rel.startswith('projects/%s/' % dirname):
                continue
            proj_rel = rel[len('projects/%s/' % dirname):]
            if _is_payload_file(proj_rel):
                ref_files[proj_rel] = data
        require(ref_files, '包内无本项目负载：' + key)
        backup_dir = store / 'sync-receipts' / ('rollback-%s' % _compact_ts(utc()))
        backup_dir.mkdir(parents=True, exist_ok=True)
        current = _collect_payload_lenient(store_dir)
        for rel, data in current.items():
            p = backup_dir / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        ts = _compact_ts(utc())
        trash_dir = store_dir / 'archive' / 'rollback-trash' / ts
        for rel in current:
            dest = trash_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(store_dir / rel), str(dest))
        for rel, data in ref_files.items():
            p = store_dir / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            atomic(p, data)
        note = {'bundle': str(bundle), 'at': utc(), 'reason': reason}
        save(store_dir / 'state' / 'rollback-note.json', note)
        relay.update_checksum_manifest(store_dir)
        return {'backup_dir': str(backup_dir), 'trash_dir': str(trash_dir),
                'commit': None, 'files_restored': len(ref_files), 'note': note}


def sync_rollback(store, toplevel, ref=None, reason='', bundle=None):
    """Rollback dispatcher: git keeps the ref path (slice 1); folder and
    server carriers roll back from an export bundle (plan 2.3 boundary:
    never pretend git exists there). A bundle-less folder/server rollback
    fails with the recovery path instead of silently reusing git."""
    cfg = load_sync_config(store)
    c = _carrier_of(store, cfg, None)
    if c == 'git':
        if bundle is not None:
            raise RelayError('git 载体请用 --to ref 回退，--bundle 仅用于 folder/server 载体')
        if not ref:
            raise RelayError('git 载体回退需要 --to ref')
        return _rollback_git(store, toplevel, ref, reason)
    if bundle is None:
        raise RelayError('%s 载体回退需要 --bundle 导出包（目录或 zip）：'
                         '先用 relay pack 留一份导出包，或用 sync-receipts/rollback-* 备份恢复'
                         % c)
    return rollback_from_bundle(store, toplevel, bundle, reason)


# ---------------------------------------------------------------- status
def sync_status(store, toplevel=None):
    """Sync state per project (plan 2.5.5 "状态可见").

    Compares the latest pack receipt's counts against current counts;
    never nags (the only reminder moment is a device-switch intent,
    which this command does not guess). Git/server rows are read-only;
    the ONE folder-carrier exception is the conflict scan (plan 2.3.4):
    scanning a synced tree isolates hits into trash/conflicts, which is
    a reversible move, not a delete -- and a status that only reported
    pollution without removing it would leave the next pack shipping
    conflict copies as payload."""
    store = Path(store)
    registry = relay.load_registry(store)
    rows = registry.get('projects', [])
    if toplevel is not None:
        row, _store_dir = relay.resolve_store_dir(store, toplevel)
        rows = [row]
    try:
        cfg = load_sync_config(store)
    except RelayError:
        cfg = None
    scan = None
    if cfg is not None and cfg.get('carrier') == 'folder':
        scan = scan_folder_conflicts(store, isolate=True)
    out = []
    receipts_dir = store / 'sync-receipts'
    for row in rows:
        if row.get('status', 'active') != 'active':
            continue
        store_dir = relay.project_store_dir(store, row)
        current_entries = len(_parse_local_entries_all(store_dir)) \
            if store_dir.is_dir() else 0
        summaries_dir = store_dir / 'state' / 'summaries'
        current_summaries = len(list(summaries_dir.glob('*.json'))) \
            if summaries_dir.is_dir() else 0
        latest = None
        if receipts_dir.is_dir():
            for path in sorted(receipts_dir.glob('pack-%s-*.json' % row['key'])):
                try:
                    data = json.loads(path.read_text(encoding='utf-8'))
                except (ValueError, UnicodeDecodeError):
                    continue
                if latest is None or data.get('at', '') > latest.get('at', ''):
                    latest = data
        if latest is None:
            row_out = {'key': row['key'], 'slug': row['slug'],
                       'display_name': row.get('display_name'),
                       'packed': False,
                       'carrier': cfg.get('carrier') if cfg else None,
                       'current_entries': current_entries,
                       'current_summaries': current_summaries}
        else:
            row_out = {'key': row['key'], 'slug': row['slug'],
                       'display_name': row.get('display_name'),
                       'packed': True,
                       'carrier': latest.get('carrier')
                       or (cfg.get('carrier') if cfg else None),
                       'last_pack_at': latest.get('at'),
                       'last_pack_seq': latest.get('seq'),
                       'last_pack_commit': latest.get('commit'),
                       'remote': latest.get('remote'),
                       'new_entries_since_pack': max(
                           0, current_entries - latest.get('entry_count', 0)),
                       'new_summaries_since_pack': max(
                           0, current_summaries - latest.get('summary_count', 0))
                       if 'summary_count' in latest else None}
        if scan is not None:
            row_out['conflicts_found'] = scan['found']
            row_out['conflicts_report'] = scan['report_path']
        out.append(row_out)
    return out


# ---------------------------------------------------------------- WIP snapshot (2.6)
# Specweave-style private git snapshot for uncommitted work. The snapshot
# commit lives only on the memory store's remote, under the reserved ref
# refs/relay/wip/<code-branch>. It is NEVER pushed to the code repo's
# remote: snapshot content is uncommitted code, more private than the
# archive. Folder/server carriers have no snapshot channel by design
# (their only fallback is sync-rollback --bundle); they must not fake one.

WIP_REF_PREFIX = 'refs/relay/wip/'
WIP_CONSUMED_PREFIX = 'refs/relay/wip-consumed/'
WIP_SIZE_LIMIT = 100 * 1024 * 1024
WIP_IGNORED_RECENT_DAYS = 7
WIP_IGNORED_LIST_CAP = 100


def _wip_ref(branch):
    return WIP_REF_PREFIX + branch


def _wip_consumed_ref(branch):
    return WIP_CONSUMED_PREFIX + branch


def _code_branch(toplevel):
    """Current code branch name; detached HEAD is refused (a WIP ref
    named 'HEAD' would be meaningless on the other device)."""
    result = relay.git(toplevel, 'rev-parse', '--abbrev-ref', 'HEAD',
                       check=False)
    branch = _text(result.stdout).strip() if result.returncode == 0 else ''
    if not branch or branch == 'HEAD':
        raise RelayError('无法确定当前代码分支（detached HEAD 或非 Git 仓），'
                         '请先检出分支再 pack/unpack')
    return branch


def _ls_remote_refs(cwd, remote, patterns):
    """{refname: sha} for the given remote ref patterns (ls-remote)."""
    result = relay.git(cwd, 'ls-remote', remote, *patterns, check=False)
    refs = {}
    if result.returncode == 0:
        for line in _text(result.stdout).splitlines():
            parts = line.split('\t')
            if len(parts) == 2:
                refs[parts[1].strip()] = parts[0].strip()
    return refs


def _git_env(cwd, env, *args, input_data=None):
    """git with an explicit environment (for GIT_INDEX_FILE snapshots);
    relay.git takes no env argument, so this uses subprocess directly.
    Batch 3, A1: same per-process autocrlf=false pin as relayctl.git."""
    return subprocess.run(['git', '-c', 'core.autocrlf=false', '-C', str(cwd),
                           *args],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          input=input_data, env=env, timeout=120)


def _recent_ignored_files(toplevel, days=WIP_IGNORED_RECENT_DAYS):
    """Ignored files modified within the last `days` days (plan 2.6.4).

    .gitignore hits never enter the snapshot, but silently skipping a
    recently-touched local config would be a future handover accident,
    so the pack receipt must list them. Returns (listed, truncated)."""
    import time
    result = relay.git(toplevel, 'ls-files', '--others', '--ignored',
                       '--exclude-standard', '-z', check=False)
    cutoff = time.time() - days * 86400
    recent = []
    if result.returncode == 0:
        for rel in result.stdout.split(b'\0'):
            if not rel:
                continue
            path = Path(toplevel) / os.fsdecode(rel)
            try:
                if path.is_file() and path.stat().st_mtime >= cutoff:
                    recent.append(os.fsdecode(rel))
            except OSError:
                continue
    recent.sort()
    if len(recent) > WIP_IGNORED_LIST_CAP:
        return recent[:WIP_IGNORED_LIST_CAP], len(recent) - WIP_IGNORED_LIST_CAP
    return recent, 0


def create_wip_snapshot(toplevel, device, device_id, pack_seq,
                        size_limit=WIP_SIZE_LIMIT):
    """Build a snapshot commit from the current worktree (plan 2.6.2).

    A private temp GIT_INDEX_FILE is seeded from HEAD (when HEAD exists)
    and updated with `git add -A` (tracked modifications + untracked
    files, .gitignore respected), so the user's real index, worktree and
    branch are never touched. Returns None when the worktree is clean,
    else a dict with sha/tree/branch/changed_files/size/ignored info.
    Raises RelayError when the new content exceeds size_limit, listing
    the largest files."""
    status = _text(relay.git(toplevel, 'status', '--porcelain',
                             check=False, honor_autocrlf=True).stdout).splitlines()
    if not status:
        return None
    branch = _code_branch(toplevel)
    head = relay.git(toplevel, 'rev-parse', 'HEAD', check=False)
    head_sha = _text(head.stdout).strip() if head.returncode == 0 else None
    with tempfile.TemporaryDirectory(prefix='relay-wip-') as tmp:
        index = str(Path(tmp) / 'index')
        env = dict(os.environ, GIT_INDEX_FILE=index)
        if head_sha:
            seeded = _git_env(toplevel, env, 'read-tree', 'HEAD')
            if seeded.returncode != 0:
                raise RelayError('WIP 快照：读取 HEAD 到临时 index 失败：'
                                 + _text(seeded.stderr).strip()[:200])
        added = _git_env(toplevel, env, 'add', '-A')
        if added.returncode != 0:
            raise RelayError('WIP 快照：暂存工作区失败：'
                             + _text(added.stderr).strip()[:200])
        if head_sha:
            diff = _git_env(toplevel, env, 'diff', '--cached', '--name-only',
                            '-z', 'HEAD')
            changed = [os.fsdecode(p) for p in diff.stdout.split(b'\0') if p]
        else:
            listed = _git_env(toplevel, env, 'ls-files', '--cached', '-z')
            changed = [os.fsdecode(p) for p in listed.stdout.split(b'\0')
                       if p]
        sizes = []
        total = 0
        for rel in changed:
            path = Path(toplevel) / rel
            try:
                size = path.stat().st_size if path.is_file() else 0
            except OSError:
                size = 0
            sizes.append((size, rel))
            total += size
        if total > size_limit:
            sizes.sort(reverse=True)
            biggest = '\n'.join('  %s (%d MB)' % (rel, size // (1024 * 1024))
                                for size, rel in sizes[:20])
            raise RelayError(
                'WIP 快照体积 %d MB 超过阈值 %d MB，拒绝生成；'
                '请先提交或拆分大文件，最大的文件：\n%s'
                % (total // (1024 * 1024), size_limit // (1024 * 1024), biggest))
        tree = _git_env(toplevel, env, 'write-tree')
        if tree.returncode != 0:
            raise RelayError('WIP 快照：写树失败：'
                             + _text(tree.stderr).strip()[:200])
        tree_sha = _text(tree.stdout).strip()
        message = ('relay wip snapshot\n\n'
                   'Relay-Snapshot: true\n'
                   'Relay-Source-Device: %s\n'
                   'Relay-Source-Device-Id: %s\n'
                   'Relay-Pack-Seq: %d\n'
                   'Relay-Snapshot-At: %s\n'
                   % (device, device_id, pack_seq, utc()))
        args = ['commit-tree', tree_sha]
        if head_sha:
            args += ['-p', head_sha]
        args += ['-m', message]
        made = _git_env(toplevel, env, *args)
        if made.returncode != 0:
            raise RelayError('WIP 快照：生成提交失败：'
                             + _text(made.stderr).strip()[:200])
        sha = _text(made.stdout).strip()
    ignored, truncated = _recent_ignored_files(toplevel)
    return {'sha': sha, 'tree': tree_sha, 'branch': branch, 'head': head_sha,
            'changed_files': sorted(changed), 'size_bytes': total,
            'ignored_recent': ignored, 'ignored_truncated': truncated,
            'message': message}


def push_wip_snapshot(toplevel, remote, snapshot, overwrite_unconsumed=False):
    """Force-push a snapshot to the memory remote's reserved ref.

    One unconsumed snapshot per branch: when the remote ref already
    points at a snapshot no device has consumed, pack must warn and
    require explicit confirmation (--overwrite-unconsumed); without the
    flag it aborts. `remote` is always the memory remote from sync
    config -- the code repo's own remotes are never used as a target."""
    branch = snapshot['branch']
    ref = _wip_ref(branch)
    consumed_ref = _wip_consumed_ref(branch)
    refs = _ls_remote_refs(toplevel, remote, [ref, consumed_ref])
    old = refs.get(ref)
    consumed = refs.get(consumed_ref)
    overwritten = False
    if old and old != consumed and not overwrite_unconsumed:
        raise RelayError(
            '上一份未提交改动快照尚未在任何设备还原，将被覆盖：分支 %s，'
            '快照 %s。请先在另一台设备执行 unpack 还原，或 pack 时加 '
            '--overwrite-unconsumed 显式确认覆盖。' % (branch, old[:12]))
    if old and old != consumed:
        overwritten = True
    result = relay.git(toplevel, 'push', remote,
                       '+%s:%s' % (snapshot['sha'], ref), check=False)
    if result.returncode != 0:
        raise RelayError('WIP 快照推送失败：%s；请检查记忆仓远端后重试'
                         % _text(result.stderr).strip()[:300])
    return {'ref': ref, 'sha': snapshot['sha'],
            'overwritten_unconsumed': overwritten, 'previous': old}


def mark_wip_consumed(toplevel, remote, branch, sha):
    """Push the consumption marker for a restored snapshot. Best effort:
    returns False instead of raising -- a failed marker push only means
    the next pack warns (safe direction), it never undoes the restore."""
    result = relay.git(toplevel, 'push', remote,
                       '+%s:%s' % (sha, _wip_consumed_ref(branch)),
                       check=False)
    return result.returncode == 0


def restore_wip_snapshot(store, toplevel, store_dir, remote):
    """Restore an unconsumed snapshot into this device's worktree (plan
    2.6.5). Only runs when the worktree is clean; a dirty worktree
    refuses the restore and reports, never overwriting local changes.
    Restored content lands as UNSTAGED uncommitted changes
    (cherry-pick --no-commit + reset), matching the state the user left
    on the old device. Returns a status dict; never raises for a
    refused/failed restore (the archive unpack that already landed must
    not be bricked by a code restore problem)."""
    try:
        branch = _code_branch(toplevel)
    except RelayError as exc:
        return {'status': 'skipped', 'note': str(exc)}
    if not remote:
        return {'status': 'skipped', 'note': '未配置记忆仓远端，无快照可还原'}
    refs = _ls_remote_refs(toplevel, remote,
                           [_wip_ref(branch), _wip_consumed_ref(branch)])
    sha = refs.get(_wip_ref(branch))
    if not sha:
        return {'status': 'none', 'branch': branch}
    if sha == refs.get(_wip_consumed_ref(branch)):
        return {'status': 'already-consumed', 'branch': branch, 'sha': sha}
    fetched = relay.git(toplevel, 'fetch', remote, _wip_ref(branch),
                        check=False)
    if fetched.returncode != 0:
        return {'status': 'fetch-failed', 'branch': branch, 'sha': sha,
                'note': _text(fetched.stderr).strip()[:200]}
    message = relay.git(toplevel, 'log', '-1', '--format=%B', 'FETCH_HEAD',
                        check=False)
    if message.returncode != 0 or 'Relay-Snapshot: true' not in _text(message.stdout):
        return {'status': 'not-a-snapshot', 'branch': branch, 'sha': sha,
                'note': '远端保留 ref 上的提交缺少 Relay-Snapshot 标记，拒绝还原'}
    dirty = _text(relay.git(toplevel, 'status', '--porcelain',
                            check=False, honor_autocrlf=True).stdout).splitlines()
    if dirty:
        return {'status': 'refused-dirty', 'branch': branch, 'sha': sha,
                'dirty': dirty[:20],
                'note': '本机工作区不干净，拒绝还原（你的改动无损）；'
                        '请先提交或清理工作区后再 unpack'}
    picked = relay.git(toplevel, 'cherry-pick', '--no-commit', 'FETCH_HEAD',
                       check=False)
    if picked.returncode != 0:
        relay.git(toplevel, 'cherry-pick', '--abort', check=False)
        return {'status': 'failed-conflict', 'branch': branch, 'sha': sha,
                'note': '快照与本机 HEAD 冲突，已中止还原，工作区已恢复干净：'
                        + _text(picked.stderr).strip()[:200]}
    relay.git(toplevel, 'reset', check=False)
    consumed_push = mark_wip_consumed(toplevel, remote, branch, sha)
    save(store_dir / 'state' / 'wip-consumed.json',
         {'branch': branch, 'sha': sha, 'at': utc(),
          'consumed_push': consumed_push})
    audit = _consistency_audit(store, toplevel, store_dir)
    return {'status': 'restored', 'branch': branch, 'sha': sha,
            'consumed_push': consumed_push, 'consistency': audit}


# ---------------------------------------------------------------- conflict adjudication (2.6)
# Slice 1/2 only recorded conflicts (merge-log + goal-conflict.json +
# fork-note lines). This command writes the adjudication back into the
# archive and clears the markers; it never picks a side by itself -- the
# user decides at adjudication time. Evidence is preserved: resolutions
# are appended to the latest merge-log's 'resolutions' array (or a
# dedicated resolve receipt when no merge-log exists), never deleted.
# NOTE (non-stickiness, by design): merge is deterministic, so
# re-importing the same unchanged bundle re-flags a keep-local goal or
# fork decision -- the underlying disagreement still exists. Adopt
# decisions stick, because the adopted content then matches on re-import.

FORK_NOTE_RE = re.compile(r'^- 分叉待裁：(.+?)（对方）：(.*)$')


def _latest_merge_log(store, key):
    """Latest merge-<key>-*.json by mtime, or None."""
    receipts = Path(store) / 'sync-receipts'
    if not receipts.is_dir():
        return None, None
    candidates = []
    for path in receipts.glob('merge-%s-*.json' % key):
        try:
            candidates.append((path.stat().st_mtime, path))
        except OSError:
            continue
    if not candidates:
        return None, None
    candidates.sort()
    path = candidates[-1][1]
    try:
        return path, json.loads(path.read_text(encoding='utf-8'))
    except (ValueError, UnicodeDecodeError):
        return path, None


def _record_resolution(store, key, record):
    """Append a resolution to the latest merge-log, or a resolve receipt
    when no merge-log exists. Returns the path written."""
    path, log = _latest_merge_log(store, key)
    if path is not None and log is not None:
        log.setdefault('resolutions', []).append(record)
        save(path, log)
        return str(path)
    receipt = _receipt_path(store, 'resolve', key)
    save(receipt, {'at': utc(), 'kind': 'resolve', 'project_key': key,
                   'resolutions': [record]})
    return str(receipt)


def list_pending_resolutions(store, toplevel):
    """Pending adjudications for one project: goal-conflict.json, fork
    lines in state.md, and backlog conflicts in the latest merge-log
    that no resolution covers yet."""
    row, store_dir = relay.resolve_store_dir(store, toplevel)
    key = row['key']
    pending = []
    conflict_path = store_dir / 'state' / 'goal-conflict.json'
    if conflict_path.exists():
        try:
            info = read_json(conflict_path)
        except (ValueError, UnicodeDecodeError):
            info = {}
        pending.append({'kind': 'goal', 'source_device': info.get('source_device'),
                        'source_seq': info.get('source_seq'),
                        'note': '目标两边不同，本地版本被保留'})
    state_path = store_dir / 'archive' / 'state.md'
    if state_path.exists():
        for line in state_path.read_text(encoding='utf-8').splitlines():
            m = FORK_NOTE_RE.match(line)
            if m:
                pending.append({'kind': 'fork', 'key': m.group(1),
                                'incoming': m.group(2)})
    _path, log = _latest_merge_log(store, key)
    if log is not None:
        resolved_backlog = {r.get('item') for r in log.get('resolutions', [])
                            if r.get('kind') == 'backlog'}
        for conflict in log.get('conflicts', []):
            if 'item' in conflict and conflict['item'] not in resolved_backlog:
                pending.append({'kind': 'backlog', 'item': conflict['item'],
                                'local': conflict.get('local'),
                                'incoming': conflict.get('incoming')})
    return {'key': key, 'slug': row['slug'], 'pending': pending}


def sync_resolve(store, toplevel, list_only=False, goal=None, fork=None,
                 backlog=None, decision=None, file=None):
    """Adjudicate one pending conflict (plan 2.6 leftover).

    --list prints pending items without writing. Actions write the
    decision into the archive, clear the marker, refresh the checksum
    manifest (any archive/ write must, or the next resume reports a
    manifest mismatch), and record the resolution. Goal adopt-incoming
    needs --file: incoming goal bytes are deliberately NOT retained in
    the merge-log (goal is never auto-merged; only hashes are recorded),
    so the user supplies the text to adopt (e.g. copied from the other
    device); the adopted text is also staged as a draft so a normal
    `relay goal confirm` completes local confirmation."""
    row, store_dir = relay.resolve_store_dir(store, toplevel)
    key = row['key']
    if list_only:
        return list_pending_resolutions(store, toplevel)
    require(decision in ('keep-local', 'adopt-incoming'),
            '裁决须指定 --decision keep-local 或 adopt-incoming')
    try:
        device = load_sync_config(store).get('device_name')
    except RelayError:
        device = None
    actions = []
    touched_archive = False
    if goal is not None:
        conflict_path = store_dir / 'state' / 'goal-conflict.json'
        require(conflict_path.exists(), '本项目无待裁目标冲突（无 goal-conflict.json）')
        if decision == 'adopt-incoming':
            require(file, '目标采纳对方版须用 --file 提供目标文本（合并日志只记哈希，不保留对方原文）')
            raw = Path(file).expanduser().read_bytes()
            require(raw.strip(), '采纳的目标文件为空')
            try:
                text = raw.decode('utf-8')
            except UnicodeDecodeError:
                raise RelayError('采纳的目标文件不是 UTF-8 文本')
            atomic(store_dir / 'archive' / 'goal.md', raw)
            relay.goal_draft(store, toplevel, text)
            touched_archive = True
            actions.append('目标已更新为 --file 内容并暂存为草案，'
                           '请运行 relay goal confirm 完成本机确认')
        else:
            actions.append('目标保留本地版本')
        conflict_path.unlink(missing_ok=True)
        record_path = _record_resolution(
            store, key, {'at': utc(), 'kind': 'goal', 'decision': decision,
                         'device': device})
        actions.append('已清除 goal-conflict.json，裁决记入：' + record_path)
    elif fork is not None:
        state_path = store_dir / 'archive' / 'state.md'
        require(state_path.exists(), '本项目无 state.md')
        lines = state_path.read_text(encoding='utf-8').splitlines()
        incoming_values = []
        for line in lines:
            m = FORK_NOTE_RE.match(line)
            if m and m.group(1) == fork and m.group(2) not in incoming_values:
                incoming_values.append(m.group(2))
        require(incoming_values, '无待裁分叉：%s' % fork)
        require(len(incoming_values) == 1,
                '分叉 %s 有多个不同对方值，请先人工定夺：%s'
                % (fork, '；'.join(incoming_values)))
        kept = []
        in_block = False
        for line in lines:
            if line == STATE_BEGIN:
                in_block = True
            elif line == STATE_END:
                in_block = False
            m = FORK_NOTE_RE.match(line)
            if m and m.group(1) == fork:
                continue
            if (in_block and decision == 'adopt-incoming'
                    and line.startswith('- %s：' % fork)):
                kept.append('- %s：%s' % (fork, incoming_values[0]))
                continue
            kept.append(line)
        atomic(state_path, ('\n'.join(kept) + '\n').encode('utf-8'))
        touched_archive = True
        record_path = _record_resolution(
            store, key, {'at': utc(), 'kind': 'fork', 'key': fork,
                         'decision': decision, 'device': device})
        actions.append('分叉 %s 已裁决（%s），裁决记入：%s'
                       % (fork, decision, record_path))
    elif backlog is not None:
        _path, log = _latest_merge_log(store, key)
        require(log is not None, '本项目无合并日志，找不到待裁待办')
        conflict = None
        for candidate in log.get('conflicts', []):
            if candidate.get('item') == backlog:
                conflict = candidate
                break
        require(conflict is not None, '合并日志中无待裁待办：%s' % backlog)
        if decision == 'adopt-incoming':
            backlog_path = store_dir / 'archive' / 'backlog.md'
            text = backlog_path.read_text(encoding='utf-8') if backlog_path.exists() else ''
            blocks, _pre = _parse_blocks(text, 'T')
            next_id = (max([b['id'] for b in blocks]) + 1) if blocks else 1
            out = text.rstrip('\n')
            out += '\n## T-%03d\n%s\n' % (next_id, (conflict.get('incoming') or '').rstrip('\n'))
            atomic(backlog_path, out.encode('utf-8'))
            touched_archive = True
            actions.append('待办 %s 已采纳对方版，追加为 T%d' % (backlog, next_id))
        else:
            actions.append('待办 %s 保留本地版本' % backlog)
        record_path = _record_resolution(
            store, key, {'at': utc(), 'kind': 'backlog', 'item': backlog,
                         'decision': decision, 'device': device})
        actions.append('裁决记入：' + record_path)
    else:
        raise RelayError('须指定裁决对象：--goal、--fork <键> 或 --backlog <事项>'
                         '（先用 --list 查看待裁项）')
    if touched_archive:
        relay.update_checksum_manifest(store_dir)
    return {'key': key, 'slug': row['slug'], 'actions': actions}


# ---------------------------------------------------------------- volume-2 acceptance (2.6-2.7 + full loop)
# Mirrors the volume-1 five-act pattern (relay_accept): eleven acts on
# two simulated devices (two stores + two code checkouts sharing one
# fake remote URL, one bare memory remote; local dirs stand in for
# servers). No network, no real ~/.project-relay/ (HOME and
# PROJECT_RELAY_HOME are redirected for the run and restored after),
# no real server. Each act asserts one plan acceptance clause.

SYNC_ACCEPT_REMOTE = 'https://example.com/demo/sync-accept.git'
SYNC_ACCEPT_REMOTE_WIP = 'https://example.com/demo/sync-accept-wip.git'


def _accept_git_env(base):
    env = dict(os.environ)
    env['HOME'] = str(base / 'home')
    env['PROJECT_RELAY_HOME'] = str(base / 'home')
    env['GIT_AUTHOR_NAME'] = 'Tester'
    env['GIT_AUTHOR_EMAIL'] = 'test@example.invalid'
    env['GIT_COMMITTER_NAME'] = 'Tester'
    env['GIT_COMMITTER_EMAIL'] = 'test@example.invalid'
    return env


class _SyncAcceptRunner:
    def __init__(self, base):
        self.base = Path(base)
        self.env = _accept_git_env(self.base)
        (self.base / 'home').mkdir(parents=True, exist_ok=True)

    def git(self, root, *args):
        return subprocess.run(['git', '-c', 'core.autocrlf=false', '-C', str(root),
                               *args],
                              check=True, capture_output=True, env=self.env)

    def make_seed(self, name):
        root = self.base / name
        root.mkdir()
        self.git(root, 'init', '-b', 'main')
        (root / 'app.py').write_bytes('value = 1\n'.encode('utf-8'))
        self.git(root, 'add', '.')
        self.git(root, 'commit', '-m', 'baseline')
        return root

    def clone_device(self, seed, name, remote):
        dest = self.base / name
        subprocess.run(['git', '-c', 'core.autocrlf=false', 'clone', '--quiet',
                        str(seed), str(dest)],
                       check=True, capture_output=True, env=self.env)
        self.git(dest, 'remote', 'set-url', 'origin', remote)
        return dest

    def setup_store(self, name, mem, device):
        store = self.base / name
        # sync_setup runs `git -C <store>`, which needs the dir to exist
        # (tests get this for free because relay.init runs first).
        store.mkdir(parents=True, exist_ok=True)
        sync_setup(store, str(mem), device_name=device)
        return store

    def init_project(self, store, repo):
        relay.init(store, repo)

    def goal(self, store, repo, text='做一个小工具'):
        relay.goal_draft(store, repo, text)
        relay.goal_confirm(store, repo)

    def capture(self, store, repo, message, session):
        return _flow().capture(store, repo, tool='codex', session=session,
                               message=message)

    def summarize(self, store, repo, semantic_text):
        return _flow().summarize(store, repo, source='accept',
                                 semantic_text=semantic_text)

    def store_dir(self, store, repo):
        return relay.resolve_store_dir(store, repo)[1]

    def payload_minus_state(self, store, repo):
        out = {}
        for rel, data in _collect_payload(self.store_dir(store, repo)).items():
            if rel.startswith('receipts/') or rel == 'archive/state.md':
                continue
            out[rel] = data
        return out

    def payloads_equal_semantic(self, store_a, repo_a, store_b, repo_b):
        """Act-1 comparison: byte equality, except decisions/lessons/
        backlog compare as parsed block sets + preamble. Merge appends
        blocks with fresh local IDs and normalizes the blank line before
        the header, so byte equality would fail on cosmetic placement
        even when every item survived; item-by-item equality is the
        plan's '逐条一致' clause."""
        pa = self.payload_minus_state(store_a, repo_a)
        pb = self.payload_minus_state(store_b, repo_b)
        if set(pa) != set(pb):
            return False, '文件集合不同：仅 A 有 %s，仅 B 有 %s' % (
                sorted(set(pa) - set(pb)), sorted(set(pb) - set(pa)))
        prefixes = {'archive/decisions.md': 'D', 'archive/lessons.md': 'L',
                    'archive/backlog.md': 'T'}
        for rel in sorted(pa):
            if rel in prefixes:
                ba, pre_a = _parse_blocks(pa[rel].decode('utf-8'), prefixes[rel])
                bb, pre_b = _parse_blocks(pb[rel].decode('utf-8'), prefixes[rel])
                if sorted(_block_body(x) for x in ba) != sorted(_block_body(x) for x in bb):
                    return False, rel + ' 块集合不同'
                if pre_a.strip() != pre_b.strip():
                    return False, rel + ' 前言不同'
            elif pa[rel] != pb[rel]:
                return False, rel + ' 字节不同'
        return True, '全部文件一致（块文件按条目比对）'

    def entry_keys(self, store, repo):
        entries = _parse_local_entries_all(self.store_dir(store, repo))
        return {(e['header'].get('source'), e['header'].get('session'),
                 e['header'].get('fact_hash')): e['body']
                for e in entries}

    def worktree_status(self, repo):
        out = relay.git(repo, 'status', '--porcelain', check=False,
                        honor_autocrlf=True)
        return _text(out.stdout).splitlines()


def run_sync_acceptance():
    """Run all eleven volume-2 acts. Returns the record dict (no
    printing); render with render_sync_record()."""
    acts = []

    def check(act, scene, criterion, passed, evidence):
        acts.append({'act': act, 'scene': scene, 'criterion': criterion,
                     'result': '通过' if passed else '未通过',
                     'evidence': evidence})
        return passed

    old_home = os.environ.get('HOME')
    old_relay_home = os.environ.get('PROJECT_RELAY_HOME')
    old_git_env = {k: os.environ.get(k) for k in
                   ('GIT_AUTHOR_NAME', 'GIT_AUTHOR_EMAIL',
                    'GIT_COMMITTER_NAME', 'GIT_COMMITTER_EMAIL')}
    with tempfile.TemporaryDirectory(prefix='relay-sync-accept-') as tmp:
        base = Path(tmp)
        runner = _SyncAcceptRunner(base)
        os.environ['HOME'] = str(base / 'home')
        os.environ['PROJECT_RELAY_HOME'] = str(base / 'home')
        # Store commits go through relayctl.git, which inherits this
        # process's env (not the runner's subprocess env).
        os.environ['GIT_AUTHOR_NAME'] = 'Tester'
        os.environ['GIT_AUTHOR_EMAIL'] = 'test@example.invalid'
        os.environ['GIT_COMMITTER_NAME'] = 'Tester'
        os.environ['GIT_COMMITTER_EMAIL'] = 'test@example.invalid'
        try:
            mem = base / 'mem.git'
            subprocess.run(['git', '-c', 'core.autocrlf=false', 'init', '--bare', '--quiet', str(mem)],
                           check=True, capture_output=True, env=runner.env)
            seed = runner.make_seed('seed')
            code_a = runner.clone_device(seed, 'code-a', SYNC_ACCEPT_REMOTE)
            code_b = runner.clone_device(seed, 'code-b', SYNC_ACCEPT_REMOTE)
            store_a = runner.setup_store('store-a', mem, 'device-A')
            store_b = runner.setup_store('store-b', mem, 'device-B')
            runner.init_project(store_a, code_a)
            runner.init_project(store_b, code_b)
            runner.goal(store_a, code_a)

            # Act 1: two-device loop, archive identical bar state.md.
            (code_a / 'app.py').write_bytes('value = 2\n'.encode('utf-8'))
            runner.git(code_a, 'add', '.')
            runner.git(code_a, 'commit', '-m', 'bump to 2')
            runner.capture(store_a, code_a, '把 app 改成 2', 's1')
            runner.summarize(store_a, code_a,
                             '## 决定\n- 采用单例模式管理全局配置\n'
                             '## 待办\n- 上线检查：跑通冒烟测试\n')
            pack_a = pack(store_a, code_a)
            unpack_b = unpack(store_b, confirm=True)
            same_payload, payload_note = runner.payloads_equal_semantic(
                store_a, code_a, store_b, code_b)
            same_entries = (runner.entry_keys(store_a, code_a)
                            == runner.entry_keys(store_b, code_b))
            goal_b = (runner.store_dir(store_b, code_b) / 'archive' / 'goal.md').read_bytes()
            goal_a = (runner.store_dir(store_a, code_a) / 'archive' / 'goal.md').read_bytes()
            check('一', '两台设备完整 loop', 'A 改→pack→B unpack 后档案逐条一致（除 state.md 基线行）',
                  same_payload and same_entries and goal_a == goal_b,
                  'pack seq=%s, entries A=%d B=%d, unpack ready=%s；%s'
                  % (pack_a['seq'], len(runner.entry_keys(store_a, code_a)),
                     len(runner.entry_keys(store_b, code_b)), unpack_b.get('ready'),
                     payload_note))

            # Act 2: both sides change the same backlog item.
            sd_a = runner.store_dir(store_a, code_a)
            sd_b = runner.store_dir(store_b, code_b)
            backlog_a = sd_a / 'archive' / 'backlog.md'
            backlog_b = sd_b / 'archive' / 'backlog.md'
            text_a = backlog_a.read_text(encoding='utf-8')
            text_b = backlog_b.read_text(encoding='utf-8')
            next_a = (max([int(n) for n in re.findall(r'^## T(\d+)', text_a, re.MULTILINE)] or [0]) + 1)
            next_b = (max([int(n) for n in re.findall(r'^## T(\d+)', text_b, re.MULTILINE)] or [0]) + 1)
            backlog_a.write_bytes((text_a + '\n## T-%03d\n- 事项：发布检查\n- Owner：Agent\n- 验证方式：甲\n- 来源：汇总 9\n- 状态：待启动\n' % next_a).encode('utf-8'))
            backlog_b.write_bytes((text_b + '\n## T-%03d\n- 事项：发布检查\n- Owner：Agent\n- 验证方式：乙\n- 来源：汇总 9\n- 状态：待启动\n' % next_b).encode('utf-8'))
            relay.update_checksum_manifest(sd_a)
            relay.update_checksum_manifest(sd_b)
            pack(store_a, code_a)
            unpack(store_b, confirm=True)
            kept_b = '验证方式：乙' in backlog_b.read_text(encoding='utf-8')
            _path, latest_log = _latest_merge_log(store_b, relay.resolve_store_dir(store_b, code_b)[0]['key'])
            conflict_items = [c.get('item') for c in (latest_log or {}).get('conflicts', [])]
            check('二', '双方改同一条', '同一待办不同体→冲突报告且双方保留',
                  kept_b and '发布检查' in conflict_items
                  and '验证方式：甲' in json.dumps(latest_log or {}, ensure_ascii=False),
                  'B 保留乙，合并日志冲突项=%s' % conflict_items)

            # Act 3: tampered bundle refused before landing, naming file.
            bundle_dir = base / 'bundle-a'
            export_bundle(store_a, [code_a], bundle_dir, 'device-A',
                          load_sync_config(store_a).get('device_id'))
            victim_rel = None
            for rel in sorted(_collect_payload(sd_a)):
                if rel.startswith('archive/') and rel.endswith('.md'):
                    victim_rel = rel
                    break
            victim = bundle_dir / 'projects' / ('%s-%s' % (
                relay.resolve_store_dir(store_a, code_a)[0]['key'],
                relay.resolve_store_dir(store_a, code_a)[0]['slug'])) / victim_rel
            victim.write_bytes(victim.read_bytes() + b'\n# evil\n')
            tree_before = {p.relative_to(store_b).as_posix(): p.read_bytes()
                           for p in store_b.rglob('*') if p.is_file()}
            try:
                verify_bundle(bundle_dir)
                tamper_ok, tamper_evidence = False, '篡改包未被拒绝'
            except RelayError as exc:
                tamper_ok = victim_rel in str(exc)
                tamper_evidence = '拒绝：%s' % str(exc)[:120]
            try:
                import_bundle(bundle_dir, store_b, confirm=True)
                tamper_ok = False
                tamper_evidence += '；落盘未被拒绝'
            except RelayError:
                pass
            tree_after = {p.relative_to(store_b).as_posix(): p.read_bytes()
                          for p in store_b.rglob('*') if p.is_file()}
            check('三', '篡改包', '落盘前拒绝并指明文件，本地零写入',
                  tamper_ok and tree_before == tree_after, tamper_evidence)

            # Act 4: goal import lands as pending-local-confirm.
            code_c = runner.clone_device(seed, 'code-c', SYNC_ACCEPT_REMOTE)
            store_c = runner.setup_store('store-c', mem, 'device-C')
            runner.init_project(store_c, code_c)
            unpack(store_c, confirm=True)
            status_c = relay.goal_status(store_c, code_c)[0]
            injected = relay.goal_inject(store_c, code_c)
            confirmed = relay._confirm_imported(store_c, code_c)
            status_after = relay.goal_status(store_c, code_c)[0]
            imported_marker = runner.store_dir(store_c, code_c) / 'state' / 'goal-imported.json'
            check('四', '目标导入', '目标为待本机确认态，横幅提示，一键确认后转已确认',
                  status_c == 'imported' and '未经本机确认' in injected
                  and status_after == 'confirmed' and not imported_marker.exists(),
                  '导入态=%s，确认后=%s，收据 goal-%04d.json'
                  % (status_c, status_after, confirmed['receipt']['seq']))

            # Act 5: git conflict markers -- health gate blocks pack;
            # marker entries in an incoming bundle are discarded.
            decisions_a = sd_a / 'archive' / 'decisions.md'
            original_decisions = decisions_a.read_bytes()
            decisions_a.write_bytes(original_decisions + b'\n<<<<<<< HEAD\n')
            try:
                pack(store_a, code_a)
                marker_pack_ok = False
            except RelayError as exc:
                marker_pack_ok = '冲突标记' in str(exc)
            decisions_a.write_bytes(original_decisions)
            relay.update_checksum_manifest(sd_a)
            bundle5 = base / 'bundle-5'
            export_bundle(store_a, [code_a], bundle5, 'device-A',
                          load_sync_config(store_a).get('device_id'))
            manifest5 = json.loads((bundle5 / 'manifest.json').read_text(encoding='utf-8'))
            entry_rel = None
            for rel in manifest5['files']:
                if rel.startswith('projects/') and '/raw/cap-' in rel and rel.endswith('.md'):
                    entry_rel = rel
                    break
            entry_path = bundle5 / entry_rel
            body = entry_path.read_bytes()
            sep = b'---relay-body---'
            entry_path.write_bytes(body + b'\n<<<<<<< HEAD\n' if sep not in body
                                   else body.replace(sep, sep + b'\n<<<<<<< HEAD\n', 1))
            manifest5['files'][entry_rel] = _sha256(entry_path.read_bytes())
            (bundle5 / 'manifest.json').write_bytes(
                json.dumps(manifest5, ensure_ascii=False, indent=2).encode('utf-8'))
            code_d = runner.clone_device(seed, 'code-d', SYNC_ACCEPT_REMOTE)
            store_d = runner.setup_store('store-d', mem, 'device-D')
            runner.init_project(store_d, code_d)
            report5 = import_bundle(bundle5, store_d, confirm=True)
            discarded = report5['projects'][0].get('discarded', [])
            marker_discarded = any(d.get('reason') == 'conflict-markers'
                                   for d in discarded)
            landed_keys = runner.entry_keys(store_d, code_d)
            check('五', 'Git 冲突标记', '含标记档案 pack 被健康门拦截；含标记入包条目被丢弃，双方数据无损',
                  marker_pack_ok and marker_discarded,
                  'pack 拦截=%s，丢弃=%s，D 侧条目 %d 条'
                  % (marker_pack_ok, discarded, len(landed_keys)))

            # Act 6: folder carrier write/read + conflict-copy isolation.
            folder = base / 'folder-store'
            sync_setup_folder(folder, device_name='folder-A')
            code_f = runner.make_seed('code-f')
            relay.init(folder, code_f)
            pack_folder(folder, code_f)
            sd_f = relay.resolve_store_dir(folder, code_f)[1]
            planted = sd_f / 'archive' / 'decisions (conflicted copy 2026).md'
            planted.write_bytes('junk\n'.encode('utf-8'))
            legit = sd_f / 'state' / 'goal-conflict.json'
            legit.write_bytes('{}'.encode('utf-8'))
            unpacked_f = unpack_folder(folder)
            isolated = list((folder / 'trash' / 'conflicts').rglob('*')) if (folder / 'trash' / 'conflicts').is_dir() else []
            check('六', '文件夹载体', '一写一读一致，冲突副本被隔离，合法标记文件不动',
                  not planted.exists() and legit.exists() and len(isolated) >= 1
                  and unpacked_f.get('ready'),
                  '隔离 %d 个，ready=%s' % (len(isolated), unpacked_f.get('ready')))

            # Act 7: server carrier interrupt + anti-overwrite.
            srv = base / 'server'
            backend = LocalDirBackend(srv)
            key_a = relay.resolve_store_dir(store_a, code_a)[0]['key']
            bundle1 = base / 'bundle-s1'
            export_bundle(store_a, [code_a], bundle1, 'device-A',
                          load_sync_config(store_a).get('device_id'), pack_seqs={key_a: 1})
            upload_server_bundle(backend, key_a, bundle1, 1, 'devA')
            before = _read_server_current(backend, key_a)
            bundle2 = base / 'bundle-s2'
            export_bundle(store_a, [code_a], bundle2, 'device-A',
                          load_sync_config(store_a).get('device_id'), pack_seqs={key_a: 2})
            faulty = FaultBackend(LocalDirBackend(srv), put_fail_after=5)
            try:
                upload_server_bundle(faulty, key_a, bundle2, 2, 'devA')
                interrupt_ok = False
            except RelayError:
                after = _read_server_current(backend, key_a)
                interrupt_ok = (after['seq'] == before['seq']
                                and after['bundle_hash'] == before['bundle_hash'])
            try:
                upload_server_bundle(backend, key_a, bundle2, 1, 'devB')
                overwrite_ok = False
                overwrite_note = '旧序列号异设备未被拒绝'
            except RelayError as exc:
                overwrite_ok = '先在本机拉取最新包合并' in str(exc)
                overwrite_note = '旧序列号异设备被拒绝并给指引'
            # A strictly newer seq from another device IS the next pack
            # (devices take turns); only stale-or-equal seqs are refused.
            try:
                upload_server_bundle(backend, key_a, bundle2, 2, 'devB')
                newer = _read_server_current(backend, key_a)
                newer_ok = newer['seq'] == 2
                overwrite_note += '；更新序列号异设备被接受为新版（seq=2）'
            except RelayError:
                newer_ok = False
                overwrite_note += '；更新序列号异设备被错误拒绝'
            check('七', '服务器载体', '上传中断后正式版本不变；陈旧序列号拒绝覆盖并给指引，更新序列号接受为新版',
                  interrupt_ok and overwrite_ok and newer_ok,
                  '中断后 seq=%s 不变；%s' % (before['seq'], overwrite_note))

            # Act 8: WIP snapshot round trip.
            mem_wip = base / 'mem-wip.git'
            subprocess.run(['git', '-c', 'core.autocrlf=false', 'init', '--bare', '--quiet', str(mem_wip)],
                           check=True, capture_output=True, env=runner.env)
            code_aw = runner.clone_device(seed, 'code-aw', SYNC_ACCEPT_REMOTE_WIP)
            code_bw = runner.clone_device(seed, 'code-bw', SYNC_ACCEPT_REMOTE_WIP)
            store_aw = runner.setup_store('store-aw', mem_wip, 'device-AW')
            store_bw = runner.setup_store('store-bw', mem_wip, 'device-BW')
            runner.init_project(store_aw, code_aw)
            runner.init_project(store_bw, code_bw)
            (code_aw / '.gitignore').write_bytes('local.conf\n'.encode('utf-8'))
            (code_aw / 'local.conf').write_bytes('token=local\n'.encode('utf-8'))
            (code_aw / 'app.py').write_bytes('value = 99\n'.encode('utf-8'))
            (code_aw / 'new_feature.py').write_bytes('x = 1\n'.encode('utf-8'))
            pack_aw = pack(store_aw, code_aw)
            wip = pack_aw['receipt'].get('wip', {})
            wip_created = wip.get('status') == 'created'
            ignored_listed = 'local.conf' in (wip.get('ignored_recent') or [])
            unpack_bw = unpack(store_bw, confirm=True, toplevel=code_bw)
            restores = unpack_bw.get('wip_restores', [])
            restored = any(r.get('status') == 'restored' for r in restores)
            status_bw = runner.worktree_status(code_bw)
            bw_app = (code_bw / 'app.py').read_text(encoding='utf-8')
            bw_has_new = (code_bw / 'new_feature.py').exists()
            bw_no_local = not (code_bw / 'local.conf').exists()
            bw_unstaged = (any(line.startswith(' M ') for line in status_bw)
                           and any(line.startswith('?? ') for line in status_bw))
            # Dirty-refusal: BW dirties, AW packs again, BW unpack refuses.
            (code_bw / 'app.py').write_bytes('value = bw-own\n'.encode('utf-8'))
            (code_aw / 'app.py').write_bytes('value = 100\n'.encode('utf-8'))
            pack(store_aw, code_aw)
            unpack_bw2 = unpack(store_bw, confirm=True, toplevel=code_bw)
            restores2 = unpack_bw2.get('wip_restores', [])
            refused = any(r.get('status') == 'refused-dirty' for r in restores2)
            bw_intact = (code_bw / 'app.py').read_text(encoding='utf-8') == 'value = bw-own\n'
            # Overwrite guard: AW packs again without BW restoring.
            (code_aw / 'app.py').write_bytes('value = 101\n'.encode('utf-8'))
            try:
                pack(store_aw, code_aw)
                guard_ok = False
            except RelayError as exc:
                guard_ok = '尚未在任何设备还原' in str(exc)
            pack_forced = pack(store_aw, code_aw, overwrite_unconsumed=True)
            guard_forced = pack_forced['receipt'].get('wip', {}).get('overwritten_unconsumed') is True
            check('八', 'WIP 快照往返', '已改未提交+未跟踪文件以未提交形态出现，ignore 文件不出现但回执有清单；脏工作区拒绝还原且改动无损；未消费覆盖须显式确认',
                  wip_created and ignored_listed and restored and bw_app == 'value = 99\n'
                  and bw_has_new and bw_no_local and bw_unstaged
                  and refused and bw_intact and guard_ok and guard_forced,
                  '快照=%s，B 状态=%s，脏拒绝=%s，守卫=%s/强制=%s'
                  % (wip.get('sha', '')[:12], status_bw, refused, guard_ok, guard_forced))

            # Act 9: same-machine flows carry no sync wording.
            rendered = _flow().render_resume(_flow().resume(store_a, code_a))
            injected = _flow().inject(store_a, code_a)
            check('九', '同机换工具', 'resume/inject 全程无同步字样',
                  '同步' not in rendered and '同步' not in injected['text'],
                  'resume %d 字，inject %d 字' % (len(rendered), len(injected['text'])))

            # Act 10: pending entries summarize before pack.
            runner.capture(store_a, code_a, '待汇总的一条', 's-pending')
            pack_pending = pack(store_a, code_a)
            step1 = [s for s in pack_pending['receipt']['steps'] if s.get('step') == 1][0]
            unpack(store_b, confirm=True)
            keys_b = runner.entry_keys(store_b, code_b)
            keys_a = runner.entry_keys(store_a, code_a)
            check('十', 'pack 前积压', 'pending 先汇总后打包，新设备档案含这批内容',
                  step1.get('status') == 'summarized'
                  and pack_pending['receipt'].get('summary_seq') is not None
                  and set(keys_a) <= set(keys_b),
                  '步骤①=%s，汇总序号=%s，B 条目 %d 条'
                  % (step1.get('status'), pack_pending['receipt'].get('summary_seq'), len(keys_b)))

            # Act 11: volume-1 acceptance still passes (regression gate).
            spec = importlib.util.spec_from_file_location(
                'relay_accept', _HERE / 'relay_accept.py')
            accept_mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(accept_mod)
            record1 = accept_mod.run_acceptance()
            check('十一', '卷一验收重演', 'relay accept 仍 PASS',
                  record1['verdict'] == 'PASS',
                  '卷一验收=%s' % record1['verdict'])
        finally:
            if old_home is None:
                os.environ.pop('HOME', None)
            else:
                os.environ['HOME'] = old_home
            if old_relay_home is None:
                os.environ.pop('PROJECT_RELAY_HOME', None)
            else:
                os.environ['PROJECT_RELAY_HOME'] = old_relay_home
            for k, v in old_git_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    verdict = 'PASS' if all(a['result'] == '通过' for a in acts) else 'FAIL'
    return {'acts': acts, 'verdict': verdict}


def render_sync_record(record):
    lines = ['# 卷二验收记录（2.6–2.7 + 全 loop）', '',
             '| 幕 | 场景 | 通过标准 | 实测结果 | 证据指针 |',
             '|---|---|---|---|---|']
    for a in record['acts']:
        lines.append('| %s | %s | %s | %s | %s |'
                     % (a['act'], a['scene'], a['criterion'], a['result'], a['evidence']))
    lines += ['', '## 判定', '', '卷二验收：**%s**' % record['verdict']]
    if record['verdict'] != 'PASS':
        failed = [a for a in record['acts'] if a['result'] != '通过']
        lines.append('未通过：' + '、'.join('%s·%s' % (a['act'], a['scene']) for a in failed))
    return '\n'.join(lines) + '\n'
