#!/usr/bin/env python3
"""project-relay v2 fork chain (plan part 03, step 3.3).

Volume 1 only made forks VISIBLE (foreign-tool entries listed at resume).
This slice completes the chain structure: every summary product carries a
based_on pointer, concurrent writes branch instead of overwriting, forks
are detected at every takeover read and before every summary write, and
disposition splits by field kind instead of picking one side.

Design notes (all load-bearing, do not "simplify" away):

- Chain unit is the SUMMARY record (state/summaries/%04d.json), not the
  individual decision/lesson/backlog line: a summary already groups one
  batch of appended items with its entry range, so based_on on the record
  covers the whole batch. Appended items keep their "汇总序号" line, which
  resolves each item to its summary and therefore to its chain position.
- Legacy records (written before this slice, no based_on key at all) are
  the pre-chain base, NOT wild forks: treating every old store as forked
  would flood long-running projects with false alarms. Only a record
  that CARRIES a based_on pointer to an unresolvable id is wild
  ("来源不明的一支").
- Branches are leaves: a branch never gains children in this design, so
  two branches off one parent trivially have no common descendant. The
  detection still implements the general descendant-disjointness rule,
  so summary-chain forks created by sync merges are judged the same way.
- Resume never writes: detection at takeover read is computed live and
  NOT persisted (the resume zero-write test pins this). The meta file
  state/forks.json is refreshed by summarize (a write command) and by
  the fork CLI only. The displayed list is therefore never staler than
  the last summarize, and resume itself shows a live computation.
- The pending-adjudication section in state.md lives OUTSIDE the managed
  STATE_BEGIN/END block, so summarize's block replacement can never wipe
  an undecided conflict. Adjudication is recorded twice: the pending
  line is rewritten to resolved, and state/fork-adjudications.json keeps
  the durable record.
- Goal freeze: a goal fork is a flagged state, not a merge. It is active
  when state/goal-fork.json exists (explicit 3.3 flag, e.g. after a
  multi-device goal divergence is observed) OR when the volume-2 sync
  goal-conflict marker state/goal-conflict.json exists. goal_inject
  freezes while either is present; the freeze lifts through the S4
  reconfirmation path (normal `relay goal confirm` clears the 3.3 flag;
  `relay sync-resolve --goal` clears the sync marker). This module never
  touches goal files or receipts itself (S4 owns those).
- Chain compression only folds summaries whose rollback path is
  self-contained: it runs only when there are no tx logs, or when the
  latest tx log carries before_state (post-A2). Otherwise a legacy
  recover derivation that reads old summary files could lose its
  previous-summary candidate. Folded records move into the package file
  (content unchanged, total hash over the concatenated record bytes);
  based_on pointers are NEVER rewritten -- the package index keeps them
  resolvable, and the anchor marker records where the live segment
  restarts.

Write discipline: every store write goes through relay.atomic()/save().
"""

import hashlib
import importlib.util
import json
import re
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
save = relay.save
read_json = relay.read_json

ARCHIVE_FILES = ('state.md', 'decisions.md', 'lessons.md', 'backlog.md')
BRANCHES_DIRNAME = 'branches'
FORKS_FILENAME = 'state/forks.json'
GOAL_FORK_FILENAME = 'state/goal-fork.json'
GOAL_CONFLICT_FILENAME = 'state/goal-conflict.json'
ADJUDICATIONS_FILENAME = 'state/fork-adjudications.json'
ANCHOR_FILENAME = 'state/chain-anchor.json'
CHAIN_ARCHIVES_DIRNAME = 'chain-archives'
PENDING_BEGIN = '<!-- relay:fork-pending-begin -->'
PENDING_END = '<!-- relay:fork-pending-end -->'
STATE_BEGIN = '<!-- relay:state-begin -->'
STATE_END = '<!-- relay:state-end -->'


def summary_id(seq):
    return 'sum-%04d' % seq


