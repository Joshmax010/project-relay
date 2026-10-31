# 路径表 v2（卷三接入工具）

> 本表是卷三每家新工具接入的路径底稿，与卷一路径表 v1 分开记。
> v1 的旧表（`~/workspace/洛瑶的接力参考项目/实机验证/路径表v1.md`）不在本仓库内，
> 本文件不改它。**三档必须分开记**：官方路径（厂商文档原话）、实测路径
> （本机真实会话验证过）、证据等级；没有实测的条目一律标「未验证」，
> 不许把文档写得像已验证（计划 3.1 第 3 条）。

## 证据等级

| 等级 | 含义 |
|---|---|
| 官方口径 | 厂商官方文档原话，但未经本机实机验证 |
| 实测 | 在实机上用真实会话验证过可读性与格式 |
| 未验证 | 只有社区/第三方口径，或完全没证据 |

## Cursor（卷三第一家，3.1）

- **证据等级：未验证**（全条目）。本机无 Cursor，沙箱无真实 Cursor 会话。
- **实测路径：无**。实机验证清单见本节末尾；升为「实测」前不得随正式版发布，
  接手输出引用其回溯结果时必须携带保真度警告「此工具的日志路径未经实机验证」。

### 官方路径（厂商文档原话，cursor.com/docs/hooks，2026-10-30 查）

| 项目 | 官方落点 |
|---|---|
| hooks 配置（项目级） | `<project>/.cursor/hooks.json`（`version: 1` 必填；项目钩子在项目根运行） |
| hooks 配置（用户级） | `~/.cursor/hooks.json`（用户钩子在 `~/.cursor/` 运行；云端 agent 不读用户级） |
| hooks 事件（Agent） | `sessionStart` / `sessionEnd`、`preToolUse` / `postToolUse` / `postToolUseFailure`、`subagentStart` / `subagentStop`、`beforeShellExecution` / `afterShellExecution`、`beforeMCPExecution` / `afterMCPExecution`、`beforeReadFile` / `afterFileEdit`、`beforeSubmitPrompt`、`preCompact`、`stop`、`afterAgentResponse` / `afterAgentThought` |
| hooks 事件（Tab / 应用） | `beforeTabFileRead` / `afterTabFileEdit`、`workspaceOpen` |
| hooks 执行 | 命令型钩子：stdin 读 JSON、stdout 回 JSON；退出码 0=成功、2=拦截、其他=fail-open（`failClosed: true` 可改 fail-closed）；`loop_limit` 默认 5（stop 类） |
| 规则文件 | `.cursor/rules/*.mdc`（指令文件落点，见配方） |

### 社区口径（非官方文档，未验证；解析器按此形状实现）

| 项目 | 社区口径 |
|---|---|
| 会话元数据 | `<Cursor User>/workspaceStorage/<hash>/state.vscdb`：cursorDiskKV 键 `composerData:<composerId>`；旧版为 ItemTable 键 `composer.composerData`（`allComposers[]`）。两版表名分歧已在解析器里双路尝试。 |
| 会话正文 | `<Cursor User>/globalStorage/state.vscdb`：cursorDiskKV 键 `composerData:<composerId>`（头）与 `bubbleId:<composerId>:<bubbleId>`（消息）。 |
| 工作区归属 | `<Cursor User>/workspaceStorage/<hash>/workspace.json` 的 `folder` 字段（文件内容）；绝不从 storage hash 反推路径，无 folder 字段的会话按 cwd=None 跳过。 |
| 消息顺序 | 以 composer 的 `fullConversationHeadersOnly` 数组为准，SQLite rowid 不代表时序。 |
| 消息字段 | bubble：`bubbleId`、`type`（1=用户，2=AI）、`text`、`_v`、`toolFormerData`（工具执行，子字段名未验证）、`thinking` / `isThought`、`currentFileLocationData`、`relevantFiles`、`attachedCodeChunks`、`codebaseContextChunks`、`tokenCount`。无效行：不用社区的长度 ≤100 启发式（会误删短消息），改用结构校验：JSON 解析失败或无 type 1/2 的行计无效。 |
| 平台 User 目录 | Linux：`~/.config/Cursor/User`；macOS：`~/Library/Application Support/Cursor/User`；Windows：`%APPDATA%\Cursor\User` |
| 未建模的键 | 新版 agent 格式键（`agentKv:*`、`checkpointId:*`）解析器不建模，命中时按缺口报告。 |
| 仅社区提及、解析器不读的候选 | `~/.cursor/projects/` 下的 agent 转录（仅论坛提及一次，无结构证据，本步不解析）。 |

### 保留策略

- **官方未公布保留策略**（2026-10-30 查，无官方来源）。
- 社区论坛报告称 cursorDiskKV 无清理上限、持续膨胀（团队已知、无修复时间表），
  该说法未验证。**缺失时段处理**：一律报「找不到」，不得归因于「已被保留策略删除」。
  只有官方公布保留策略后，才按计划 3.1 第 5 条报「该时段日志已被 Cursor 按其保留策略删除」。

### 实机验证清单（升「实测」前必须逐项打勾，留待用户电脑实机演示）

1. 在装有 Cursor 的机器上开一个真实会话，确认 workspaceStorage/globalStorage
   两库真实存在且表名与社区口径一致（cursorDiskKV / ItemTable 二选一）。
2. 确认 `workspace.json` 的 folder 字段与真实项目根一致。
3. 确认 bubble 的 type/text/toolFormerData 字段名与解析器假设一致。
4. 确认 `stop` / `sessionEnd` 钩子按配方真实触发（见 references/v2-cursor-recipe.md）。
5. 确认新版 agent 会话的键形态（是否命中 agentKv），决定是否扩建模。
6. 以上任一项不符：修正解析器与本表，不许把未验证写成已验证。
