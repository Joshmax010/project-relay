"""project-relay v2 stats (plan part 03, step 3.4): cost measurement,
tuning procedure, and the cost fuse. Local-only by construction: every
row lives under <store>/stats/, which the sync payload filter never
ships (it only carries archive/, receipts/, state-included files, and
raw/cap-*.md) and the checksum manifest never covers (archive/ only).
Nothing here touches the network, and no row ever leaves the device.

Token convention (plan S7): without a host tokenizer, tokens are
estimated as ceil(chars * 2 / 3) -- chars / 1.5 -- and every token
figure carries a measured/estimated flag. Reports list measured and
estimated sums separately, never blended.

Location rule (deliberate, plan 3.4 vs the resume zero-write contract):
stats live OUTSIDE the project store tree, at
<store-root>/stats/<key>-<slug>/ (derived from the store dir's
<root>/projects/<name> layout; any other layout falls back to
<store>/stats/). This serves both masters at once: every takeover can
be recorded (plan 3.4 wants every handoff counted) without ever
changing the store tree, so resume's sections 1-3/5 stay strictly
read-only and its zero-write tests keep passing unmodified. It also
strengthens the local-only rule: the sync payload only ever reads the
store dir itself, so stats cannot ship to another device even if the
payload filter changed.

Time convention: rows carry both ISO `at` (human-readable, same utc()
as the rest of the codebase) and numeric `at_epoch` (window math).
Every public function takes an optional now_epoch so tests can inject
time instead of waiting two real weeks.
"""

import importlib.util
import json
import math
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location('relay', _HERE / 'relay.py')
relay = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(relay)

RelayError = relay.RelayError
require = relay.require
utc = relay.utc
save = relay.save
read_json = relay.read_json

STATS_DIRNAME = 'stats'
CAPTURE_ROWS = 'capture.jsonl'
SUMMARIZE_ROWS = 'summarize.jsonl'
HANDOFF_ROWS = 'handoff.jsonl'
TUNING_ROWS = 'tuning.jsonl'
FUSE_STATE = 'fuse.json'
CONFIG_PATH = ('state', 'config.json')

# Tuning knobs (plan 3.4 item 5). Each maps to one state/config.json key.
# Ranges are hard guardrails; the +-50% per-step rule is applied on top.
KNOBS = {
    'volume_files': {'config_key': 'threshold_files', 'kind': 'int', 'min': 1},
    'volume_lines': {'config_key': 'threshold_lines', 'kind': 'int', 'min': 1},
    'time_days': {'config_key': 'time_days', 'kind': 'int', 'min': 1},
    'inject_budget': {'config_key': 'inject_budget_tokens', 'kind': 'int',
                      'min': 500, 'max': 8000},
    'precompress_sample_cap': {'config_key': 'precompress_sample_cap',
                               'kind': 'optional_int', 'min': 1},
}
# Defaults mirror relay_flow.DEFAULT_CONFIG (kept here so this module
# stays loadable without relay_flow; tests pin both sides equal).
KNOB_DEFAULTS = {
    'threshold_files': 10,
    'threshold_lines': 800,
    'time_days': 7,
    'inject_budget_tokens': 2000,
    'precompress_sample_cap': None,
    'monthly_llm_token_cap': None,
}

# Fuse constants (plan 3.4 item 6). A weekly spike is a 3x jump over the
# previous week AND at least this many LLM tokens, so tiny projects with
# one summarize can never trip it on noise alone.
FUSE_SPIKE_RATIO = 3.0
FUSE_SPIKE_FLOOR_TOKENS = 5000
TUNING_WINDOW_DAYS = 14
TUNING_MAX_STEP_RATIO = 0.5
TUNING_NO_IMPROVE_STRIKES = 2


