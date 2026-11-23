"""project-relay v3 sync: single-project zip handoff (plan part 03, v3 slim).

v3 model: one project, one archive tree at <project root>/.project-relay/
(per-agent memory under agents/<agent>/, outer goal/state/backlog/
decisions/stack/outlook/index). No registry, no project key, no sync
config, no carriers (git/folder/server all deleted), no unclaimed
landing: import-zip merges straight into the local project; a missing
local archive is created by relay.init first. The only carrier is a zip
file carried by hand (export-zip / import-zip).

Standard library only. Loaded lazily by scripts/relay.py like relay_flow.
The sibling modules share one process-wide relay copy (see the module
top), so a RelayError raised here is the same class main() catches.

Bundle format 2.0: manifest + projects/<slug>/ + the whole
.project-relay/ tree (minus device-local files, see _is_payload_file).
Format 1.x bundles (archive/ layout) are refused: their paths no longer
merge.
"""

import importlib.util
import hashlib
import io
import json
from contextlib import nullcontext
import os
import re
import secrets
import shlex
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
# Reuse the process-wide relay copy when one is already registered
# (relay.py claims the name at its own top; the CLI's __main__ copy
# and the test harness's copy both count). A fresh copy would carry a
# different RelayError class, which main()'s handler cannot catch.
_relay_mod = sys.modules.get('relay')
if _relay_mod is None:
    _spec = importlib.util.spec_from_file_location('relay', _HERE / 'relay.py')
    _relay_mod = importlib.util.module_from_spec(_spec)
    sys.modules['relay'] = _relay_mod
    _spec.loader.exec_module(_relay_mod)
relay = _relay_mod

RelayError = relay.RelayError
require = relay.require
utc = relay.utc
digest = relay.digest
atomic = relay.atomic
save = relay.save
read_json = relay.read_json
git = relay.git

BUNDLE_FORMAT = 'project-relay-export'
FORMAT_MAJOR = 2
FORMAT_MINOR = 0
TOOL_VERSION = 'project-relay-v3'
# Local zip import is user-mediated, but a malformed or malicious zip
# must not exhaust memory/disk before manifest verification. These are
# deliberately generous for a text archive and still bound a zip bomb.
MAX_BUNDLE_ENTRIES = 20000
MAX_BUNDLE_FILE_BYTES = 128 * 1024 * 1024
MAX_BUNDLE_TOTAL_BYTES = 512 * 1024 * 1024
MAX_BUNDLE_COMPRESSION_RATIO = 1000

MARKER_PREFIXES = ('<<<<<<<', '=======', '>>>>>>>')
_PROTECTED_HEADER_FIELDS = ('fingerprint', 'fact_hash', 'backfill', 'anchor')
BODY_DELIM = '\n---relay-body---\n'
STATE_BEGIN = '<!-- relay:state-begin -->'
STATE_END = '<!-- relay:state-end -->'
FORK_NOTE_RE = re.compile(r'^- 分叉待裁：(.+?)（对方）：(.*)$')
_PROJECT_SLUG_RE = re.compile(r'^[a-z0-9._-]+$')

# Device-local files inside the store: never shipped, never merged.
# The sets live in relay.py so the ZIP payload and the checksum manifest
# use exactly the same definition of "machine-local".
_LOCAL_ONLY_OUTER = relay.LOCAL_ONLY_OUTER
_LOCAL_ONLY_AGENT_STATE = relay.LOCAL_ONLY_AGENT_STATE
_LOCAL_ONLY_AGENT_FILES = relay.LOCAL_ONLY_AGENT_FILES
_LOCAL_ONLY_AGENT_DIRS = relay.LOCAL_ONLY_AGENT_DIRS


def _is_payload_file(rel):
    """True for store files that may travel in a zip bundle.

    Everything shippable is the archive itself: outer files, per-agent
    memory/decisions/lessons/backlog/changes/goal/log/raw/state
    (last-summary + summaries), receipts, and goal draft/tentative
    machine state. Device-local files never ship: lock files, the
    checksum manifest (regenerated after landing), rollback trash,
    outer config + adjudication markers, per-agent measurement state
    (cursor/baseline/numbers/digest/enforce/forks/backlog-notice),
    recovery state (tx/backups), per-agent stats (stats/), install
    records (recipe.json); config.json and state/fingerprint-cache.json
    are machine-local too.
    """
    if rel.endswith('.lock'):
        return False
    if rel == 'checksum-manifest.json':
        return False
    if 'rollback-trash/' in rel:
        return False
    if relay.is_local_only_path(rel):
        return False
    if rel.startswith('agents/'):
        return True
    if rel.startswith('receipts/'):
        return True
    if rel in ('goal.md', 'state.md', 'backlog.md', 'decisions.md',
               'stack.md', 'outlook.md', 'index.md', 'project.json'):
        return True
    if rel.startswith('state/goal-tentative.') \
            or rel.startswith('state/goal-draft.'):
        return True
    return False


def _is_raw_entry_rel(rel):
    """True for payload paths that are capture entry files."""
    if not (rel.startswith('agents/') and '/raw/' in rel
            and rel.endswith('.md')):
        return False
    name = rel.rsplit('/raw/', 1)[-1]
    return name.startswith('cap-') or name.startswith('archived/cap-')


def _collect_payload(store_dir):
    """Read a project's shippable files. Returns {relpath: bytes}."""
    store_dir = Path(store_dir)
    require(store_dir.is_dir(), '项目仓不存在：' + str(store_dir))
    out = {}
    for path in sorted(store_dir.rglob('*')):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(store_dir).as_posix()
        if _is_payload_file(rel):
            out[rel] = path.read_bytes()
    require('goal.md' in out, '档案缺失：goal.md')
    return out


# Lazy singletons: must exist at module level -- _flow() and
# _template_state_values() both test them with `is None` first, and an
# unassigned global raises NameError on first use (batch 18 catch).
_FLOW = None
_TEMPLATE_STATE_VALUES = None


def _flow():
    """relay_flow, loaded once (single source of truth for redact patterns,
    summarize, and queue observation). Reuses the process-wide copy when
    one is registered, so its RelayError is the same class as this
    module's (both load the same relay copy; see the module top)."""
    global _FLOW
    if _FLOW is None:
        mod = sys.modules.get('relay_flow')
        if mod is None:
            spec = importlib.util.spec_from_file_location('relay_flow', _HERE / 'relay_flow.py')
            mod = importlib.util.module_from_spec(spec)
            sys.modules['relay_flow'] = mod
            spec.loader.exec_module(mod)
        _FLOW = mod
    return _FLOW


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


def _no_symlink_components(base, rel):
    """Reject any symlink component under base (files and dirs alike)."""
    p = base
    for part in PurePosixPath(rel).parts:
        p = p / part
        require(not p.is_symlink(), '包内含符号链接：' + rel)


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
    LF entry round-trips byte-identical. A CRLF entry is normalized
    to LF first (batch 5, step-3 Windows re-run -- see
    _parse_entry_text): the rescan then succeeds and repairs the
    local file to LF via _fix_local_redactions. Raises ValueError on
    a malformed entry (caller converts to a fail-closed RelayError)."""
    text = text.replace('\r\n', '\n')
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
    repairs the local copy too (atomic write, counted in the receipt).
    All writes are rolled back if one fails, so a partial repair cannot
    leave the local archive half-redacted with a stale manifest."""
    changed = [rel for rel in sorted(original) if original[rel] != redacted[rel]]
    fixed = []
    try:
        for rel in changed:
            atomic(store_dir / rel, redacted[rel])
            fixed.append(rel)
    except Exception:
        for rel in fixed:
            try:
                atomic(store_dir / rel, original[rel])
            except Exception:
                pass
        raise
    return fixed


def _parse_entry_text(text):
    """Parse one cap file. Raises RelayError on any structural problem.

    Batch 5 (step-3 Windows re-run): a cap file may hold CRLF newlines
    (a text-mode writer on Windows, or any outside tool). Normalize
    CRLF to LF before splitting: the BODY_DELIM is LF-only, and the
    header/body contents are newline-insensitive here. Without this,
    a CRLF entry fails the split and every reader of it -- export,
    import, verify -- reports it as format-damaged.
    """
    text = text.replace('\r\n', '\n')
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


def _is_template_state_outside(before, after):
    """True when the outside-block text is byte-identical to the init
    template's outside-block text (batch 10). Fail-closed: an unreadable
    template answers False, so nothing is adopted on a guess."""
    # v3: the managed block lives in each agent's memory.md; its
    # template is memory.md when it exists, else the outer state.md
    # template (same block keys; fail-closed when neither reads).
    for _name in ('memory.md', 'state.md'):
        try:
            text = (relay.TEMPLATES_DIR / _name).read_text(encoding='utf-8')
            break
        except (OSError, ValueError):
            continue
    else:
        return False
    if STATE_BEGIN not in text or STATE_END not in text:
        return False
    start = text.index(STATE_BEGIN)
    stop = text.index(STATE_END) + len(STATE_END)
    return (before or '') == text[:start] and (after or '') == text[stop:]


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
        text = ''
        for _name in ('memory.md', 'state.md'):
            try:
                text = (relay.TEMPLATES_DIR / _name).read_text(encoding='utf-8')
                break
            except (OSError, ValueError):
                continue
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


