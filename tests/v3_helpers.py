"""Shared harness for the v3 test suite (batch 18).

Style: real temporary directories, real Git repos, no network, no real
project state. The v3 store lives at <project>/.project-relay/ -- there
is no registry, no PROJECT_RELAY_HOME, no global store root. Modules
are loaded by absolute path (importlib), like the old suites, so each
test file stays runnable on its own: ``python -m unittest
tests.test_v3_layout``.

Every v3 test file imports this module with:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from v3_helpers import V3Base, load, relay, flow, sync, stats, fork
"""
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]


def load(name, path):
    # Register before executing: the sibling modules reuse the
    # process-wide 'relay' copy from sys.modules (see their module
    # tops), so their RelayError is this harness's RelayError -- the
    # same identity the real CLI gets from relay.py's own top.
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


relay = load('relay', SKILL / 'scripts/relay.py')
flow = load('relay_flow', SKILL / 'scripts/relay_flow.py')
sync = load('relay_sync', SKILL / 'scripts/relay_sync.py')
stats = load('relay_stats', SKILL / 'scripts/relay_stats.py')
fork = load('relay_fork', SKILL / 'scripts/relay_fork.py')
RelayError = relay.RelayError

GOAL_TEXT = (
    '# 项目目标\n\n状态：已确认\n\n'
    '- 目标与交付物：做一个能跑的演示\n'
    '- 范围及不做事项：只做演示，不做部署\n'
    '- 约束与偏好（附来源）：用户说的\n'
    '- 验收标准：演示能跑起来\n'
    '- 最近核对时间：今天\n'
)


class V3Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='relay-v3-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / 'home'
        self.home.mkdir()

    def git(self, root, *args):
        return subprocess.run(['git', '-C', str(root), *args],
                              capture_output=True, check=True)

    def make_repo(self, name):
        root = Path(self.temp.name) / name
        root.mkdir(parents=True, exist_ok=True)
        self.git(root, 'init', '-b', 'main')
        self.git(root, 'config', 'user.name', 'Tester')
        self.git(root, 'config', 'user.email', 'test@example.invalid')
        self.git(root, 'config', 'commit.gpgsign', 'false')
        (root / 'app.py').write_text('value = 1\n', encoding='utf-8')
        self.git(root, 'add', '.')
        self.git(root, 'commit', '-m', 'baseline')
        return root

    def init(self, name='proj'):
        root = self.make_repo(name)
        relay.init(root)
        return root

    def store_dir(self, root):
        return relay.store_dir_for(root)

    def adir(self, root, agent):
        return self.store_dir(root) / 'agents' / agent

    def entries(self, root, agent):
        return flow.list_entries(self.adir(root, agent))

    def capture(self, root, agent='codex', **kw):
        return flow.capture(root, agent, **kw)

    def summarize(self, root, agent='codex', **kw):
        return flow.summarize(root, agent, **kw)

    def resume(self, root):
        return flow.resume(root)

    def resume_text(self, root):
        return flow.render_resume(self.resume(root))

    def confirm_goal(self, root, text=None):
        relay.goal_draft(root, text or GOAL_TEXT)
        return relay.goal_confirm(root)

    def cli(self, *args):
        """Run relay.main(argv) capturing stdout; returns (code, text)."""
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = relay.main([str(a) for a in args])
        return code, buf.getvalue()

    def change(self, root, rel, text):
        """Modify a repo file (so the next capture sees a change)."""
        p = Path(root) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding='utf-8')
        return p

    def fill_semantic(self, root, agent, detail='做了改动，因为需要，决定这样做，留下一个待验证点。'):
        """Fill the semantic section of the agent's latest entry in place
        (remove the placeholder marker, fill the four ### sections), the
        way a harness would. Returns the entry dict."""
        entries = self.entries(root, agent)
        assert entries, 'no entries for %s' % agent
        last = entries[-1]
        text = last['path'].read_text(encoding='utf-8')
        assert flow.SEMANTIC_PLACEHOLDER in text, 'entry has no placeholder'
        text = text.replace(flow.SEMANTIC_PLACEHOLDER, '')
        for section in ('做了什么', '为什么', '决定了什么', '留下什么尾巴'):
            text = text.replace('### %s\n' % section,
                                '### %s\n%s\n' % (section, detail), 1)
        last['path'].write_text(text, encoding='utf-8')
        return last
