#!/usr/bin/env python3
"""project-relay v2 promotion channel (plan part 03, step 3.2).

Scope: the narrow third lane beside the two automatic ones. Goals still go
through the S4 confirmation flow and ordinary archive stays fully automatic;
promotion covers ONLY rule-type, high-impact content, and only when a
countable trigger fires -- never because a model judged something
"important". No importance score exists anywhere in this module.

Triggers (all countable, plan 3.2.1):
  1. correction: the same error class was user-corrected >= 2 times
     (correction events carry a `correction_of` mark from the capture layer).
  2. lesson_repeat: the same rule-type lesson text appears in lessons.md
     under >= 3 distinct lesson entries (different events).
  3. formal_file: a record is queued for a file that constrains all future
     sessions (project instruction file, CI config, release script) -- this
     always promotes, regardless of count.

Disposal (plan 3.2.4): adopt / reject (a one-line reason is mandatory and
archived with the proposal) / shelve (auto-expires after 30 days, never
re-proposed without fresh evidence). Disposal is batched through
`relay promote decide` and rendered in a fixed section of the handoff and
resume outputs -- never a per-item popup interrupting a session.

Anti-repeat (plan 3.2.5, enforced in code, not by convention): rejecting a
proposal resets its evidence count. It may only be proposed again once
fresh evidence reaches 2x the original threshold, and the re-proposal must
carry the previous rejection reason plus a "what changed" paragraph.
Shelved-then-expired proposals follow the same rule.

Managed sections (plan 3.2.6): adopted content lands in the target file
inside the same marker format the recipe route already uses
(`<!-- relay:begin <marker> v1 hash:<sha> -->` ... `<!-- relay:end
<marker> -->`). Relay only ever touches its own marked span; the user (or
a later proposal) may revise or delete the whole span.

Write discipline: every store write goes through relay.atomic(). This
module never touches goal files or receipts (S4 owns those) and never
writes ordinary archive files except the adoption decision record that
plan 3.2.4 explicitly requires.
"""

import difflib
import importlib.util
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location('relay', _HERE / 'relay.py')
relay = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(relay)

RelayError = relay.RelayError
require = relay.require
utc = relay.utc
digest = relay.digest
atomic = relay.atomic
read_json = relay.read_json

# Entry-file format shared with relay_flow (header JSON + delimiter + body).
BODY_DELIM = '\n---relay-body---\n'

PROMOTE_DIR = 'promotion'
PROPOSALS_FILE = 'proposals.json'
INTENTS_FILE = 'intents.jsonl'

CORRECTION_THRESHOLD = 2
LESSON_THRESHOLD = 3
FORMAL_THRESHOLD = 1
REPEAT_MULTIPLIER = 2
SHELVE_DAYS = 30
DIFF_PREVIEW_MAX_LINES = 60
PROMOTE_VERSION = '1'

# Content kinds. Only 'rule' may be produced by the automatic detectors.
# 'preference' / 'goal' require a verbatim user quote with its session;
# content the assistant proposed and the user merely answered "好" to must
# be recorded as 'decision', never promoted as a user preference.
CONTENT_KINDS = ('rule', 'preference', 'goal', 'decision')
ORIGINS = ('rule', 'user_words', 'assistant_proposal_accepted')


# ---------------------------------------------------------------- time
def _coerce_now(now):
    """Normalize a time input (None / ISO string / datetime) to aware UTC."""
    if now is None:
        return datetime.now(timezone.utc)
    if isinstance(now, datetime):
        return now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    text = str(now).strip()
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


# ---------------------------------------------------------------- storage
def _promote_dir(store_dir):
    d = Path(store_dir) / PROMOTE_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load_proposals(store_dir):
    path = Path(store_dir) / PROMOTE_DIR / PROPOSALS_FILE
    if not path.exists():
        return {'version': 1, 'next_id': 1, 'proposals': []}
    doc = read_json(path)
    doc.setdefault('next_id', 1)
    doc.setdefault('proposals', [])
    return doc