def _merge_state_md(local_text, incoming_text, filename='archive/state.md'):
    """S6 state.md rule on the v1 managed block.

    filename names the file being merged (v3: an agent's memory.md);
    it labels conflicts and notes, nothing else.

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
    display = filename.rsplit('/', 1)[-1]
    # Batch 10/Batch 19: do NOT blanket-delete 分叉待裁 notes here. An
    # unresolved fork may still be waiting for sync_resolve; deleting it
    # made the conflict disappear from list_pending_resolutions without a
    # decision. Existing notes are parsed after the block is split and
    # only dropped when this merge proves them resolved (local value now
    # equals incoming) or stale (the peer's value changed again).
    if STATE_BEGIN not in local_text or STATE_END not in local_text:
        if local_text != incoming_text:
            conflicts.append({'file': filename,
                              'rule': 'unmanaged-state-kept-local',
                              'note': '本地 %s 无受管块，保留本地全文' % display})
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
    # Pull existing 分叉待裁 notes out of the outside-block text. They are
    # re-added only if still unresolved after this merge (see below).
    existing_fork = []
    kept_after = []
    for line in l_after.splitlines(True):
        m = FORK_NOTE_RE.match(line.strip())
        if m:
            existing_fork.append((m.group(1), m.group(2), line.rstrip('\r\n')))
        else:
            kept_after.append(line)
    l_after = ''.join(kept_after)
    if (l_before, l_after) != (i_before, i_after):
        # Batch 10: on a fresh device the outside-block text is the init
        # template's explanatory prose, so 'different' means 'local never
        # wrote anything here'. Adopting incoming outside text in that
        # case keeps the takeover report clean and loses nothing (the
        # template prose is reproduced from the template on demand).
        if _is_template_state_outside(l_before, l_after):
            l_before, l_after = i_before, i_after
            notes.append('%s 块外文本仍为模板原文，采纳对方内容' % display)
        else:
            conflicts.append({'file': filename,
                              'rule': 'outside-block-kept-local',
                              'note': '块外文本不同，保留本地版本'})

    def parse_block(block):
        order = []
        values = {}
        for line in block.splitlines():
            m = re.match(r'^- ([^：]+)：(.*)$', line.strip())
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
            notes.append('%s 的 %s 仍为模板原文，采纳对方内容' % (display, key))
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
            # Batch 10: adopt when the local value carries no information
            # (template placeholder or a stale '分叉待裁' value left by an
            # earlier import). Keeping a known-empty line and stacking a
            # fork note on it was exactly the fresh-device noise; a real
            # local value still wins and still gets its note.
            # The branch line is local measurement (D-sync-3): it must
            # keep the local value even when that value is the template
            # '未知', so a fresh device never claims the peer's commit.
            if key != '当前分支@基线' and (
                    not l_values[key].strip() or l_values[key] in ('未知', '无')):
                merged_values[key] = i_values[key]
                notes.append('%s 的 %s 本机为空，采纳对方内容' % (display, key))
                continue
            fork_notes.append((key, '- 分叉待裁：%s（对方）：%s' % (key, i_values[key])))
            conflicts.append({'file': filename, 'key': key,
                              'rule': 'fork-note-kept-local',
                              'local': l_values[key], 'incoming': i_values[key]})
    for key in i_values:
        if key not in merged_values:
            order.append(key)
            merged_values[key] = i_values[key]
    # Existing 分叉待裁 notes: keep only those still unresolved. A note is
    # resolved when the merged value now equals the peer's value; it is
    # stale when the peer has since changed again (the loop above then
    # emits a fresh note for the current values).
    kept_existing = []
    kept_keys = set()
    dropped_existing = 0
    for key, old_incoming, line in existing_fork:
        if key in i_values:
            if merged_values.get(key) == i_values[key]:
                dropped_existing += 1
                continue
            if i_values[key] != old_incoming:
                dropped_existing += 1
                continue
        kept_existing.append(line)
        kept_keys.add(key)
    if dropped_existing:
        notes.append('%s 清理了 %d 条已裁决或过期的「分叉待裁」记录'
                     % (display, dropped_existing))
    block_lines = [STATE_BEGIN]
    for key in order:
        block_lines.append('- %s：%s' % (key, merged_values[key]))
    block_lines.append(STATE_END)
    merged = l_before + '\n'.join(block_lines)
    all_fork_lines = kept_existing + [line for key, line in fork_notes
                                      if key not in kept_keys]
    if all_fork_lines:
        merged += '\n' + '\n'.join(all_fork_lines)
    merged += l_after
    return merged, conflicts, notes


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


def _map_queue(incoming_queue):
    if incoming_queue in ('summarized', 'archived'):
        return 'summarized'
    return 'pending'


def _shadow_copy(local_dir):
    """Copy the formal project store to a scratch dir (symlinks refused).

    Lock files and rollback-trash are skipped: they are not archive payload,
    and a persistent land backup can be large. Every *.lock is skipped, not
    just the archive-wide one, because another writer may hold an agent lock
    open (Windows refuses to read a locked file)."""
    local_dir = Path(local_dir)
    shadow = Path(tempfile.mkdtemp(prefix='relay-merge-'))
    for root, dirs, files in os.walk(local_dir):
        root = Path(root)
        kept_dirs = []
        for d in dirs:
            here = root / d
            rel_parts = here.relative_to(local_dir).parts
            if rel_parts[:2] == ('archive', 'rollback-trash'):
                continue
            if here.is_symlink():
                raise RelayError('本地仓含符号链接，拒绝合并：' + str(here))
            kept_dirs.append(d)
        dirs[:] = kept_dirs
        for f in files:
            src = root / f
            if f.endswith('.lock'):
                continue
            if src.is_symlink():
                raise RelayError('本地仓含符号链接，拒绝合并：' + str(src))
            rel = src.relative_to(local_dir)
            dest = shadow / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(src.read_bytes())
    return shadow


def _land_files(local_dir, merged_files, backup_dir=None):
    """Atomic-ish landing with full rollback on failure.

    Every replaced file is first backed up; on any error the backups are
    written back and new files removed, so the store keeps its pre-merge
    bytes. Returns the backup dir (caller removes on success). When
    backup_dir is given, the backup lives under the store's
    archive/rollback-trash/ so a later post-land failure or process crash
    still leaves the pre-merge bytes available for recovery."""
    local_dir = Path(local_dir)
    if backup_dir is not None:
        backup = Path(backup_dir)
        backup.mkdir(parents=True, exist_ok=True)
    else:
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


def _restore_land_backup(local_dir, merged_files, backup):
    """Best-effort rollback of a landed merge from its persistent backup."""
    local_dir = Path(local_dir)
    backup = Path(backup)
    for rel in sorted(merged_files):
        snap = backup / rel
        live = local_dir / rel
        if snap.exists():
            atomic(live, snap.read_bytes())
        else:
            try:
                live.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass


def write_bundle_zip(bundle_dir, zip_path):
    """Pack a bundle directory into a zip with identical internal layout.

    The archive is written to a sibling temp file and published with
    os.replace only after every entry is written.  A failed pack therefore
    never leaves a partial target zip behind -- export_zip refuses to
    overwrite an existing target, so a partial file would otherwise be a
    permanent obstacle until removed by hand.
    """
    bundle = Path(bundle_dir)
    zip_path = Path(zip_path)
    require((bundle / 'manifest.json').exists(), '非导出包目录：' + str(bundle))
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=zip_path.name + '.',
                                    suffix='.tmp', dir=str(zip_path.parent))
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        with zipfile.ZipFile(tmp_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(bundle.rglob('*')):
                if path.is_file():
                    zf.write(path, path.relative_to(bundle).as_posix())
        os.replace(tmp_path, zip_path)
    except Exception:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise
    return zip_path


def read_bundle_zip(zip_path, dest_dir):
    """Unpack a bundle zip, rejecting absolute/escaping/symlink entries.

    Batch 7 (manual folder carrier): every entry is validated BEFORE
    anything is written -- the old code checked-and-wrote one entry at
    a time, so a bad entry late in the list left earlier entries on
    disk. Directory entries are skipped (our writer never emits them);
    duplicate names are refused.

    Extraction is staged in a sibling temp directory and published with
    os.replace only after every entry has been read and written.  A
    corrupt/truncated entry late in the zip therefore cannot leave a
    partial destination behind.
    """
    dest = Path(dest_dir)
    require(not dest.exists() or not any(dest.iterdir()), '解包目标目录非空：' + str(dest))
    dest.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.relay-unzip-', dir=str(dest.parent)))
    try:
        with zipfile.ZipFile(zip_path) as zf:
            infos = [info for info in zf.infolist()
                     if not info.is_dir() and not info.filename.endswith('/')]
            require(len(infos) <= MAX_BUNDLE_ENTRIES,
                    '包内条目过多（%d > %d），拒绝解包'
                    % (len(infos), MAX_BUNDLE_ENTRIES))
            names = [info.filename for info in infos]
            require(len(set(names)) == len(names), '包内有重名条目，拒绝解包')
            declared_total = 0
            for info in infos:
                _jail_check(info.filename)
                if (info.external_attr >> 16) & 0o170000 == 0o120000:
                    raise RelayError('包内含符号链接条目：' + info.filename)
                require(info.file_size <= MAX_BUNDLE_FILE_BYTES,
                        '包内单文件过大（%s），拒绝解包：%s'
                        % (info.file_size, info.filename))
                declared_total += info.file_size
                require(declared_total <= MAX_BUNDLE_TOTAL_BYTES,
                        '包内解压总量过大（> %d 字节），拒绝解包'
                        % MAX_BUNDLE_TOTAL_BYTES)
                if info.compress_size > 0 and info.file_size > 0:
                    ratio = info.file_size / info.compress_size
                    require(ratio <= MAX_BUNDLE_COMPRESSION_RATIO,
                            '包内条目压缩比异常（%.0f:1），拒绝解包：%s'
                            % (ratio, info.filename))
            written_total = 0
            for info in infos:
                # Read one byte past the per-file cap instead of zf.read(info):
                # the central-directory size can lie, and an unbounded read
                # would allocate the whole decompressed bomb in memory first.
                with zf.open(info) as src:
                    data = src.read(MAX_BUNDLE_FILE_BYTES + 1)
                require(len(data) <= MAX_BUNDLE_FILE_BYTES,
                        '包内单文件实际过大，拒绝解包：' + info.filename)
                written_total += len(data)
                require(written_total <= MAX_BUNDLE_TOTAL_BYTES,
                        '包内解压总量实际过大，拒绝解包')
                target = staging / info.filename
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
        if dest.exists():
            try:
                dest.rmdir()
            except OSError:
                raise RelayError('解包目标目录非空：' + str(dest))
        os.replace(staging, dest)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return dest


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
    # Validate the manifest's identity fields BEFORE import_bundle can
    # init a missing store or land files; these used to be read only after
    # merging, so a malformed bundle could leave a half-initialized store.
    require(isinstance(manifest.get('source_device'), str)
            and manifest['source_device'].strip(),
            'manifest 缺少 source_device，拒绝导入')
    require(isinstance(manifest.get('source_device_id'), str),
            'manifest 的 source_device_id 不是字符串，拒绝导入')
    require(isinstance(manifest.get('exported_at'), str)
            and manifest['exported_at'].strip(),
            'manifest 缺少 exported_at，拒绝导入')
    projects = manifest.get('projects')
    require(isinstance(projects, list) and len(projects) == 1,
            'manifest 的 projects 必须是单项目列表，拒绝导入')
    require(isinstance(projects[0], dict)
            and isinstance(projects[0].get('slug'), str)
            and projects[0]['slug'].strip(),
            'manifest 的项目缺少 slug，拒绝导入')
    slug = projects[0]['slug']
    require(_PROJECT_SLUG_RE.match(slug) is not None and slug not in ('.', '..'),
            'manifest 的项目名非法：%r' % (slug,))
    prefix = 'projects/%s/' % slug
    listed = manifest.get('files')
    require(isinstance(listed, dict) and listed, 'manifest 缺少文件清单，拒绝导入')
    for rel in listed:
        _jail_check(rel)
        require(rel.startswith(prefix), '清单含其它项目文件，拒绝导入：' + rel)
        require(isinstance(listed[rel], str)
                and re.fullmatch(r'[0-9a-f]{64}', listed[rel]) is not None,
                '清单哈希格式非法，拒绝导入：' + rel)
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


def _merge_goal(local_dir, incoming_goal, source_info, incoming_receipts):
    """S6 goal rule on v1 files: NEVER auto-merge.

    Returns (action, merged_bytes_or_None, pending_info_or_None, conflict).
    adopt: local has no goal (missing/skeleton/blank) -> take incoming and
      mark it imported-pending-local-confirm. skip: bytes identical and
      locally confirmed. mark-pending: bytes identical but locally
      unconfirmed -> record provenance without touching the file.
      conflict: bytes differ -> keep local, report, flag for re-confirm."""
    skeleton = relay._skeleton_bytes()
    local_path = local_dir / 'goal.md'
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
        'file': 'goal.md', 'rule': 'goal-never-auto-merge',
        'local_hash': digest(local_goal), 'incoming_hash': digest(incoming_goal),
        'note': '两边目标不同，保留本地版本，需按 S4 重新确认'}


# ---------------------------------------------------------------- export quality gate
# Batch 9 (2026-11-11 DSH takeover audit): the first real zip handoff
# shipped with a baseline commit that no longer resolved locally, two
# captures whose semantic sections were still placeholders, and backlog
# blocks whose Owner lines had been truncated by the summarize regex.
# The receiving harness had to rediscover all three by hand. The export
# side now inspects what it is about to ship and records the gaps in the
# manifest; --strict turns block-level gaps into a refusal.
QUALITY_GAP_SEMANTIC = 'semantic_pending'
QUALITY_GAP_BASELINE = 'baseline_unreachable'
QUALITY_GAP_BACKLOG = 'backlog_field_truncated'
QUALITY_GAP_GOAL_RECEIPT = 'goal_receipt_mismatch'
QUALITY_BLOCK = 'block'
QUALITY_WARN = 'warn'
SEMANTIC_PENDING_MARKER = '<!-- relay:semantic pending -->'
BASELINE_LINE_RE = re.compile(r'当前分支@基线：[^@\s]+@([0-9a-fA-F]{7,40})')


def _quality_gap(code, severity, message, files):
    return {'code': code, 'severity': severity,
            'message': message, 'files': list(files)}


def _export_quality_gaps(toplevel, payload):
    """Read-only export-side quality gate (batch 9, v3 paths).

    Inspects the exact payload about to be written into a bundle and
    returns a list of {code, severity, message, files}. Never raises:
    export_bundle records every gap and only strict refuses a bundle
    when a block-severity gap is present. Checks are local-only -- no
    network -- so an offline export still works.
    """
    gaps = []
    # 1. capture entries whose semantic section is still the placeholder.
    raw_rels = sorted(rel for rel in payload if _is_raw_entry_rel(rel))
    pending = []
    for rel in raw_rels:
        try:
            _header, body = _parse_entry_text(payload[rel].decode('utf-8'))
        except Exception:
            continue
        if SEMANTIC_PENDING_MARKER in body:
            pending.append(rel)
    if pending:
        latest = raw_rels[-1]
        severity = QUALITY_BLOCK if latest in pending else QUALITY_WARN
        names = '、'.join(p.rsplit('/', 1)[-1] for p in pending[:5])
        gaps.append(_quality_gap(
            QUALITY_GAP_SEMANTIC, severity,
            '%d 条采集语义未填%s：%s' % (
                len(pending),
                '（含最新条目）' if latest in pending else '（仅历史条目）',
                names),
            pending))
    # 2. a state baseline commit must resolve in the code repo. v3 keeps
    # the baseline line in the outer state.md or an agent memory.md when
    # one was recorded; absent everywhere is not a gap.
    for rel in ['state.md'] + sorted(
            r for r in payload if r.startswith('agents/')
            and r.endswith('/memory.md')):
        state = payload.get(rel)
        if state is None:
            continue
        match = BASELINE_LINE_RE.search(state.decode('utf-8'))
        if match:
            sha = match.group(1)
            try:
                check = relay.git(toplevel, 'cat-file', '-e',
                                  sha + '^{commit}', check=False)
                reachable = check.returncode == 0
            except Exception:
                reachable = True  # git unavailable: not evidence of a gap
            if not reachable:
                gaps.append(_quality_gap(
                    QUALITY_GAP_BASELINE, QUALITY_WARN,
                    '%s 的基线 %s 在代码仓本地不可解析（可能未推送或已被重写）'
                    % (rel, sha[:12]),
                    [rel]))
    # 3. backlog blocks must keep their machine-readable fields (outer
    # backlog + every agent's backlog).
    backlog_rels = ['backlog.md'] + sorted(
        r for r in payload if r.startswith('agents/')
        and r.endswith('/backlog.md'))
    for rel in backlog_rels:
        backlog = payload.get(rel)
        if backlog is None:
            continue
        # Normalize CRLF: a store written on Windows may hand us CRLF
        # even though the entry parser normalizes cap files. Without
        # this the block regex silently misses every line.
        text = backlog.decode('utf-8').replace('\r\n', '\n')
        for match in re.finditer(r'^## (T-\d+)[ \t]*\n(.*?)(?=\n## |\Z)',
                                 text, re.S | re.M):
            tid, block = match.group(1), match.group(2)
            if not re.search(r'^- 验证方式：', block, re.M):
                gaps.append(_quality_gap(
                    QUALITY_GAP_BACKLOG, QUALITY_WARN,
                    '%s（%s）缺少「- 验证方式：」行' % (tid, rel),
                    [rel]))
                continue
            for line in block.splitlines():
                stripped = line.strip()
                if not stripped.startswith('- Owner：'):
                    continue
                match = re.search(r'验证：(.*)$', stripped)
                if not match:
                    continue
                verify_text = match.group(1).strip()
                # summarize()'s regex captures up to the first whitespace,
                # so a truncated field has no sentence-ending punctuation
                # (T-001 '在', T-002 'claude', T-005 '远端只剩'). A field
                # that ends cleanly is the summarize format working as
                # designed and must not be reported as a gap.
                if verify_text and verify_text[-1] not in '。！？.!?':
                    gaps.append(_quality_gap(
                        QUALITY_GAP_BACKLOG, QUALITY_WARN,
                        '%s（%s）的 Owner 行验证字段被截断：%s'
                        % (tid, rel, stripped[:60]),
                        [rel]))
    # 4. goal receipt must still certify the goal bytes.
    goal = payload.get('goal.md')
    if goal is not None:
        goal_sha = _sha256(goal)
        for rel in sorted(payload):
            if not (rel.startswith('receipts/goal-')
                    and rel.endswith('.json')):
                continue
            try:
                receipt = json.loads(payload[rel].decode('utf-8'))
            except (ValueError, UnicodeDecodeError):
                continue
            recorded = receipt.get('goal_hash') if isinstance(receipt, dict) else None
            if recorded and recorded != goal_sha:
                gaps.append(_quality_gap(
                    QUALITY_GAP_GOAL_RECEIPT, QUALITY_WARN,
                    '%s 记录的 goal_hash 与 goal.md 不一致'
                    % rel.rsplit('/', 1)[-1],
                    [rel, 'goal.md']))
    return gaps


def _zip_device(device_name=None, device_id=None):
    """Device identity for a manual zip export.

    v3 has no sync config: an explicit name/id wins, otherwise the
    hostname (name) and '' (id), like the old pack_folder fallback.
    """
    return device_name or socket.gethostname(), device_id or ''


def export_bundle(store_dir, dest_dir, device_name, device_id, strict=False):
    """Build a 2.0 export bundle directory for one project.

    dest_dir is created fresh (an existing non-empty directory is
    refused, so a stale bundle can never be mistaken for a new one).
    Local files repaired by the rescan are written back only after every
    quality gate has passed (see _fix_local_redactions), so a strict-mode
    rejection leaves the local archive untouched. Returns the manifest dict.
    """
    dest = Path(dest_dir)
    require(not dest.exists() or not any(dest.iterdir()), '导出目标目录非空：' + str(dest))
    store_dir = Path(store_dir)
    slug = relay.slug_for(store_dir.parent.name)
    payload = _collect_payload(store_dir)
    redacted, hits = _redact_payload(payload)
    quality_gaps = _export_quality_gaps(store_dir.parent, redacted)
    if strict:
        blocked = [g for g in quality_gaps
                   if g.get('severity') == QUALITY_BLOCK]
        if blocked:
            raise RelayError(
                '导出前置检查未通过（%d 项阻断）：%s'
                % (len(blocked), '；'.join(g['message'] for g in blocked)))
    # Create the destination only after every preflight gate has passed:
    # a strict refusal must not leave an empty directory behind.
    dest.mkdir(parents=True, exist_ok=True)
    manifest_files = {}
    entry_count = 0
    proj_dir = dest / 'projects' / slug
    for rel in sorted(redacted):
        target = proj_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(redacted[rel])
        manifest_files['projects/%s/%s' % (slug, rel)] = _sha256(redacted[rel])
    for rel in sorted(redacted):
        if not _is_raw_entry_rel(rel):
            continue
        _parse_entry_text(redacted[rel].decode('utf-8'))
        entry_count += 1
    manifest = {
        'format': BUNDLE_FORMAT, 'format_major': FORMAT_MAJOR,
        'format_minor': FORMAT_MINOR, 'exported_at': utc(),
        'source_device': device_name, 'source_device_id': device_id,
        'tool_version': TOOL_VERSION, 'entry_count': entry_count,
        'redacted_count': hits, 'files': manifest_files,
        'projects': [{'slug': slug,
                      'display_name': store_dir.parent.name,
                      'files': sorted('projects/%s/%s' % (slug, r)
                                      for r in redacted)}],
        # Encryption reservation (plan 2.4.6, slice 2): whole-bundle
        # encryption itself is deferred, but the fields are fixed now.
        # 'none' is the only value produced today; a future encrypted
        # bundle must set encryption to its scheme name and
        # encryption_salt to the derivation salt (format reserved).
        'encryption': 'none', 'encryption_salt': None,
        # Optional field: old importers ignore unknown manifest fields,
        # so a bundle with gaps still imports on either side. The export
        # side is where the gaps become visible before anyone ships it.
        'quality_gaps': quality_gaps,
    }
    # All gates passed: only now repair the local copies, then refresh the
    # checksum manifest. A strict rejection above leaves local bytes and
    # the old manifest exactly as they were.
    if _fix_local_redactions(store_dir, payload, redacted):
        relay.update_checksum_manifest(store_dir)
    save(dest / 'manifest.json', manifest)
    return manifest


def export_zip(toplevel, zip_path, device_name=None, device_id=None,
               strict=False):
    """Manual zip carrier, export half: build a 2.0 bundle for the project
    at toplevel and pack it into a zip file.

    The zip holds exactly the bundle directory (manifest + project
    files); device-local config and keys never enter the bundle --
    export_bundle only ships _is_payload_file files out of the project
    store dir. An existing target zip is refused, never silently
    overwritten. Returns a summary dict.
    """
    toplevel = Path(toplevel)
    store_dir = relay.resolve_store_dir(toplevel)
    zip_path = Path(zip_path)
    require(not zip_path.exists(), '目标 zip 已存在，不覆盖：' + str(zip_path))
    name, did = _zip_device(device_name, device_id)
    # Hold the archive-wide lock across read-redact-gate-writeback-pack so
    # a concurrent capture/summarize cannot change bytes between the
    # redaction snapshot and the local writeback.
    with relay.outer_lock(store_dir):
        # Re-check under the lock: another export process can create the
        # target after the fast pre-check above but before we acquire.
        require(not zip_path.exists(), '目标 zip 已存在，不覆盖：' + str(zip_path))
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='relay-export-zip-') as tmp:
            bundle_dir = Path(tmp) / 'bundle'
            manifest = export_bundle(store_dir, bundle_dir, name, did,
                                     strict=strict)
            write_bundle_zip(bundle_dir, zip_path)
    return {'zip': str(zip_path), 'source_device': name,
            'source_device_id': did,
            'entry_count': manifest['entry_count'],
            'file_count': len(manifest['files']),
            'redacted_count': manifest['redacted_count'],
            'quality_gaps': manifest.get('quality_gaps', [])}


def import_zip(toplevel, zip_path, confirm=False):
    """Manual zip carrier, import half: unzip and run the full
    import_bundle pipeline (manifest + hash verification, redact
    re-scan, merge into the local project).

    Dry-run (confirm=False) writes NOTHING persistent: the zip is
    extracted to a throwaway temp dir and import_bundle itself is
    pure without confirm. Returns {'landed', 'report', 'zip'}.
    """
    toplevel = Path(toplevel)
    zip_path = Path(zip_path)
    require(zip_path.is_file(), 'zip 不存在：' + str(zip_path))
    with tempfile.TemporaryDirectory(prefix='relay-import-zip-') as tmp:
        bundle_dir = read_bundle_zip(zip_path, Path(tmp) / 'bundle')
        report = import_bundle(bundle_dir, toplevel, confirm=confirm)
    return {'landed': confirm, 'report': report, 'zip': str(zip_path)}


# ---------------------------------------------------------------- entries (v3 layout)
def _parse_local_entries_all(store_dir):
    """All local entries in file order, across agents. A damaged local
    entry is fatal: merging must never silently drop local data.

    This is the enumeration to use: after a same-key conflict merge, two
    files can share one merge key, and the dict version below collapses
    them (last wins). Each entry carries its owning 'agent' (the
    directory it lives in -- authoritative for merge placement).
    """
    out = []
    agents_dir = Path(store_dir) / 'agents'
    if not agents_dir.is_dir():
        return out
    for agent in sorted(d.name for d in agents_dir.iterdir() if d.is_dir()):
        raw = agents_dir / agent / 'raw'
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
                        'header': header, 'path': path, 'agent': agent})
    return out


# ---------------------------------------------------------------- merge helpers (v3)
_FACT_HEADING_RE = re.compile(r'^## 事实\s*$')
_FACT_LINE_RE = re.compile(r'^- (.+?)：(.*)$')
_CHANGES_HEADING_RE = re.compile(r'^## 汇总 (\d+)\s*$')
_AGENT_GOAL_HASH_RE = re.compile(r'^总目标哈希：([0-9a-f]{64})\s*$', re.M)
# 四家现役都有自己的一套文件，没有零文件 Agent。保留这个空元组以兼容
# 现有调用点（判断 a in _FILELESS_AGENTS 恒为假）。
_FILELESS_AGENTS = ()


def _recorded_goal_hash(text):
    """Total-goal hash stamped on an agent goal.md's first line, or None."""
    m = _AGENT_GOAL_HASH_RE.search(text or '')
    return m.group(1) if m else None