# ---------------------------------------------------------------- chain reading
def _package_records(store_dir):
    """Folded summary records, from chain-archive packages (content as
    stored; each record keeps its original seq and based_on)."""
    out = []
    d = store_dir / 'state' / CHAIN_ARCHIVES_DIRNAME
    if not d.is_dir():
        return out
    for p in sorted(d.glob('*.json')):
        try:
            pkg = read_json(p)
        except ValueError:
            continue
        for rec in pkg.get('records', []):
            if isinstance(rec, dict) and isinstance(rec.get('seq'), int):
                out.append(rec)
    return out


def list_summaries(store_dir):
    """All summary records: live files plus packaged (folded) ones,
    ordered by seq. Each entry: {id, seq, based_on, origin, branch,
    source, at, record, packaged}."""
    out = []
    d = store_dir / 'state' / 'summaries'
    if d.is_dir():
        for p in sorted(d.glob('*.json')):
            try:
                rec = read_json(p)
            except ValueError:
                continue
            if not isinstance(rec, dict) or not isinstance(rec.get('seq'), int):
                continue
            out.append(_summary_entry(rec, packaged=False))
    for rec in _package_records(store_dir):
        out.append(_summary_entry(rec, packaged=True))
    out.sort(key=lambda e: e['seq'])
    return out


def _summary_entry(rec, packaged):
    return {'id': summary_id(rec['seq']), 'seq': rec['seq'],
            'based_on': rec.get('based_on', None),
            'has_based_on': 'based_on' in rec,
            'origin': rec.get('origin'),
            'branch': rec.get('branch'),
            'source': rec.get('source'), 'at': rec.get('at'),
            'entry_range': rec.get('entry_range'),
            'rolled_back': bool(rec.get('rolled_back')),
            'record': rec, 'packaged': packaged}


def packaged_seqs(store_dir):
    """Seqs living in chain-archive packages (for _next_summary_seq:
    reusing a folded seq would collide with packaged history)."""
    return [rec['seq'] for rec in _package_records(store_dir)]


def latest_summary(store_dir):
    """Latest live summary (packaged records are history, never the
    chain head for a new based_on pointer)."""
    live = [e for e in list_summaries(store_dir) if not e['packaged']]
    return live[-1] if live else None


def chain_anchor(store_dir):
    p = store_dir / ANCHOR_FILENAME
    try:
        return read_json(p) if p.exists() else None
    except ValueError:
        return None


def list_branches(store_dir):
    """Branch records (state/branches/*.json), ordered by creation."""
    d = store_dir / 'state' / BRANCHES_DIRNAME
    out = []
    if not d.is_dir():
        return out
    for p in sorted(d.glob('*.json')):
        try:
            rec = read_json(p)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get('id'):
            out.append(rec)
    return out


