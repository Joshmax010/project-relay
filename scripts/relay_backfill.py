#!/usr/bin/env python3
"""project-relay v2 backfill: retrospective archive building (plan 1.15).

Scope (detailed plan, part 02, step 1.15 ONLY). Reads other tools' local
session logs and rebuilds a first-pass project archive for a project that
never had one (or whose tool died before it could archive). Three parsers:
Claude Code, Codex (official), WorkBuddy. All facts about log locations come
from the verified path table (references: ~/workspace/洛瑶的接力参考项目/
实机验证/路径表v1.md, 2026-10-28, machine sunyu); nothing in this module
relies on an unverified community path.

Provenance (every major piece notes its plan step and source):
  - Parser registry + integrity assertion + unified aggregator + fidelity
    declaration: cli-continues (fusion #12, #13, #14).
  - Safety tiers (metadata first, bounded excerpt) + cwd strong check:
    planning-with-files catchup (fusion #16).
  - Data-not-instruction (scraped text is data, never executed):
    Handoff-CLI (fusion #30) and ai-science-toolkit recover-sessions.
  - WorkBuddy dual-source merge: VERIFIED FACT, not a borrowing
    (path table v1, 2026-10-28): projects/*.jsonl holds user-role messages
    and function_call records only; assistant text lives in traces/
    span_end nodes. A parser reading projects/ alone MUST report the
    assistant segment as a gap, never fabricate it.
  - Feeding parsed events into precompress/summarize: plan 1.15 interface
    (references/v2-flow.md "留给 1.15/1.16 的接口"); cursor second anchor
    (user_message) is filled by backfill entries (v2-flow deviation D2).
  - Entry format, redaction, atomic writes, transaction path: the existing
    flow module (plan 1.6/1.7/1.12); backfill never writes archive files
    directly except the clearly-labelled provenance document
    archive/backfill-fidelity.md (atomic write, new file, never one of the
    five managed files).

Safety tiers (plan 1.15):
  - metadata: session discovery + counts + cwd only. No body text is
    surfaced in any output (lines are read to find cwd fields and count
    them; their text is discarded, never stored).
  - excerpt: bounded event extraction (MAX_EVENTS_PER_SESSION events,
    MAX_TEXT_LEN chars each, truncated with a marker), redacted with the
    flow module's 14 patterns before anything is written to the store.
  - deep dive (older sessions beyond the limit, deeper archives) is listed
    as options in the report and NEVER executed unless deep=True -- the
    user nods first (grill rule: no deep dig without approval).

Environment override (tests only): callers pass an explicit log home
(--log-root); production defaults to Path.home(). The store root always
comes from the caller (relay --store-root / PROJECT_RELAY_HOME); this
module never computes a store root or touches ~/.project-relay/ directly.
"""
import importlib.util
import json
import os
import re
from pathlib import Path

_HERE = Path(__file__).resolve().parent


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


relay = _load('relay', _HERE / 'relay.py')
flow = _load('relay_flow', _HERE / 'relay_flow.py')

RelayError = relay.RelayError
require = relay.require
utc = relay.utc
digest = relay.digest
atomic = relay.atomic
save = relay.save
read_json = relay.read_json

MAX_EVENTS_PER_SESSION = 200
MAX_TEXT_LEN = 2000
DEFAULT_SESSION_LIMIT = 5
TRUNC_MARKER = '…（截断）'


def _norm_cwd(path):
    """Normalize a cwd for strong comparison (plan 1.15: never trust the
    directory slug; only the cwd recorded inside the events counts)."""
    if not path:
        return None
    try:
        return os.path.normcase(os.path.normpath(str(path)))
    except Exception:
        return None


def _truncate(text, limit=MAX_TEXT_LEN):
    if text is None:
        return ''
    text = str(text)
    if len(text) > limit:
        return text[:limit] + TRUNC_MARKER
    return text


def _read_jsonl(path):
    """Yield (record, error_count) pairs; malformed lines are counted and
    skipped, never fatal (a single corrupt line must not kill a backfill)."""
    errors = 0
    try:
        with open(path, encoding='utf-8', errors='replace') as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line), 0
                except Exception:
                    errors += 1
    except OSError:
        return
    if errors:
        yield None, errors


def _iter_jsonl(path):
    """Simpler iterator returning records only, plus an error count."""
    records = []
    errors = 0
    for record, err in _read_jsonl(path):
        if record is None:
            errors += err
        else:
            records.append(record)
    return records, errors