def _split_facts_section(text):
    """Split text into (pre, facts_lines, post). facts_lines is None when
    the text has no ## 事实 section. pre includes the heading line."""
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if _FACT_HEADING_RE.match(line):
            start = i
            break
    if start is None:
        return text, None, ''
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].startswith('## '):
            end = i
            break
    pre = '\n'.join(lines[:start + 1])
    facts = lines[start + 1:end]
    post = '\n'.join(lines[end:])
    return pre, facts, post


def _parse_fact_lines(fact_lines):
    """Parse '- key：value' lines: (facts dict, key order, other lines)."""
    facts = {}
    order = []
    rest = []
    for line in fact_lines:
        m = _FACT_LINE_RE.match(line)
        if m:
            key = m.group(1).strip()
            if key not in facts:
                order.append(key)
            facts[key] = m.group(2).strip()
        else:
            rest.append(line)
    return facts, order, rest


def _merge_facts_section(local_text, incoming_text):
    """Merge the ## 事实 section of two memory.md texts by key.

    Same key + same value: keep. Same key + different value: incoming
    wins, noted (the peer saw something newer; the conflict stays
    visible in the merge log, not silently dropped). Keys only on one
    side are kept. Returns (new_text, notes).
    """
    local_pre, local_facts, local_post = _split_facts_section(local_text)
    _inc_pre, inc_facts, _inc_post = _split_facts_section(incoming_text)
    if local_facts is None and inc_facts is None:
        return local_text, []
    if local_facts is None:
        inc_dict, inc_order, _ = _parse_fact_lines(inc_facts)
        if not inc_dict:
            return local_text, []
        block = ['## 事实'] + ['- %s：%s' % (k, inc_dict[k]) for k in inc_order]
        sep = '' if (not local_text or local_text.endswith('\n')) else '\n'
        return local_text + sep + '\n'.join(block) + '\n', []
    local_dict, local_order, local_rest = _parse_fact_lines(local_facts)
    inc_dict, inc_order, _ = _parse_fact_lines(inc_facts or [])
    notes = []
    merged = dict(local_dict)
    order = list(local_order)
    for key in inc_order:
        if key not in merged:
            merged[key] = inc_dict[key]
            order.append(key)
        elif merged[key] != inc_dict[key]:
            notes.append('事实 %s 两边不同，采纳对方值（本地：%s；对方：%s）'
                         % (key, merged[key], inc_dict[key]))
            merged[key] = inc_dict[key]
    lines = ['- %s：%s' % (k, merged[k]) for k in order] + local_rest
    new_text = local_pre + '\n' + '\n'.join(lines)
    if local_post:
        new_text += '\n' + local_post
    if not new_text.endswith('\n'):
        new_text += '\n'
    return new_text, notes


