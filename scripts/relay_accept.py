#!/usr/bin/env python3
"""project-relay v2 acceptance: the five-act trial script (plan 1.16).

Runs the whole volume-1 chain end to end in a throwaway trial project:
  Act 1  建仓建目标 (confirmed / tentative / declined goal states)
  Act 2  A 干活 (Claude Code simulated via capture; change-scale trigger;
         dead-end four fields into decisions)
  Act 3  B 接手 (resume five sections + smoke + continued growth)
  Act 4  弄死 A 回溯补档 (killed WorkBuddy session fixtures -> backfill
         -> fidelity declaration + gap assessment, no hallucination)
  Act 5  目标防线与脱敏 (tampered goal stops injection; secret-bearing
         backfill material is redacted, zero occurrences in the store)

Everything runs in temporary directories. The store root and the fake tool
log home are passed in explicitly; this script NEVER touches the real
~/.project-relay/, ~/.claude, ~/.codex or ~/.workbuddy. Real tools are
simulated: Act 2/3 drive the real relay commands, Act 4 drives the real
backfill parser over fixture logs shaped like the verified path table
(2026-10-28). No model calls are made anywhere: semantic increments are
script-supplied text (counted honestly as 1 simulated agent call in Act 2)
or deterministic assembly (0 calls).

Output: a five-column record (幕/场景/通过标准/实测结果/证据指针) plus a
cost ledger. Exit code 0 iff every act passes; otherwise 1.
"""
import argparse
import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


relay = _load('relay', _HERE / 'relay.py')
flow = _load('relay_flow', _HERE / 'relay_flow.py')
backfill_mod = _load('relay_backfill', _HERE / 'relay_backfill.py')

FlowRelayError = flow.relay.RelayError
# NOTE: each importlib load creates its own relayctl copy, so relay-raised
# errors and flow-raised errors are different class objects; catch both.
RelayErrors = (relay.RelayError, flow.relay.RelayError)
GOAL_TEXT = ('状态：已建\n\n- 目标：试验项目接力验收\n'
             '- 约束与偏好（附来源）：Python 标准库 only（来源：档案既有）\n'
             '- 验收标准：测试全过\n- 最近核对时间：未核对\n')
SECRET = 'AKIAIOSFODNN7EXAMPLE'  # AWS documentation example key, not real.


def _write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w', encoding='utf-8', newline='') as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + '\n')


def claude_session_lines(cwd, messages):
    lines = []
    for item in messages:
        if item[0] == 'tool':
            _tag, name, payload = item
            lines.append({'type': 'assistant', 'cwd': cwd,
                          'message': {'role': 'assistant', 'content': [
                              {'type': 'tool_use', 'name': name, 'input': payload}]}})
        else:
            role, text = item
            lines.append({'type': role, 'cwd': cwd,
                          'message': {'role': role, 'content': [
                              {'type': 'text', 'text': text}]}})
    return lines


def codex_rollout_lines(cwd, messages):
    lines = [{'type': 'event_msg', 'payload': {'type': 'session_meta', 'cwd': cwd}}]
    for item in messages:
        if item[0] == 'tool':
            _tag, name, payload = item
            lines.append({'type': 'response_item', 'payload': {
                'type': 'custom_tool_call', 'tool': name, 'args': payload}})
        else:
            role, text = item
            lines.append({'type': 'event_msg', 'payload': {
                'type': 'message', 'role': role, 'text': text}})
    return lines


def workbuddy_session_lines(cwd, messages):
    lines = []
    for item in messages:
        if item[0] == 'tool':
            _tag, name, payload = item
            lines.append({'type': 'function_call', 'cwd': cwd, 'name': name,
                          'params': payload})
            lines.append({'type': 'function_call_result', 'cwd': cwd,
                          'name': name, 'output': 'ok'})
        else:
            role, text = item
            lines.append({'type': 'message', 'cwd': cwd, 'role': role, 'text': text})
    lines.append({'type': 'session-meta', 'cwd': cwd})
    return lines


