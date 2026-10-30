#!/usr/bin/env python3
"""project-relay v2 backfill extension: Cursor parser (plan part 03, step 3.1).

Scope: Cursor ONLY. Gemini CLI, Copilot, Cline and siblings are NOT touched
here (plan 3.1: one tool per step, separately accepted).

Evidence status (read this before trusting any output from this module):
  - Hooks capability: CONFIRMED from Cursor's official docs
    (cursor.com/docs/hooks, fetched 2026-10-30): full lifecycle hooks
    (sessionStart/sessionEnd, preToolUse/postToolUse, beforeShellExecution,
    beforeReadFile/afterFileEdit, beforeSubmitPrompt, preCompact, stop),
    registered in <project>/.cursor/hooks.json or ~/.cursor/hooks.json.
    Recipe tier is therefore 甲档 (complete lifecycle hooks).
  - Storage layout: COMMUNITY sources only, NOT official Cursor docs.
    Cursor publishes no official document for its local chat storage
    layout (as of 2026-10-30). The dual-database shape below comes from
    community extractors (cursor-chronicle, cursor-composer-export,
    cursor-helper) and is UNVERIFIED on a real machine.
  - Retention: NO official retention policy found. A Cursor community
    forum report says cursorDiskKV grows with no cleanup (team aware, no
    ETA). That is a community report, not a policy: a missing period
    must be reported as "not found", never attributed to retention.
  - This parser has NEVER run against a real Cursor session. Its
    evidence_tier is '未验证' and every handoff output citing its results
    MUST carry the fidelity warning UNVERIFIED_WARNING (plan 3.1 item 3).

Design constraints (plan S5 + plan 3.1 item 7, core zero-change hard line):
  - This file is STANDALONE (stdlib only). It never imports relay_backfill
    or any other repo core module, so it cannot mutate the core registry
    by importing it. It mirrors the v1 BaseParser method shape
    (root/discover/sessions/events) so future wiring into the live flow
    is drop-in, and it additionally carries the S5 metadata attributes
    (tool_id/display_name/log_locations/evidence_tier/retention_note,
    fidelity_warnings(), capabilities) that the v1 parsers predate.
  - Read-only iron rule (S5 item 9): databases are opened with
    sqlite3 URI mode=ro. The parser never writes to any Cursor directory.
    Tests assert the database bytes are unchanged after a parse.
  - Attribution (S5 item 2): a session belongs to a project ONLY via the
    folder field inside workspace.json (file content). The
    workspaceStorage hash is NEVER reverse-mapped to a path. A workspace
    without a readable folder field is listed with cwd=None and is
    skipped by the caller's cwd strong check, exactly like v1.
  - Ordering: message order follows the composer's
    fullConversationHeadersOnly array, never SQLite rowid (community
    sources are explicit that rowid does not reflect chronology).
  - Version drift: community sources disagree on the workspace database
    table (cursorDiskKV vs ItemTable with key composer.composerData).
    Both are tried (cursorDiskKV first); if neither yields composers,
    the workspace contributes no sessions and records a parse error.
    Newer agent-format keys (agentKv:*, checkpointId:*) are NOT modelled
    in this step; they are reported as a fidelity gap, never silently
    dropped from the coverage statistics.

Dual-database shape (community sources, unverified):
  - <Cursor User>/workspaceStorage/<hash>/state.vscdb: composer metadata
    (cursorDiskKV keys composerData:<composerId>, or legacy ItemTable key
    composer.composerData holding {"allComposers": [...]}).
  - <Cursor User>/workspaceStorage/<hash>/workspace.json: {"folder": ...}.
  - <Cursor User>/globalStorage/state.vscdb: cursorDiskKV keys
    composerData:<composerId> (headers) and
    bubbleId:<composerId>:<bubbleId> (message content).
  - Composer fields: composerId, name, createdAt/lastUpdatedAt (ms),
    fullConversationHeadersOnly (ordered bubble ids).
  - Bubble fields: bubbleId, type (1=user, 2=AI), text, _v,
    toolFormerData (tool execution, subfield names unverified: read
    defensively), thinking/isThought, currentFileLocationData,
    relevantFiles, attachedCodeChunks, codebaseContextChunks, tokenCount.
  - Invalid bubbles: community extractors skip values of LENGTH <= 100
    (deleted/placeholder rows). This parser does NOT use that heuristic:
    it would also delete short legitimate messages (a one-line user
    reply serialises to well under 100 bytes). Instead a row is invalid
    only when it fails JSON parsing or lacks a usable speaker type
    (type 1/2). The deviation is deliberate and recorded here.
  - Platform User dirs: Linux ~/.config/Cursor/User,
    macOS ~/Library/Application Support/Cursor/User,
    Windows %APPDATA%/Cursor/User.
"""