def _merge_changes_sections(local_text, incoming_text):
    """Union ## 汇总 N sections of two changes.md texts by number.

    Same number + same body: skip. Same number + different body: keep
    local, note it (the two sides summarized the same slot
    differently; both summaries stay readable in their own raw
    entries). Returns (new_text, added, notes).
    """
    def split(text):
        sections = {}
        order = []
        preamble = []
        cur = None
        for line in text.splitlines():
            m = _CHANGES_HEADING_RE.match(line)
            if m:
                cur = int(m.group(1))
                sections[cur] = []
                order.append(cur)
            elif cur is not None:
                sections[cur].append(line)
            else:
                preamble.append(line)
        return preamble, sections, order
    local_pre, local_secs, local_order = split(local_text)
    _inc_pre, inc_secs, inc_order = split(incoming_text)
    added = 0
    notes = []
    merged = {n: list(lines) for n, lines in local_secs.items()}
    order = list(local_order)
    for num in inc_order:
        if num not in merged:
            merged[num] = list(inc_secs[num])
            order.append(num)
            added += 1
        else:
            local_body = '\n'.join(merged[num]).strip()
            inc_body = '\n'.join(inc_secs[num]).strip()
            if local_body != inc_body:
                notes.append('汇总 %d 两边正文不同，保留本地版本' % num)
    if added == 0:
        return local_text, 0, notes
    out = list(local_pre)
    for num in sorted(order):
        out.append('## 汇总 %d' % num)
        out.extend(merged[num])
    new_text = '\n'.join(out)
    if not new_text.endswith('\n'):
        new_text += '\n'
    return new_text, added, notes