def workbuddy_trace_lines(session_id, assistant_texts):
    return [{'type': 'span_end', 'session_id': session_id,
             'span_end': {'assistant': text}} for text in assistant_texts]


class Runner:
    def __init__(self, store, workdir, log_home):
        self.store = Path(store)
        self.work = Path(workdir)
        self.log_home = Path(log_home)
        self.acts = []
        self.model_calls = 0

    def git(self, root, *args):
        return subprocess.run(['git', '-C', str(root), *args],
                              capture_output=True, check=True)

    def make_repo(self, name, remote=None):
        root = self.work / name
        root.mkdir(parents=True, exist_ok=True)
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

    def store_dir(self, root):
        row = relay.where(self.store, path=root)['row']
        return self.store / 'projects' / (row['key'] + '-' + row['slug'])

    def act(self, act, scene, criterion, passed, evidence):
        self.acts.append({'act': act, 'scene': scene, 'criterion': criterion,
                          'result': '通过' if passed else '未通过',
                          'evidence': evidence})

    def act1(self):
        confirmed = self.make_repo(
            'proj-confirmed', 'https://github.com/example/proj-confirmed.git')
        relay.init(self.store, confirmed)
        relay.goal_draft(self.store, confirmed, GOAL_TEXT)
        relay.goal_confirm(self.store, confirmed)
        state, _ = relay.goal_check(self.store, confirmed)
        ok_conf = (state == 'confirmed'
                   and '目标：试验项目接力验收' in relay.goal_inject(self.store, confirmed))
        self.act('一', '建仓建目标·已确认', 'goal_check 为 confirmed，注入返回目标正文无水印',
                 ok_conf, 'goal_check=%s' % state)
        tentative = self.make_repo(
            'proj-tentative', 'https://github.com/example/proj-tentative.git')
        relay.init(self.store, tentative)
        relay.goal_draft(self.store, tentative, GOAL_TEXT)
        relay.goal_confirm(self.store, tentative, tentative=True)
        ok_tent = relay.TENTATIVE_WATERMARK in relay.goal_inject(self.store, tentative)
        self.act('一', '建仓建目标·暂定', '注入带暂定水印，不可当硬约束',
                 ok_tent, 'watermark present=%s' % ok_tent)
        declined = self.make_repo(
            'proj-declined', 'https://github.com/example/proj-declined.git')
        relay.init(self.store, declined)
        relay.goal_decline(self.store, declined)
        ok_decl = relay.goal_inject(self.store, declined) == '本项目无目标档案。\n'
        self.act('一', '建仓建目标·拒绝建目标', '注入明示无目标档案，不反复追问',
                 ok_decl, 'inject=%r' % relay.goal_inject(self.store, declined))
        return confirmed

    def act2(self, proj):
        for i in range(10):
            (proj / ('mod_%02d.py' % i)).write_bytes(('# module %d\n' % i).encode('utf-8'))
        (proj / 'app.py').write_bytes('value = 2\n'.encode('utf-8'))
        flow.capture(self.store, proj, tool='claude-code', session='act2')
        enf = flow.enforce(self.store, proj, tool='claude-code', session='act2')
        ok_enforce = enf.get('action') == 'block'
        scales = flow.scales(self.store, proj)
        change_row = [r for r in scales['rows'] if r['scale'] == 'change'][0]
        ok_scales = change_row['reached'] is True
        semantic = ('## 已验证\n- 试验项目改动 11 个文件（验证：改动量刻度已到线）。\n'
                    '## 决定\n- 放弃缓存方案：试法 内存缓存；为何试 降低延迟；'
                    '失败于 压测命中率不足（证据：压测输出）；重试条件 命中率模型变化后。\n')
        record = flow.summarize(self.store, proj, source='act2',
                                semantic_text=semantic)
        self.model_calls += record.get('model_calls', 0)
        store_dir = self.store_dir(proj)
        decisions = (store_dir / 'archive' / 'decisions.md').read_text(encoding='utf-8')
        digest_ok = (store_dir / 'state' / 'digest.json').exists()
        summary_ok = (store_dir / 'state' / 'summaries' / ('%04d.json' % record['seq'])).exists()
        ok_chain = (digest_ok and summary_ok and '放弃缓存方案' in decisions
                    and '重试条件' in decisions)
        self.act('二', 'A 干活·执法链', '语义占位时 Stop 拦截（执法链工作）',
                 ok_enforce, 'enforce action=%s' % enf.get('action'))
        self.act('二', 'A 干活·改动量刻度', '改动量刻度到线（文件 ≥10）',
                 ok_scales, 'change row=%s' % change_row)
        self.act('二', 'A 干活·四级产物', '原料/整理稿/汇总记录/档案增量齐全且可回指；死胡同四字段进决策',
                 ok_chain, 'summary seq=%s' % record.get('seq'))
        return record

    def act3(self, proj):
        store_dir = self.store_dir(proj)
        cfg = flow.load_config(store_dir)
        cfg['smoke_command'] = sys.executable + ' -c "import sys; sys.exit(0)"'
        flow.save_config(store_dir, cfg)
        result = flow.resume(self.store, proj, tool='codex')
        ok_resume = (not result['no_archive'] and len(result['sections']) == 5
                     and '通过' in result['sections'][4] and not result.get('banner'))
        before = len(list((store_dir / 'state' / 'summaries').glob('*.json')))
        flow.capture(self.store, proj, tool='codex', session='act3')
        flow.summarize(self.store, proj, source='act3', semantic_auto=True)
        after = len(list((store_dir / 'state' / 'summaries').glob('*.json')))
        self.act('三', 'B 接手·五段报告', 'resume 五段齐全、冒烟通过、无带病横幅',
                 ok_resume, 'sections=%d banner=%s' % (len(result['sections']), result.get('banner')))
        self.act('三', 'B 接手·继续生长', 'Codex 收尾 capture 后汇总，档案继续生长',
                 after > before, 'summaries %d -> %d' % (before, after))

    def act4(self, proj):
        wb = self.log_home / '.workbuddy'
        proj_str = str(proj)
        dead = wb / 'projects' / ('c-' + proj.name) / 'wb-dead.jsonl'
        _write_jsonl(dead, workbuddy_session_lines(proj_str, [
            ('user', '把 app.py 的 value 改成 3，并记录决定：采用单例模式'),
            ('tool', 'Write', {'file': 'app.py'}),
            ('tool', 'Edit', {'file': 'app.py'}),
        ]))
        traces = wb / 'traces' / '1001'
        _write_jsonl(traces / 'trace_a.jsonl', workbuddy_trace_lines(
            'wb-dead', ['已把 value 改成 3。决定：采用单例模式，因为全局只需一份配置。']))
        cd = wb / 'changes-detail' / 'wb-dead'
        cd.mkdir(parents=True, exist_ok=True)
        (cd / 'cd_1.json').write_bytes(json.dumps(
            {'files': [{'path': 'app.py', 'diff': '-value = 2\n+value = 3\n'}]}).encode('utf-8'))
        (proj / 'app.py').write_bytes('value = 3\n'.encode('utf-8'))
        notraces = wb / 'projects' / ('c-' + proj.name) / 'wb-notraces.jsonl'
        _write_jsonl(notraces, workbuddy_session_lines(proj_str, [
            ('user', '再改一个无关文件'),
            ('tool', 'Write', {'file': 'notes.txt'}),
        ]))
        foreign = wb / 'projects' / 'c-other' / 'wb-foreign.jsonl'
        _write_jsonl(foreign, workbuddy_session_lines('/other/project', [
            ('user', '他项目的工作'),
        ]))
        report = backfill_mod.backfill(self.store, proj, home=self.log_home,
                                       tools=['workbuddy'], tier='excerpt',
                                       summarize_after=True)
        store_dir = self.store_dir(proj)
        decisions = (store_dir / 'archive' / 'decisions.md').read_text(encoding='utf-8')
        fidelity = store_dir / 'archive' / 'backfill-fidelity.md'
        ok_created = len(report['entries_created']) == 2
        skipped_ids = [s['session_id'] for s in report['tools']['workbuddy']['skipped']]
        ok_foreign = (report['tools']['workbuddy']['matched'] == 2
                      and 'wb-foreign' in skipped_ids)
        ok_gap = 'wb-notraces' in report['assistant_gap_sessions']
        ok_decision = '采用单例模式' in decisions
        ok_fidelity = fidelity.exists()
        self.act('四', '弄死 A 回溯补档·条目', '两段本项目会话建条目，他项目会话被跳过',
                 ok_created and ok_foreign,
                 'created=%s matched=%s' % (report['entries_created'],
                                            report['tools']['workbuddy']['matched']))
        self.act('四', '弄死 A 回溯补档·缺口不脑补', '无 traces 会话的 assistant 段列入缺口，不拼凑',
                 ok_gap, 'assistant_gap_sessions=%s' % report['assistant_gap_sessions'])
        self.act('四', '弄死 A 回溯补档·建档', '决定进决策记录，保真度声明落盘，汇总完成',
                 ok_decision and ok_fidelity and report['summary_seq'] is not None,
                 'summary_seq=%s fidelity=%s' % (report['summary_seq'], fidelity.exists()))
        rerun = backfill_mod.backfill(self.store, proj, home=self.log_home,
                                      tools=['workbuddy'], tier='excerpt')
        self.act('四', '弄死 A 回溯补档·幂等', '重跑回溯不重复建条目',
                 rerun['entries_created'] == [] and len(rerun['already_backfilled']) == 2,
                 'already=%s' % rerun['already_backfilled'])

    def act5(self, proj):
        store_dir = self.store_dir(proj)
        goal_path = store_dir / 'archive' / 'goal.md'
        original = goal_path.read_text(encoding='utf-8')
        goal_path.write_bytes((original + '被篡改的一行\n').encode('utf-8'))
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                relay.goal_check(self.store, proj)
            tamper_blocked = False
        except RelayErrors:
            tamper_blocked = True
        flag = '目标待确认'
        inject_text = relay.goal_inject(self.store, proj)
        ok_tamper = tamper_blocked and flag in inject_text
        self.act('五', '目标防线·篡改停注', '目标被改后 goal_check 拦截、注入只给只读副本',
                 ok_tamper, 'blocked=%s flag=%s' % (tamper_blocked, flag in inject_text))
        relay.goal_draft(self.store, proj, original)
        relay.goal_confirm(self.store, proj)
        codex_home = self.log_home / '.codex'
        session_file = codex_home / 'sessions' / '2026' / '10' / '28' / 'rollout-secret.jsonl'
        _write_jsonl(session_file, codex_rollout_lines(str(proj), [
            ('user', '我的密钥是 %s 请记住' % SECRET),
        ]))
        report = backfill_mod.backfill(self.store, proj, home=self.log_home,
                                       tools=['codex'], tier='excerpt')
        created = report['entries_created']
        leaked = False
        redacted_hits = 0
        for path in store_dir.rglob('*'):
            if path.is_file():
                try:
                    data = path.read_bytes()
                except OSError:
                    continue
                if SECRET.encode() in data:
                    leaked = True
        for seq in created:
            header, _body = flow._parse_entry(store_dir / 'raw' / ('cap-%04d.md' % seq))
            redacted_hits += header.get('redacted_count', 0)
        self.act('五', '目标防线·脱敏', '带密钥原料落盘已脱敏，全仓零检出',
                 (not leaked) and redacted_hits > 0,
                 'leaked=%s redacted_count=%d' % (leaked, redacted_hits))