def _content_text(content):
    """Extract plain text from a Claude-style content list or string."""
    if content is None:
        return ''
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get('type') == 'text':
                parts.append(str(item.get('text', '')))
        return ''.join(parts)
    return ''


def _classify(text, role):
    """Explicit-marker classification only (plan 1.15, honesty rule): a
    decision/lesson/todo is extracted ONLY when the text carries an
    explicit marker. Nothing is inferred from tone or topic. A decision
    marker counts wherever it appears ('…决定：X…' is still an explicit
    decision statement, and the full text is preserved so the reader sees
    the context); lesson/todo markers must lead the text, because
    mid-sentence mentions of failure or TODOs are too ambiguous."""
    stripped = (text or '').strip()
    if '决定：' in stripped or 'Decision:' in stripped:
        return 'decision'
    if stripped.startswith('教训：') or stripped.startswith('失败：'):
        return 'lesson'
    if stripped.startswith('待办：') or stripped.startswith('TODO'):
        return 'todo'
    if role == 'user':
        return 'user_message'
    if role == 'assistant':
        return 'assistant_message'
    return 'other'


def _mk_event(session_id, role, kind, text, tool=None, file=None, source=''):
    return {'session_id': session_id, 'role': role, 'kind': kind,
            'text': _truncate(text), 'tool': tool, 'file': file,
            'source': source}


# ---------------------------------------------------------------- registry
class BaseParser:
    """Parser interface (plan 1.15, cli-continues #12). Every parser must
    implement discover/sessions/events; check_registry() asserts it at
    startup so a half-registered tool can never start silently."""
    tool = None

    def root(self, home):
        raise NotImplementedError

    def discover(self, home):
        raise NotImplementedError

    def sessions(self, home):
        raise NotImplementedError

    def events(self, session):
        raise NotImplementedError


def check_registry(registry=None):
    """Integrity assertion: every registered tool has a parser class
    implementing the full interface. Raises RelayError otherwise."""
    registry = registry if registry is not None else PARSERS
    for tool, cls in registry.items():
        require(isinstance(cls, type) and issubclass(cls, BaseParser),
                '回溯注册表不完整：%s 缺解析器类' % tool)
        for method in ('root', 'discover', 'sessions', 'events'):
            require(callable(getattr(cls, method, None)),
                    '回溯注册表不完整：%s 缺 %s 方法' % (tool, method))
    return True


# ---------------------------------------------------------------- parsers
class ClaudeCodeParser(BaseParser):
    """Claude Code (verified path table v1): <home>/.claude/projects/
    <slug>/<session>.jsonl. Record types observed: user/assistant message
    lines (message.content with text/tool_use/tool_result parts), plus
    file-history-snapshot lines (counted as file evidence only)."""
    tool = 'claude-code'

    def root(self, home):
        return Path(home) / '.claude' / 'projects'

    def discover(self, home):
        base = self.root(home)
        if not base.is_dir():
            return []
        return sorted(base.glob('*/*.jsonl'))

    def _scan(self, path):
        """Return (cwd, lines, file_history_count, parse_errors)."""
        records, errors = _iter_jsonl(path)
        cwd = None
        history = 0
        for rec in records:
            if cwd is None and isinstance(rec.get('cwd'), str):
                cwd = rec['cwd']
            if rec.get('type') == 'file-history-snapshot':
                history += 1
        return cwd, len(records), history, errors

    def sessions(self, home):
        out = []
        for path in self.discover(home):
            cwd, lines, history, errors = self._scan(path)
            out.append({'tool': self.tool, 'session_id': path.stem, 'cwd': cwd,
                        'file': str(path), 'lines': lines,
                        'mtime': path.stat().st_mtime,
                        'file_history': history, 'parse_errors': errors,
                        'traces': None, 'changes_files': 0})
        return out

    def events(self, session):
        records, _ = _iter_jsonl(Path(session['file']))
        events = []
        for rec in records:
            rtype = rec.get('type')
            if rtype not in ('user', 'assistant'):
                continue
            msg = rec.get('message') or {}
            content = msg.get('content', [])
            if isinstance(content, str):
                content = [{'type': 'text', 'text': content}]
            for item in content:
                if not isinstance(item, dict):
                    continue
                itype = item.get('type')
                if itype == 'text':
                    role = msg.get('role') or rtype
                    events.append(_mk_event(session['session_id'], role,
                                            _classify(item.get('text', ''), role),
                                            item.get('text', ''),
                                            source='claude-jsonl'))
                elif itype == 'tool_use':
                    events.append(_mk_event(session['session_id'], 'assistant',
                                            'tool_call', json.dumps(item.get('input', {}),
                                                                   ensure_ascii=False),
                                            tool=item.get('name'), source='claude-jsonl'))
                elif itype == 'tool_result':
                    events.append(_mk_event(session['session_id'], 'assistant',
                                            'tool_result', _content_text(item.get('content')),
                                            source='claude-jsonl'))
        return events[:MAX_EVENTS_PER_SESSION]