# ---------------------------------------------------------------- detection
def detect_forks(store_dir, persist=False):
    """Fork detection (plan 3.3 item 2).

    Nodes are summary records plus branch records. A fork point is a
    parent with >= 2 children whose descendant sets are disjoint (no
    common descendant). Wild nodes carry a based_on pointer to an id no
    record or package resolves. Legacy records (no based_on key) are the
    pre-chain base, never wild. Goal-fork state is reported alongside
    (flag file or volume-2 sync goal-conflict marker)."""
    summaries = list_summaries(store_dir)
    branches = list_branches(store_dir)
    nodes = {}
    for s in summaries:
        nodes[s['id']] = {'id': s['id'], 'kind': 'summary',
                          'based_on': s['based_on'] if s['has_based_on'] else None,
                          'source': s['source'], 'at': s['at'],
                          'seq': s['seq'], 'entry_range': s['entry_range']}
    for b in branches:
        nodes[b['id']] = {'id': b['id'], 'kind': 'branch',
                          'based_on': b.get('based_on'),
                          'source': b.get('source'), 'at': b.get('at'),
                          'seq': None, 'entry_range': b.get('entry_range')}
    children = {}
    for nid, node in nodes.items():
        parent = node['based_on']
        if parent:
            children.setdefault(parent, []).append(nid)

    def descendants(nid):
        seen = set()
        stack = list(children.get(nid, []))
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(children.get(cur, []))
        return seen

    def tip(nid):
        cur = nid
        while len(children.get(cur, [])) == 1:
            cur = children[cur][0]
        return cur

    forks = []
    for parent, kids in sorted(children.items()):
        if len(kids) < 2:
            continue
        # A fork needs at least one pair with no common descendant.
        for i in range(len(kids)):
            for j in range(i + 1, len(kids)):
                if descendants(kids[i]).isdisjoint(descendants(kids[j])):
                    forks.append({
                        'point': parent,
                        'branches': [_branch_info(nodes, tip(kids[i]), store_dir),
                                     _branch_info(nodes, tip(kids[j]), store_dir)],
                    })
                    break
            else:
                continue
            break
    wild = []
    for nid, node in nodes.items():
        # Only records that CARRY a pointer can dangle; legacy records
        # (summaries without the key) are the pre-chain base.
        carried = node['based_on'] is not None
        if node['kind'] == 'summary':
            entry = next(s for s in summaries if s['id'] == nid)
            carried = entry['has_based_on'] and entry['based_on'] is not None
        if carried and node['based_on'] not in nodes:
            wild.append({'id': nid, 'based_on': node['based_on'],
                         'label': '来源不明的一支',
                         'reason': 'based_on 对不上任何已知记录'})
    goal_fork = goal_fork_info(store_dir)
    result = {'forks': forks, 'wild': wild, 'goal_fork': goal_fork,
              'branches': [{'id': b['id'], 'based_on': b.get('based_on'),
                            'source': b.get('source'), 'at': b.get('at'),
                            'status': b.get('status', 'open')}
                           for b in branches],
              'detected_at': utc()}
    if persist:
        save(store_dir / FORKS_FILENAME, result)
    return result


def _branch_info(nodes, tip_id, store_dir):
    node = nodes[tip_id]
    info = {'id': tip_id, 'kind': node['kind'], 'source': node['source'],
            'at': node['at'], 'based_on': node['based_on'],
            'entry_range': node['entry_range']}
    if node['kind'] == 'branch':
        snap = store_dir / 'state' / BRANCHES_DIRNAME / tip_id
        main_state = _state_block_text(store_dir / 'archive' / 'state.md')
        branch_state = _state_block_text(snap / 'state.md') if (snap / 'state.md').exists() else None
        info['field_diff'] = field_diff(main_state, branch_state) if branch_state is not None else []
    else:
        info['field_diff'] = []
        info['note'] = '状态卷文本未留档（汇总记录只记条目范围），差异以条目计'
    return info


def _state_block_text(path):
    try:
        text = path.read_text(encoding='utf-8')
    except OSError:
        return ''
    if STATE_BEGIN in text and STATE_END in text:
        return text[text.index(STATE_BEGIN):text.index(STATE_END) + len(STATE_END)]
    return text[:1000]


def field_diff(state_a, state_b):
    """Field-level diff of two managed state blocks: only fields whose
    values differ are listed, as (field, value_a, value_b)."""
    def fields(text):
        out = {}
        for line in (text or '').splitlines():
            m = re.match(r'^-\s*(.+?)：(.*)$', line.strip())
            if m:
                out[m.group(1).strip()] = m.group(2).strip()
        return out
    fa, fb = fields(state_a), fields(state_b)
    diff = []
    for key in sorted(set(fa) | set(fb)):
        if fa.get(key) != fb.get(key):
            diff.append({'field': key, 'main': fa.get(key), 'branch': fb.get(key)})
    return diff


