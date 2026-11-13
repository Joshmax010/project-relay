"""project-relay v3 stats: cost measurement (record-only) and the cost fuse.

Local-only by construction: every row lives under <store>/stats/, which
the zip payload filter never ships (see relay_sync._is_payload_file)
and the checksum manifest never covers (see relay._manifest_files), so
rows can be appended by capture / summarize without tripping resume's
integrity check. Nothing here touches the network, and no row ever
leaves the device.

Token convention (plan S7): without a host tokenizer, tokens are
estimated as ceil(chars * 2 / 3) -- chars / 1.5 -- and every token
figure carries a measured/estimated flag.

v3 slimming: the report (build_report/render_report) and the tuning
procedure (apply/evaluate_tuning, knob writes, rollback lessons) are
deleted -- two weeks of dogfooding produced seven rows and zero tuning
runs, and the report was never the takeover path. What stays is the
record layer (capture/summarize/handoff rows) and the fuse
(compute/evaluate/fuse_tripped): summarize checks the fuse before
writing, and the fuse state persists its trip/clear transitions.

Time convention: rows carry both ISO `at` (human-readable, same utc()
as the rest of the codebase) and numeric `at_epoch` (window math).
Every public function takes an optional now_epoch so tests can inject
time instead of waiting two real weeks.
"""

import importlib.util
import json
import sys
import time
from pathlib import Path

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
save = relay.save
read_json = relay.read_json

STATS_DIRNAME = 'stats'
CAPTURE_ROWS = 'capture.jsonl'
SUMMARIZE_ROWS = 'summarize.jsonl'
HANDOFF_ROWS = 'handoff.jsonl'
FUSE_STATE = 'fuse.json'

# Fuse constants (plan 3.4 item 6). A weekly spike is a 3x jump over the
# previous week AND at least this many LLM tokens, so tiny projects with
# one summarize can never trip it on noise alone.
FUSE_SPIKE_RATIO = 3.0
FUSE_SPIKE_FLOOR_TOKENS = 5000


def _stats_dir(store_dir):
    """Stats root: <store>/stats/ -- inside the store tree, but
    device-local by construction (see the module docstring)."""
    return Path(store_dir) / STATS_DIRNAME


def _read_config(store_dir):
    """Fuse config, v3: the outer config.json at the store root.

    Only monthly_llm_token_cap is read here (the fuse's soft cap); the
    other caps live in the same file but are owned by relay_flow.
    Returns (cfg, path).
    """
    cfg = {'monthly_llm_token_cap': None}
    path = Path(store_dir) / 'config.json'
    if path.exists():
        try:
            value = read_json(path)
        except ValueError:
            value = {}
        if isinstance(value, dict):
            cfg.update(value)
    return cfg, path


def tokens_for_chars(n):
    """Token estimate for a character count (chars / 1.5, ceiling)."""
    return -(-int(n) * 2 // 3)


def estimate_tokens(text):
    """Same estimator as relay_flow.estimate_tokens (chars / 1.5), always
    labelled an estimate, never presented as a metered count."""
    return tokens_for_chars(len(text))


def _append(store_dir, filename, row, now_epoch=None):
    now_epoch = time.time() if now_epoch is None else now_epoch
    row = dict(row)
    row.setdefault('at', utc())
    row.setdefault('at_epoch', now_epoch)
    d = _stats_dir(store_dir)
    d.mkdir(parents=True, exist_ok=True)
    path = d / filename
    with open(path, 'a', encoding='utf-8', newline='') as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + '\n')
    return row


def _rows(store_dir, filename):
    path = _stats_dir(store_dir) / filename
    out = []
    if not path.exists():
        return out
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def capture_rows(store_dir):
    return _rows(store_dir, CAPTURE_ROWS)


def summarize_rows(store_dir):
    return _rows(store_dir, SUMMARIZE_ROWS)