class CodexParser(BaseParser):
    """Codex official desktop (verified path table v1):
    <home>/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl. Top-level types:
    event_msg (payload: message/session_meta/...), response_item (payload:
    message/custom_tool_call/custom_tool_call_output/reasoning/...).
    User messages are double-written (event_msg + response_item): adjacent
    duplicates with identical role+text from different sources are collapsed
    to one event (verified, not an error)."""
    tool = 'codex'

    def root(self, home):
        return Path(home) / '.codex' / 'sessions'

    def discover(self, home):
        base = self.root(home)
        if not base.is_dir():
            return []
        return sorted(p for p in base.rglob('rollout-*.jsonl'))

    def _scan(self, path):
        records, errors = _iter_jsonl(path)
        cwd = None
        for rec in records:
            payload = rec.get('payload') or {}
            if cwd is None and isinstance(payload.get('cwd'), str):
                cwd = payload['cwd']
            if cwd is None and isinstance(rec.get('cwd'), str):
                cwd = rec['cwd']
        return cwd, len(records), errors

    def sessions(self, home):
        out = []
        for path in self.discover(home):
            cwd, lines, errors = self._scan(path)
            out.append({'tool': self.tool, 'session_id': path.stem, 'cwd': cwd,
                        'file': str(path), 'lines': lines,
                        'mtime': path.stat().st_mtime,
                        'file_history': 0, 'parse_errors': errors,
                        'traces': None, 'changes_files': 0})
        return out

    def events(self, session):
        records, _ = _iter_jsonl(Path(session['file']))
        raw = []
        for rec in records:
            rtype = rec.get('type')
            payload = rec.get('payload') or {}
            if rtype == 'event_msg':
                ptype = payload.get('type')
                if ptype == 'message':
                    role = payload.get('role')
                    text = payload.get('text') or _content_text(payload.get('content'))
                    raw.append((rtype, _mk_event(session['session_id'], role,
                                                _classify(text, role), text,
                                                source='codex-event_msg')))
                # session_meta / task_* / token_count: structural only.
            elif rtype == 'response_item':
                ptype = payload.get('type')
                if ptype == 'message':
                    role = payload.get('role')
                    text = payload.get('text') or _content_text(payload.get('content'))
                    raw.append((rtype, _mk_event(session['session_id'], role,
                                                _classify(text, role), text,
                                                source='codex-response_item')))
                elif ptype == 'custom_tool_call':
                    tool = payload.get('tool') or payload.get('name') or 'exec'
                    raw.append((rtype, _mk_event(session['session_id'], 'assistant',
                                                'tool_call', _truncate(json.dumps(payload, ensure_ascii=False)),
                                                tool=tool, source='codex-response_item')))
                elif ptype == 'custom_tool_call_output':
                    raw.append((rtype, _mk_event(session['session_id'], 'assistant',
                                                'tool_result', _truncate(json.dumps(payload, ensure_ascii=False)),
                                                source='codex-response_item')))
                elif ptype == 'reasoning':
                    raw.append((rtype, _mk_event(session['session_id'], 'assistant',
                                                'reasoning', payload.get('text', '') or payload.get('summary', ''),
                                                source='codex-response_item')))
        # Collapse adjacent double-written user messages (verified pattern).
        events = []
        for i, (src, ev) in enumerate(raw):
            if events and ev['kind'] == 'user_message' and events[-1]['kind'] == 'user_message' \
                    and events[-1]['text'] == ev['text'] and events[-1]['source'] != src:
                continue
            events.append(ev)
        return events[:MAX_EVENTS_PER_SESSION]