def _save_proposals(store_dir, doc):
    d = _promote_dir(store_dir)
    atomic(d / PROPOSALS_FILE,
           json.dumps(doc, ensure_ascii=False, indent=2).encode('utf-8'))
    return doc


def _load_intents(store_dir):
    path = Path(store_dir) / PROMOTE_DIR / INTENTS_FILE
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def _append_intent(store_dir, intent):
    d = _promote_dir(store_dir)
    path = d / INTENTS_FILE
    existing = path.read_bytes() if path.exists() else b''
    atomic(path, existing + (json.dumps(intent, ensure_ascii=False) + '\n').encode('utf-8'))
    return intent


def _norm_content(text):
    return re.sub(r'\s+', ' ', str(text)).strip()


def _fingerprint(trigger, target, content):
    return digest(json.dumps({'trigger': trigger, 'target': str(target),
                              'content': _norm_content(content)},
                             sort_keys=True, ensure_ascii=False).encode('utf-8'))


# ---------------------------------------------------------------- evidence
def _list_raw_entries(store_dir):
    """Capture headers only (no relay_flow import: entry format is stable).

    Returns [{seq, correction_of, user_message, session, path}] sorted by seq.
    Unparseable files are skipped, never fatal: promotion is auxiliary to
    capture, and a corrupt raw file must not block the proposal scan.
    """
    raw = Path(store_dir) / 'raw'
    out = []
    if not raw.is_dir():
        return out
    for path in sorted(raw.glob('cap-*.md')):
        try:
            text = path.read_text(encoding='utf-8')
            header_text, _, _ = text.partition(BODY_DELIM)
            header = json.loads(header_text)
        except (ValueError, OSError):
            continue
        out.append({'seq': header.get('seq'), 'correction_of': header.get('correction_of'),
                    'user_message': header.get('user_message'),
                    'session': header.get('session'), 'path': path})
    out.sort(key=lambda e: e['seq'] if isinstance(e['seq'], int) else 0)
    return out


_LESSON_RE = re.compile(r'^## (L-\d+)\n- 内容：(.*)\n', re.M)


def _list_lessons(store_dir):
    """Parse archive/lessons.md into [{lid, content}] in file order."""
    path = Path(store_dir) / 'archive' / 'lessons.md'
    if not path.exists():
        return []
    text = path.read_text(encoding='utf-8')
    return [{'lid': m.group(1), 'content': m.group(2).strip()}
            for m in _LESSON_RE.finditer(text)]


def _current_count(store_dir, proposal):
    """Re-count the evidence behind a proposal (anti-repeat baseline)."""
    trigger = proposal['trigger']
    key = proposal['evidence_key']
    if trigger == 'correction':
        return sum(1 for e in _list_raw_entries(store_dir) if e['correction_of'] == key)
    if trigger == 'lesson_repeat':
        return sum(1 for le in _list_lessons(store_dir) if _norm_content(le['content']) == key)
    if trigger == 'formal_file':
        return sum(1 for it in _load_intents(store_dir) if it['fingerprint'] == key)
    return proposal.get('evidence_count', 0)


def _check_quote_discipline(content_kind, origin, quote, quote_session):
    """Plan 3.2.3, enforced: goal/preference proposals need verbatim user
    words with their session; assistant-proposed content the user merely
    answered "好" to may only be recorded as a decision, never promoted
    as a user preference."""
    require(content_kind in CONTENT_KINDS, '未知的提议内容类型：%r' % (content_kind,))
    require(origin in ORIGINS, '未知的提议来源：%r' % (origin,))
    if content_kind in ('preference', 'goal'):
        require(quote and str(quote).strip(), '目标/偏好类提议必须附用户原话')
        require(quote_session and str(quote_session).strip(),
                '目标/偏好类提议必须注明原话出处会话')
    if origin == 'user_words':
        require(quote and str(quote).strip(), '源自用户原话的提议必须逐字引用原话')
        require(quote_session and str(quote_session).strip(),
                '源自用户原话的提议必须注明出处会话')
    if origin == 'assistant_proposal_accepted' and content_kind != 'decision':
        raise RelayError('助理提议、用户只回“好”的内容只能记为决定类记录，'
                         '不得包装成用户偏好来晋升')