def _merge_log_lines(local_text, incoming_text):
    """Union two log.jsonl texts by identical line.

    Lines are JSON records; the union is sorted by the 'at' field for a
    deterministic file. Returns (new_text, added).
    """
    local_lines = [l for l in local_text.splitlines() if l.strip()]
    incoming_lines = [l for l in incoming_text.splitlines() if l.strip()]
    seen = set(local_lines)
    added = [l for l in incoming_lines if l not in seen]
    if not added:
        return local_text, 0

    def _key(line):
        try:
            return json.loads(line).get('at', '')
        except ValueError:
            return ''
    all_lines = local_lines + added
    all_lines.sort(key=lambda l: (_key(l), l))
    return '\n'.join(all_lines) + '\n', len(added)


def _is_template_outer(rel, local_bytes):
    """True when the local outer file is still template/blank bytes.

    The template files may not exist yet (init then writes empty
    files); in that case blank is the template. When a template
    exists, compare bytes exactly.
    """
    if not local_bytes or not local_bytes.strip():
        return True
    src = relay.TEMPLATES_DIR / rel
    try:
        if src.is_file():
            return src.read_bytes() == local_bytes
    except OSError:
        pass
    return False


def _is_session_card_rel(rel):
    parts = rel.split('/')
    return (len(parts) == 4 and parts[0] == 'agents'
            and parts[2] == 'sessions' and parts[3].startswith('card-')
            and parts[3].endswith('.md'))


def _parse_session_card_bytes(data):
    text = data.decode('utf-8')
    require(BODY_DELIM in text, '会话卡格式损坏')
    head, body = text.split(BODY_DELIM, 1)
    header = json.loads(head)
    require(isinstance(header, dict) and header.get('version') == 1,
            '会话卡版本不受支持')
    require(isinstance(header.get('seq'), int) and header['seq'] > 0,
            '会话卡序号无效')
    require(isinstance(header.get('agent'), str) and header['agent'],
            '会话卡 Agent 缺失')
    require(isinstance(header.get('session'), str) and header['session'].strip(),
            '会话卡 session 缺失')
    require(header.get('stage') in ('session-end', 'milestone'),
            '会话卡阶段无效')
    require(body.strip(), '会话卡正文为空')
    cap = header.get('char_cap', 3000)
    require(isinstance(cap, int) and cap > 0 and len(body) <= cap + 40,
            '会话卡超过声明长度上限')
    return header, body


def _is_handled(rel):
    """True when merge_project has a rule for this incoming path.

    Anything not handled is reported as a note, never silently
    dropped -- a future version's new file must show up in the log.
    """
    if _is_raw_entry_rel(rel):
        return True
    if rel in ('goal.md', 'state.md', 'backlog.md', 'decisions.md',
               'stack.md', 'outlook.md', 'index.md', 'project.json'):
        return True
    if rel.startswith('receipts/'):
        return True
    if rel.startswith('state/goal-tentative.') \
            or rel.startswith('state/goal-draft.'):
        return True
    if rel.startswith('agents/'):
        parts = rel.split('/')
        if len(parts) < 3:
            return False
        sub = '/'.join(parts[2:])
        if sub in ('memory.md', 'decisions.md', 'lessons.md',
                   'backlog.md', 'changes.md', 'goal.md', 'log.jsonl'):
            return True
        if sub == 'state/last-summary.json' \
                or sub.startswith('state/summaries/'):
            return True
        if _is_session_card_rel(rel):
            return True
        return False
    return False


