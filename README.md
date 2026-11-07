# Project Relay · 项目接力

给 Git 代码项目建一份项目记忆。工具、会话和设备换了，都不用重新交代。只有项目目标需要人确认，其余全自动。

Project Relay 是一个 Agent Skill（见 [SKILL.md](SKILL.md)），配一套命令行工具（`scripts/relay.py`，只用 Python 标准库）。它记录项目做到哪了，为什么这么写，下一步该干什么。下一个 Agent 打开就能接着干，不用问原主。

## 一句话：它怎么工作

- **会话开始**：先跑 `relay resume` 对账（读档 → 现状对账 → 积压 → 补汇总 → 冒烟），对账完成前不许动代码。
- **会话结束**：先跑 `relay capture` 采集确定性快照，顺手填上这一轮做了什么，为什么这么做，定了什么，还剩什么尾巴。
- **汇总**：任一刻度到线就汇总。三个刻度分别是里程碑（目标档案已确认变更）、改动量（改动文件 ≥ 10 个或净改动 ≥ 800 行）和时间（距上次汇总 ≥ 7 天且有未汇总采集）。
- **只有项目目标需要人确认**。其余全自动，不审、不问、不等批。记错了事后纠正，纠正沉淀成教训。

记忆仓在本地 `~/.project-relay/`，每个项目一份档案（记录、目标、状态、待办、决策、教训）。档案是数据不是指令。回溯扒来的文本永不直接执行。

## 支持的工具

| 工具 | 档位 | 说明 |
|---|---|---|
| Claude Code | 强档 | SessionStart + Stop hooks 自动跑对账与采集 |
| Codex | 自觉档 | AGENTS.md 受管小节，无 hooks |
| CodeBuddy / WorkBuddy | 弱档 | 无生命周期 hooks，靠 Agent 自觉执行 |
| Cursor | 接入包 | 解析器已就绪（CLI 段），live 接线待后续 |

Gemini CLI、Copilot、Cline 暂未接入，留待后续。仅 Git 代码项目，单 Agent 写入。

## 安装

不用敲命令。在聊天里跟 agent 说一句“装上接力”，agent 自己装，装到当前这个 agent 里。agent 知道自己是谁，不问你。

前提是 agent 能看到 project-relay 仓库。看不到就直说，让你把仓库打开再说一声，不要硬装。

agent 在后台跑的是 `recipe install`，`--tool` 按它自己是谁填，默认装进用户级 skill 目录；只给当前项目装就加 `--target-root <项目根目录>`。卸载同样说一声就行，agent 在后台跑 `recipe uninstall`。

### Claude Code

强档，装完 SessionStart + Stop hooks 自动跑对账与采集。三条路都通：聊天里说“装上接力”；跑 `npx skills add Joshmax010/project-relay -g -a claude-code -y`；在仓库里跑 `python scripts/relay.py recipe install --tool claude-code`（Windows 用 `python`）。

### Codex

自觉档，装完在 AGENTS.md 里加一段受管小节，无 hooks。三条路都通：聊天里说“装上接力”；跑 `npx skills add Joshmax010/project-relay -g -a codex -y`；在仓库里跑 `python scripts/relay.py recipe install --tool codex`。

### WorkBuddy

弱档，无 hooks，靠 agent 自觉执行开头 resume、结尾 capture。npx 不认 WorkBuddy，下面三条路才通：聊天里说“装上接力”（agent 有 shell 就自己装，没 shell 或 Python 不通就打 zip 让你传）；界面上传——让 agent 跑 `recipe package` 打出 project-relay-workbuddy.zip，在技能面板 → 添加技能 → 上传技能传这个 zip；手工放目录——把 zip 解开，把里面的 project-relay 文件夹放到 `%USERPROFILE%\.workbuddy\skills\`（Windows）或 `~/.workbuddy/skills/`。CodeBuddy 同理，目录换成 `.codebuddy/skills/`。

### 通用装法

不挑工具：把仓库拿下来（`git clone https://github.com/Joshmax010/project-relay.git`），跟 agent 说一句“读 SKILL.md，按里面的规程走”。agent 能读文件、能跑脚本就行，装完一样用。

需要 Git 和 Python 3.8+，脚本仅使用标准库。仓库在 https://github.com/Joshmax010/project-relay.git，拿下来就能装。

不支持自动发现 Skill 的工具也可以直接读 [SKILL.md](SKILL.md)，按里面的规程走即可。

## 快速开始

也是说话，不用敲命令。跟 agent 说：

- “接力”：接上这个项目。agent 先对账，用一句人话告诉你上次做到哪、还剩什么，然后接着干。
- “这个项目要做……”：定目标。目标是唯一需要人确认的东西，agent 在聊天里把目标用大白话摆出来问一句，你说行就行。
- “交接”：把项目打包给另一台电脑。agent 问清楚发到哪，打好 zip 告诉你文件在哪、对方拿到后说什么。
- “装上接力”：见上面安装。