# ---------------------------------------------------------------- creation
def create_proposal(store_dir, *, trigger, content, target, evidence, consequence,
                    content_kind='rule', origin='rule', quote=None, quote_session=None,
                    threshold, evidence_key, replaces=None, intent_ids=None, now=None,
                    identity=None):
    """Create a pending proposal, or refuse it under the anti-repeat rules.

    Returns {'created': <id>} on success, or {'created': None, ...} with
    'existing' (a live proposal already covers it) or 'blocked' (anti-repeat:
    fresh evidence has not reached 2x the threshold yet). Refusals are
    returned, never raised: a blocked candidate is a normal outcome, not an
    error, and callers (capture/summarize hooks) must not fail over it.
    """
    require(str(content).strip(), '提议内容不能为空')
    require(str(target).strip(), '拟写入位置不能为空')
    _check_quote_discipline(content_kind, origin, quote, quote_session)
    now_dt = _coerce_now(now)
    # Identity is the stable unit anti-repeat reasons about. It defaults to
    # the content, but correction proposals are identified by their error
    # class: the proposed wording follows the latest correction message,
    # and keying identity on that wording would let every rewording dodge
    # the 2x rule (a rejected class would re-propose on the next event).
    fp = _fingerprint(trigger, target, identity if identity is not None else content)
    doc = _load_proposals(store_dir)
    proposals = doc['proposals']
    for p in proposals:
        if p['fingerprint'] == fp and p['status'] == 'pending':
            # Still pending: refresh its evidence list so the displayed
            # count tracks new events. Content stays as first proposed --
            # only evidence (the countable part) moves.
            p['evidence'] = [{'pointer': e['pointer'], 'summary': e['summary']}
                             for e in evidence]
            p['evidence_count'] = len(evidence)
            _save_proposals(store_dir, doc)
            return {'created': None, 'existing': p['id'],
                    'reason': '已有待处置的相同提议（证据已刷新）', 'updated': True}
        if p['fingerprint'] == fp and p['status'] == 'adopted':
            return {'created': None, 'existing': p['id'], 'reason': '相同内容已采纳生效'}
    blocked = None
    for p in sorted((x for x in proposals
                     if x['fingerprint'] == fp and x['status'] in ('rejected', 'expired')),
                    key=lambda x: (x.get('decided_at') or '', x['id'])):
        blocked = p
    previous_reason = None
    what_changed = None
    if blocked is not None:
        at_decision = blocked.get('evidence_count_at_decision', 0)
        fresh = len(evidence) - at_decision
        need = REPEAT_MULTIPLIER * blocked['threshold']
        if fresh < need:
            return {'created': None, 'blocked': blocked['id'], 'need': need,
                    'fresh': fresh, 'reason': '证据未翻倍，禁止重复提议'}
        previous_reason = blocked.get('reject_reason') or '上次搁置到期自动失效'
        new_items = evidence[at_decision:] if at_decision >= 0 else evidence
        what_changed = ('上次否决理由：%s。自上次否决后新增证据 %d 条（原阈值 %d，'
                        '再提需 ≥%d 条），新增事件：%s'
                        % (previous_reason, fresh, blocked['threshold'], need,
                           '；'.join(e['pointer'] for e in new_items) or '无'))
    pid = 'P-%04d' % doc['next_id']
    doc['next_id'] += 1
    proposal = {
        'id': pid, 'trigger': trigger, 'content_kind': content_kind, 'origin': origin,
        'content': str(content), 'target': str(target),
        'evidence': [{'pointer': e['pointer'], 'summary': e['summary']} for e in evidence],
        'evidence_count': len(evidence), 'threshold': threshold,
        'evidence_key': evidence_key, 'fingerprint': fp,
        'consequence': consequence, 'status': 'pending',
        'created_at': _iso(now_dt), 'decided_at': None,
        'reject_reason': None, 'shelved_until': None,
        'evidence_count_at_decision': None,
        'previous_reject_reason': previous_reason, 'what_changed': what_changed,
        'quote': quote, 'quote_session': quote_session,
        'replaces': replaces, 'intent_ids': intent_ids or [],
    }
    proposals.append(proposal)
    _save_proposals(store_dir, doc)
    return {'created': pid}