def handoff_rows(store_dir):
    return _rows(store_dir, HANDOFF_ROWS)


def record_capture(store_dir, tool=None, session=None, entries=0,
                   changed_files=None, duration_s=0.0, deduped=False,
                   degraded=False, now_epoch=None):
    """One capture stats row (plan 3.4 item 1). Zero LLM by construction:
    capture never calls a model, so there is no token field at all."""
    return _append(store_dir, CAPTURE_ROWS, {
        'tool': tool, 'session': session, 'entries': int(entries),
        'changed_files': changed_files, 'duration_s': round(float(duration_s), 3),
        'deduped': bool(deduped), 'degraded': bool(degraded),
    }, now_epoch=now_epoch)


def record_summarize(store_dir, source, trigger, entry_count=0,
                     entry_range=None, new_raw_tokens_est=0,
                     old_summary_tokens_est=0, llm_calls=0,
                     llm_in_tokens=0, llm_out_tokens=0, llm_measured=True,
                     assembled_chars=0, semantic_chars=0,
                     archive_delta_tokens=0, noop=False, degraded=False,
                     branched=False, fused=False, now_epoch=None):
    """One summarize stats row (plan 3.4 item 2). llm_measured says
    whether in/out tokens came from a caller-reported usage object (True)
    or were estimated from the semantic text length (False). Assembled
    runs (no semantic text) are measured True with zero LLM tokens: the
    tool itself provably made no model call."""
    return _append(store_dir, SUMMARIZE_ROWS, {
        'source': source, 'trigger': trigger, 'entry_count': int(entry_count),
        'entry_range': entry_range,
        'new_raw_tokens_est': int(new_raw_tokens_est),
        'old_summary_tokens_est': int(old_summary_tokens_est),
        'llm_calls': int(llm_calls),
        'llm_in_tokens': int(llm_in_tokens),
        'llm_out_tokens': int(llm_out_tokens),
        'llm_tokens': int(llm_in_tokens) + int(llm_out_tokens),
        'llm_measured': bool(llm_measured),
        'assembled_chars': int(assembled_chars),
        'semantic_chars': int(semantic_chars),
        'archive_delta_tokens': int(archive_delta_tokens),
        'noop': bool(noop), 'degraded': bool(degraded),
        'branched': bool(branched), 'fused': bool(fused),
    }, now_epoch=now_epoch)


def record_handoff(store_dir, layers, reconcile, catchup_summaries=0,
                   prev_summarize_llm_tokens=0, now_epoch=None):
    """One handoff stats row (plan 3.4 item 3). layers is the
    per-layer injection measurement {index, timeline, full, total} in
    estimated tokens; reconcile counts the takeover reconcile diffs by
    class; total_cost is the plan's headline metric: the previous
    summarize's LLM tokens plus this takeover's injection tokens."""
    layers = dict(layers or {})
    total = int(layers.get('total', 0))
    prev = int(prev_summarize_llm_tokens or 0)
    return _append(store_dir, HANDOFF_ROWS, {
        'injection_index_tokens': int(layers.get('index', 0)),
        'injection_timeline_tokens': int(layers.get('timeline', 0)),
        'injection_full_tokens': int(layers.get('full', 0)),
        'injection_total_tokens': total,
        'injection_estimated': True,
        'reconcile': dict(reconcile or {}),
        'reconcile_total': sum(int(v) for v in (reconcile or {}).values()),
        'unrecorded_proxy': _unrecorded_proxy(reconcile or {}),
        'catchup_summaries': int(catchup_summaries),
        'prev_summarize_llm_tokens': prev,
        'total_cost_tokens': prev + total,
    }, now_epoch=now_epoch)


def _unrecorded_proxy(reconcile):
    """Leak-rate proxy (plan 3.4 item 3): the share of reconcile diffs
    that are 'work since baseline not yet in the archive' (changed files
    since the summary baseline). It is a proxy, labelled as such in the
    report -- reconcile diffs also include staleness classes (missing
    files, branch drift) that are not leaks."""
    total = sum(int(v) for v in reconcile.values())
    if not total:
        return 0.0
    return round(int(reconcile.get('changed_files', 0)) / total, 4)


