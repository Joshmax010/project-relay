#!/usr/bin/env python3
"""project-relay v2 backfill extension registry (plan part 03, step 3.1).

Why this file exists (plan 3.1 item 7, core zero-change hard line):
the live registry is the hardcoded PARSERS dict inside
scripts/relay_backfill.py, which is core code. Step 3.1 forbids touching
it, so a new tool cannot be added to the live flow in this step. This
module is the extension registry: it IMPORTS the core parsers (read-only,
by value) and adds the step-3.1 parsers alongside them, in a NEW file
only. Importing this module MUST NOT mutate relay_backfill.PARSERS --
tests assert the core dict still holds exactly the v1 tools afterwards.

What this module does and does not do:
  - DOES: discover step-3.1 parsers through one explicit dict;
    assert interface completeness for every entry (v1 methods
    root/discover/sessions/events, plus S5 metadata for parsers that
    declare it); assert each declaring parser's fixture directory exists
    and parses (presence + parseability -- the BEHAVIOURAL fixture
    comparison lives in tests, not here).
  - DOES NOT: wire anything into the live `relay backfill` flow or the
    `relay recipe install` command. Both still use the hardcoded core
    lists. Wiring this registry into the live flow is a DEFERRED CORE
    CHANGE (it touches relay_backfill.py / relay.py) and is explicitly
    out of scope for step 3.1. Until then, Cursor backfill runs only
    through this module's API (get_parser('cursor')) and its tests.
  - S5 metadata note: the v1 parsers predate the S5 metadata attributes
    (their metadata lives in the v1 path table, not in code). The
    completeness assertion therefore requires S5 metadata only from
    parsers that declare it (currently: cursor). v1 entries are exempt
    with this note, not silently passed as S5-complete.

Handoff consequence (plan 3.1 item 3, fidelity rule): any handoff
output citing Cursor backfill results MUST carry the warning
'此工具的日志路径未经实机验证'. The warning is produced by
CursorParser.fidelity_warnings(); this module does not strip it.
"""

import importlib.util
import json
from pathlib import Path

_HERE = Path(__file__).resolve().parent


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


backfill_mod = _load('relay_backfill', _HERE / 'relay_backfill.py')
cursor_mod = _load('relay_cursor', _HERE / 'relay_cursor.py')
RelayError = backfill_mod.relay.RelayError

# Copy, never a reference: EXTENDED_PARSERS['cursor'] = ... must not
# leak back into backfill_mod.PARSERS.
EXTENDED_PARSERS = dict(backfill_mod.PARSERS)
EXTENDED_PARSERS['cursor'] = cursor_mod.CursorParser

_V1_METHODS = ('root', 'discover', 'sessions', 'events')
_S5_ATTRS = ('tool_id', 'display_name', 'log_locations', 'evidence_tier',
             'retention_note', 'capabilities')
# Fixture files a declaring parser must ship (presence + JSON parse).
_FIXTURE_FILES = ('workspace.json', 'workspace-db.json', 'global-db.json',
                  'expected-events.json')


def fixture_dir(parser_cls):
    """Fixture directory declared by a parser class, or None."""
    fn = getattr(parser_cls, 'fixture_dir', None)
    if callable(fn):
        return Path(fn())
    return None


def check_extended_registry(registry=None):
    """Completeness assertion for the extension registry. Raises
    RelayError naming the missing piece (S5 item 6: a half-registered
    tool must never start silently)."""
    registry = registry if registry is not None else EXTENDED_PARSERS
    for tool, cls in registry.items():
        if not (isinstance(cls, type)):
            raise RelayError('扩展注册表不完整：%s 缺解析器类' % tool)
        for method in _V1_METHODS:
            if not callable(getattr(cls, method, None)):
                raise RelayError('扩展注册表不完整：%s 缺 %s 方法'
                                 % (tool, method))
        if all(hasattr(cls, attr) for attr in _S5_ATTRS):
            # S5-declaring parser: metadata must be non-empty, and its
            # fidelity warnings must be callable.
            for attr in _S5_ATTRS:
                if getattr(cls, attr) in (None, '', [], {}):
                    raise RelayError('扩展注册表不完整：%s 的 S5 元数据 %s 为空'
                                     % (tool, attr))
            if not callable(getattr(cls, 'fidelity_warnings', None)):
                raise RelayError('扩展注册表不完整：%s 缺 fidelity_warnings'
                                 % tool)
        fdir = fixture_dir(cls)
        if fdir is not None:
            if not fdir.is_dir():
                raise RelayError('扩展注册表不完整：%s 缺 fixture 目录 %s'
                                 % (tool, fdir))
            for fname in _FIXTURE_FILES:
                fpath = fdir / fname
                if not fpath.is_file():
                    raise RelayError('扩展注册表不完整：%s 缺 fixture 文件 %s'
                                     % (tool, fpath))
                try:
                    with open(fpath, encoding='utf-8') as fh:
                        json.load(fh)
                except ValueError as exc:
                    raise RelayError('扩展注册表不完整：%s 的 fixture %s '
                                     '无法解析：%s' % (tool, fpath, exc))
    return True


def get_parser(tool):
    """Parser class for a tool id, or raise naming the unknown tool."""
    if tool not in EXTENDED_PARSERS:
        raise RelayError('未知回溯工具：%s' % tool)
    return EXTENDED_PARSERS[tool]


def list_tools():
    return sorted(EXTENDED_PARSERS)