class WorkBuddyParser(BaseParser):
    """WorkBuddy (verified path table v1, 5.9.3): <home>/.workbuddy with
    projects/<slug>/<session>.jsonl, file-history/, workspace/sessions/
    modify_backup, changes-detail/<session>/cd_*.json, traces/<pid>/
    trace_*.jsonl, logs/.

    DUAL-SOURCE RULE (iron rule): projects/*.jsonl message records are ALL
    user role -- there is NO assistant text there. Assistant text lives
    ONLY in traces/ span_end nodes. The parser therefore merges:
      projects -> user messages, function_call, function_call_result,
                  reasoning, session-meta cwd, file-history evidence
      traces   -> assistant text from span_end nodes only
      changes-detail -> file_edit events from per-file diffs
    If traces are missing, assistant events are NOT synthesized: the
    session is flagged with an assistant gap (see scan() fidelity notes)."""
    tool = 'workbuddy'

    def root(self, home):
        return Path(home) / '.workbuddy'

    def discover(self, home):
        base = self.root(home) / 'projects'
        if not base.is_dir():
            return []
        return sorted(base.glob('*/*.jsonl'))

    def _trace_files(self, home):
        base = self.root(home) / 'traces'
        if not base.is_dir():
            return []
        return sorted(base.rglob('trace_*.jsonl'))

    def _scan(self, home, path):
        records, errors = _iter_jsonl(path)
        cwd = None
        history = 0
        for rec in records:
            if cwd is None and isinstance(rec.get('cwd'), str):
                cwd = rec['cwd']
            if rec.get('type') == 'file-history-snapshot':
                history += 1
        return cwd, len(records), history, errors

    def sessions(self, home):
        traces = self._trace_files(home)
        out = []
        for path in self.discover(home):
            cwd, lines, history, errors = self._scan(home, path)
            cd_dir = self.root(home) / 'changes-detail' / path.stem
            changes = len(list(cd_dir.glob('*.json'))) if cd_dir.is_dir() else 0
            out.append({'tool': self.tool, 'session_id': path.stem, 'cwd': cwd,
                        'file': str(path), 'lines': lines,
                        'mtime': path.stat().st_mtime,
                        'file_history': history, 'parse_errors': errors,
                        'traces': bool(traces), 'changes_files': changes,
                        '_home': str(home)})
        return out

    def _assistant_text(self, rec):
        node = rec.get('span_end') if isinstance(rec.get('span_end'), dict) else rec
        for key in ('assistant', 'text', 'output'):
            value = node.get(key)
            if isinstance(value, str) and value.strip():
                return value
        return _content_text(node.get('content'))

    def _trace_events(self, home, session):
        """Assistant events from traces/, attributed by session id when
        present; otherwise to the latest session of the same project (marked
        inferred -- see scan() fidelity notes)."""
        events = []
        for path in self._trace_files(home):
            records, _ = _iter_jsonl(path)
            for rec in records:
                is_end = rec.get('type') == 'span_end' or 'span_end' in rec
                if not is_end:
                    continue
                text = self._assistant_text(rec)
                if not text:
                    continue
                sid = rec.get('session_id') or rec.get('session') or rec.get('sessionId')
                node = rec.get('span_end') if isinstance(rec.get('span_end'), dict) else rec
                if sid is None:
                    sid = node.get('session_id') or node.get('session')
                if sid is not None and sid != session['session_id']:
                    continue
                ev = _mk_event(session['session_id'], 'assistant',
                               _classify(text, 'assistant'), text,
                               source='workbuddy-traces')
                ev['attribution'] = 'explicit' if sid is not None else 'inferred'
                events.append(ev)
        return events

    def events(self, session):
        home = session.get('_home')
        records, _ = _iter_jsonl(Path(session['file']))
        events = []
        for rec in records:
            rtype = rec.get('type')
            if rtype == 'message':
                role = rec.get('role', 'user')
                text = rec.get('text') or _content_text(rec.get('content'))
                events.append(_mk_event(session['session_id'], role,
                                        _classify(text, role), text,
                                        source='workbuddy-projects'))
            elif rtype == 'function_call':
                tool = rec.get('name') or rec.get('function') or rec.get('tool')
                params = rec.get('params') if isinstance(rec.get('params'), dict) else {}
                called_file = (params.get('file') or params.get('path')
                               or params.get('filepath') or params.get('filePath'))
                events.append(_mk_event(session['session_id'], 'assistant',
                                        'tool_call', _truncate(json.dumps(rec.get('params', rec), ensure_ascii=False)),
                                        tool=tool, file=called_file,
                                        source='workbuddy-projects'))
            elif rtype == 'function_call_result':
                events.append(_mk_event(session['session_id'], 'assistant',
                                        'tool_result', _truncate(json.dumps(rec, ensure_ascii=False)),
                                        source='workbuddy-projects'))
            elif rtype == 'reasoning':
                events.append(_mk_event(session['session_id'], 'assistant',
                                        'reasoning', rec.get('text', '') or rec.get('summary', ''),
                                        source='workbuddy-projects'))
        if home is not None:
            events.extend(self._trace_events(home, session))
            cd_dir = self.root(home) / 'changes-detail' / session['session_id']
            if cd_dir.is_dir():
                for path in sorted(cd_dir.glob('*.json')):
                    try:
                        obj = json.loads(path.read_text(encoding='utf-8'))
                    except Exception:
                        continue
                    files = obj.get('files') if isinstance(obj, dict) else None
                    if isinstance(files, list):
                        for item in files:
                            if isinstance(item, dict) and item.get('path'):
                                events.append(_mk_event(session['session_id'], 'assistant',
                                                        'file_edit', _truncate(item.get('diff', '')),
                                                        file=item['path'], source='workbuddy-changes'))
        return events[:MAX_EVENTS_PER_SESSION]