def latest_summarize_row(store_dir):
    rows = summarize_rows(store_dir)
    return rows[-1] if rows else None


def _month_key(epoch):
    import datetime
    dt = datetime.datetime.fromtimestamp(epoch, tz=datetime.timezone.utc)
    return '%04d-%02d' % (dt.year, dt.month)


def compute_fuse(store_dir, now_epoch=None):
    """Pure fuse computation (no writes): the weekly-spike rule and the
    monthly soft cap, from summarize rows. Resume uses this so the
    takeover read path stays write-free; summarize uses evaluate_fuse,
    which persists transitions."""
    now_epoch = time.time() if now_epoch is None else now_epoch
    rows = [r for r in summarize_rows(store_dir) if not r.get('noop')]
    week = sum(r['llm_tokens'] for r in rows
               if r.get('at_epoch', 0) >= now_epoch - 7 * 86400)
    prev = sum(r['llm_tokens'] for r in rows
               if now_epoch - 14 * 86400 <= r.get('at_epoch', 0) < now_epoch - 7 * 86400)
    month = sum(r['llm_tokens'] for r in rows
                if _month_key(r.get('at_epoch', 0)) == _month_key(now_epoch))
    cfg, _ = _read_config(store_dir)
    cap = cfg.get('monthly_llm_token_cap', None)
    tripped = prev > 0 and week >= FUSE_SPIKE_FLOOR_TOKENS \
        and week > FUSE_SPIKE_RATIO * prev
    reason = ('周内汇总 LLM 成本 %d tokens，超上周 %d tokens 的 3 倍'
              % (week, prev)) if tripped else ''
    return {'tripped': tripped, 'reason': reason,
            'week_llm_tokens': week, 'prev_week_llm_tokens': prev,
            'month_llm_tokens': month, 'monthly_cap': cap,
            'cap_alert': cap is not None and month > cap}


def evaluate_fuse(store_dir, now_epoch=None):
    """Weekly-spike fuse + monthly soft cap (plan 3.4 item 6). Spike rule:
    this week's LLM tokens exceed 3x last week's AND clear a 5000-token
    floor. A missing previous week means no baseline, so no trip. The
    monthly cap only alerts (soft): it never degrades on its own. The
    returned dict is also the persisted fuse state (tripped transitions
    are recorded with timestamps)."""
    now_epoch = time.time() if now_epoch is None else now_epoch
    computed = compute_fuse(store_dir, now_epoch=now_epoch)
    tripped = computed['tripped']
    reason = computed['reason']
    week = computed['week_llm_tokens']
    prev = computed['prev_week_llm_tokens']
    month = computed['month_llm_tokens']
    cap = computed['monthly_cap']
    cap_alert = computed['cap_alert']
    state_path = _stats_dir(store_dir) / FUSE_STATE
    prev_state = {}
    if state_path.exists():
        try:
            prev_state = read_json(state_path)
        except ValueError:
            prev_state = {}
    state = {'tripped': tripped, 'reason': reason,
             'week_llm_tokens': week, 'prev_week_llm_tokens': prev,
             'month_llm_tokens': month, 'monthly_cap': cap,
             'cap_alert': cap_alert,
             'tripped_at': prev_state.get('tripped_at'),
             'cleared_at': prev_state.get('cleared_at')}
    if tripped and not prev_state.get('tripped'):
        state['tripped_at'] = utc()
    if not tripped and prev_state.get('tripped'):
        state['cleared_at'] = utc()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    save(state_path, state)
    return state


def fuse_tripped(store_dir, now_epoch=None):
    return bool(evaluate_fuse(store_dir, now_epoch=now_epoch).get('tripped'))