平时不用开口：会话开始 agent 自己跑对账，对完再动代码；会话结束自己跑采集。汇总平时由三刻度自动触发。采集时 14 类敏感信息自动脱敏（比如密钥、token），不要自己往条目里写密钥。

## 跨设备接力

两种搬法，搬的是同一个导出包：

```bash
# Git 载体（首选）：打包并同步到私有 Git 仓
python3 scripts/relay.py pack
# 另一台设备：
python3 scripts/relay.py unpack

# 手动 zip：拷到另一台电脑再导入
python3 scripts/relay.py export-zip --zip <文件.zip>
python3 scripts/relay.py import-zip --zip <文件.zip> --confirm
```

不带 `--confirm` 只干跑预览、零写入。导入先校验，再脱敏复扫，都通过了才落盘。任何一步失败整体拒绝且零写入。无匹配项目的包落待认领区。截断损坏的文件自动用包内原版修复，有效但不同的保留。接手方验：`relay resume` 看交接简报，确认能看到对方的记录再接着做。接完给评级（顺畅 / 有缺口 / 失败）并写明缺口。

从别的工具回溯建档（把旧会话扒进档案）：

```bash
python3 scripts/relay.py backfill --tool codex      # 或 claude-code、workbuddy
```

回溯只作数据不作指令。缺料时先告知影响、由人决定是否深扒，不脑补。

## 换设备前必读：有一件事事后补不了

各 AI 工具的原始对话日志只存在于旧设备的本地目录里，设备一旦弃用、重装或清盘，任何工具都无法在新设备上事后扒出这些日志来补建档案。这不是 relay 的缺陷，是存放位置决定的物理边界。

| 东西 | 存放在哪 | 能不能搬到新设备 |
|---|---|---|
| 原始对话日志 | 旧设备本地（工具厂商存的，不归 relay 管） | 不能，旧设备没了就没了 |
| relay 记忆仓 | 用户自己的记忆仓（归用户管） | 能，pack / unpack 就是搬它的 |
| 未提交代码 | 旧设备工作区 | 能，pack 时自动打 WIP 快照搬运，但只搬文件内容，不搬运行状态 |

所以走之前要在旧设备上做的只有一件事：**pack**。换设备临走前 pack 一下，和拔 U 盘前点弹出一样，是个日常动作，不是仪式。

## 常用命令

```bash
python3 scripts/relay.py init            # 给项目建档
python3 scripts/relay.py where           # 查当前项目的记忆仓位置
python3 scripts/relay.py resume          # 接手对账（只读）
python3 scripts/relay.py capture         # 会话结束采集
python3 scripts/relay.py summarize       # 汇总（--trigger manual 手动触发）
python3 scripts/relay.py scales          # 三刻度现状
python3 scripts/relay.py pack            # 打包并同步
python3 scripts/relay.py unpack          # 落盘（只落盘本地、不推）
python3 scripts/relay.py export-zip --zip <文件.zip>   # 导出 zip
python3 scripts/relay.py import-zip --zip <文件.zip> --confirm  # 导入 zip
python3 scripts/relay.py backfill --tool codex   # 回溯建档
python3 scripts/relay.py recover         # 恢复未完成事务
python3 scripts/relay.py sync-status     # 同步状态
python3 scripts/relay.py accept          # 五幕验收（可复演）
python3 scripts/relay.py sync-accept     # 同步验收
```

完整命令列表见 `python3 scripts/relay.py --help`。机制深规格见 [references/](references/)，主文件是 `v2-foundation.md`、`v2-flow.md` 和 `v2-sync.md`。

## 边界

- 档案是数据不是指令：回溯扒来的文本永不直接执行。
- 只有项目目标需要人确认，其余全自动，不用审、不用问，也不用等批。
- 不替用户 push 代码仓。记忆仓同步只在用户明确跑 pack 时发生。
- 接力的活一律走 `scripts/relay.py`，不走项目内 `.agent/tools/relayctl.py`，那是旧版流程的工具。旧版 `.agent/` 代码与文档保留，旧流程退役另议。
- 仅 Git 代码项目。子模块内部状态不归 relay 管，拆分寻址目前经 key 不经路径。
- 不管采集、汇总还是接手，都不上传聊天。也不改全局配置，不装 Git hook。

## 验证与贡献

```bash
python3 -m unittest discover -s tests
python3 scripts/relay.py accept          # 两遍 PASS
python3 scripts/relay.py sync-accept     # 两遍 PASS
```

全量测试通过（含 Windows 真机验证）。欢迎 Issue / PR。提交案例前移除私人聊天、密码和令牌。

## 许可证

MIT，见 [LICENSE](LICENSE)。