PARSERS = {'claude-code': ClaudeCodeParser,
           'codex': CodexParser,
           'workbuddy': WorkBuddyParser}


# ---------------------------------------------------------------- scan
def _match_sessions(home, toplevel, tools):
    """Discover + cwd-strong-filter sessions (plan 1.15). Directory slugs
    are NEVER used to infer the project: only the cwd recorded inside the
    events counts. Returns (matched, skipped, per_tool)."""
    check_registry()
    target = _norm_cwd(toplevel)
    matched, skipped, per_tool = [], [], {}
    for tool in tools:
        parser = PARSERS[tool]()
        found = parser.sessions(home)
        tool_matched, tool_skipped = [], []
        for sess in found:
            sess['_home'] = str(home)
            if sess.get('cwd') is None:
                tool_skipped.append({**sess, 'reason': '无 cwd 记录，无法强校验（跳过）'})
            elif _norm_cwd(sess['cwd']) != target:
                tool_skipped.append({**sess, 'reason': 'cwd 与当前项目不一致（他项目会话）'})
            else:
                tool_matched.append(sess)
        tool_matched.sort(key=lambda s: s['mtime'])
        matched.extend(tool_matched)
        skipped.extend(tool_skipped)
        per_tool[tool] = {'found': len(found), 'matched': len(tool_matched),
                          'skipped': [{'session_id': s['session_id'], 'reason': s['reason']}
                                      for s in tool_skipped]}
    matched.sort(key=lambda s: s['mtime'])
    return matched, skipped, per_tool


def _session_time_gap(matched):
    """Largest gap (days) between consecutive matched sessions, if any."""
    if len(matched) < 2:
        return 0.0
    worst = 0.0
    for prev, cur in zip(matched, matched[1:]):
        worst = max(worst, (cur['mtime'] - prev['mtime']) / 86400.0)
    return round(worst, 1)


def assess_gaps(report, goal_state):
    """Structured gap-impact assessment (plan 1.15). Categories: 目标/过程/
    决策/验证. Each gap: impact （高/中/低 + reason) and deep-dive options
    with cost tiers. Options are LISTED ONLY -- never executed here; the
    caller needs deep=True (the user's nod) to run them."""
    gaps = []
    tools = report['tools']
    wb = tools.get('workbuddy', {})
    if report.get('assistant_gap_sessions'):
        gaps.append({'category': '过程', 'item': 'WorkBuddy 会话缺少 traces：assistant 文本不可见',
                     'impact': '高', 'reason': '模型推理与回复缺失，接手方无法复原决策上下文',
                     'sessions': report['assistant_gap_sessions'],
                     'options': [{'action': '从 traces 更早归档或备份中补找', 'cost': '中'},
                                 {'action': '用户口述补录关键结论', 'cost': '低'}]})
    # Decision/verification gaps can only be asserted after bodies are read
    # (excerpt tier). In the metadata tier they are unknown, not absent, so
    # they are not listed -- claiming absence without reading would be a
    # false gap report.
    if report.get('tier') == 'excerpt' and not report.get('decision_events'):
        gaps.append({'category': '决策', 'item': '回溯事件中无显式决定标记',
                     'impact': '中', 'reason': '决策只能从改动推断，无法逐条回指原话',
                     'options': [{'action': '从提交信息与代码注释二次推断', 'cost': '低'}]})
    if report.get('tier') == 'excerpt' and not report.get('tool_result_events'):
        gaps.append({'category': '验证', 'item': '回溯事件中无工具执行结果信号',
                     'impact': '中', 'reason': '无法确认哪些改动真正跑通过',
                     'options': [{'action': '对当前工作区重跑冒烟测试', 'cost': '低'}]})
    if goal_state == 'none':
        gaps.append({'category': '目标', 'item': '项目无目标档案',
                     'impact': '中', 'reason': '回溯无法重建用户目标，需用户确认或口述',
                     'options': [{'action': '走目标提取流程建目标档案', 'cost': '低'}]})
    elif goal_state == 'tentative':
        gaps.append({'category': '目标', 'item': '目标仅为暂定状态',
                     'impact': '低', 'reason': '暂定目标不可当硬约束执行，接手时需复访',
                     'options': [{'action': '里程碑到达时复访确认', 'cost': '低'}]})
    worst = _session_time_gap(report.get('_matched_meta', []))
    if worst >= 7:
        gaps.append({'category': '过程', 'item': '匹配会话之间存在 %.1f 天时间缺口' % worst,
                     'impact': '中', 'reason': '缺口期的工作无任何工具日志可扒',
                     'options': [{'action': '深挖更早归档（--deep）', 'cost': '中'},
                                 {'action': '用户口述补录缺口期工作', 'cost': '低'}]})
    return gaps


