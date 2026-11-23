# Project Relay DSH hooks

把 Project Relay 的 Stop / 阶段写卡接到 DSH 事件。插件监听 `workspace/session-stop` 和 `session/event` 的 `turn/end`：前者调用 `relay hook stop`，后者每 `stageEveryTurns` 个完成的 turn 调用 `relay hook stage`。卡片正文取最近一条 assistant 文本并截到 3000 字符，不读取其他聊天内容。

安装：在 DSH 的 **Plugins > Add plugin** 选择本目录。