# ---------------------------------------------------------------- detectors
def _default_target(toplevel):
    return str(Path(toplevel) / 'AGENTS.md')


def detect_corrections_for(store_dir, toplevel, correction_of, now=None):
    """Correction trigger for one error class (capture hook entry point)."""
    entries = [e for e in _list_raw_entries(store_dir) if e['correction_of'] == correction_of]
    if len(entries) < CORRECTION_THRESHOLD:
        return {'proposed': [], 'count': len(entries), 'threshold': CORRECTION_THRESHOLD}
    latest = entries[-1]
    message = (latest['user_message'] or '').strip()
    content = message if message else '纠正：%s' % correction_of
    evidence = [{'pointer': 'raw/' + e['path'].name,
                 'summary': ((e['user_message'] or '').strip().splitlines() or [''])[0][:80]
                 or ('纠正：%s' % correction_of)}
                for e in entries]
    result = create_proposal(
        store_dir, trigger='correction', content=content,
        target=_default_target(toplevel), evidence=evidence,
        consequence='不采纳：%s 类错误可能重复出现，每次都要用户再纠正一次。' % correction_of,
        content_kind='rule', origin='user_words' if message else 'rule',
        quote=message or None, quote_session=latest['session'] if message else None,
        threshold=CORRECTION_THRESHOLD, evidence_key=correction_of, now=now,
        identity=correction_of)
    return {'proposed': [result['created']] if result['created'] else [],
            'count': len(entries), 'threshold': CORRECTION_THRESHOLD, 'detail': result}


def detect_corrections(store_dir, toplevel, now=None):
    """All correction classes at/over threshold (refresh entry point)."""
    classes = []
    for e in _list_raw_entries(store_dir):
        if e['correction_of'] and e['correction_of'] not in classes:
            classes.append(e['correction_of'])
    out = {'proposed': [], 'detail': {}}
    for cls in classes:
        r = detect_corrections_for(store_dir, toplevel, cls, now=now)
        out['detail'][cls] = r
        out['proposed'].extend(r['proposed'])
    return out


def detect_lessons(store_dir, toplevel, now=None):
    """Lesson-repeat trigger: same normalized lesson text under >= 3
    distinct lesson entries (different events)."""
    groups = {}
    for le in _list_lessons(store_dir):
        groups.setdefault(_norm_content(le['content']), []).append(le)
    out = {'proposed': [], 'detail': {}}
    for content_norm, items in groups.items():
        if len(items) < LESSON_THRESHOLD:
            continue
        content = items[0]['content']
        evidence = [{'pointer': 'archive/lessons.md#' + le['lid'],
                     'summary': le['content'][:80]} for le in items]
        result = create_proposal(
            store_dir, trigger='lesson_repeat', content=content,
            target=_default_target(toplevel), evidence=evidence,
            consequence='不采纳：该教训只留在档案里，不约束未来会话，同类问题可能重犯。',
            content_kind='rule', origin='rule',
            threshold=LESSON_THRESHOLD, evidence_key=content_norm, now=now)
        out['detail'][content_norm[:40]] = result
        if result['created']:
            out['proposed'].append(result['created'])
    return out


