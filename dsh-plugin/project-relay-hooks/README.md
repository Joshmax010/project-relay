# Project Relay DSH hooks

把 Project Relay 的 Stop / 阶段写卡接到 DSH 事件。插件监听 `agent/turn-stopping`（agent 循环停下那一刻，即 Stop 钩子的时刻）和 `session/event` 的 `turn/end`：前者调用 `relay hook stop`，后者每 `stageEveryTurns` 个完成的 turn 调用 `relay hook stage`。注意不要用 `workspace/session-stop`：那是会话归档落盘之后才派发的事件，平时停下根本不会触发。

卡片正文取最近一条 assistant 文本并截到 3000 字符：优先用 `turn/end` 事件自带的正文，事件流里没带时才回退到读一次会话事件日志，不读取其他聊天内容。没有正文时 `relay hook stop` 照常跑（capture / enforce 不跳过），只是不写卡。relay 子进程失败会打到日志，不再静默吞掉。

安装：在 DSH 的 **Plugins > Add plugin** 选择本目录。

真实事件字段和排查步骤见 `references/dsh-plugin-integration.md`：DSH 0.2.2 的 `agent` 只保证 `id`，Session 用 `ctx.sessions.get(agent.id)`；当前项目目录用 `ctx.workingDirectory.get(session)`，不要用 `session.header.cwd`。
