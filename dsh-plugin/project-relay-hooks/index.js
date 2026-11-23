import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { spawn } from 'node:child_process'

function findRelayScript(cwd) {
  const candidates = [
    path.join(cwd, '.dsh', 'skills', 'project-relay', 'scripts', 'relay.py'),
    path.join(cwd, '.agents', 'skills', 'project-relay', 'scripts', 'relay.py'),
    path.join(os.homedir(), '.dsh', 'skills', 'project-relay', 'scripts', 'relay.py'),
    path.join(os.homedir(), '.agents', 'skills', 'project-relay', 'scripts', 'relay.py'),
  ]
  return candidates.find((candidate) => fs.existsSync(candidate))
}

function lastAssistantText(session) {
  const events = typeof session.snapshotEvents === 'function' ? session.snapshotEvents() : []
  for (let i = events.length - 1; i >= 0; i -= 1) {
    const event = events[i]
    if (event?.type !== 'assistant/message') continue
    const blocks = event.data?.message?.content
    if (!Array.isArray(blocks)) continue
    const text = blocks
      .filter((block) => block?.type === 'text' && typeof block.text === 'string')
      .map((block) => block.text)
      .join('\n')
      .trim()
    if (text) return text.slice(0, 3000)
  }
  return ''
}

async function runRelay(cwd, args, text, cardFlag = '--card-file') {
  const script = findRelayScript(cwd)
  if (!script || !text) return
  const tmp = path.join(os.tmpdir(), 'project-relay-card-' + process.pid + '-' + Date.now() + '.md')
  fs.writeFileSync(tmp, text, 'utf8')
  try {
    await new Promise((resolve) => {
      const python = process.env.DSH_PYTHON || (process.platform === 'win32' ? 'python' : 'python3')
      const child = spawn(python, [script, '--root', cwd, ...args, cardFlag, tmp], {
        cwd,
        stdio: 'ignore',
        windowsHide: true,
      })
      child.on('error', () => resolve())
      child.on('exit', () => resolve())
    })
  } finally {
    try { fs.unlinkSync(tmp) } catch {}
  }
}

export function apply(ctx, config = {}) {
  const agent = config.agent || 'deepseek-harness'
  const stageEveryTurns = Number.isInteger(config.stageEveryTurns) && config.stageEveryTurns > 0
    ? config.stageEveryTurns : 10
  const counts = new Map()

  const stop = ctx.on('workspace/session-stop', async ({ sessionId }) => {
    const session = ctx.sessions.get(sessionId)
    const cwd = session?.header?.cwd
    if (!cwd) return
    await runRelay(cwd, ['hook', 'stop', '--agent', agent, '--session', sessionId], lastAssistantText(session))
  })

  const turn = ctx.on('session/event', async (session, event) => {
    if (event?.type !== 'turn/end' || event.data?.reason?.kind !== 'completed') return
    const sessionId = String(session.id)
    const count = (counts.get(sessionId) || 0) + 1
    counts.set(sessionId, count)
    if (count % stageEveryTurns !== 0) return
    const cwd = session.header?.cwd
    if (!cwd) return
    await runRelay(cwd, ['hook', 'stage', '--agent', agent, '--session', sessionId], lastAssistantText(session), '--file')
  })

  return () => { stop(); turn() }
}

export const inject = ['sessions']
