import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import crypto from 'node:crypto'
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

function logWarn(ctx, message) {
  if (ctx && ctx.logger && typeof ctx.logger.warn === 'function') ctx.logger.warn(message)
  else console.warn(message)
}

function sessionCwd(ctx, session) {
  return ctx?.workingDirectory?.get?.(session) || session?.header?.cwd
}

function textFromBlocks(blocks) {
  if (!Array.isArray(blocks)) return ''
  return blocks
    .filter((block) => block?.type === 'text' && typeof block.text === 'string')
    .map((block) => block.text)
    .join('\n')
    .trim()
    .slice(0, 3000)
}

// Fallback only: materializes the session's whole event log, so it runs
// at most when no turn/end payload text was ever captured for a session.
function snapshotLastText(session) {
  const events = session && typeof session.snapshotEvents === 'function' ? session.snapshotEvents() : []
  for (let i = events.length - 1; i >= 0; i -= 1) {
    const event = events[i]
    if (event?.type !== 'assistant/message') continue
    const text = textFromBlocks(event.data?.message?.content)
    if (text) return text
  }
  return ''
}

// turn/end payloads carry the turn's assistant text; capturing it as
// events stream by keeps card text O(1) per turn instead of rescanning
// the full session history at every milestone.
function payloadText(data) {
  if (!data) return ''
  if (typeof data.content === 'string') return data.content.trim().slice(0, 3000)
  return textFromBlocks(data.message?.content)
}

async function runRelay(ctx, cwd, args, text, cardFlag = '--card-file') {
  const script = findRelayScript(cwd)
  if (!script) {
    logWarn(ctx, 'project-relay: relay.py not found for cwd ' + cwd + '; hook ' + args[1] + ' skipped')
    return
  }
  const argv = [script, '--root', cwd, ...args]
  let tmp = null
  if (text) {
    tmp = path.join(os.tmpdir(), 'project-relay-card-' + process.pid + '-' + Date.now() + '-' + crypto.randomUUID() + '.md')
    fs.writeFileSync(tmp, text, { encoding: 'utf8', mode: 0o600 })
    argv.push(cardFlag, tmp)
  }
  try {
    const { code, stderr } = await new Promise((resolve) => {
      const python = process.env.DSH_PYTHON || (process.platform === 'win32' ? 'python' : 'python3')
      const child = spawn(python, argv, {
        cwd,
        stdio: ['ignore', 'pipe', 'pipe'],
        windowsHide: true,
      })
      let err = ''
      child.stderr?.on('data', (chunk) => { err += chunk })
      child.on('error', (error) => resolve({ code: -1, stderr: String(error) }))
      child.on('exit', (exitCode) => resolve({ code: exitCode ?? -1, stderr: err }))
    })
    if (code !== 0) {
      logWarn(ctx, 'project-relay: `relay ' + args.join(' ') + '` exited ' + code + (stderr ? ': ' + stderr.slice(-500) : ''))
    }
  } finally {
    if (tmp) {
      try { fs.unlinkSync(tmp) } catch {}
    }
  }
}

export function apply(ctx, config = {}) {
  const agent = config.agent || 'deepseek-harness'
  const stageEveryTurns = Number.isInteger(config.stageEveryTurns) && config.stageEveryTurns > 0
    ? config.stageEveryTurns : 10
  const counts = new Map()
  const lastText = new Map()

  function latestText(sessionId, session) {
    return lastText.get(sessionId) || snapshotLastText(session)
  }

  const turn = ctx.on('session/event', async (session, event) => {
    const sessionId = String(session.id)
    if (event?.type !== 'turn/end') return
    const carried = payloadText(event.data)
    if (carried) lastText.set(sessionId, carried)
    if (event.data?.reason?.kind !== 'completed') return
    const count = (counts.get(sessionId) || 0) + 1
    counts.set(sessionId, count)
    if (count % stageEveryTurns !== 0) return
    const cwd = sessionCwd(ctx, session)
    if (!cwd) return
    const text = latestText(sessionId, session)
    if (!text) return
    await runRelay(ctx, cwd, ['hook', 'stage', '--agent', agent, '--session', sessionId], text, '--file')
  })

  // The Stop moment is the agent loop's turn-stopping interception,
  // which fires inside the open turn. `workspace/session-stop` is a
  // different event: it is dispatched only after a session archive is
  // durable, so normal stops never reach it.
  const stop = ctx.on('agent/turn-stopping', async (input, next) => {
    const result = typeof next === 'function' ? await next() : undefined
    try {
      // Official agent/turn-stopping payload carries the agent subject.
      // DSH's Agent exposes its SessionId as agent.id; the Session object
      // lives in ctx.sessions, not as agent.session.
      const agentObj = input?.agent
      const sessionId = String(agentObj?.id ?? agentObj?.session?.id ?? '')
      const session = agentObj?.session || (sessionId && ctx.sessions?.get?.(sessionId))
      const cwd = sessionCwd(ctx, session)
      if (!sessionId || !cwd) {
        logWarn(ctx, 'project-relay: turn-stopping without a session id or cwd; hook stop skipped')
        return result
      }
      // Empty card text must not skip the stop: relay hook stop still
      // runs capture/enforce and simply writes no card.
      const text = latestText(sessionId, session)
      await runRelay(ctx, cwd, ['hook', 'stop', '--agent', agent, '--session', sessionId], text)
    } catch (error) {
      logWarn(ctx, 'project-relay: hook stop failed: ' + String(error))
    }
    return result
  })

  const disposed = ctx.on('session/disposed', (session) => {
    const sessionId = String(session.id)
    counts.delete(sessionId)
    lastText.delete(sessionId)
  })

  return () => { disposed(); stop(); turn() }
}

export const inject = ['sessions', 'workingDirectory']
