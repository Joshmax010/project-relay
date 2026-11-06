# Cursor 装配配方（卷三 3.1，未经实机验证）

> **档位：甲档**（有完整生命周期 hooks，能力来自 Cursor 官方文档
> cursor.com/docs/hooks，2026-10-30 查）。
> **兜底强度：hooks 兜底 + 指令文件指针。**
> **配方本身未经实机验证**：hooks.json 的落点与事件名来自官方文档，
> 但本配方从未在真实 Cursor 里触发过；`relay recipe install --tool`
> 尚未接 Cursor（该命令的工具清单在核心代码里写死，本步按核心零改动
> 硬线不动它），以下文件需手动落盘，落盘后按文末清单实机验证。
>
> **保真度硬性要求**：任何接手输出在引用 Cursor 回溯结果时，必须携带
> 警告「此工具的日志路径未经实机验证」——该警告由解析器
> `fidelity_warnings()` 产出，配方与接手流程不得剥离它。

## 1. hooks.json（项目级，手动落盘）

落点：`<project>/.cursor/hooks.json`（项目钩子在项目根运行；云端 agent
只读项目级、只跑命令型钩子，不读用户级 `~/.cursor/hooks.json`）。

```json
{
  "version": 1,
  "hooks": {
    "stop": [
      {
        "command": ".cursor/hooks/relay-capture.sh",
        "timeout": 60
      }
    ],
    "sessionEnd": [
      {
        "command": ".cursor/hooks/relay-capture.sh",
        "timeout": 60
      }
    ]
  }
}
```

事件选择理由：`stop`（agent 完成）与 `sessionEnd`（会话生命周期结束）
是官方文档列出的事件；二者都触发同一采集脚本，脚本自身去重，
重复触发不产生重复采集（relay capture 的去重语义见主流程）。

## 2. 采集脚本（手动落盘，需 chmod +x）

落点：`<project>/.cursor/hooks/relay-capture.sh`。
设计原则：fail-open——脚本永不拦截 agent（任何失败都 exit 0），
只做采集；hook 输入的 JSON 结构未经实机验证，会话 id 只做尽力提取，
提不到就按无会话 id 采集。

```bash
#!/usr/bin/env bash
# relay Cursor 采集钩子（3.1 配方，未经实机验证）。
# Cursor 以 stdin 传 JSON；本脚本只尽力提取会话 id，提不到也不报错。
set -u
input="$(cat)"
session="$(printf '%s' "$input" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    for k in ('session_id', 'sessionId', 'composer_id', 'composerId'):
        if isinstance(d.get(k), str) and d[k]:
            print(d[k]); break
except Exception:
    pass
" 2>/dev/null)"
if [ -n "${session:-}" ]; then
  python3 scripts/relay.py capture --tool cursor --session "$session" >> .cursor/hooks/relay-capture.log 2>&1 || true
else
  python3 scripts/relay.py capture --tool cursor >> .cursor/hooks/relay-capture.log 2>&1 || true
fi
exit 0
```

注意：

- `scripts/relay.py` 的相对路径依赖“项目钩子在项目根运行”
  （官方文档原话）；若仓库根与 Cursor 项目根不一致，需改绝对路径。
- Windows 上桌面端经 PowerShell 传 JSON（官方文档原话），
  上述 `.sh` 仅适用于 POSIX 环境；Windows 需一份等价 `.ps1`
  （本步未写，列入实机验证清单）。
- `relay capture` 的 `--tool cursor` 采集口径与三家旧工具一致；
  回溯（backfill）目前只能经扩展注册表模块
  `scripts/relay_parser_registry.py` 的 `get_parser('cursor')` 调用，
  尚未接进 `relay backfill` 实机流程（核心零改动硬线， deferred）。

## 3. 规则文件指针（指令文件落点）

落点：`<project>/.cursor/rules/relay-handoff.mdc`。
Cursor 读取 `.cursor/rules` 下的规则文件（社区口径 + 官方规则文档通识，
本步未在官方文档中单独核对该目录名，标注为社区口径）。
内容（按项目情况改写项目名与记忆仓位置）：

```markdown
---
description: 会话结束时按 project-relay 规程留交接
globs: []
alwaysApply: true
---

会话结束前，按 project-relay 规程做三件事（缺一不可）：

1. 把本次改动、未完成事项、已确认的决定写进交接记录，
   不要只写“完成了”，要写“做到哪一步、下一步是什么、卡在哪”。
2. 目标（要交付的东西）有变化时先向用户确认，不要擅自改目标。
3. 若引用 Cursor 历史会话的回溯结果，必须在输出里携带警告：
   「此工具的日志路径未经实机验证」。
```

## 4. 用户级替代（可选）

所有项目都想装时，把第 1 节的 hooks.json 放到 `~/.cursor/hooks.json`，
脚本路径改为 `~/.cursor/hooks/relay-capture.sh`，运行目录随之变为
`~/.cursor/`（官方文档原话），脚本里的 `scripts/relay.py` 相对路径
必须改成绝对路径。云端 agent 不读用户级配置，不要指望它。

## 5. 实机验证清单（落盘后必须逐项打勾）

1. 在 Cursor 设置里确认 Hooks 已加载（官方文档：改动 hooks.json
   会自动重载；不出现时重启 Cursor 并查看 Hooks 输出通道）。
2. 跑一次真实 agent 会话到 stop，确认 `.cursor/hooks/relay-capture.log`
   有记录且 relay 侧多了一条 capture 条目。
3. 确认 `sessionEnd` 与 `stop` 双触发没有产生重复采集。
4. Windows 机器上补一份 `.ps1` 等价脚本并验证。
5. 任一项不符：修正本配方，不许把未验证写成已验证。