def _project_id_from_bytes(data):
    """Return project_id only from a JSON object; damaged identity is ignored."""
    try:
        value = json.loads(data.decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    project_id = value.get('project_id')
    return project_id if isinstance(project_id, str) else None


def merge_project(local_dir, incoming_files, source_info, fresh_store=False):
    """Pure merge preview AND apply input (no writes).

    local_dir is the live project store dir (read-only here).
    incoming_files maps project-relative posix paths to
    already-verified, already-redacted bytes. Returns a result dict;
    merged_files holds only changed/added files (land writes exactly
    these). Cursor and baseline are never in the merged set (they are
    local measurement state, not shipped: see _is_payload_file).

    v3 rules, per area:
    - raw entries: union by merge key, renumbered per agent (the
      incoming entry lands in its own agent's raw/ with that agent's
      next seq; same key + different body keeps both, conflicted).
    - decisions/lessons/backlog: per-agent + outer shared files, merged
      by ## ID block (backlog conflicts recorded).
    - memory.md: managed block via _merge_state_md, ## 事实 section by
      key (incoming wins on conflict, noted).
    - outer goal.md: never auto-merged (_merge_goal). Each agent's
      goal.md: the version whose stamped total-goal hash matches the
      merged outer goal wins, else local is kept.
    - outer state.md: never adopted (resume recomputes it). index.md:
      never adopted (re-rendered from directories on land).
    - stack.md/outlook.md: local template/blank adopts incoming, else
      local is kept and the divergence noted.
    - changes.md: union by ## 汇总 N. log.jsonl: union by line.
    - per-agent state/last-summary.json: higher seq wins;
      state/summaries/*.json: content-hash union, renumber on collision.
    - receipts/: filename union, rename on collision.
    - outer state/goal-tentative + goal-draft: adopted only when
      consistent / absent (same rule as v2).
    """
    local_dir = Path(local_dir)
    conflicts = []
    notes = []
    discarded = []
    quarantined = {}
    merged = {}
    counts = {'entries_added': 0, 'entries_skipped': 0, 'entries_conflicted': 0,
              'blocks_added': 0, 'blocks_skipped': 0, 'files_added': 0,
              'log_lines_added': 0}
    local_entries_list = _parse_local_entries_all(local_dir)
    local_by_key = {}
    for e in local_entries_list:
        local_by_key.setdefault(_entry_key(e['header']), []).append(e)
    max_seq = {}
    for e in local_entries_list:
        max_seq[e['agent']] = max(max_seq.get(e['agent'], 0), e['seq'])

    def next_seq(agent):
        max_seq[agent] = max_seq.get(agent, 0) + 1
        return max_seq[agent]

    # ---- raw entries (append-type, union by merge key, per agent)
    incoming_entries = []
    for rel in sorted(incoming_files):
        if not _is_raw_entry_rel(rel):
            continue
        agent = rel.split('/')[1]
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
        incoming_entries.append((rel, agent, header, body))
    # Source seq order, NOT fact-hash order: renumbering must preserve the
    # source device's chronology, so an empty local store ends up with
    # the same filenames (a previous sort by merge key swapped two
    # entries' filenames nondeterministically across runs).
    incoming_entries.sort(key=lambda t: t[2].get('seq', 0))
    for rel, agent, header, body in incoming_entries:
        key = _entry_key(header)
        existing_list = local_by_key.get(key)
        if existing_list is not None and \
                any(e['body'] == body for e in existing_list):
            counts['entries_skipped'] += 1
            continue
        seq = next_seq(agent)
        new_header = dict(header)
        new_header['seq'] = seq
        new_header['agent'] = agent
        new_header['queue'] = _map_queue(header.get('queue', 'pending'))
        if existing_list is not None:
            new_header['merge_note'] = (
                '与本地同键条目内容不同，双方保留（来源：%s，源序号 %s）'
                % (source_info.get('device'), header.get('seq')))
            counts['entries_conflicted'] += 1
            conflicts.append({'file': 'agents/%s/raw/cap-%04d.md' % (agent, seq),
                              'rule': 'entry-same-key-different-body',
                              'local_seq': existing_list[0]['seq'],
                              'incoming_seq': header.get('seq')})
        else:
            counts['entries_added'] += 1
        merged['agents/%s/raw/cap-%04d.md' % (agent, seq)] = (
            json.dumps(new_header, ensure_ascii=False, sort_keys=True, indent=2)
            + BODY_DELIM + body).encode('utf-8')

    def local_text(rel):
        p = local_dir / rel
        return p.read_text(encoding='utf-8') if p.exists() else ''

    def local_bytes(rel):
        p = local_dir / rel
        return p.read_bytes() if p.exists() else b''

    # ---- project identity: keep the local ID; adopt only when absent
    if 'project.json' in incoming_files:
        local_project = local_bytes('project.json')
        local_id = _project_id_from_bytes(local_project) if local_project else None
        incoming_id = _project_id_from_bytes(incoming_files['project.json'])
        if not local_project:
            merged['project.json'] = incoming_files['project.json']
        elif incoming_id is None:
            notes.append('包内 project.json 不是有效身份对象，忽略')
        elif fresh_store:
            # relay.init just made a placeholder identity for this import;
            # adopt the archive's identity instead of minting a new one.
            merged['project.json'] = incoming_files['project.json']
        elif local_id and incoming_id and local_id != incoming_id:
            notes.append('项目身份证 ID 与本地不同，保留本地 project.json')
        elif incoming_files['project.json'] != local_project:
            notes.append('项目身份证内容不同，保留本地 project.json')

    # ---- outer goal first (per-agent goal alignment needs its hash)
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
    if 'goal.md' in incoming_files:
        goal_action, goal_bytes, pending_info, goal_conflict = _merge_goal(
            local_dir, incoming_files['goal.md'], source_info,
            incoming_receipts)
        if goal_action == 'adopt':
            merged['goal.md'] = goal_bytes
        if goal_conflict is not None:
            conflicts.append(goal_conflict)
    if 'goal.md' in merged:
        effective_goal = merged['goal.md']
    elif (local_dir / 'goal.md').exists():
        effective_goal = (local_dir / 'goal.md').read_bytes()
    else:
        effective_goal = None
    current_total_hash = digest(effective_goal) if effective_goal else None

    # ---- agents present on either side
    incoming_agents = set()
    for rel in incoming_files:
        if rel.startswith('agents/'):
            parts = rel.split('/')
            if len(parts) > 1:
                incoming_agents.add(parts[1])
    agents = sorted(incoming_agents | set(relay.list_agents(local_dir)))

    # ---- per-agent managed files
    for agent in agents:
        adir = 'agents/%s' % agent
        for rel, prefix in ((adir + '/decisions.md', 'D'),
                            (adir + '/lessons.md', 'L')):
            if rel not in incoming_files:
                continue
            new_text, added, skipped = _merge_id_blocks(
                local_text(rel), incoming_files[rel].decode('utf-8'), prefix)
            if new_text != local_text(rel):
                merged[rel] = new_text.encode('utf-8')
            counts['blocks_added'] += added
            counts['blocks_skipped'] += skipped
        if adir + '/backlog.md' in incoming_files:
            new_text, added, skipped, bl_conflicts = _merge_backlog(
                local_text(adir + '/backlog.md'),
                incoming_files[adir + '/backlog.md'].decode('utf-8'))
            if new_text != local_text(adir + '/backlog.md'):
                merged[adir + '/backlog.md'] = new_text.encode('utf-8')
            counts['blocks_added'] += added
            counts['blocks_skipped'] += skipped
            for c in bl_conflicts:
                c = dict(c, file=adir + '/backlog.md',
                         rule='backlog-same-item-different-body')
                conflicts.append(c)
        if adir + '/memory.md' in incoming_files:
            inc_mem = incoming_files[adir + '/memory.md'].decode('utf-8')
            new_text, st_conflicts, st_notes = _merge_state_md(
                local_text(adir + '/memory.md'), inc_mem,
                filename=adir + '/memory.md')
            new_text, fact_notes = _merge_facts_section(new_text, inc_mem)
            if new_text != local_text(adir + '/memory.md'):
                merged[adir + '/memory.md'] = new_text.encode('utf-8')
            conflicts.extend(st_conflicts)
            notes.extend(st_notes)
            notes.extend(fact_notes)
        if adir + '/changes.md' in incoming_files:
            new_text, added, ch_notes = _merge_changes_sections(
                local_text(adir + '/changes.md'),
                incoming_files[adir + '/changes.md'].decode('utf-8'))
            if new_text != local_text(adir + '/changes.md'):
                merged[adir + '/changes.md'] = new_text.encode('utf-8')
            counts['blocks_added'] += added
            notes.extend(ch_notes)
        if adir + '/log.jsonl' in incoming_files:
            new_text, added = _merge_log_lines(
                local_text(adir + '/log.jsonl'),
                incoming_files[adir + '/log.jsonl'].decode('utf-8'))
            if new_text != local_text(adir + '/log.jsonl'):
                merged[adir + '/log.jsonl'] = new_text.encode('utf-8')
            counts['log_lines_added'] += added
        # per-agent goal: the version stamped with the current total-goal
        # hash wins; anything else keeps the local file.
        if adir + '/goal.md' in incoming_files:
            inc_text = incoming_files[adir + '/goal.md'].decode('utf-8')
            inc_hash = _recorded_goal_hash(inc_text)
            loc_text = local_text(adir + '/goal.md')
            if not loc_text:
                merged[adir + '/goal.md'] = incoming_files[adir + '/goal.md']
                if inc_hash != current_total_hash:
                    notes.append('%s 的目标已采纳（本地原来没有），但它记录的'
                                 '总目标哈希与当前总目标不符，待该 Agent 下次'
                                 '写目标时对齐' % agent)
            else:
                loc_hash = _recorded_goal_hash(loc_text)
                if inc_hash == current_total_hash \
                        and loc_hash != current_total_hash:
                    merged[adir + '/goal.md'] = incoming_files[adir + '/goal.md']
                    notes.append('%s 的目标已采纳对方版（与当前总目标对齐）'
                                 % agent)
                elif inc_hash == current_total_hash \
                        and loc_hash == current_total_hash:
                    if inc_text != loc_text:
                        notes.append('%s 的目标正文两边不同（都对齐当前总目标），'
                                     '保留本地版本' % agent)
                else:
                    notes.append('%s 的目标未对齐当前总目标，保留本地版本'
                                 % agent)
        # Session cards: content-hash union. Never apply memory's
        # incoming-wins rule; same-event different bodies remain visible.
        card_prefix = adir + '/sessions/'
        local_card_hashes = set()
        local_card_events = {}
        used_card_nums = set()
        card_dir = local_dir / card_prefix
        if card_dir.is_dir():
            for path in card_dir.glob('card-*.md'):
                data = path.read_bytes()
                local_card_hashes.add(_sha256(data))
                try:
                    used_card_nums.add(int(path.stem.split('-')[-1]))
                    header, body = _parse_session_card_bytes(data)
                    event = header.get('event_id') or (
                        'legacy', header.get('session'), header.get('stage'))
                    local_card_events.setdefault(event, []).append(path)
                except (ValueError, UnicodeDecodeError, RelayError):
                    pass
        next_card_num = (max(used_card_nums) + 1) if used_card_nums else 1
        accepted_card_events = {}
        for rel in sorted(incoming_files):
            if not _is_session_card_rel(rel) or not rel.startswith(card_prefix):
                continue
            data = incoming_files[rel]
            if _sha256(data) in local_card_hashes:
                continue
            try:
                header, body = _parse_session_card_bytes(data)
                require(header['agent'] == agent, '会话卡 Agent 与目录不符')
            except (ValueError, UnicodeDecodeError, RelayError) as exc:
                discarded.append({'file': rel, 'reason': 'malformed-session-card',
                                  'detail': str(exc)})
                quarantined[rel] = data
                continue
            seq = header['seq']
            if seq in used_card_nums:
                while next_card_num in used_card_nums:
                    next_card_num += 1
                seq = next_card_num
            used_card_nums.add(seq)
            next_card_num = max(next_card_num, seq + 1)
            new_rel = '%scard-%04d.md' % (card_prefix, seq)
            event = header.get('event_id') or (
                'legacy', header.get('session'), header.get('stage'))
            prior = [str(p.relative_to(local_dir)).replace('\\', '/')
                     for p in local_card_events.get(event, [])]
            prior += accepted_card_events.get(event, [])
            if prior:
                conflicts.append({'file': new_rel,
                                  'rule': 'session-card-same-event-different-body',
                                  'event_id': header.get('event_id'),
                                  'local': prior,
                                  'incoming': rel})
            if seq != header['seq']:
                header = dict(header, seq=seq)
                lines = body.splitlines(True)
                if lines and lines[0].startswith('# 会话卡 card-'):
                    lines[0] = '# 会话卡 card-%04d\n' % seq
                    body = ''.join(lines)
                data = (json.dumps(header, ensure_ascii=False, sort_keys=True,
                                   indent=2) + BODY_DELIM + body).encode('utf-8')
            merged[new_rel] = data
            accepted_card_events.setdefault(event, []).append(new_rel)
            local_card_hashes.add(_sha256(data))
            counts['files_added'] += 1

        # per-agent state: last-summary (higher seq wins) + summaries
        # (content-hash union, renumber on collision).
        sum_prefix = adir + '/state/summaries/'
        local_sum_hashes = {}
        used_nums = set()
        sum_dir = local_dir / sum_prefix
        if sum_dir.is_dir():
            for p in sum_dir.glob('*.json'):
                local_sum_hashes[_sha256(p.read_bytes())] = p.name
                try:
                    used_nums.add(int(p.stem))
                except ValueError:
                    pass
        next_num = (max(used_nums) + 1) if used_nums else 1
        for rel in sorted(incoming_files):
            if not rel.startswith(sum_prefix) or not rel.endswith('.json'):
                continue
            data = incoming_files[rel]
            if _sha256(data) in local_sum_hashes:
                continue
            while ('%04d.json' % next_num) in used_nums:
                next_num += 1
            name = '%04d.json' % next_num
            used_nums.add(next_num)
            next_num += 1
            merged[sum_prefix + name] = data
            counts['files_added'] += 1
        if adir + '/state/last-summary.json' in incoming_files:
            loc_path = local_dir / (adir + '/state/last-summary.json')
            if not loc_path.exists():
                merged[adir + '/state/last-summary.json'] = \
                    incoming_files[adir + '/state/last-summary.json']
            else:
                try:
                    loc_seq = json.loads(
                        loc_path.read_text(encoding='utf-8')).get('seq', 0)
                    inc_seq = json.loads(
                        incoming_files[adir + '/state/last-summary.json']
                        .decode('utf-8')).get('seq', 0)
                except (ValueError, UnicodeDecodeError):
                    loc_seq, inc_seq = 0, 0
                if inc_seq > loc_seq:
                    merged[adir + '/state/last-summary.json'] = \
                        incoming_files[adir + '/state/last-summary.json']

    # ---- outer shared files (decisions + backlog by ID block)
    if 'decisions.md' in incoming_files:
        new_text, added, skipped = _merge_id_blocks(
            local_text('decisions.md'),
            incoming_files['decisions.md'].decode('utf-8'), 'D')
        if new_text != local_text('decisions.md'):
            merged['decisions.md'] = new_text.encode('utf-8')
        counts['blocks_added'] += added
        counts['blocks_skipped'] += skipped
    if 'backlog.md' in incoming_files:
        new_text, added, skipped, bl_conflicts = _merge_backlog(
            local_text('backlog.md'),
            incoming_files['backlog.md'].decode('utf-8'))
        if new_text != local_text('backlog.md'):
            merged['backlog.md'] = new_text.encode('utf-8')
        counts['blocks_added'] += added
        counts['blocks_skipped'] += skipped
        for c in bl_conflicts:
            c = dict(c, file='backlog.md',
                     rule='backlog-same-item-different-body')
            conflicts.append(c)
    # ---- outer state.md + index.md are derived, never adopted
    if 'state.md' in incoming_files:
        notes.append('忽略对方外层 state.md（本机 resume 现算，不采纳）')
    if 'index.md' in incoming_files:
        if incoming_files['index.md'] != local_bytes('index.md'):
            notes.append('忽略对方指路表（本机按目录重算）')
    # ---- stack.md / outlook.md: template/blank adopts incoming, else keep local
    for rel in ('stack.md', 'outlook.md'):
        if rel not in incoming_files:
            continue
        inc = incoming_files[rel]
        loc = local_bytes(rel)
        if inc == loc:
            continue
        if _is_template_outer(rel, loc):
            merged[rel] = inc
        else:
            notes.append('%s 两边不同，保留本地版本（分歧待人工对齐）' % rel)
    # ---- tentative / draft state (adopt only when consistent / absent)
    if goal_action == 'adopt' and 'state/goal-tentative.json' in incoming_files:
        try:
            tent = json.loads(incoming_files['state/goal-tentative.json'].decode('utf-8'))
            if tent.get('content_hash') == digest(merged['goal.md']):
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
    # ---- receipts (content-hash union, rename on collision)
    taken = set()
    base_dir = local_dir / 'receipts'
    local_hashes = {}
    if base_dir.is_dir():
        taken = {p.name for p in base_dir.glob('*') if p.is_file()}
        for p in base_dir.glob('*'):
            if p.is_file():
                local_hashes[_sha256(p.read_bytes())] = p.name
    for rel in sorted(incoming_files):
        if not rel.startswith('receipts/'):
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
        merged['receipts/' + name] = data
        counts['files_added'] += 1
    # ---- anything else in the payload is local-only or unknown: report it
    for rel in sorted(incoming_files):
        if not _is_handled(rel):
            notes.append('忽略未纳入合并的文件：' + rel)
    return {'merged_files': merged, 'conflicts': conflicts, 'counts': counts,
            'goal_action': goal_action, 'pending_info': pending_info,
            'discarded': discarded, 'quarantined': quarantined, 'notes': notes}


# ---------------------------------------------------------------- health
def health_check(store_dir):
    """Pre-merge health gate (plan 2.5 step 2), v3 layout.

    Problems abort the operation; warnings do not. "Unrecovered
    transaction" means the LATEST tx log of an agent shows partial
    application: some files still at before-bytes while others already
    left before-bytes. A merely-present log from a completed summarize
    is healthy (summarize never deletes its tx log, it doubles as
    undo), and later evolution away from both before and after bytes
    is normal (new summarizes, merges) -- it only means rollback is no
    longer clean, which recover() already refuses on its own.
    """
    store_dir = Path(store_dir)
    problems = []
    warnings = []
    for name in ('goal.md', 'state.md', 'backlog.md', 'decisions.md',
                 'stack.md', 'outlook.md', 'index.md'):
        p = store_dir / name
        if not p.exists():
            problems.append('档案缺失：' + name)
            continue
        try:
            text = p.read_text(encoding='utf-8')
        except (UnicodeDecodeError, OSError) as exc:
            problems.append('档案不可读：%s（%s）' % (name, exc))
            continue
        if _has_markers(text):
            problems.append('档案含冲突标记：' + name)
    state_text = (store_dir / 'state.md').read_text(encoding='utf-8') \
        if (store_dir / 'state.md').exists() else ''
    if STATE_BEGIN in state_text and STATE_END not in state_text:
        problems.append('state.md 受管块缺结束标记')
    if STATE_BEGIN not in state_text:
        warnings.append('state.md 无受管块（下次 resume 将重算）')
    try:
        all_entries = _parse_local_entries_all(store_dir)
    except RelayError as exc:
        problems.append('原料条目损坏：' + str(exc))
        all_entries = []
    entry_seqs = {}
    for e in all_entries:
        entry_seqs.setdefault(e['agent'], set()).add(e['seq'])
    for agent in relay.list_agents(store_dir):
        if agent in _FILELESS_AGENTS:
            continue
        adir = store_dir / 'agents' / agent
        for name in ('memory.md', 'decisions.md', 'lessons.md',
                     'backlog.md', 'changes.md', 'goal.md'):
            p = adir / name
            if not p.exists():
                problems.append('档案缺失：agents/%s/%s' % (agent, name))
                continue
            try:
                text = p.read_text(encoding='utf-8')
            except (UnicodeDecodeError, OSError) as exc:
                problems.append('档案不可读：agents/%s/%s（%s）'
                                % (agent, name, exc))
                continue
            if _has_markers(text):
                problems.append('档案含冲突标记：agents/%s/%s' % (agent, name))
        # Transaction gate: only the LATEST tx log matters (vol-1
        # recover() itself only looks at the latest: a log means "this
        # summarize can still be rolled back", not "a transaction is in
        # progress"). File names in the log are relative to the agent
        # dir (archive == adir in v3 flow).
        tx_dir = adir / 'tx'
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
                    problems.append('事务日志无法解析：agents/%s/tx/%s（%s）'
                                    % (agent, path.name, exc))
                    files = {}
                at_before = []
                left_before = []
                for name, rec in files.items():
                    current = (adir / name).read_bytes() \
                        if (adir / name).exists() else b''
                    try:
                        before = base64.b64decode(rec['before_b64'])
                    except Exception:
                        problems.append('事务日志损坏：agents/%s/tx/%s'
                                        % (agent, path.name))
                        break
                    # A file the transaction intentionally left unchanged
                    # has after_sha == digest(before): current == before
                    # is its CORRECT applied state, not an unapplied write.
                    if current == before and digest(current) != rec['after_sha']:
                        at_before.append(name)
                    elif current != before:
                        left_before.append(name)
                if at_before and left_before:
                    problems.append('未恢复事务：agents/%s/tx/%s 部分应用'
                                    '（仍在事务前：%s；已离开事务前：%s）'
                                    % (agent, path.name,
                                       '、'.join(at_before),
                                       '、'.join(left_before)))
        cursor_path = adir / 'state' / 'cursor.json'
        if cursor_path.exists():
            try:
                cursor = json.loads(cursor_path.read_text(encoding='utf-8'))
                seq = cursor.get('entry_seq', 0)
                if seq and seq not in entry_seqs.get(agent, set()):
                    problems.append('游标指向不存在的条目：agents/%s/cap-%04d'
                                    % (agent, seq))
            except (ValueError, UnicodeDecodeError) as exc:
                problems.append('游标文件无法解析：agents/%s/state/cursor.json'
                                '（%s）' % (agent, exc))
    receipts = store_dir / 'receipts'
    if receipts.is_dir():
        for path in receipts.glob('goal-*.json'):
            try:
                json.loads(path.read_text(encoding='utf-8'))
            except (ValueError, UnicodeDecodeError):
                problems.append('收据无法解析：receipts/' + path.name)
    return problems, warnings


# ---------------------------------------------------------------- shadow land
def _merge_log_path(store_dir):
    """Next receipts/merge-<timestamp>.json (dedup-suffixed on clash)."""
    d = Path(store_dir) / 'receipts'
    d.mkdir(parents=True, exist_ok=True)
    base = 'merge-%s' % _compact_ts(utc())
    path = d / (base + '.json')
    n = 2
    while path.exists():
        path = d / ('%s-%d.json' % (base, n))
        n += 1
    return path


def _latest_merge_log(store_dir):
    """Latest receipts/merge-*.json by mtime, or (None, None)."""
    receipts = Path(store_dir) / 'receipts'
    if not receipts.is_dir():
        return None, None
    candidates = []
    for path in receipts.glob('merge-*.json'):
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


def _record_resolution(store_dir, record):
    """Append a resolution to the latest merge-log, or a resolve receipt
    when no merge-log exists. Returns the path written."""
    path, log = _latest_merge_log(store_dir)
    if path is not None and log is not None:
        log.setdefault('resolutions', []).append(record)
        save(path, log)
        return str(path)
    receipt = Path(store_dir) / 'receipts' / ('resolve-%s.json' % _compact_ts(utc()))
    save(receipt, {'at': utc(), 'kind': 'resolve',
                   'resolutions': [record]})
    return str(receipt)


def _land_project(store_dir, merge_result, source_info, marker_files=None,
                  quarantined=None):
    """Shadow-check, land, then write flags + merge-log.

    marker_files: incoming files excluded for conflict markers are
    preserved under the merge-log's conflicted/ folder (both versions
    retained, never silently dropped). quarantined (batch 2, B2): bytes
    of entries merge discarded (malformed / id-hash-mismatch / non-UTF-8)
    are preserved under the merge-log's quarantined/ folder -- outside
    raw/, outside the payload, never counted -- so a discard is
    inspectable and repairable instead of silently gone.
    """
    store_dir = Path(store_dir)
    shadow = _shadow_copy(store_dir)
    for rel, data in merge_result['merged_files'].items():
        dest = shadow / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    problems, _warnings = health_check(shadow)
    if problems:
        raise RelayError('影子合并健康检查失败，正式仓未改动（影子目录保留供排查：%s）：%s'
                         % (shadow, '；'.join(problems)))
    backup_dir = store_dir / relay.ROLLBACK_TRASH_PREFIX / ('land-' + uuid.uuid4().hex)
    backup = None
    success = False
    log_path = None
    # Derived state files written after the payload lands: remember their
    # prior bytes so a later failure can restore them together with the
    # payload instead of leaving a half-applied import behind.
    flag_paths = [store_dir / 'state' / 'goal-imported.json',
                  store_dir / 'state' / 'goal-conflict.json']
    flag_before = {p: (p.read_bytes() if p.exists() else None) for p in flag_paths}
    try:
        backup = _land_files(store_dir, merge_result['merged_files'],
                             backup_dir=backup_dir)
        # A merge may have added a new agent dir: re-render the index from
        # the directories actually present, then refresh the integrity
        # manifest so the next takeover checks post-merge bytes.
        relay.write_index(store_dir)
        relay.update_checksum_manifest(store_dir)
        if merge_result.get('pending_info') is not None:
            save(store_dir / 'state' / 'goal-imported.json',
                 merge_result['pending_info'])
        if any(c.get('file') == 'goal.md' for c in merge_result['conflicts']):
            save(store_dir / 'state' / 'goal-conflict.json',
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
        log_path = _merge_log_path(store_dir)
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
        success = True
    except Exception:
        # Post-land failure: put every replaced/added file back, restore
        # derived state files, drop a partial merge log, and refresh the
        # index/manifest. Keep the backup on disk for inspection, then
        # re-raise the original error.
        if backup is not None:
            try:
                _restore_land_backup(store_dir, merge_result['merged_files'],
                                     backup)
            except Exception:
                pass
            for path, prior in flag_before.items():
                try:
                    if prior is None:
                        if path.exists():
                            path.unlink()
                    else:
                        atomic(path, prior)
                except OSError:
                    pass
            if log_path is not None:
                try:
                    if log_path.exists():
                        log_path.unlink()
                    shutil.rmtree(Path(str(log_path)[:-5]),
                                  ignore_errors=True)
                except OSError:
                    pass
            try:
                relay.write_index(store_dir)
                relay.update_checksum_manifest(store_dir)
            except Exception:
                pass
        raise
    finally:
        if success:
            shutil.rmtree(Path(backup), ignore_errors=True)
        shutil.rmtree(shadow, ignore_errors=True)
    return {'changed_files': sorted(merge_result['merged_files']),
            'conflicts': merge_result['conflicts'],
            'counts': merge_result['counts'],
            'goal_action': merge_result['goal_action'],
            'merge_log': str(log_path),
            'quarantined_files': quarantined_files}


# ---------------------------------------------------------------- import
def import_bundle(bundle_dir, toplevel, confirm=False):
    """Import a verified bundle (plan 2.1 steps 3-5), v3.

    Dry-run (confirm=False) writes NOTHING: merge_project is pure, so
    the report is a faithful preview. On confirm the single project is
    merged transactionally into the local archive (a missing local
    archive is created by relay.init first, then merged) and a
    merge-log receipt is written. There is no unclaimed landing and no
    registry: a zip is always for this project.
    """
    manifest, files, warnings = verify_bundle(bundle_dir)
    projects = manifest.get('projects', [])
    require(len(projects) == 1,
            'v3 导出包只含一个项目，包内有 %d 个' % len(projects))
    proj = projects[0]
    slug = proj.get('slug') or ''
    require(isinstance(slug, str) and _PROJECT_SLUG_RE.match(slug)
            and slug not in ('.', '..'),
            '包内项目名非法：%r' % (slug,))
    toplevel = Path(toplevel)
    store_dir = relay.store_dir_for(toplevel)
    fresh_store = not store_dir.is_dir()
    if store_dir.is_dir():
        store_dir = relay.resolve_store_dir(toplevel)
    elif confirm:
        relay.init(toplevel)
        store_dir = relay.resolve_store_dir(toplevel)
    # else: keep the (nonexistent) store path; merge_project is pure and
    # previews against an empty archive without writing anything.
    # The preview is pure, but landing must be serialized with every other
    # archive writer (capture/summarize/export). Take the archive-wide lock
    # for the whole merge+land transaction on confirm; a dry-run takes no
    # lock and creates no lock file, preserving its zero-write promise.
    lock = relay.outer_lock(store_dir) if confirm else nullcontext()
    with lock:
        prefix = 'projects/%s/' % slug
        payload = {rel[len(prefix):]: data for rel, data in files.items()
                   if rel.startswith(prefix)}
        files_in, hits, marker_files, redacted_discarded, redacted_quarantined = \
            _redact_incoming(payload)
        source_info = {'device': manifest['source_device'],
                       'device_id': manifest['source_device_id'], 'seq': None}
        merge_result = merge_project(store_dir, files_in, source_info,
                                     fresh_store=fresh_store)
        merge_result['discarded'].extend(redacted_discarded)
        merge_result['quarantined'].update(redacted_quarantined)
        entry = {'slug': slug, 'display_name': proj.get('display_name'),
                 'status': 'matched', 'redacted_count': hits,
                 'marker_files': sorted(marker_files),
                 'discarded': list(merge_result['discarded']),
                 'quarantined': sorted(merge_result['quarantined']),
                 'counts': merge_result['counts'],
                 'conflicts': merge_result['conflicts'],
                 'goal_action': merge_result['goal_action'],
                 'notes': merge_result['notes'],
                 'changed_files': sorted(merge_result['merged_files'])}
        merge_logs = []
        if confirm:
            landed = _land_project(store_dir, merge_result, source_info,
                                   marker_files=marker_files,
                                   quarantined=merge_result['quarantined'])
            if fresh_store:
                # The import may have adopted an identity during merge;
                # refresh the locator row so the old placeholder ID is gone.
                project = relay.ensure_project_file(store_dir, toplevel)
                relay.register_project(toplevel, store_dir, project)
            entry['merge_log'] = landed['merge_log']
            entry['quarantined_files'] = landed['quarantined_files']
            merge_logs.append(landed['merge_log'])
    return {'landed': confirm, 'exported_at': manifest['exported_at'],
            'source_device': manifest['source_device'],
            'source_device_id': manifest['source_device_id'],
            'warnings': warnings, 'projects': [entry],
            'merge_logs': merge_logs}


# ---------------------------------------------------------------- resolutions
def list_pending_resolutions(store_dir):
    """Pending adjudications for one project: goal-conflict.json, fork
    lines in agent memory.md files, and backlog conflicts in the latest
    merge-log that no resolution covers yet."""
    store_dir = Path(store_dir)
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
    for agent in relay.list_agents(store_dir):
        mem_path = store_dir / 'agents' / agent / 'memory.md'
        if not mem_path.exists():
            continue
        for line in mem_path.read_text(encoding='utf-8').splitlines():
            m = FORK_NOTE_RE.match(line)
            if m:
                pending.append({'kind': 'fork', 'agent': agent,
                                'key': m.group(1), 'incoming': m.group(2)})
    _path, log = _latest_merge_log(store_dir)
    if log is not None:
        resolved_backlog = {r.get('item') for r in log.get('resolutions', [])
                            if r.get('kind') == 'backlog'}
        for conflict in log.get('conflicts', []):
            if 'item' in conflict and conflict['item'] not in resolved_backlog:
                pending.append({'kind': 'backlog', 'item': conflict['item'],
                                'local': conflict.get('local'),
                                'incoming': conflict.get('incoming'),
                                'file': conflict.get('file')})
    return {'pending': pending}


def sync_resolve(toplevel, list_only=False, goal=None, fork=None,
                 backlog=None, decision=None, file=None, agent=None):
    """Adjudicate one pending conflict (plan 2.6 leftover), v3 paths.

    --list prints pending items without writing. Actions write the
    decision into the archive, clear the marker, refresh the checksum
    manifest (any archive write must, or the next resume reports a
    manifest mismatch), and record the resolution. Goal adopt-incoming
    needs --file: incoming goal bytes are deliberately NOT retained in
    the merge-log (goal is never auto-merged; only hashes are recorded),
    so the user supplies the text to adopt (e.g. copied from the other
    device); the adopted text is also staged as a draft so a normal
    `relay goal confirm` completes local confirmation.

    A fork key can live in several agents' memory.md files; when it
    does, --agent picks which one.
    """
    toplevel = Path(toplevel)
    store_dir = relay.resolve_store_dir(toplevel)
    if list_only:
        return list_pending_resolutions(store_dir)
    require(decision in ('keep-local', 'adopt-incoming'),
            '裁决须指定 --decision keep-local 或 adopt-incoming')
    device = socket.gethostname()
    actions = []
    touched = False
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
            atomic(store_dir / 'goal.md', raw)
            relay.goal_draft(toplevel, text)
            touched = True
            actions.append('目标已更新为 --file 内容并暂存为草案，'
                           '请运行 relay goal confirm 完成本机确认')
        else:
            actions.append('目标保留本地版本')
        conflict_path.unlink(missing_ok=True)
        record_path = _record_resolution(
            store_dir, {'at': utc(), 'kind': 'goal', 'decision': decision,
                        'device': device})
        actions.append('已清除 goal-conflict.json，裁决记入：' + record_path)
    elif fork is not None:
        holders = []
        for a in relay.list_agents(store_dir):
            mem_path = store_dir / 'agents' / a / 'memory.md'
            if not mem_path.exists():
                continue
            for line in mem_path.read_text(encoding='utf-8').splitlines():
                m = FORK_NOTE_RE.match(line)
                if m and m.group(1) == fork:
                    holders.append(a)
                    break
        require(holders, '无待裁分叉：%s' % fork)
        if agent is not None:
            require(agent in holders,
                    'Agent %s 的记忆里无待裁分叉：%s' % (agent, fork))
        else:
            require(len(holders) == 1,
                    '分叉 %s 出现在多个 Agent 的记忆里（%s），须指定 --agent 裁决'
                    % (fork, '、'.join(holders)))
            agent = holders[0]
        mem_path = store_dir / 'agents' / agent / 'memory.md'
        lines = mem_path.read_text(encoding='utf-8').splitlines()
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
        atomic(mem_path, ('\n'.join(kept) + '\n').encode('utf-8'))
        touched = True
        record_path = _record_resolution(
            store_dir, {'at': utc(), 'kind': 'fork', 'agent': agent,
                        'key': fork, 'decision': decision, 'device': device})
        actions.append('分叉 %s（%s）已裁决（%s），裁决记入：%s'
                       % (fork, agent, decision, record_path))
    elif backlog is not None:
        _path, log = _latest_merge_log(store_dir)
        require(log is not None, '本项目无合并日志，找不到待裁待办')
        conflict = None
        for candidate in log.get('conflicts', []):
            if candidate.get('item') == backlog:
                conflict = candidate
                break
        require(conflict is not None, '合并日志中无待裁待办：%s' % backlog)
        if decision == 'adopt-incoming':
            target_rel = conflict.get('file') or 'backlog.md'
            backlog_path = store_dir / target_rel
            text = backlog_path.read_text(encoding='utf-8') \
                if backlog_path.exists() else ''
            blocks, _pre = _parse_blocks(text, 'T')
            next_id = (max([b['id'] for b in blocks]) + 1) if blocks else 1
            out = text.rstrip('\n')
            out += '\n## T-%03d\n%s\n' % (next_id, (conflict.get('incoming') or '').rstrip('\n'))
            atomic(backlog_path, out.encode('utf-8'))
            touched = True
            actions.append('待办 %s 已采纳对方版，追加为 T%d（%s）'
                           % (backlog, next_id, target_rel))
        else:
            actions.append('待办 %s 保留本地版本' % backlog)
        record_path = _record_resolution(
            store_dir, {'at': utc(), 'kind': 'backlog', 'item': backlog,
                        'decision': decision, 'device': device})
        actions.append('裁决记入：' + record_path)
    else:
        raise RelayError('须指定裁决对象：--goal、--fork <键> 或 --backlog <事项>'
                         '（先用 --list 查看待裁项）')
    if touched:
        relay.update_checksum_manifest(store_dir)
    return {'actions': actions}