def _fidelity_warnings(matched, per_tool, workbuddy_gap_sessions):
    warnings = []
    for tool, info in per_tool.items():
        if info['found'] and not info['matched']:
            warnings.append('%s：发现 %d 个会话，全部因 cwd 不符/无 cwd 被跳过（非本项目）'
                            % (tool, info['found']))
    for sess in matched:
        if sess['tool'] == 'workbuddy' and not sess.get('traces'):
            warnings.append('WorkBuddy 会话 %s：traces 缺失，assistant 文本不可见（双源规则）'
                            % sess['session_id'])
        if sess.get('parse_errors'):
            warnings.append('%s 会话 %s：%d 行解析失败，已跳过（不影响其余行）'
                            % (sess['tool'], sess['session_id'], sess['parse_errors']))
    for sid in workbuddy_gap_sessions:
        if not any(sid in w for w in warnings):
            warnings.append('WorkBuddy 会话 %s：assistant 文本无 traces 来源，标注为缺口' % sid)
    return warnings


# ---------------------------------------------------------------- backfill
_TEXT_KINDS = ('user_message', 'assistant_message', 'decision', 'lesson',
               'todo', 'reasoning')


def _entry_body(tool, session, events, files_changed):
    # Counts and excerpt sections are ROLE-based, not kind-based: a user
    # sentence classified as a decision is still user speech, and an
    # assistant sentence classified as a decision is still assistant text.
    # Counting by kind silently dropped classified speech from the excerpt.
    lines = ['# 回溯条目（来源：%s 会话 %s）\n' % (tool, session['session_id']),
             '## 确定性事实', '',
             '- 会话文件：' + session['file'],
             '- 事件数：%d（用户 %d / 助手 %d / 工具调用 %d）'
             % (len(events),
                sum(1 for e in events if e['role'] == 'user'
                    and e['kind'] in _TEXT_KINDS),
                sum(1 for e in events if e['role'] == 'assistant'
                    and e['kind'] in _TEXT_KINDS),
                sum(1 for e in events if e['kind'] == 'tool_call')),
             '- 改动文件：' + ('、'.join(sorted(files_changed)) if files_changed else '（无）'),
             '', '## 回溯摘录（已脱敏、有界）', '',
             '### 用户原话']
    for ev in events:
        if ev['role'] == 'user' and ev['kind'] in _TEXT_KINDS:
            lines.append('- ' + ev['text'].replace('\n', ' '))
    lines += ['', '### 模型回复']
    for ev in events:
        if ev['role'] == 'assistant' and ev['kind'] in _TEXT_KINDS:
            lines.append('- ' + ev['text'].replace('\n', ' '))
    lines += ['', '### 工具动作']
    for ev in events:
        if ev['kind'] in ('tool_call', 'tool_result', 'file_edit'):
            lines.append('- %s %s %s' % (ev['kind'], ev['tool'] or '', ev['file'] or ''))
    return '\n'.join(lines) + '\n'


def _build_semantic(report, aggregated):
    """Deterministic semantic text for the summarize channel (plan 1.12,
    zero model calls, semantic_auto=True). Only explicit markers become
    decisions/lessons/todos; everything else stays a coverage fact."""
    parts = ['## 已验证',
             '- 回溯覆盖 %d 个会话，事件 %d 条，改动文件 %d 个（证据：回溯报告 state/backfill-report.md）。'
             % (aggregated['sessions'], aggregated['events'], len(aggregated['files']))]
    if aggregated['decisions']:
        parts.append('## 决定')
        for text, sid in aggregated['decisions']:
            parts.append('- %s（来源：回溯会话 %s）' % (text, sid))
    if aggregated['lessons']:
        parts.append('## 教训')
        for text, sid in aggregated['lessons']:
            parts.append('- %s（原料：回溯会话 %s）' % (text, sid))
    if aggregated['todos']:
        parts.append('## 待办')
        for text, sid in aggregated['todos']:
            parts.append('- %s（Owner：Agent；验证：未指定）' % text)
    return '\n'.join(parts) + '\n'