def run_acceptance(store=None, workdir=None, log_home=None, keep_dir=False):
    """Run all five acts. Returns the record dict (no printing).

    A None store means a fresh throwaway store (same isolation as workdir
    and log_home): acceptance must never inherit a previous run's store,
    or fixture projects keyed by fixed remote URLs collide across runs."""
    if store is None:
        store = Path(tempfile.mkdtemp(prefix='relay-accept-store-'))
    store = Path(store)
    tmp = None
    if workdir is None or log_home is None:
        tmp = tempfile.TemporaryDirectory(prefix='relay-accept-')
        base = Path(tmp.name)
        workdir = Path(workdir) if workdir else base / 'work'
        log_home = Path(log_home) if log_home else base / 'loghome'
        workdir.mkdir(parents=True, exist_ok=True)
        log_home.mkdir(parents=True, exist_ok=True)
    runner = Runner(store, workdir, log_home)
    try:
        confirmed = runner.act1()
        runner.act2(confirmed)
        runner.act3(confirmed)
        runner.act4(confirmed)
        runner.act5(confirmed)
    finally:
        if tmp is not None and not keep_dir:
            tmp.cleanup()
    inject_estimate = 0
    try:
        info = flow.inject(runner.store, confirmed)
        inject_estimate = info.get('tokens_estimate', 0)
    except Exception:
        pass
    verdict = 'PASS' if all(a['result'] == '通过' for a in runner.acts) else 'FAIL'
    return {'acts': runner.acts, 'verdict': verdict,
            'cost': {'capture_model_calls': 0, 'precompress_model_calls': 0,
                     'summarize_model_calls': runner.model_calls,
                     'inject_tokens_estimate': inject_estimate,
                     'note': '采集与预压缩路径按代码构造不含模型调用；'
                             'Act 2 的语义增量为脚本代写的 agent 文本，计 1 次模拟调用；'
                             '其余汇总均为确定性拼装（semantic_auto），计 0 次。'}}