def render_fork_section(store_dir, detection):
    """The takeover '分叉清单' section (plan 3.3 item 3). Always rendered,
    so a fork can never be invisible; the clean case is one line."""
    lines = ['分叉清单']
    forks = detection.get('forks', [])
    wild = detection.get('wild', [])
    pending = pending_adjudications(store_dir)
    if not forks and not wild and not pending and not detection.get('goal_fork', {}).get('active'):
        lines.append('（无分叉，无待裁项）')
        return '\n'.join(lines)
    for f in forks:
        lines.append('- 分叉点 %s：' % f['point'])
        for b in f['branches']:
            lines.append('  - 支 %s（%s，来源 %s，最后更新 %s，基于 %s）'
                         % (b['id'], '支' if b['kind'] == 'branch' else '汇总链',
                            b['source'], b['at'], b['based_on']))
            if b.get('entry_range'):
                lines.append('    覆盖条目：%s' % (b['entry_range'],))
            for d in b.get('field_diff', []):
                lines.append('    差异字段 %s：主支 %s / 本支 %s'
                             % (d['field'], d['main'], d['branch']))
            if b.get('note'):
                lines.append('    %s' % b['note'])
    for w in wild:
        lines.append('- 野分叉 %s：%s（based_on=%s）' % (w['id'], w['label'], w['based_on']))
    for p in pending:
        if p.get('status') == 'open':
            lines.append('- 待裁：分叉 %s 的字段 %s（主支：%s；支：%s；来源 %s，%s）'
                         % (p['fork'], p['field'], p.get('main_value', ''),
                            p.get('branch_value', ''), p.get('branch_source', ''),
                            p.get('branch_at', '')))
        else:
            lines.append('- 已裁：分叉 %s 的字段 %s → %s（%s，%s）'
                         % (p['fork'], p['field'], p['winner'],
                            p.get('note', ''), p.get('decided_at', '')))
    gf = detection.get('goal_fork', {})
    if gf.get('active'):
        lines.append('- 目标分叉已冻结注入：%s（走 S4 重新确认流程）' % gf.get('reason', ''))
    lines.append('（分叉零动作即可：追加型内容已合并保留，状态冲突见待裁区，目标分叉冻结注入）')
    return '\n'.join(lines)


# ---------------------------------------------------------------- goal fork
def goal_fork_info(store_dir):
    flag = store_dir / GOAL_FORK_FILENAME
    conflict = store_dir / GOAL_CONFLICT_FILENAME
    if flag.exists():
        try:
            info = read_json(flag)
        except ValueError:
            info = {}
        return {'active': True, 'reason': info.get('reason', '目标分叉（3.3 标记）'),
                'at': info.get('at')}
    if conflict.exists():
        return {'active': True, 'reason': '目标同步冲突待裁（sync goal-conflict）',
                'at': None}
    return {'active': False, 'reason': None, 'at': None}


def goal_fork_active(store_dir):
    return goal_fork_info(store_dir)['active']


def flag_goal_fork(store_dir, reason):
    save(store_dir / GOAL_FORK_FILENAME,
         {'reason': reason, 'at': utc()})
    return {'flagged': True, 'reason': reason}


def clear_goal_fork(store_dir):
    (store_dir / GOAL_FORK_FILENAME).unlink(missing_ok=True)
    return {'cleared': True}


# ---------------------------------------------------------------- branches
def write_branch(store_dir, snapshot, based_on, source, entry_range,
                 appended, mismatches):
    """Persist a branched summary product (plan 3.3 item 5): the product
    is stored as its own branch, never overwriting the main archive."""
    content_hash = digest(json.dumps(
        {'snapshot': {k: digest(v.encode('utf-8')) for k, v in snapshot.items()},
         'based_on': based_on, 'source': source,
         'entry_range': entry_range}, sort_keys=True).encode('utf-8'))
    branch_id = 'br-' + content_hash[:12]
    d = store_dir / 'state' / BRANCHES_DIRNAME
    n = 1
    candidate = branch_id
    while (d / (candidate + '.json')).exists():
        n += 1
        candidate = '%s-%d' % (branch_id, n)
    branch_id = candidate
    snap_dir = d / branch_id
    for name, text in snapshot.items():
        atomic(snap_dir / name, text.encode('utf-8'))
    goal_path = store_dir / 'archive' / 'goal.md'
    if goal_path.exists():
        atomic(snap_dir / 'goal.md', goal_path.read_bytes())
    record = {'id': branch_id, 'based_on': based_on, 'source': source,
              'at': utc(), 'entry_range': entry_range,
              'appended': appended, 'mismatches': mismatches,
              'status': 'open'}
    save(d / (branch_id + '.json'), record)
    return record