def record_formal_intent(store_dir, toplevel, target, content, source_pointer=None,
                         consequence=None, content_kind='rule', origin='rule',
                         quote=None, quote_session=None, now=None):
    """Queue a record destined for a constraining file (plan 3.2.1③).

    The intent itself is the evidence ledger: intents are never deleted, so
    post-rejection re-proposals can count fresh intents. Recording an
    intent immediately attempts a proposal (threshold 1: formal-file
    triggers always promote, regardless of count)."""
    _check_quote_discipline(content_kind, origin, quote, quote_session)
    require(str(content).strip(), '拟写入内容不能为空')
    resolved = Path(target) if Path(target).is_absolute() else Path(toplevel) / target
    fp = _fingerprint('formal_file', str(resolved), content)
    intents = _load_intents(store_dir)
    seq = (max((it['seq'] for it in intents), default=0) + 1)
    intent = {'seq': seq, 'at': _iso(_coerce_now(now)), 'target': str(resolved),
              'content': str(content), 'source_pointer': source_pointer,
              'consequence': consequence, 'content_kind': content_kind,
              'origin': origin, 'quote': quote, 'quote_session': quote_session,
              'fingerprint': fp}
    _append_intent(store_dir, intent)
    result = detect_formal_for(store_dir, fp, now=now)
    return {'intent_seq': seq, 'fingerprint': fp, 'proposal': result}


def detect_formal_for(store_dir, fingerprint, now=None):
    """Propose for one formal-file fingerprint from its intent ledger."""
    intents = [it for it in _load_intents(store_dir) if it['fingerprint'] == fingerprint]
    if not intents:
        return {'proposed': [], 'count': 0}
    latest = intents[-1]
    evidence = [{'pointer': latest['source_pointer'] or ('promote intent #%d' % it['seq']),
                 'summary': (it['content'].strip().splitlines() or [''])[0][:80]}
                for it in intents]
    result = create_proposal(
        store_dir, trigger='formal_file', content=latest['content'],
        target=latest['target'], evidence=evidence,
        consequence=latest['consequence'] or '不采纳：该内容不会写入正式文件，未来会话不受其约束。',
        content_kind=latest['content_kind'], origin=latest['origin'],
        quote=latest['quote'], quote_session=latest['quote_session'],
        threshold=FORMAL_THRESHOLD, evidence_key=fingerprint,
        intent_ids=[it['seq'] for it in intents], now=now)
    return {'proposed': [result['created']] if result['created'] else [],
            'count': len(intents), 'detail': result}


def detect_formal(store_dir, toplevel, now=None):
    """All formal-file fingerprints (refresh entry point)."""
    out = {'proposed': [], 'detail': {}}
    for fp in dict.fromkeys(it['fingerprint'] for it in _load_intents(store_dir)):
        r = detect_formal_for(store_dir, fp, now=now)
        out['detail'][fp[:12]] = r
        out['proposed'].extend(r['proposed'])
    return out


def expire_due(store_dir, now=None):
    """Materialize shelve expiry: shelved past 30 days becomes expired.

    The evidence baseline is re-counted at expiry so the 2x anti-repeat
    rule for shelved-expired proposals measures only post-expiry evidence.
    This is a write and must only run from write commands (decide/refresh),
    never from resume/handoff rendering."""
    now_dt = _coerce_now(now)
    doc = _load_proposals(store_dir)
    expired = []
    for p in doc['proposals']:
        if p['status'] != 'shelved' or not p.get('shelved_until'):
            continue
        if _coerce_now(p['shelved_until']) <= now_dt:
            p['status'] = 'expired'
            p['expired_at'] = _iso(now_dt)
            p['evidence_count_at_decision'] = _current_count(store_dir, p)
            expired.append(p['id'])
    if expired:
        _save_proposals(store_dir, doc)
    return {'expired': expired}


def refresh(store_dir, toplevel, now=None):
    """Write-command entry point: expire due shelved items, then run all
    detectors. Safe to call from capture/summarize hooks and the CLI."""
    out = {'expired': expire_due(store_dir, now=now)['expired'],
           'proposed': [], 'detail': {}}
    for name, result in (('correction', detect_corrections(store_dir, toplevel, now=now)),
                         ('lesson', detect_lessons(store_dir, toplevel, now=now)),
                         ('formal', detect_formal(store_dir, toplevel, now=now))):
        out['detail'][name] = result
        out['proposed'].extend(result['proposed'])
    return out



# ---------------------------------------------------------------- disposal
def list_pending(store_dir, now=None):
    """Pending proposals only, read-only (no expiry materialization).

    Shelved proposals past their deadline are excluded here -- they are
    due for expiry, which `expire_due` materializes on the next write
    command -- but they never reappear as pending on their own."""
    out = []
    for p in _load_proposals(store_dir)['proposals']:
        if p['status'] != 'pending':
            continue
        out.append(p)
    out.sort(key=lambda p: p['id'])
    return out