def backfill(store, toplevel, home=None, tools=None, tier='excerpt',
             deep=False, session_limit=DEFAULT_SESSION_LIMIT,
             summarize_after=False):
    """Run a backfill (plan 1.15). tier='metadata' reports only and writes
    nothing except the report; tier='excerpt' writes bounded redacted raw
    entries. Deep-dive beyond the session limit requires deep=True."""
    home = Path(home).expanduser() if home else Path.home()
    tools = tools or list(PARSERS)
    for tool in tools:
        require(tool in PARSERS, '未知回溯工具：' + tool)
    try:
        row, store_dir = relay.resolve_store_dir(store, toplevel)
    except RelayError:
        # Foundation rule: a store is created only at init or backfill.
        relay.init(store, toplevel)
        row, store_dir = relay.resolve_store_dir(store, toplevel)
    toplevel = relay.git_toplevel(toplevel)
    matched, skipped, per_tool = _match_sessions(home, toplevel, tools)
    report = {'version': 1, 'at': utc(), 'project': str(toplevel),
              'tier': tier, 'deep': deep, 'tools': per_tool,
              'entries_created': [], 'already_backfilled': [],
              'deferred': [], 'fidelity': {}, 'gaps': [],
              'summary_seq': None, 'fidelity_doc': None,
              'assistant_gap_sessions': [], 'decision_events': 0,
              'tool_result_events': 0, '_matched_meta': matched}
    if tier == 'metadata':
        report['fidelity'] = {'sources': ['%s：%d/%d 个会话匹配'
                                          % (t, i['matched'], i['found'])
                                          for t, i in per_tool.items()],
                              'warnings': _fidelity_warnings(matched, per_tool, [])}
        report['gaps'] = assess_gaps(report, relay.goal_status(store, toplevel)[0])
        _save_report(store_dir, report)
        return report
    # Excerpt tier: process most recent N (chronological write order).
    work = matched if deep else matched[-session_limit:] if session_limit else matched
    deferred = [s for s in matched if s not in work]
    report['deferred'] = [{'tool': s['tool'], 'session_id': s['session_id'],
                           'reason': '超出会话上限（未执行深扒，需 --deep）'} for s in deferred]
    existing = flow.list_entries(store_dir, include_archived=True)
    backfilled_keys = {e['header'].get('backfill', {}).get('session_key')
                       for e in existing}
    aggregated = {'sessions': 0, 'events': 0, 'files': set(),
                  'decisions': [], 'lessons': [], 'todos': []}
    created = []
    for sess in work:
        key = '%s:%s' % (sess['tool'], sess['session_id'])
        if key in backfilled_keys:
            report['already_backfilled'].append(key)
            continue
        parser = PARSERS[sess['tool']]()
        events = parser.events(sess)
        # WorkBuddy assistant gap: no assistant narrative at all. Narrative
        # means assistant-role text in ANY explicit-marker bucket (a trace
        # sentence classified as a decision is still assistant text); tool
        # calls and user-role text do not count.
        if sess['tool'] == 'workbuddy':
            has_assistant = any(
                e['kind'] in ('assistant_message', 'decision', 'lesson', 'todo')
                and e['role'] == 'assistant' for e in events)
            if not has_assistant:
                report['assistant_gap_sessions'].append(sess['session_id'])
        files_changed = {e['file'] for e in events if e.get('file')}
        body, hits = flow.redact(_entry_body(sess['tool'], sess, events, files_changed))
        # Cursor anchor 2: the session's last user utterance, whatever
        # explicit-marker bucket it was classified into (a user sentence
        # classified as a decision is still a user message).
        user_msgs = [e['text'] for e in events
                     if e['role'] == 'user' and e['kind'] in _TEXT_KINDS]
        anchor_msg = _truncate(user_msgs[-1], 500) if user_msgs else None
        if anchor_msg is not None:
            anchor_msg, _ = flow.redact(anchor_msg)
        snap = flow.snapshot_v2(toplevel)
        seq = flow._next_seq(store_dir)
        prev = existing[-1] if existing else None
        header = {'version': 1, 'seq': seq, 'captured_at': utc(),
                  'tool': sess['tool'], 'session': sess['session_id'],
                  'anchor': snap['anchor'], 'fingerprint': snap,
                  'prev_seq': prev['header']['seq'] if prev else None,
                  'fact_hash': digest(json.dumps(
                      {'backfill': key, 'events': len(events)},
                      sort_keys=True, ensure_ascii=False).encode('utf-8')),
                  'queue': 'pending', 'redacted_count': hits,
                  'retries': 0, 'semantic': 'pending', 'degraded': False,
                  'source': 'backfill', 'user_message': anchor_msg,
                  'backfill': {'session_key': key, 'log_file': sess['file'],
                               'events': len(events)}}
        flow._write_entry(store_dir, header, body)
        created.append(seq)
        existing = flow.list_entries(store_dir, include_archived=True)
        aggregated['sessions'] += 1
        aggregated['events'] += len(events)
        aggregated['files'] |= files_changed
        for ev in events:
            if ev['kind'] == 'decision':
                aggregated['decisions'].append((ev['text'], sess['session_id']))
            elif ev['kind'] == 'lesson':
                aggregated['lessons'].append((ev['text'], sess['session_id']))
            elif ev['kind'] == 'todo':
                aggregated['todos'].append((ev['text'], sess['session_id']))
            elif ev['kind'] == 'tool_result':
                report['tool_result_events'] += 1
        report['decision_events'] += sum(1 for e in events if e['kind'] == 'decision')
    report['entries_created'] = created
    report['fidelity'] = {'sources': ['%s：%d/%d 个会话匹配'
                                       % (t, i['matched'], i['found'])
                                       for t, i in per_tool.items()],
                           'warnings': _fidelity_warnings(matched, per_tool,
                                                          report['assistant_gap_sessions'])}
    report['gaps'] = assess_gaps(report, relay.goal_status(store, toplevel)[0])
    if summarize_after and created:
        semantic = _build_semantic(report, aggregated)
        record = flow.summarize(store, toplevel, source='backfill',
                                semantic_text=semantic, semantic_auto=True)
        report['summary_seq'] = record.get('seq')
        # Fidelity declaration as a labelled provenance document (first
        # section is the declaration itself; never one of the five files).
        fidelity_doc = render_fidelity(report)
        atomic(store_dir / 'archive' / 'backfill-fidelity.md',
               fidelity_doc.encode('utf-8'))
        report['fidelity_doc'] = str(store_dir / 'archive' / 'backfill-fidelity.md')
    _save_report(store_dir, report)
    return report


