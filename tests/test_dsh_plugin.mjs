import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { apply } from '../dsh-plugin/project-relay-hooks/index.js'

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'relay-plugin-test-'))
const relayDir = path.join(tmp, '.dsh', 'skills', 'project-relay', 'scripts')
fs.mkdirSync(relayDir, { recursive: true })
fs.writeFileSync(path.join(relayDir, 'relay.py'), [
  'import json, pathlib, sys',
  'pathlib.Path("hook-calls.jsonl").open("a", encoding="utf-8").write(json.dumps(sys.argv[1:]) + "\\n")',
  '',
].join('\n'))

const listeners = new Map()
const warnings = []
const ctx = {
  logger: { warn: (message) => warnings.push(message) },
  on: (name, fn) => { listeners.set(name, fn); return () => {} },
}

try {
  const cleanup = apply(ctx, { stageEveryTurns: 10 })
  const stop = listeners.get('agent/turn-stopping')
  assert.equal(typeof stop, 'function')
  const result = await stop({
    agent: { id: 'agent-1', session: { id: 'sess-1', header: { cwd: tmp } } },
    turn: 1,
    signal: {},
  }, async () => 'next-result')
  assert.equal(result, 'next-result')
  const calls = fs.readFileSync(path.join(tmp, 'hook-calls.jsonl'), 'utf8').trim().split('\n').map((line) => JSON.parse(line))
  assert.equal(calls.length, 1)
  assert.ok(calls[0].includes('--root') && calls[0].includes(tmp))
  assert.ok(calls[0].includes('hook') && calls[0].includes('stop'))
  assert.ok(calls[0].includes('--session') && calls[0].includes('sess-1'))

  await stop({ agent: {} }, async () => 'empty')
  assert.ok(warnings.some((message) => message.includes('without a session')))
  assert.equal(typeof listeners.get('session/disposed'), 'function')
  listeners.get('session/disposed')({ id: 'sess-1' })
  cleanup()
  console.log('dsh plugin event tests OK')
} finally {
  fs.rmSync(tmp, { recursive: true, force: true })
}
