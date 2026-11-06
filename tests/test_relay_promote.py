"""v2 promotion channel tests (plan part 03, step 3.2).

Style follows the flow suite: real temporary directories, real Git repos,
store redirected with PROJECT_RELAY_HOME. No mocks: correction events are
real captures, lesson repeats go through the real summarize path, and
shelve expiry uses injected time (never a real 30-day wait).
"""

import contextlib
import importlib.util
import io
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SKILL = Path(__file__).resolve().parents[1]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


relay = _load('relay', SKILL / 'scripts/relay.py')
flow = _load('relay_flow', SKILL / 'scripts/relay_flow.py')
promote = _load('relay_promote', SKILL / 'scripts/relay_promote.py')
FlowRelayError = flow.relay.RelayError
PromoteRelayError = promote.relay.RelayError


def tree_bytes(root):
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in root.rglob('*') if p.is_file()}


class PromoteBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='relay-promote-')
        self.addCleanup(self.temp.cleanup)
        self.store = Path(self.temp.name) / 'store'
        self._env = mock.patch.dict(os.environ, {'PROJECT_RELAY_HOME': str(Path(self.temp.name))})
        self._env.start()
        self.addCleanup(self._env.stop)

    def make_repo(self, name, remote=None):
        root = Path(self.temp.name) / name
        root.mkdir()
        self.git(root, 'init', '-b', 'main')
        self.git(root, 'config', 'user.name', 'Tester')
        self.git(root, 'config', 'user.email', 'test@example.invalid')
        self.git(root, 'config', 'commit.gpgsign', 'false')
        (root / 'app.py').write_bytes('value = 1\n'.encode('utf-8'))
        self.git(root, 'add', '.')
        self.git(root, 'commit', '-m', 'baseline')
        if remote:
            self.git(root, 'remote', 'add', 'origin', remote)
        return root

    def git(self, root, *args, check=True):
        return subprocess.run(['git', '-C', str(root), *args],
                              capture_output=True, check=check)

    def call(self, fn, *args, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return fn(*args, **kwargs)

    def init(self, name='proj', remote=None):
        root = self.make_repo(name, remote=remote or f'https://github.com/example/{name}.git')
        self.call(relay.init, self.store, root)
        return root

    def store_dir(self, root):
        row = relay.where(self.store, path=root)['row']
        return self.store / 'projects' / (row['key'] + '-' + row['slug'])

    def edit(self, root, rel, text):
        (root / rel).write_bytes(text.encode('utf-8'))

    def correct_twice(self, root, cls='imports', msg1='导入必须用绝对路径', msg2='导入必须用绝对路径，别用相对导入'):
        """Two distinct correction events (distinct sessions + changed state,
        so capture dedup -- unchanged by design -- never merges them)."""
        self.edit(root, 'app.py', 'value = 2\n')
        first = flow.capture(self.store, root, session='s1', message=msg1,
                             correction_of=cls)
        self.edit(root, 'app.py', 'value = 3\n')
        second = flow.capture(self.store, root, session='s2', message=msg2,
                              correction_of=cls)
        return first, second


class TestCorrectionTrigger(PromoteBase):
    def test_second_correction_proposes_in_takeover_output(self):
        """Acceptance 3.2-1: after 2 corrections, the next takeover output
        carries one format-compliant proposal."""
        root = self.init()
        first, second = self.correct_twice(root)
        self.assertFalse(first.get('promotion', {}).get('proposed'))
        self.assertEqual(['P-0001'], second['promotion']['proposed'])
        sd = self.store_dir(root)
        pending = promote.list_pending(sd)
        self.assertEqual(1, len(pending))
        rendered = promote.render_section(sd)
        for part in ('提议内容', '证据链', '拟写入位置', 'diff 预览', '不采纳的后果'):
            self.assertIn(part, rendered)
        self.assertNotIn('评分', rendered)
        self.assertIn('P-0001', rendered)
        # Same block rides the resume output (fixed position, alongside the
        # five sections) and the handoff package body.
        result = flow.resume(self.store, root)
        self.assertEqual(5, len(result['sections']))
        self.assertIn('P-0001', result['promotion'])
        self.assertIn('P-0001', flow.render_resume(result))
        info = flow.make_handoff(self.store, root)
        self.assertIn('P-0001', Path(info['package']).read_text(encoding='utf-8'))

    def test_one_correction_does_not_propose(self):
        root = self.init()
        self.edit(root, 'app.py', 'value = 2\n')
        info = flow.capture(self.store, root, session='s1', message='导入必须用绝对路径',
                            correction_of='imports')
        self.assertEqual([], info['promotion']['proposed'])
        self.assertEqual([], promote.list_pending(self.store_dir(root)))


class TestAntiRepeat(PromoteBase):
    def test_reject_blocks_until_evidence_doubles(self):
        """Acceptance 3.2-2: rejected proposals never reappear before the
        fresh-evidence bar (2x threshold) is met, and re-proposals carry
        the old reason plus what changed."""
        root = self.init()
        self.correct_twice(root)
        sd = self.store_dir(root)
        promote.decide(sd, 'P-0001', 'reject', reason='这条规则太绝对，先不进指令')
        self.assertEqual([], promote.list_pending(sd))
        # Fresh evidence re-accumulates from zero after rejection and must
        # reach 2x the threshold (4 fresh) before re-proposal: counts 3-5
        # (fresh 1-3) stay blocked.
        for i, value in enumerate((4, 5, 6), start=3):
            self.edit(root, 'app.py', 'value = %d\n' % value)
            flow.capture(self.store, root, session='s%d' % i, message='导入必须用绝对路径',
                         correction_of='imports')
            self.assertEqual([], promote.refresh(sd, root)['proposed'])
            self.assertEqual([], promote.list_pending(sd))
        # Fourth fresh correction (6 total, 4 fresh) -- re-proposal allowed.
        # The capture hook itself creates it; refresh afterwards finds the
        # pending proposal and reports nothing new.
        self.edit(root, 'app.py', 'value = 7\n')
        flow.capture(self.store, root, session='s6', message='导入必须用绝对路径',
                     correction_of='imports')
        self.assertEqual([], promote.refresh(sd, root)['proposed'])
        pending = promote.list_pending(sd)
        self.assertEqual(['P-0002'], [p['id'] for p in pending])
        self.assertEqual(1, len(pending))
        self.assertEqual('这条规则太绝对，先不进指令', pending[0]['previous_reject_reason'])
        rendered = promote.render_proposal(pending[0])
        self.assertIn('上次否决理由', rendered)
        self.assertIn('新增证据', rendered)

    def test_reject_requires_reason_and_decide_twice_refused(self):
        root = self.init()
        self.correct_twice(root)
        sd = self.store_dir(root)
        with self.assertRaises(PromoteRelayError):
            promote.decide(sd, 'P-0001', 'reject')
        with self.assertRaises(PromoteRelayError):
            promote.decide(sd, 'P-0001', 'reject', reason='   ')
        promote.decide(sd, 'P-0001', 'reject', reason='暂不采纳')
        with self.assertRaises(PromoteRelayError):
            promote.decide(sd, 'P-0001', 'adopt')


class TestAdopt(PromoteBase):
    def test_adopt_writes_managed_section_only(self):
        """Acceptance 3.2-3: adopted content lands inside relay markers;
        everything outside the span is byte-identical."""
        root = self.init()
        target = Path(self.temp.name) / 'RULES.md'
        target.write_bytes('# 手册\n\n旧规则\n'.encode('utf-8'))
        before_outside = target.read_bytes()
        result = promote.record_formal_intent(
            self.store_dir(root), root, str(target), '提交前必须跑全部测试。',
            source_pointer='raw/cap-0001.md')
        self.assertEqual(1, len(result['proposal']['proposed']))
        pid = result['proposal']['proposed'][0]
        out = promote.decide(self.store_dir(root), pid, 'adopt')
        text = target.read_text(encoding='utf-8')
        self.assertIn('<!-- relay:begin promote-%s v1 ' % pid, text)
        self.assertIn('<!-- relay:end promote-%s -->' % pid, text)
        self.assertIn('提交前必须跑全部测试。', text)
        self.assertTrue(text.startswith('# 手册\n\n旧规则\n'))
        self.assertIn(before_outside.decode('utf-8'), text)
        decisions = (self.store_dir(root) / 'archive' / 'decisions.md').read_text(encoding='utf-8')
        self.assertIn('来源：用户明确要求', decisions)
        self.assertIn(pid, decisions)
        self.assertEqual(out['decision'], 'D-001')
        present, mismatches = relay.verify_checksum_manifest(self.store_dir(root))
        self.assertTrue(present)
        self.assertEqual([], mismatches)

    def test_adopt_revision_replaces_old_span(self):
        """A later proposal declaring `replaces` revises the whole old span
        instead of appending a duplicate section for one rule."""
        root = self.init()
        target = Path(self.temp.name) / 'RULES.md'
        target.write_bytes('# 手册\n'.encode('utf-8'))
        sd = self.store_dir(root)
        first = promote.record_formal_intent(sd, root, str(target), '旧规则。')
        pid1 = first['proposal']['proposed'][0]
        promote.decide(sd, pid1, 'adopt')
        second = promote.create_proposal(
            sd, trigger='formal_file', content='新规则。', target=str(target),
            evidence=[{'pointer': 'raw/cap-0002.md', 'summary': '新规则'}],
            consequence='不采纳：旧规则继续生效。', threshold=1,
            evidence_key='other-fingerprint', replaces=pid1)
        pid2 = second['created']
        self.assertIsNotNone(pid2)
        promote.decide(sd, pid2, 'adopt')
        text = target.read_text(encoding='utf-8')
        self.assertNotIn('旧规则。', text)
        self.assertIn('新规则。', text)
        self.assertEqual(1, text.count('relay:begin promote-'))
        self.assertTrue(text.startswith('# 手册\n'))


class TestShelve(PromoteBase):
    def test_shelve_expires_after_30_days_with_time_injection(self):
        """Acceptance 3.2-4: shelved proposals expire after 30 days
        (injected time, never a real wait) and then follow anti-repeat."""
        root = self.init()
        self.correct_twice(root)
        sd = self.store_dir(root)
        out = promote.decide(sd, 'P-0001', 'shelve')
        shelved_until = datetime.fromisoformat(out['shelved_until'])
        self.assertEqual([], promote.list_pending(sd))
        # Day 29: still shelved.
        self.assertEqual([], promote.expire_due(sd, now=shelved_until - timedelta(days=1))['expired'])
        # Day 31: expired, baseline re-counted at expiry (2 corrections).
        self.assertEqual(['P-0001'],
                         promote.expire_due(sd, now=shelved_until + timedelta(days=1))['expired'])
        self.assertEqual([], promote.list_pending(sd))
        # Fresh evidence 1..3 (< 2x2): still blocked.
        for i, value in enumerate((4, 5, 6), start=3):
            self.edit(root, 'app.py', 'value = %d\n' % value)
            flow.capture(self.store, root, session='s%d' % i, message='导入必须用绝对路径',
                         correction_of='imports')
            self.assertEqual([], promote.refresh(sd, root)['proposed'])
        # Fourth fresh correction (6 total): re-proposal, flagged as expired-shelve.
        # Again the capture hook creates it; refresh afterwards is quiet.
        self.edit(root, 'app.py', 'value = 7\n')
        flow.capture(self.store, root, session='s6', message='导入必须用绝对路径',
                     correction_of='imports')
        self.assertEqual([], promote.refresh(sd, root)['proposed'])
        pending = promote.list_pending(sd)
        self.assertEqual(['P-0002'], [p['id'] for p in pending])
        self.assertEqual('上次搁置到期自动失效', pending[0]['previous_reject_reason'])


class TestLessonTrigger(PromoteBase):
    def test_lesson_repeat_through_real_summaries(self):
        """Lesson trigger via the real summarize path: the same lesson
        content under 3 distinct entries proposes; 2 do not."""
        root = self.init()
        # No digits in the lesson line: the number whitelist check would
        # otherwise append "（未核实）" to some rounds and split the group.
        lesson = '## 教训\n- 提交前跑测试（证据：原料失败记录）\n'
        for i, value in enumerate((2, 3, 4), start=1):
            self.edit(root, 'app.py', 'value = %d\n' % value)
            flow.capture(self.store, root, session='s%d' % i)
            rec = flow.summarize(self.store, root, source='test', semantic_text=lesson)
            if i < 3:
                self.assertEqual([], rec['promotions']['proposed'])
            else:
                self.assertEqual(['P-0001'], rec['promotions']['proposed'])
        pending = promote.list_pending(self.store_dir(root))
        self.assertEqual(1, len(pending))
        self.assertEqual('lesson_repeat', pending[0]['trigger'])
        self.assertEqual(3, len(pending[0]['evidence']))


class TestQuoteDiscipline(PromoteBase):
    def test_preference_needs_verbatim_quote(self):
        root = self.init()
        sd = self.store_dir(root)
        with self.assertRaises(PromoteRelayError):
            promote.create_proposal(
                sd, trigger='formal_file', content='用户喜欢短函数', target=str(root / 'AGENTS.md'),
                evidence=[], consequence='无', content_kind='preference',
                origin='user_words', threshold=1, evidence_key='x')
        with self.assertRaises(PromoteRelayError):
            promote.create_proposal(
                sd, trigger='formal_file', content='用户喜欢短函数', target=str(root / 'AGENTS.md'),
                evidence=[], consequence='无', content_kind='preference',
                origin='assistant_proposal_accepted', quote='好', quote_session='s1',
                threshold=1, evidence_key='x')
        ok = promote.create_proposal(
            sd, trigger='formal_file', content='用户喜欢短函数', target=str(root / 'AGENTS.md'),
            evidence=[{'pointer': 'raw/cap-0001.md', 'summary': '用户说喜欢短函数'}],
            consequence='无', content_kind='preference', origin='user_words',
            quote='我喜欢短函数', quote_session='s1', threshold=1, evidence_key='x')
        self.assertEqual('P-0001', ok['created'])
        rendered = promote.render_proposal(promote.list_pending(sd)[0])
        self.assertIn('用户原话（s1）：我喜欢短函数', rendered)


class TestResumeContract(PromoteBase):
    def test_resume_keeps_five_sections_and_zero_write(self):
        """The promotion block rides alongside resume's five sections, not
        as a sixth one, and rendering writes nothing."""
        root = self.init()
        self.correct_twice(root)
        # Consume the correction entries first: resume catch-up-summarizes
        # pending entries (section 4 is the writing section), so a
        # zero-write assertion needs an empty queue -- exactly like the
        # original five-section test. The proposal survives summarizing.
        flow.summarize(self.store, root, source='test')
        sd = self.store_dir(root)
        before = tree_bytes(sd)
        result = flow.resume(self.store, root)
        self.assertEqual(5, len(result['sections']))
        self.assertTrue(result['sections'][0].startswith('一段'))
        self.assertTrue(result['sections'][4].startswith('五段'))
        self.assertEqual(before, tree_bytes(sd))
        self.assertIn('晋升提议', result['promotion'])
        self.assertIn('P-0001', flow.render_resume(result))
        self.assertEqual(before, tree_bytes(sd))