def _parse_blocks(text, prefix):
    """Archive item blocks: {id: {'header': line, 'body': lines}}."""
    blocks = {}
    current = None
    for line in text.splitlines():
        m = re.match(r'^## (%s-\d+)\s*$' % re.escape(prefix), line.strip())
        if m:
            current = m.group(1)
            blocks[current] = {'header': line, 'body': []}
        elif current is not None:
            blocks[current]['body'].append(line)
    return blocks


def _semantic_key(prefix, block):
    want = {'D': '- 选择：', 'L': '- 内容：', 'T': '- 事项：'}[prefix]
    for line in block['body']:
        if line.strip().startswith(want):
            return line.strip()
    return '\n'.join([block['header']] + block['body']).strip()


def _next_item_id(text, prefix):
    ids = re.findall(re.escape(prefix) + r'-(\d+)', text)
    return '%s-%03d' % (prefix, (max(int(i) for i in ids) + 1) if ids else 1)


def merge_branches(store_dir, texts, summary_seq):
    """Merge open branches into working archive texts (plan 3.3 item 4).

    Append-type items (decisions/lessons/backlog) are all kept, time
    order preserved by append order, content-hash deduped (same semantic
    line on both sides collapses to one), colliding ids reassigned.
    State-field conflicts are NOT picked: both values go to the pending
    section in state.md for the next takeover agent to adjudicate
    against the live code. Returns (new_texts, merged_ids, notes)."""
    merged_ids = []
    notes = []
    new_texts = dict(texts)
    main_state = _state_block_text_from_text(new_texts.get('state.md', ''))
    for branch in list_branches(store_dir):
        if branch.get('status') == 'merged':
            continue
        snap_dir = store_dir / 'state' / BRANCHES_DIRNAME / branch['id']
        if not snap_dir.is_dir():
            continue
        branch_state = ''
        if (snap_dir / 'state.md').exists():
            branch_state = _state_block_text(snap_dir / 'state.md')
        for fname, prefix in (('decisions.md', 'D'), ('lessons.md', 'L'),
                              ('backlog.md', 'T')):
            main_text = new_texts.get(fname, '')
            snap_path = snap_dir / fname
            if not snap_path.exists():
                continue
            snap_text = snap_path.read_text(encoding='utf-8')
            main_blocks = _parse_blocks(main_text, prefix)
            main_keys = {digest(_semantic_key(prefix, b).encode('utf-8'))
                         for b in main_blocks.values()}
            used_ids = set(main_blocks)
            for item_id, block in _parse_blocks(snap_text, prefix).items():
                key = digest(_semantic_key(prefix, block).encode('utf-8'))
                if key in main_keys:
                    notes.append('支 %s 的 %s 与主支内容相同，已去重' % (branch['id'], item_id))
                    continue
                target_id = item_id
                if target_id in used_ids:
                    target_id = _next_item_id(main_text, prefix)
                    notes.append('支 %s 的 %s 与主支 id 冲突，重编号为 %s'
                                 % (branch['id'], item_id, target_id))
                rendered = ['## %s' % target_id] + block['body']
                main_text = main_text.rstrip('\n') + '\n' + '\n'.join(rendered) + '\n'
                used_ids.add(target_id)
                main_keys.add(key)
            new_texts[fname] = main_text
        for diff in field_diff(main_state, branch_state):
            existing = pending_adjudications_from_text(new_texts.get('state.md', ''))
            if any(p.get('fork') == branch['id'] and p.get('field') == diff['field']
                   and p.get('status') == 'open' for p in existing):
                continue
            item = {'fork': branch['id'], 'field': diff['field'],
                    'main_value': diff['main'], 'branch_value': diff['branch'],
                    'branch_source': branch.get('source'),
                    'branch_at': branch.get('at'), 'status': 'open'}
            new_texts['state.md'] = append_pending_adjudication(
                new_texts.get('state.md', ''), item)
            notes.append('支 %s 的字段 %s 进入待裁区' % (branch['id'], diff['field']))
        branch['status'] = 'merged'
        branch['merged_in'] = summary_seq
        save(store_dir / 'state' / BRANCHES_DIRNAME / (branch['id'] + '.json'), branch)
        merged_ids.append(branch['id'])
    return new_texts, merged_ids, notes


