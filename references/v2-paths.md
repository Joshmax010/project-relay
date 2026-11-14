# 路径表 v3（四家工具的安装落点）

> 本表只记 v3 留下的四家工具的安装落点与证据等级。Cursor、Gemini、
> Copilot、Cline 的回溯路径已随回溯建档一起删除，不在本文件里。
> **三档必须分开记**：官方路径（厂商文档原话）、实测路径（本机真实
> 会话验证过）、证据等级；没有实测的条目一律标「未验证」，不许把
> 文档写得像已验证。

## 证据等级

| 等级 | 含义 |
|---|---|
| 官方口径 | 厂商官方文档原话，但未经本机实机验证 |
| 实测 | 在实机上用真实会话验证过可读性与格式 |
| 未验证 | 只有社区/第三方口径，或完全没证据 |

## Claude Code（强档，SessionStart + Stop hooks）

- **证据等级：实测**（recipe install 路径；skills 命令行路径见 README）。
- 配方落点（`recipe install --tool claude-code`）：
  - hooks：用户级 `~/.claude/settings.json`，SessionStart 跑 `hook resume`，
    Stop 跑 `hook stop`（先 capture 再 enforce）。
  - 规程：用户级 `~/.claude/CLAUDE.md` 里 relay 受管小节。
  - 安装记录：项目档案 `agents/claude-code/recipe.json`。
- 卸载（`recipe uninstall --tool claude-code`）拆掉 hooks 与受管小节，删安装记录。

## Codex（自觉档，AGENTS.md 受管小节，无 hooks）

- **证据等级：实测**（recipe install 路径；skills 命令行路径见 README）。
- 配方落点（`recipe install --tool codex`）：
  - 规程：项目内 `AGENTS.md` 受管小节（`<!-- relay:begin relay -->` …
    `<!-- relay:end relay -->`，带哈希，改动即失效重装）。
  - 安装记录：项目档案 `agents/codex/recipe.json`。
- 卸载拆掉受管小节，删安装记录。无 hooks，全靠 Agent 自觉执行开头
  resume、结尾 capture。

## WorkBuddy（弱档，无 hooks）

- **证据等级：实测**（界面上传 zip 与手工放目录两条路，见 README；
  `recipe package` 打包字节一致已测）。
- 配方落点（`recipe install --tool workbuddy`）：
  - 技能目录：项目级 `.workbuddy/skills/project-relay/`，内含 SKILL.md
    （注入 `agent_created: true` 的是打包出的那份，仓库正本不加）。
  - 安装记录：项目档案 `agents/workbuddy/recipe.json`。
- 没 shell 或 Python 不通时走 `recipe package`：打出
  `project-relay-workbuddy.zip`（顶层唯一文件夹 `project-relay/`，内含
  SKILL.md、整个 `scripts/`、深规格文档），用户在技能面板上传。
- 卸载删技能目录与安装记录。无 hooks，全靠 Agent 自觉执行。

## DeepSeek Harness（弱档，技能目录）

- **落点**：项目内 `.deepseek-harness/skills/project-relay/SKILL.md`，
  由 `recipe install --tool deepseek-harness` 写入（与 WorkBuddy 同款弱档，
  注入 `agent_created: true`）。无生命周期 hooks，靠 Agent 自觉执行。
- **卸载**：`recipe uninstall --tool deepseek-harness` 删技能目录与安装记录。
- **证据等级：本机安装流程已实测**（脚本单测覆盖）；DSH 宿主侧真实会话
  调用仍待真机验证。