def _next_decision_id(decisions_text):
    ids = re.findall(r'D-(\d+)', decisions_text)
    return 'D-%03d' % ((max(int(i) for i in ids) + 1) if ids else 1)


def _section_text(marker, body):
    h = digest(body.encode('utf-8'))
    return ('<!-- relay:begin %s v%s hash:%s -->\n%s\n<!-- relay:end %s -->\n'
            % (marker, PROMOTE_VERSION, h, body, marker))


def _find_section(text, marker):
    begin = '<!-- relay:begin %s ' % marker
    end = '<!-- relay:end %s -->' % marker
    start = text.find(begin)
    if start == -1:
        return None
    stop = text.find(end, start)
    require(stop != -1, '受管小节 %s 缺少结束标记，拒绝改写' % marker)
    return (start, stop + len(end))


def _apply_section(text, marker, body):
    """Pure section splice (same marker format as the recipe route).

    Returns (new_text, changed). Only the marked span is ever touched;
    everything outside it is passed through byte-identical."""
    section = _section_text(marker, body)
    span = _find_section(text, marker)
    if span is None:
        if text.strip() == '':
            return section, True
        return text.rstrip('\n') + '\n\n' + section, True
    start, stop = span
    if text[start:stop].strip() == section.strip():
        return text, False
    return text[:start] + section + text[stop:], True


def _remove_section(text, marker):
    """Pure removal of one managed span; untouched text passes through."""
    span = _find_section(text, marker)
    if span is None:
        return text, False
    return text[:span[0]] + text[span[1]:], True


def _diff_preview(target_path, marker, body):
    """Unified diff between the target file now and after adoption."""
    old = target_path.read_text(encoding='utf-8') if target_path.exists() else ''
    new, changed = _apply_section(old, marker, body)
    if not changed:
        return '（受管小节已是最新，无变更）'
    lines = list(difflib.unified_diff(
        old.splitlines(), new.splitlines(),
        fromfile='a/' + target_path.name, tofile='b/' + target_path.name,
        lineterm=''))
    if len(lines) > DIFF_PREVIEW_MAX_LINES:
        total = len(lines)
        lines = lines[:DIFF_PREVIEW_MAX_LINES] + ['…（diff 预览已截断，共 %d 行）' % total]
    return '\n'.join(lines)


