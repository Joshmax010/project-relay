# Cursor 解析 fixture（合成样本，未经实机验证）

> **本目录全部内容均为按公开文档格式合成的样本，从未见过真实 Cursor 会话。**
> 不得冒充真实样本，不得用于声称“Cursor 已验证”。
>
> - 形状来源（社区口径，非官方文档）：cursor-chronicle 的数据库结构说明、
>   cursor-composer-export 的抽取流程、cursor-helper 的 export-chat 说明。
> - 官方文档（cursor.com/docs/hooks，2026-10-30 查）只确认了 hooks 能力，
>   未确认任何存储布局；存储布局部分没有任何官方来源。
> - `workspace.json` 与 `expected-events.json` 里的 `__PROJECT_ROOT__` /
>   `__GLOBAL_DB__` 是占位符，测试搭建器会替换为临时目录的真实路径。
> - 搭建方式（见 tests/test_relay_cursor.py）：用 Python 标准库 sqlite3
>   把 `workspace-db.json` / `global-db.json` 的行写成真正的 state.vscdb
>   文件（cursorDiskKV(key TEXT, value BLOB)、ItemTable(key TEXT, value TEXT)），
>   并故意把 global 行按与 header 相反的顺序写入，以证明解析顺序
>   跟 header 数组走、跟 rowid 无关。

## 样本会话 comp-001（header 顺序即对话顺序）

| bubble | 类型 | 预期处理 |
|---|---|---|
| b-user-1 | type 1，用户文本，含“决定：” | decision_candidate 事件 |
| b-asst-1 | type 2，助手文本 + relevantFiles | text 事件，files_touched=[app.py] |
| b-tool-1 | type 2，toolFormerData | tool_call 事件（工具名尽力提取） |
| b-think-1 | type 2，isThought | 跳过，thinking_skipped=1 |
| b-user-2 | type 1，用户文本 | text 事件 |
| b-missing-1 | 只在 header 里，无 global 行 | missing_bubbles=1 |
| b-invalid-1 | global 行值 `{"x":1}`（无 type 字段，结构无效） | invalid_bubbles=1 |

## 证据等级

全部文件：未验证。实机验证清单见 references/v2-paths.md。