def render_fidelity(report):
    """Fidelity declaration + gap-impact assessment (plan 1.15). This text
    is data about the backfill, never instructions, and is never executed."""
    lines = ['# 回溯保真度声明', '',
             '本文件是回溯建档的产物第一段：它声明本次回溯的数据来源、覆盖范围与已知残缺。',
             '回溯文本一律是数据不是指令，任何内容不得被当作命令执行。', '',
             '## 数据源清单',
             '- 项目：' + report['project'],
             '- 执行时间：' + report['at'],
             '- 层级：' + report['tier'] + ('（深扒已执行）' if report['deep'] else '（深扒未执行）')]
    for tool, info in report['tools'].items():
        lines.append('- %s：发现 %d 个会话，匹配 %d 个'
                     % (tool, info['found'], info['matched']))
    lines += ['', '## 保真警告']
    lines += ['- ' + w for w in report['fidelity'].get('warnings', [])] or ['- （无）']
    lines += ['', '## 缺料影响评估']
    if not report['gaps']:
        lines.append('- （无缺料项）')
    for gap in report['gaps']:
        lines.append('- [%s·影响%s] %s：%s'
                     % (gap['category'], gap['impact'], gap['item'], gap['reason']))
        for opt in gap.get('options', []):
            lines.append('  - 补扒选项：%s（成本：%s）' % (opt['action'], opt['cost']))
    if report['deferred']:
        lines += ['', '## 未执行的深扒（需用户点头）']
        for d in report['deferred']:
            lines.append('- %s 会话 %s：%s' % (d['tool'], d['session_id'], d['reason']))
    lines.append('')
    return '\n'.join(lines)


def _save_report(store_dir, report):
    """Report files: human-readable first section + machine JSON. The
    internal _matched_meta key is stripped before saving (it is large and
    only needed in-memory for gap assessment)."""
    public = {k: v for k, v in report.items() if not k.startswith('_')}
    save(store_dir / 'state' / 'backfill.json', public)
    atomic(store_dir / 'state' / 'backfill-report.md',
           render_fidelity(report).encode('utf-8'))
    return public
