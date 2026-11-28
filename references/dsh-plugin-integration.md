# DSH 插件真实接线经验（0.2.2-rc.5）

这份文档记录 Project Relay DSH 插件在真实桌面会话里**加载成功但不写卡**的排查经验，避免以后重复踩坑。

## 结论

插件能出现在 DSH 的 Plugins 列表里，只说明 bundle 已安装并启用，不代表它拿到了正确的会话和项目路径。真实 Stop 钩子要同时拿到：

- **Session**：`agent/turn-stopping` 的载荷里，`agent` 对象至少要提供 `id`；Session 对象通过 `ctx.sessions.get(agent.id)` 取。
- **当前工作目录**：`session.header.cwd` 是会话创建时的初始目录，不等于用户后来切换的 `workingDirectory`。应使用 `ctx.workingDirectory.get(session)`。

## 错误写法与正确写法

### 1. 会话

错误：

```js
const session = input?.agent?.session
const sessionId = String(session?.id ?? '')
```

真实 DSH 0.2.2 桌面会话里，`agent` 只有 `id`（SessionId），`agent.session` 可能是 `undefined`，插件会直接跳过 Stop。

正确：

```js
const agentObj = input?.agent
const sessionId = String(agentObj?.id ?? agentObj?.session?.id ?? '')
const session = agentObj?.session || (sessionId && ctx.sessions?.get?.(sessionId))
```

### 2. cwd

错误：

```js
const cwd = session?.header?.cwd
```

桌面会话可能是在 DSH 工作区根目录创建的，`header.cwd` 会一直保留成 `D:/AAAAA/dsh` 这类非项目目录；用户后来通过 `working-directory/change` 切到项目目录时，`header.cwd` 不会变。

正确：

```js
function sessionCwd(ctx, session) {
  return ctx?.workingDirectory?.get?.(session) || session?.header?.cwd
}
```

插件需要声明：

```js
export const inject = ['sessions', 'workingDirectory']
```

### 3. 事件

- `agent/turn-stopping`：Stop 时刻，正常每个 turn 完成时派发。
- `session/event` 的 `turn/end`：可用于阶段卡；回调第一参数是 Session。
- `session/disposed`：独立事件，不是 `session/event` 的 type，用于清理缓存。

## 排查顺序

1. 确认 DSH Plugins 里 `Project Relay 钩子` 已启用。
2. 如果插件有日志，先看是否出现 `turn-stopping without a session id or cwd`。
3. 检查 `input.agent` 的字段：真实版本可能只有 `id`；用 `ctx.sessions.get(agent.id)`。
4. 检查当前目录：用 `ctx.workingDirectory.get(session)`，不要只用 `session.header.cwd`。
5. 确认插件能找到 `relay.py`：`cwd/.dsh/skills/project-relay/scripts/relay.py`、`cwd/.agents/skills/...`、`~/.dsh/skills/...`、`~/.agents/skills/...`。
6. 最后才怀疑事件是否触发：`agent/turn-stopping` 每个 assistant turn 结束都会触发，不需要手动关闭会话。

## 测试

`tests/test_dsh_plugin.mjs` 使用真实形状：

```js
{ agent: { id: 'sess-1' }, turn: 1, signal: {} }
```

并通过 fake `ctx.sessions.get` 和 fake `ctx.workingDirectory.get` 验证插件会调用 `relay hook stop`。headless 测试通过不代表桌面通过：至少要在真实桌面 profile 里跑一次，看是否生成 `sessions/card-*.md`。