def tokens_for_chars(n):
    """Token estimate for a character count (chars / 1.5, ceiling)."""
    return -(-int(n) * 2 // 3)


def estimate_tokens(text):
    """Same estimator as relay_flow.estimate_tokens (chars / 1.5), always
    labelled an estimate, never presented as a metered count."""
    return tokens_for_chars(len(text))


def _stats_dir(store_dir):
    """Stats root for a project store. Never inside the store tree
    (see the module docstring): <store-root>/stats/<key>-<slug>/."""
    store_dir = Path(store_dir)
    if store_dir.parent.name == 'projects':
        return store_dir.parent.parent / STATS_DIRNAME / store_dir.name
    return store_dir / STATS_DIRNAME


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


def tuning_rows(store_dir):
    return _rows(store_dir, TUNING_ROWS)


# ------------------------------------------------------------------ record
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


# ------------------------------------------------------------------ report
def _mean(values):
    return round(sum(values) / len(values), 1) if values else 0


def _mean3(values):
    """Durations are typically sub-second; one decimal would round them
    all to 0.0, so duration means keep three decimals."""
    return round(sum(values) / len(values), 3) if values else 0


def _p95(values):
    if not values:
        return 0
    ordered = sorted(values)
    idx = math.ceil(0.95 * len(ordered)) - 1
    return ordered[max(0, idx)]


def _week_start(epoch):
    """UTC week bucket (Monday 00:00) as an ISO date string."""
    import datetime
    dt = datetime.datetime.fromtimestamp(epoch, tz=datetime.timezone.utc)
    monday = dt - datetime.timedelta(days=dt.weekday())
    return monday.date().isoformat()


def _window(rows, now_epoch, weeks):
    cutoff = now_epoch - weeks * 7 * 86400
    return [r for r in rows if r.get('at_epoch', 0) >= cutoff]


def _trigger_class(source, trigger):
    """Normalize a summarize row's trigger to a report class. Explicit
    trigger wins; otherwise the source maps: resume-backfill -> handoff,
    manual/accept -> manual, pack -> pack, anything else -> other. The
    three scale classes (milestone/volume/time) only ever arrive via an
    explicit trigger, because no current caller derives them."""
    if trigger:
        return trigger
    mapping = {'resume-backfill': 'handoff', 'manual': 'manual',
               'accept': 'manual', 'pack': 'pack'}
    return mapping.get(source, 'other')


def build_report(store_dir, weeks=4, now_epoch=None):
    """Assemble the `relay stats` report (plan 3.4 item 4). All figures
    come from local rows only. Measured and estimated LLM tokens are
    reported separately, never blended."""
    now_epoch = time.time() if now_epoch is None else now_epoch
    caps = capture_rows(store_dir)
    sums = summarize_rows(store_dir)
    hands = handoff_rows(store_dir)
    caps_w = _window(caps, now_epoch, weeks)
    sums_w = _window(sums, now_epoch, weeks)
    hands_w = _window(hands, now_epoch, weeks)
    real_sums = [r for r in sums_w
                 if not r.get('noop') and not r.get('degraded')
                 and not r.get('branched')]
    llm_totals = [r['llm_tokens'] for r in real_sums]
    measured = sum(r['llm_tokens'] for r in real_sums if r.get('llm_measured'))
    estimated = sum(r['llm_tokens'] for r in real_sums if not r.get('llm_measured'))
    triggers = {}
    for r in real_sums:
        cls = _trigger_class(r.get('source'), r.get('trigger'))
        triggers[cls] = triggers.get(cls, 0) + 1
    scale_total = sum(triggers.get(c, 0) for c in ('milestone', 'volume', 'time'))
    trigger_share = {}
    for cls, count in sorted(triggers.items()):
        trigger_share[cls] = {
            'count': count,
            'share_of_all': round(count / len(real_sums), 4) if real_sums else 0,
            'share_of_scales': (round(count / scale_total, 4)
                                if cls in ('milestone', 'volume', 'time') and scale_total else 0),
        }
    top_trigger = None
    if triggers:
        top_trigger = max(sorted(triggers), key=lambda c: triggers[c])
    handoff_totals = [r['total_cost_tokens'] for r in hands_w]
    injection_totals = [r['injection_total_tokens'] for r in hands_w]
    handoff_llm = sum(r['prev_summarize_llm_tokens'] for r in hands_w)
    handoff_inj = sum(injection_totals)
    llm_share = (round(handoff_llm / (handoff_llm + handoff_inj), 4)
                 if (handoff_llm + handoff_inj) else 0)
    curve = {}
    for r in real_sums:
        curve.setdefault(_week_start(r['at_epoch']), {'llm_tokens': 0, 'summarizes': 0})
        curve[_week_start(r['at_epoch'])]['llm_tokens'] += r['llm_tokens']
        curve[_week_start(r['at_epoch'])]['summarizes'] += 1
    for r in hands_w:
        bucket = curve.setdefault(_week_start(r['at_epoch']),
                                  {'llm_tokens': 0, 'summarizes': 0})
        bucket['injection_tokens'] = bucket.get('injection_tokens', 0) + r['injection_total_tokens']
        bucket['handoffs'] = bucket.get('handoffs', 0) + 1
    for bucket in curve.values():
        bucket.setdefault('injection_tokens', 0)
        bucket.setdefault('handoffs', 0)
    fuse = evaluate_fuse(store_dir, now_epoch=now_epoch)
    suggestions = _suggestions(real_sums, triggers, llm_share, fuse, hands_w)
    return {
        'weeks': weeks, 'window_rows': {
            'capture': len(caps_w), 'summarize': len(sums_w),
            'summarize_real': len(real_sums), 'handoff': len(hands_w)},
        'capture': {
            'duration_s_mean': _mean3([r['duration_s'] for r in caps_w]),
            'duration_s_p95': _p95([r['duration_s'] for r in caps_w]),
            'changed_files_mean': _mean([r['changed_files'] for r in caps_w
                                         if r.get('changed_files') is not None]),
            'deduped': sum(1 for r in caps_w if r.get('deduped')),
            'degraded': sum(1 for r in caps_w if r.get('degraded')),
        },
        'summarize': {
            'llm_tokens_mean': _mean(llm_totals),
            'llm_tokens_p95': _p95(llm_totals),
            'llm_tokens_measured': measured,
            'llm_tokens_estimated': estimated,
            'llm_calls_total': sum(r['llm_calls'] for r in real_sums),
            'assembled_chars_mean': _mean([r['assembled_chars'] for r in real_sums]),
            'semantic_chars_mean': _mean([r['semantic_chars'] for r in real_sums]),
        },
        'triggers': trigger_share,
        'top_trigger': top_trigger,
        'handoff': {
            'total_cost_mean': _mean(handoff_totals),
            'total_cost_p95': _p95(handoff_totals),
            'injection_mean': _mean(injection_totals),
            'injection_p95': _p95(injection_totals),
            'llm_share_of_handoff_cost': llm_share,
            'mean_leak_proxy': _mean([r['unrecorded_proxy'] for r in hands_w]),
            'catchup_summaries_total': sum(r['catchup_summaries'] for r in hands_w),
        },
        # The plan's three questions, answered first-class:
        'three_questions': {
            'avg_tokens_per_handoff': _mean(handoff_totals),
            'llm_summarize_share': llm_share,
            'most_frequent_trigger': top_trigger,
        },
        'weekly_curve': [{'week': w, **curve[w]} for w in sorted(curve)],
        'fuse': fuse,
        'suggestions': suggestions,
    }


def _suggestions(real_sums, triggers, llm_share, fuse, hands_w):
    """Rule-generated tuning suggestions (plan 3.4 item 4). Every rule
    states its evidence; a suggestion never fires on an empty window."""
    out = []
    if not real_sums:
        return ['暂无汇总数据：先跑几轮采集与汇总，报告才有调优依据。']
    if triggers.get('time', 0) == 0 and triggers.get('volume', 0) > 0:
        out.append('时间刻度从未独立触发过，改动量总是先到——时间阈值可放宽'
                   '（time_days 上调），或接受改动量刻度为主。')
    scale_total = sum(triggers.get(c, 0) for c in ('milestone', 'volume', 'time'))
    if scale_total and triggers.get('volume', 0) / scale_total > 0.8:
        out.append('改动量刻度占刻度触发八成以上：汇总偏频繁，可考虑上调'
                   '改动量文件数/行数阈值，减少汇总次数。')
    if llm_share > 0.7 and hands_w:
        out.append('接力成本中 LLM 汇总占七成以上：可上调汇总阈值（减少汇总'
                   '频率）或下调注入预算，两者一次只动一个。')
    if fuse.get('tripped'):
        out.append('保险丝已触发（周内汇总成本异常飙升）：先查触发逻辑是否'
                   '出 bug 导致汇总风暴，再清保险丝；不要直接放宽上限。')
    if fuse.get('cap_alert'):
        out.append('本月汇总 LLM 成本已超月度软上限：仅告警不停用，如需继续'
                   '请上调 monthly_llm_token_cap。')
    if not out:
        out.append('当前分布无明显失衡：三刻度与成本占比都在常规范围，暂不调整。')
    return out


def render_report(report):
    """Plain-text rendering for `relay stats` (Chinese, CLI style)."""
    L = []
    L.append('成本报告（最近 %d 周，本地数据）' % report['weeks'])
    w = report['window_rows']
    L.append('样本：采集 %d 条，汇总 %d 条（有效 %d 条），接手 %d 条'
             % (w['capture'], w['summarize'], w['summarize_real'], w['handoff']))
    q = report['three_questions']
    L.append('三个问题：每接力一次平均花 %s tokens；其中 LLM 汇总占 %s；'
             '三刻度最常触发的是 %s'
             % (q['avg_tokens_per_handoff'],
                ('%.0f%%' % (q['llm_summarize_share'] * 100))
                if report['window_rows']['handoff'] else '—',
                q['most_frequent_trigger'] or '—'))
    c = report['capture']
    L.append('采集：耗时均值 %ss，P95 %ss；单次改动文件均值 %s；去重 %d 条，降级 %d 条'
             % (c['duration_s_mean'], c['duration_s_p95'],
                c['changed_files_mean'], c['deduped'], c['degraded']))
    s = report['summarize']
    L.append('汇总：LLM tokens 均值 %s，P95 %s；实测 %d，估算 %d（分开列，不混算）；'
             'LLM 调用 %d 次；拼装层均值 %s 字符，LLM 层均值 %s 字符'
             % (s['llm_tokens_mean'], s['llm_tokens_p95'],
                s['llm_tokens_measured'], s['llm_tokens_estimated'],
                s['llm_calls_total'], s['assembled_chars_mean'],
                s['semantic_chars_mean']))
    if report['triggers']:
        parts = ['%s %d 条（全部占比 %.0f%%）' % (k, v['count'], v['share_of_all'] * 100)
                 for k, v in sorted(report['triggers'].items())]
        L.append('触发：' + '；'.join(parts))
    else:
        L.append('触发：无有效汇总')
    h = report['handoff']
    L.append('接手：单次总成本均值 %s tokens，P95 %s；注入均值 %s，P95 %s；'
             '漏记率代理均值 %s；补汇总 %d 次'
             % (h['total_cost_mean'], h['total_cost_p95'],
                h['injection_mean'], h['injection_p95'],
                h['mean_leak_proxy'], h['catchup_summaries_total']))
    if report['weekly_curve']:
        L.append('成本曲线（按周）：')
        for b in report['weekly_curve']:
            L.append('  %s：汇总 LLM %d tokens（%d 次），接手注入 %d tokens（%d 次）'
                     % (b['week'], b['llm_tokens'], b['summarizes'],
                        b['injection_tokens'], b['handoffs']))
    f = report['fuse']
    L.append('保险丝：%s%s' % (
        '已触发（%s），汇总已降级为只拼装' % f['reason'] if f['tripped'] else '未触发',
        '；月度软上限告警' if f.get('cap_alert') else ''))
    L.append('调优建议：')
    for sug in report['suggestions']:
        L.append('  - ' + sug)
    return '\n'.join(L) + '\n'


# ------------------------------------------------------------------ config
def _read_config(store_dir):
    cfg = dict(KNOB_DEFAULTS)
    path = Path(store_dir) / Path(*CONFIG_PATH)
    if path.exists():
        try:
            value = read_json(path)
        except ValueError:
            value = {}
        if isinstance(value, dict):
            cfg.update(value)
    return cfg, path


def _write_config(store_dir, cfg):
    path = Path(store_dir) / Path(*CONFIG_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    save(path, cfg)
    return path


def current_knob_value(store_dir, knob):
    spec = KNOBS[knob]
    cfg, _ = _read_config(store_dir)
    return cfg.get(spec['config_key'], KNOB_DEFAULTS[spec['config_key']])


# ------------------------------------------------------------------ tuning
def _handoff_mean_in_window(store_dir, start_epoch, end_epoch):
    totals = [r['total_cost_tokens'] for r in handoff_rows(store_dir)
              if start_epoch <= r.get('at_epoch', 0) < end_epoch]
    return (_mean(totals), len(totals))


def apply_tuning(store_dir, knob, new_value, note='', now_epoch=None):
    """One-knob adjustment (plan 3.4 item 5). Guardrails, in order:
    known knob, integer value, hard range, single-step +-50% of the
    current value (a first set from None is exempt: there is no step
    to measure). One call moves exactly one knob; a second knob needs
    a second call and its own decision row. Returns the decision row;
    the config write happens here so the row and the value never part."""
    now_epoch = time.time() if now_epoch is None else now_epoch
    require(knob in KNOBS, '未知旋钮：' + str(knob))
    spec = KNOBS[knob]
    require(isinstance(new_value, int) and not isinstance(new_value, bool),
            '旋钮值须为整数：%r' % (new_value,))
    if spec['kind'] == 'int':
        require(new_value >= spec['min'],
                '旋钮值低于下限 %d：%d' % (spec['min'], new_value))
        if 'max' in spec:
            require(new_value <= spec['max'],
                    '旋钮值高于上限 %d：%d' % (spec['max'], new_value))
    else:
        require(new_value >= spec['min'],
                '旋钮值低于下限 %d：%d' % (spec['min'], new_value))
    cfg, _ = _read_config(store_dir)
    key = spec['config_key']
    old = cfg.get(key, KNOB_DEFAULTS[key])
    if old is not None:
        step = abs(new_value - old) / old if old else 0
        require(step <= TUNING_MAX_STEP_RATIO,
                '单次调整超护栏：%s 从 %s 到 %s（变动 %.0f%%，上限 50%%）'
                % (knob, old, new_value, step * 100))
    before_mean, before_n = _handoff_mean_in_window(
        store_dir, now_epoch - TUNING_WINDOW_DAYS * 86400, now_epoch)
    cfg[key] = new_value
    _write_config(store_dir, cfg)
    return _append(store_dir, TUNING_ROWS, {
        'kind': 'tune', 'knob': knob, 'old': old, 'new': new_value,
        'note': note, 'before_mean': before_mean, 'before_n': before_n,
        'after_mean': None, 'after_n': None, 'improved': None,
        'status': 'pending',
    }, now_epoch=now_epoch)


def evaluate_tuning(store_dir, now_epoch=None, force=False):
    """Evaluate the latest pending tune once its after-window closes
    (14 days after the tune, or immediately with force=True for tests).
    Improvement means the mean handoff cost fell in the after-window
    versus the before-window, with data on both sides. A tune with no
    data on either side stays pending: absence of evidence is not
    evidence of no improvement, and must not count toward rollback.
    Two consecutive not-improved tunes roll the knob back to the value
    before the streak and write the conclusion into lessons.md.
    Returns a dict describing what happened (possibly nothing)."""
    now_epoch = time.time() if now_epoch is None else now_epoch
    rows = tuning_rows(store_dir)
    pending = [r for r in rows if r.get('status') == 'pending'
               and r.get('kind') == 'tune']
    if not pending:
        return {'evaluated': False, 'reason': '无待评估的调整'}
    row = pending[-1]
    tune_at = row.get('at_epoch', 0)
    if not force and now_epoch < tune_at + TUNING_WINDOW_DAYS * 86400:
        return {'evaluated': False, 'reason': '后窗口未满两周'}
    after_mean, after_n = _handoff_mean_in_window(
        store_dir, tune_at, tune_at + TUNING_WINDOW_DAYS * 86400)
    before_mean, before_n = row.get('before_mean', 0), row.get('before_n', 0)
    if not before_n or not after_n:
        return {'evaluated': False, 'reason': '前后窗口缺接手数据，保持待评估'}
    improved = after_mean < before_mean
    row['after_mean'] = after_mean
    row['after_n'] = after_n
    row['improved'] = improved
    row['status'] = 'evaluated'
    _rewrite_tuning_rows(store_dir, rows)
    out = {'evaluated': True, 'knob': row['knob'], 'old': row['old'],
           'new': row['new'], 'before_mean': before_mean,
           'after_mean': after_mean, 'improved': improved,
           'rolled_back': False}
    if improved:
        return out
    streak = []
    for r in reversed(rows):
        if r.get('kind') == 'tune' and r.get('status') == 'evaluated' \
                and r.get('improved') is False:
            streak.append(r)
        elif r.get('kind') == 'tune' and r.get('status') == 'evaluated':
            break
        elif r.get('kind') == 'rollback':
            break
    if len(streak) < TUNING_NO_IMPROVE_STRIKES:
        out['strikes'] = len(streak)
        return out
    stable = streak[-1]['old']
    spec = KNOBS[row['knob']]
    cfg, _ = _read_config(store_dir)
    cfg[spec['config_key']] = stable
    _write_config(store_dir, cfg)
    lesson = _write_rollback_lesson(store_dir, row['knob'], stable, streak)
    _append(store_dir, TUNING_ROWS, {
        'kind': 'rollback', 'knob': row['knob'], 'old': row['new'],
        'new': stable, 'note': '连续两次调整无改善，回退到上一个稳定值',
        'lesson': lesson, 'status': 'done',
    }, now_epoch=now_epoch)
    out['rolled_back'] = True
    out['stable'] = stable
    out['lesson'] = lesson
    return out


def _rewrite_tuning_rows(store_dir, rows):
    d = _stats_dir(store_dir)
    d.mkdir(parents=True, exist_ok=True)
    path = d / TUNING_ROWS
    with open(path, 'w', encoding='utf-8', newline='') as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + '\n')


def _write_rollback_lesson(store_dir, knob, stable, streak):
    """The rollback conclusion goes into the formal lessons file (plan
    3.4 item 5: '把结论写进教训'), then the checksum manifest refreshes
    so the next resume does not report the archive as corrupt."""
    archive = Path(store_dir) / 'archive'
    path = archive / 'lessons.md'
    text = path.read_text(encoding='utf-8') if path.exists() else ''
    import re as _re
    ids = [int(m.group(1)) for m in _re.finditer(r'^## L-(\d+)', text, _re.MULTILINE)]
    lid = 'L-%04d' % (max(ids) + 1 if ids else 1)
    block = ('\n## %s\n- 内容：调优回退：旋钮 %s 连续两次调整无改善（%s），'
             '已回退到上一个稳定值 %s。\n- 原因可信程度：数据（两次评估无改善，见 stats/tuning.jsonl）\n'
             '- 来源：stats 调优回退\n'
             % (lid, knob,
                '、'.join('%s %s→%s' % (r['knob'], r['old'], r['new']) for r in streak),
                stable))
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'a', encoding='utf-8', newline='') as fh:
        fh.write(block)
    relay.update_checksum_manifest(Path(store_dir))
    return lid


# ------------------------------------------------------------------ fuse
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


def clear_fuse(store_dir, now_epoch=None):
    """Manual fuse reset (operator override). Records the clear; the
    next evaluation re-trips if the spike condition still holds."""
    state = evaluate_fuse(store_dir, now_epoch=now_epoch)
    state['tripped'] = False
    state['reason'] = '人工清除'
    state['cleared_at'] = utc()
    d = _stats_dir(store_dir)
    d.mkdir(parents=True, exist_ok=True)
    save(d / FUSE_STATE, state)
    return state


def fuse_tripped(store_dir, now_epoch=None):
    return bool(evaluate_fuse(store_dir, now_epoch=now_epoch).get('tripped'))