def decide(store_dir, proposal_id, action, reason=None, now=None):
    """Dispose of a pending proposal. Adopt writes the managed section and
    the decision record (source: 用户明确要求); reject requires a reason;
    shelve parks it for 30 days. Only pending proposals can be decided."""
    require(action in ('adopt', 'reject', 'shelve'), '未知的处置：%r' % (action,))
    now_dt = _coerce_now(now)
    doc = _load_proposals(store_dir)
    proposal = next((p for p in doc['proposals'] if p['id'] == proposal_id), None)
    require(proposal is not None, '找不到提议：%s' % proposal_id)
    require(proposal['status'] == 'pending',
            '提议 %s 已处置（%s），不可重复处置' % (proposal_id, proposal['status']))
    if action == 'reject':
        require(reason and str(reason).strip(), '否决必须填写一句理由')
        proposal['status'] = 'rejected'
        proposal['reject_reason'] = str(reason).strip()
        proposal['evidence_count_at_decision'] = _current_count(store_dir, proposal)
        proposal['decided_at'] = _iso(now_dt)
        _save_proposals(store_dir, doc)
        return {'id': proposal_id, 'action': 'rejected'}
    if action == 'shelve':
        proposal['status'] = 'shelved'
        proposal['shelved_until'] = _iso(now_dt + timedelta(days=SHELVE_DAYS))
        proposal['decided_at'] = _iso(now_dt)
        _save_proposals(store_dir, doc)
        return {'id': proposal_id, 'action': 'shelved',
                'shelved_until': proposal['shelved_until']}
    # Adopt: managed section first, decision record second. A later
    # proposal that revises this one declares `replaces` with the old id;
    # its old span is removed before the new span is inserted, so the
    # target never accumulates duplicate sections for one rule.
    marker = 'promote-' + proposal_id
    target_path = Path(proposal['target'])
    text = target_path.read_text(encoding='utf-8') if target_path.exists() else ''
    if proposal.get('replaces'):
        old_marker = 'promote-' + proposal['replaces']
        text, _ = _remove_section(text, old_marker)
    new_text, changed = _apply_section(text, marker, proposal['content'])
    if changed or not target_path.exists():
        target_path.parent.mkdir(parents=True, exist_ok=True)
        atomic(target_path, new_text.encode('utf-8'))
    archive = Path(store_dir) / 'archive'
    decisions_path = archive / 'decisions.md'
    decisions_text = decisions_path.read_text(encoding='utf-8') if decisions_path.exists() else ''
    did = _next_decision_id(decisions_text)
    first_line = (proposal['content'].strip().splitlines() or [''])[0]
    decisions_text += ('\n## %s\n- 选择：晋升采纳 %s：%s\n- 来源：用户明确要求\n'
                       '- 采集方式：晋升采纳\n- 目标文件：%s\n'
                       % (did, proposal_id, first_line, proposal['target']))
    atomic(decisions_path, decisions_text.encode('utf-8'))
    relay.update_checksum_manifest(store_dir)
    proposal['status'] = 'adopted'
    proposal['decided_at'] = _iso(now_dt)
    _save_proposals(store_dir, doc)
    return {'id': proposal_id, 'action': 'adopted', 'changed': changed,
            'target': proposal['target'], 'decision': did}


# ---------------------------------------------------------------- rendering
def render_proposal(proposal):
    """One page, four fixed parts, no importance score (plan 3.2.2)."""
    trigger_text = {
        'correction': '同一错误被用户纠正 ×%d（阈值 %d）' % (
            proposal['evidence_count'], proposal['threshold']),
        'lesson_repeat': '同一教训重复出现 ×%d（阈值 %d）' % (
            proposal['evidence_count'], proposal['threshold']),
        'formal_file': '将写入约束未来会话的正式文件（与次数无关）',
    }[proposal['trigger']]
    lines = ['【晋升提议 %s】触发：%s' % (proposal['id'], trigger_text)]
    lines.append('提议内容：')
    lines.append(proposal['content'])
    if proposal.get('quote'):
        lines.append('用户原话（%s）：%s' % (proposal['quote_session'], proposal['quote']))
    lines.append('证据链（%d 条）：' % len(proposal['evidence']))
    for e in proposal['evidence']:
        lines.append('- %s：%s' % (e['pointer'], e['summary']))
    if proposal.get('previous_reject_reason'):
        lines.append('再提说明：%s' % proposal['what_changed'])
    lines.append('拟写入位置：%s' % proposal['target'])
    lines.append('diff 预览：')
    lines.append(_diff_preview(Path(proposal['target']),
                               'promote-' + proposal['id'], proposal['content']))
    lines.append('不采纳的后果：%s' % proposal['consequence'])
    lines.append('处置：relay promote decide --id %s --adopt | '
                 '--reject --reason "一句话理由" | --shelve' % proposal['id'])
    return '\n'.join(lines)


def render_section(store_dir, now=None):
    """Fixed-position batch disposal block for handoff/resume outputs.

    Read-only: pending proposals are listed as stored, and shelved items
    past their deadline are simply not pending here (expiry materializes
    on the next write command via expire_due)."""
    pending = list_pending(store_dir, now=now)
    out = ['## 晋升提议（待处置）']
    if not pending:
        out.append('（无待处置的晋升提议）')
    else:
        out.extend(render_proposal(p) for p in pending)
    out.append('批量处置入口：relay promote list 查看全部；'
               'relay promote decide --id <编号> --adopt / '
               '--reject --reason "一句话理由" / --shelve（不逐条弹窗）')
    return '\n\n'.join(out)