import json
import os
import sqlite3
from pathlib import Path

TOOL_ID = 'cursor'
DISPLAY_NAME = 'Cursor'
EVIDENCE_TIER = '未验证'
RETENTION_NOTE = ('官方未公布保留策略；Cursor 社区论坛报告称 cursorDiskKV '
                  '无清理上限（团队已知、无修复时间表），该说法未验证；'
                  '缺失时段一律报“找不到”，不得归因于保留策略删除。')
UNVERIFIED_WARNING = '此工具的日志路径未经实机验证'

LOG_LOCATIONS = [
    '{cursor_user}/workspaceStorage/<hash>/state.vscdb',
    '{cursor_user}/workspaceStorage/<hash>/workspace.json',
    '{cursor_user}/globalStorage/state.vscdb',
]

CAPABILITIES = {
    # Tool executions are recorded (toolFormerData), but their exact
    # subfield layout is unverified, so tool names are best-effort.
    'has_tool_calls': True,
    # thinking / isThought fields exist in the community-documented schema.
    'has_reasoning': True,
    # Bubble file fields (relevantFiles, attached chunks) are CONTEXT
    # hints, not edit evidence: no verified edit-diff record exists in
    # the community schema. Change-file sets must therefore come from
    # git, not from bubble attribution.
    'has_file_attribution': False,
}

MAX_EVENTS_PER_SESSION = 200
MAX_TEXT_LEN = 2000
TRUNC_MARKER = '…（截断）'
_MAX_FILES_TOUCHED = 50


def fidelity_warnings():
    """S5 item 4: known incompleteness of this tool's logs, each with scope."""
    return [
        {'id': 'cursor-unverified-path',
         'text': UNVERIFIED_WARNING + '：存储布局来自社区抽取工具文档，'
                 '未经官方文档确认、未经实机验证。',
         'scope': '全部 Cursor 回溯结果'},
        {'id': 'cursor-dual-db',
         'text': '双库分离：workspace 库仅元数据与顺序，global 库才有正文；'
                 '任一库缺失则该会话不完整（正文为空并计缺口）。',
         'scope': '会话完整性'},
        {'id': 'cursor-header-order',
         'text': '消息顺序以 fullConversationHeadersOnly 为准，SQLite rowid '
                 '不代表时序；顺序数组缺失的会话无法还原对话顺序。',
         'scope': '会话内消息顺序'},
        {'id': 'cursor-agent-format',
         'text': '新版 agent 格式键（agentKv:*、checkpointId:*）未建模；'
                 '命中时按缺口报告，不静默忽略。',
         'scope': '新版 agent 会话'},
        {'id': 'cursor-thinking-not-surfaced',
         'text': 'thinking 气泡存在但不进入事件流，仅计数；推理过程不可复原。',
         'scope': '推理过程'},
        {'id': 'cursor-no-model-name',
         'text': '模型名不落盘，无法从日志确定会话使用的模型。',
         'scope': '模型归因'},
        {'id': 'cursor-file-context-not-edit',
         'text': '气泡文件字段只是上下文提示，不是改动证据；改动文件集'
                 '只能来自 git 交叉核对。',
         'scope': '改动文件集'},
        {'id': 'cursor-version-drift',
         'text': 'workspace 库表名在不同版本为 cursorDiskKV 或 ItemTable，'
                 '两者都尝试；皆无则该 workspace 按缺口处理。',
         'scope': '跨版本 workspace 库'},
    ]