def render_record(record):
    lines = ['# 卷一验收记录（1.16 五幕剧本）', '',
             '| 幕 | 场景 | 通过标准 | 实测结果 | 证据指针 |',
             '|---|---|---|---|---|']
    for a in record['acts']:
        lines.append('| %s | %s | %s | %s | %s |'
                     % (a['act'], a['scene'], a['criterion'], a['result'], a['evidence']))
    lines += ['', '## 成本账', '']
    cost = record['cost']
    lines.append('- 采集模型调用：%d' % cost['capture_model_calls'])
    lines.append('- 预压缩模型调用：%d' % cost['precompress_model_calls'])
    lines.append('- 汇总模型调用：%d' % cost['summarize_model_calls'])
    lines.append('- 接手注入 token 粗估：%d' % cost['inject_tokens_estimate'])
    lines.append('- 说明：' + cost['note'])
    lines += ['', '## 判定', '', '卷一验收：**%s**' % record['verdict']]
    if record['verdict'] != 'PASS':
        failed = [a for a in record['acts'] if a['result'] != '通过']
        lines.append('未通过：' + '、'.join('%s·%s' % (a['act'], a['scene']) for a in failed))
        lines.append('按计划：未通过的幕回到对应步骤返修后单独重演该幕 + 其下游幕。')
    return '\n'.join(lines) + '\n'


def main(argv=None):
    parser = argparse.ArgumentParser(description='project-relay v2 acceptance (plan 1.16)')
    parser.add_argument('--store-root', default=None)
    parser.add_argument('--workdir', default=None)
    parser.add_argument('--log-home', default=None)
    parser.add_argument('--keep-dir', action='store_true')
    args = parser.parse_args(argv)
    if args.store_root:
        store = Path(args.store_root).expanduser()
    else:
        store = Path(tempfile.mkdtemp(prefix='relay-accept-store-'))
    record = run_acceptance(store, workdir=args.workdir,
                            log_home=args.log_home, keep_dir=args.keep_dir)
    print(render_record(record))
    return 0 if record['verdict'] == 'PASS' else 1


if __name__ == '__main__':
    sys.exit(main())