def _state_block_text_from_text(text):
    if STATE_BEGIN in text and STATE_END in text:
        return text[text.index(STATE_BEGIN):text.index(STATE_END) + len(STATE_END)]
    return text[:1000]


# ---------------------------------------------------------------- pending adjudications
def pending_adjudications(store_dir):
    path = store_dir / 'archive' / 'state.md'
    if not path.exists():
        return []
    return pending_adjudications_from_text(path.read_text(encoding='utf-8'))


def pending_adjudications_from_text(text):
    if PENDING_BEGIN not in text or PENDING_END not in text:
        return []
    section = text[text.index(PENDING_BEGIN):text.index(PENDING_END)]
    items = []
    for line in section.splitlines():
        m = re.match(r'^-\s*fork=(\S+)\s+field=(\S+)\s+status=(\S+)(?:\s+main=(.*?))?(?:\s+branch=(.*?))?(?:\s+source=(\S+))?(?:\s+at=(\S+))?(?:\s+winner=(\S+))?(?:\s+note=(.*))?$',
                     line.strip())
        if not m:
            continue
        item = {'fork': m.group(1), 'field': m.group(2), 'status': m.group(3)}
        if m.group(4) is not None:
            item['main_value'] = m.group(4)
        if m.group(5) is not None:
            item['branch_value'] = m.group(5)
        if m.group(6) is not None:
            item['branch_source'] = m.group(6)
        if m.group(7) is not None:
            item['branch_at'] = m.group(7)
        if m.group(8) is not None:
            item['winner'] = m.group(8)
        if m.group(9) is not None:
            item['note'] = m.group(9)
        items.append(item)
    return items


def append_pending_adjudication(text, item):
    """Append one pending item to the section OUTSIDE the managed state
    block (so summarize's block replacement never wipes it)."""
    line = ('- fork=%s field=%s status=open main=%s branch=%s'
            % (item['fork'], item['field'],
               (item.get('main_value') or '').replace('\n', ' '),
               (item.get('branch_value') or '').replace('\n', ' ')))
    if item.get('branch_source'):
        line += ' source=%s' % item['branch_source']
    if item.get('branch_at'):
        line += ' at=%s' % item['branch_at']
    if PENDING_BEGIN in text and PENDING_END in text:
        start = text.index(PENDING_BEGIN)
        stop = text.index(PENDING_END)
        return text[:stop] + line + '\n' + text[stop:]
    return (text.rstrip('\n') + '\n\n' + PENDING_BEGIN + '\n' + line + '\n'
            + PENDING_END + '\n')