def _truncate(text, limit=MAX_TEXT_LEN):
    if text is None:
        return ''
    text = str(text)
    if len(text) > limit:
        return text[:limit] + TRUNC_MARKER
    return text


def _classify(text, role):
    """Explicit-marker classification only, mirroring the v1 rule: a
    decision/lesson/todo is extracted ONLY when the text carries an
    explicit marker. Nothing is inferred from tone or topic."""
    stripped = (text or '').strip()
    if '决定：' in stripped or 'Decision:' in stripped:
        return 'decision_candidate'
    if stripped.startswith('教训：') or stripped.startswith('失败：'):
        return 'decision_candidate'
    if stripped.startswith('待办：') or stripped.startswith('TODO'):
        return 'decision_candidate'
    if role == 'user':
        return 'text'
    if role == 'assistant':
        return 'text'
    return 'text'


def _file_candidates(value):
    """Best-effort file paths from a bubble file-context field. Accepts
    plain strings or dicts with path/file/uri keys; anything else is
    ignored. Returns a list of strings (context hints, not edit proof)."""
    out = []
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for key in ('path', 'file', 'uri', 'fsPath'):
            if isinstance(value.get(key), str):
                out.append(value[key])
    elif isinstance(value, list):
        for item in value:
            out.extend(_file_candidates(item))
    return out


def _connect_ro(path):
    """Open a SQLite database strictly read-only (S5 item 9)."""
    return sqlite3.connect('file:%s?mode=ro' % str(path), uri=True)


def _read_kv(db_path, table):
    """Return {key: raw bytes} for a key-value table, or None if the
    table does not exist. Corrupt rows are skipped, never fatal."""
    try:
        conn = _connect_ro(db_path)
    except sqlite3.Error:
        return None
    try:
        try:
            rows = conn.execute(
                'SELECT key, value FROM "%s"' % table).fetchall()
        except sqlite3.Error:
            return None
        out = {}
        for key, value in rows:
            if not isinstance(key, str):
                continue
            if isinstance(value, str):
                value = value.encode('utf-8')
            if isinstance(value, (bytes, bytearray)):
                out[key] = bytes(value)
        return out
    finally:
        conn.close()