def adjudicate(store_dir, fork_id, winner, note):
    """Resolve one pending item (plan 3.3 item 4②): the next takeover
    agent checks the live code, picks the true side, and the裁定 is
    recorded both in the pending section and in the durable log."""
    require(winner in ('branch', 'main'), 'winner 须为 branch 或 main')
    path = store_dir / 'archive' / 'state.md'
    require(path.exists(), '本项目无 state.md')
    text = path.read_text(encoding='utf-8')
    require(PENDING_BEGIN in text and PENDING_END in text,
            '本项目无待裁区')
    start = text.index(PENDING_BEGIN)
    stop = text.index(PENDING_END)
    section = text[start:stop]
    lines = section.splitlines()
    found = False
    for i, line in enumerate(lines):
        m = re.match(r'^-\s*fork=(\S+)\s+field=(\S+)\s+status=open\b', line.strip())
        if m and m.group(1) == fork_id:
            # Rebuild in canonical field order (fork field status main
            # branch winner note): a bare 'status=open' replacement would
            # put winner/note before main/branch and the pending-section
            # parser (fixed-order optional groups) would drop the line.
            parsed = pending_adjudications_from_text(
                PENDING_BEGIN + '\n' + line + '\n' + PENDING_END)
            item = parsed[0] if parsed else {}
            rebuilt = ('- fork=%s field=%s status=resolved main=%s branch=%s'
                       % (fork_id, m.group(2),
                          item.get('main_value', ''), item.get('branch_value', '')))
            if item.get('branch_source'):
                rebuilt += ' source=%s' % item['branch_source']
            if item.get('branch_at'):
                rebuilt += ' at=%s' % item['branch_at']
            rebuilt += ' winner=%s note=%s' % (winner, (note or '').replace('\n', ' '))
            lines[i] = rebuilt
            found = True
            break
    require(found, '无待裁项：分叉 %s' % fork_id)
    new_text = text[:start] + '\n'.join(lines) + '\n' + text[stop:]
    atomic(path, new_text.encode('utf-8'))
    log_path = store_dir / ADJUDICATIONS_FILENAME
    log = []
    if log_path.exists():
        try:
            log = read_json(log_path)
        except ValueError:
            log = []
    if not isinstance(log, list):
        log = []
    log.append({'fork': fork_id, 'winner': winner, 'note': note,
                'decided_at': utc()})
    save(log_path, log)
    # Codex PR #5 rounds 7-12, item 7: adjudication rewrites
    # archive/state.md, which is inside the checksum manifest's scope
    # (archive/**), so the manifest must be refreshed here -- otherwise
    # the next resume reports a manifest mismatch for state.md and
    # blocks automatic summarization until repaired. The adjudication
    # log itself (state/fork-adjudications.json) is outside manifest
    # scope and needs no refresh.
    relay.update_checksum_manifest(store_dir)
    return {'fork': fork_id, 'winner': winner, 'note': note}


# ---------------------------------------------------------------- compression
def maybe_compress_chain(store_dir, threshold=200, keep=10):
    """Fold the chain head segment into one hashed archive package
    (plan 3.3 item 6). Live summaries beyond the threshold are folded
    except the most recent `keep`; the package keeps full record
    content, so based_on pointers stay resolvable and no record content
    changes. Returns the package info, or None when under threshold."""
    summaries_dir = store_dir / 'state' / 'summaries'
    live = []
    if summaries_dir.is_dir():
        for p in sorted(summaries_dir.glob('*.json')):
            try:
                rec = read_json(p)
            except ValueError:
                continue
            if isinstance(rec, dict) and isinstance(rec.get('seq'), int):
                live.append((p, rec))
    live.sort(key=lambda t: t[1]['seq'])
    if len(live) <= threshold:
        return None
    # Rollback safety: only fold when the latest tx log is self-contained
    # (carries before_state) or when there are no tx logs; a legacy
    # recover derivation reads old summary files for its previous
    # candidate, and folding those would silently change its answer.
    tx_dir = store_dir / 'tx'
    logs = sorted(p for p in tx_dir.glob('*.json')
                  if tx_dir.is_dir() and not p.name.endswith('.recovered.json')) \
        if tx_dir.is_dir() else []
    if logs:
        try:
            latest_tx = read_json(logs[-1])
        except ValueError:
            latest_tx = {}
        if not isinstance(latest_tx, dict) or 'before_state' not in latest_tx:
            return None
    folded = live[:len(live) - keep] if keep > 0 else live
    if not folded:
        return None
    records = [rec for _p, rec in folded]
    blob = b''.join(json.dumps(rec, sort_keys=True, ensure_ascii=False).encode('utf-8')
                    for rec in records)
    anchor_id = summary_id(records[-1]['seq'])
    package = {'version': 1, 'anchor_id': anchor_id,
               'folded_ids': [summary_id(rec['seq']) for rec in records],
               'folded_count': len(records),
               'total_hash': digest(blob),
               'records': records, 'folded_at': utc()}
    pkg_dir = store_dir / 'state' / CHAIN_ARCHIVES_DIRNAME
    save(pkg_dir / (anchor_id + '.json'), package)
    for p, _rec in folded:
        p.unlink()
    save(store_dir / ANCHOR_FILENAME,
         {'anchor_id': anchor_id, 'package': 'state/%s/%s.json' % (CHAIN_ARCHIVES_DIRNAME, anchor_id),
          'total_hash': package['total_hash'], 'folded_count': len(records),
          'folded_at': package['folded_at']})
    return package