def _parse_json(raw):
    try:
        obj = json.loads(raw.decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        return None
    return obj if isinstance(obj, dict) else None


def _composers_from_workspace_db(db_path):
    """Return (composers, errors). Composers are dicts with composerId,
    name, createdAt, lastUpdatedAt, fullConversationHeadersOnly. Tries
    cursorDiskKV first, then the legacy ItemTable key (version drift)."""
    errors = 0
    kv = _read_kv(db_path, 'cursorDiskKV')
    composers = []
    if kv:
        for key, raw in kv.items():
            if not key.startswith('composerData:'):
                continue
            obj = _parse_json(raw)
            if obj is None or 'composerId' not in obj:
                errors += 1
                continue
            composers.append(obj)
    if composers:
        return composers, errors
    kv = _read_kv(db_path, 'ItemTable')
    if kv:
        raw = kv.get('composer.composerData')
        if raw is not None:
            try:
                obj = json.loads(raw.decode('utf-8'))
            except (ValueError, UnicodeDecodeError):
                errors += 1
                obj = None
            if isinstance(obj, dict):
                all_composers = obj.get('allComposers')
                if isinstance(all_composers, list):
                    for item in all_composers:
                        if isinstance(item, dict) and 'composerId' in item:
                            composers.append(item)
                        else:
                            errors += 1
    return composers, errors


def _platform_user_dir(home):
    """Default Cursor User dir for this platform, or None if unknown."""
    home = Path(home)
    if os.name == 'nt':
        appdata = os.environ.get('APPDATA')
        if appdata:
            return Path(appdata) / 'Cursor' / 'User'
        return home / 'AppData' / 'Roaming' / 'Cursor' / 'User'
    if os.name == 'posix':
        if Path('/Library').exists() and str(home).startswith('/Users/'):
            return home / 'Library' / 'Application Support' / 'Cursor' / 'User'
        return home / '.config' / 'Cursor' / 'User'
    return None


def _candidate_user_dirs(home):
    """User dirs to scan: home itself when it already looks like a User
    dir (tests pass the User dir directly), plus the platform default.
    Deduplicated, order-preserving."""
    out = []
    home = Path(home)
    if (home / 'workspaceStorage').is_dir() or (home / 'globalStorage').is_dir():
        out.append(home)
    default = _platform_user_dir(home)
    if default is not None and default not in out:
        out.append(default)
    return out


def _workspace_folder(ws_dir):
    """The folder field inside workspace.json (file content, never the
    storage hash). Returns None when absent or unreadable."""
    try:
        with open(ws_dir / 'workspace.json', encoding='utf-8') as fh:
            obj = json.load(fh)
    except (OSError, ValueError):
        return None
    if isinstance(obj, dict):
        folder = obj.get('folder')
        if isinstance(folder, str) and folder:
            return folder
    return None


class CursorParser:
    """Cursor backfill parser (S5 interface + v1 method shape).

    v1-compatible methods: root/discover/sessions/events.
    S5 metadata: class attributes tool_id/display_name/log_locations/
    evidence_tier/retention_note/capabilities + fidelity_warnings().
    """

    tool = 'cursor'
    tool_id = TOOL_ID
    display_name = DISPLAY_NAME
    log_locations = LOG_LOCATIONS
    evidence_tier = EVIDENCE_TIER
    retention_note = RETENTION_NOTE
    capabilities = CAPABILITIES

    @classmethod
    def fixture_dir(cls):
        """Synthetic fixture directory (see tests/fixtures/cursor/README.md).
        Declared so the extension registry can assert its presence."""
        return Path(__file__).resolve().parent.parent / 'tests' / 'fixtures' / 'cursor'

    @staticmethod
    def fidelity_warnings():
        """S5 item 4 (class-level delegate of the module function)."""
        return fidelity_warnings()

    def root(self, home):
        """Candidate User dirs (list: v1 root() returns one path, but
        Cursor needs home-as-User-dir plus the platform default)."""
        return _candidate_user_dirs(home)

    def discover(self, home):
        """Session candidates: {session_id, file, cwd, name, created_ms,
        updated_ms, bubbles, global_db, global_missing}. Missing or
        unreadable locations yield [] (never raise)."""
        out = []
        for user_dir in self.root(home):
            ws_root = user_dir / 'workspaceStorage'
            if not ws_root.is_dir():
                continue
            global_db = user_dir / 'globalStorage' / 'state.vscdb'
            for ws_dir in sorted(ws_root.iterdir()):
                db_path = ws_dir / 'state.vscdb'
                if not db_path.is_file():
                    continue
                cwd = _workspace_folder(ws_dir)
                composers, errors = _composers_from_workspace_db(db_path)
                for comp in composers:
                    headers = comp.get('fullConversationHeadersOnly')
                    if not isinstance(headers, list):
                        headers = []
                    out.append({
                        'tool': self.tool,
                        'session_id': comp['composerId'],
                        'name': comp.get('name'),
                        'cwd': cwd,
                        'file': str(db_path),
                        'created_ms': comp.get('createdAt'),
                        'updated_ms': comp.get('lastUpdatedAt'),
                        'bubbles': len(headers),
                        'headers': [h for h in headers if isinstance(h, str)],
                        'mtime': (comp.get('lastUpdatedAt') or 0) / 1000.0,
                        'global_db': str(global_db),
                        'global_missing': not global_db.is_file(),
                        'parse_errors': errors,
                    })
        return out

    def sessions(self, home):
        """discover() output, plus per-session gap counters initialised
        (filled in by events())."""
        sessions = self.discover(home)
        for sess in sessions:
            sess['missing_bubbles'] = 0
            sess['invalid_bubbles'] = 0
            sess['thinking_skipped'] = 0
        return sessions

    def events(self, session):
        """Normalized events in fullConversationHeadersOnly order.
        Each event: session_id, role, kind, text, tool, file,
        files_touched[], raw_ref, synthetic. Missing global DB yields []
        (the gap is visible in session['global_missing'])."""
        global_db = Path(session.get('global_db') or '')
        if not global_db.is_file():
            return []
        composer_id = session['session_id']
        kv = _read_kv(global_db, 'cursorDiskKV')
        if kv is None:
            session['invalid_bubbles'] = session.get('bubbles', 0)
            return []
        # Header order comes from the session record produced by
        # sessions()/discover() (the composer's fullConversationHeadersOnly
        # array). A session without headers cannot prove message order,
        # so it yields no events rather than an unordered guess.
        headers = session.get('headers')
        if not isinstance(headers, list):
            session['invalid_bubbles'] = session.get('bubbles', 0)
            return []
        events = []
        missing = invalid = thinking = 0
        for bubble_id in headers:
            if len(events) >= MAX_EVENTS_PER_SESSION:
                break
            raw = kv.get('bubbleId:%s:%s' % (composer_id, bubble_id))
            if raw is None:
                missing += 1
                continue
            bubble = _parse_json(raw)
            if bubble is None:
                invalid += 1
                continue
            if bubble.get('isThought'):
                thinking += 1
                continue
            btype = bubble.get('type')
            if btype == 1:
                role = 'user'
            elif btype == 2:
                role = 'assistant'
            else:
                invalid += 1
                continue
            text = bubble.get('text') or ''
            files_touched = []
            for field in ('relevantFiles', 'attachedCodeChunks',
                          'codebaseContextChunks'):
                files_touched.extend(_file_candidates(bubble.get(field)))
            loc = bubble.get('currentFileLocationData')
            files_touched.extend(_file_candidates(loc))
            seen = set()
            files_touched = [f for f in files_touched
                             if not (f in seen or seen.add(f))]
            files_touched = files_touched[:_MAX_FILES_TOUCHED]
            raw_ref = ('%s:bubbleId:%s:%s'
                       % (session.get('global_db'), composer_id, bubble_id))
            tool_data = bubble.get('toolFormerData')
            if isinstance(tool_data, dict):
                tool_name = None
                for key in ('name', 'toolName', 'tool'):
                    if isinstance(tool_data.get(key), str):
                        tool_name = tool_data[key]
                        break
                events.append({
                    'session_id': composer_id, 'role': role,
                    'kind': 'tool_call',
                    'text': _truncate(json.dumps(tool_data, ensure_ascii=False)),
                    'tool': tool_name,
                    'file': files_touched[0] if files_touched else None,
                    'files_touched': files_touched,
                    'raw_ref': raw_ref, 'synthetic': False,
                })
                continue
            events.append({
                'session_id': composer_id, 'role': role,
                'kind': _classify(text, role),
                'text': _truncate(text),
                'tool': None,
                'file': files_touched[0] if files_touched else None,
                'files_touched': files_touched,
                'raw_ref': raw_ref, 'synthetic': False,
            })
        session['missing_bubbles'] = missing
        session['invalid_bubbles'] = invalid
        session['thinking_skipped'] = thinking
        return events
