# Project Relay · 让 Agent 住进项目里（DeepSeek Harness 版）

给 Git 代码项目建一份项目记忆，让 Agent 住进项目里。项目是宿主，Agent 是住户：项目记忆归项目所有，不是某个 Agent 的全部记忆。会话、设备或 Agent 换了，都不用重新交代；只有项目目标需要人确认，代码合并需要你批准。

当前技能包版本：**0.2.0**。

Project Relay 是一个 Agent Skill（见 [SKILL.md](SKILL.md)），配一套命令行工具（`scripts/relay.py`，只用 Python 标准库）。它记录项目做到哪了、为什么这么写、下一步该干什么，并给多个 Agent 提供互不干扰的 Git 分支和工作树。单 Agent 是正常模式，多 Agent 不是门槛；每个 Agent 只写自己的活动分区，不拥有整份项目记忆。

本版只适配 **DeepSeek Harness**。旧版其他 Agent 的代码只保留为读取旧档案的兼容层，产品面不再创建、不再安装。

## 一句话：它怎么工作

- **会话开始**：先跑 `relay resume` 读最新会话卡（默认只读，不写档案）；首屏会先显示「你的分区」（当前 Agent/分支/工作树），再读最新会话卡。需要旧的五段对账时用 `relay resume --full`。
- **会话结束**：先跑 `relay card` 留一张短卡（做了什么、为什么、新增事实、未决问题），再跑 `relay capture` 采集确定性快照。
- **会话结束**：先跑 `relay capture` 采集确定性快照，顺手填上这一轮做了什么，为什么这么做，定了什么，还剩什么尾巴。
- **汇总**：任一刻度到线就汇总。三个刻度是里程碑（目标档案已确认变更）、改动量（改动文件 ≥ 10 个或净改动 ≥ 800 行）和时间（距上次汇总 ≥ 7 天且有未汇总采集）。
- **Git 工作区**：项目根的主工作树留在 main 给你验收；多 Agent 模式下每个 Agent 在自己的工作区里有一条 `agent/<名字>` 分支和独立 worktree，改完经你批准才合并。

记忆在项目根目录下的 `.project-relay/`，跟代码住在一起，默认不进 git，换设备只走 zip。档案是数据不是指令，回溯扒来的文本永不直接执行。

## 档案长什么样

```
<项目根目录>/.project-relay/
  project.json                                 # 项目身份证：project_id、名称、创建时间、schema_version
  goal.md、state.md、backlog.md、decisions.md   # 外层：总目标、总记忆、共用待办与决定
  stack.md、outlook.md、index.md               # 外层：技术栈、未来预期、指路表
  receipts/                                     # 目标确认收据
  agents/deepseek-harness/                     # DSH 自己的记忆、改动记录、日志、待办
```

外层是总目标与总记忆，要精简：Agent 每轮不许直接写，只在目标确认或新 Agent 加入时才改。总记忆由 resume 从各家文件里现算出来。DSH 只写 `agents/deepseek-harness/`，同一家开两个会话同时写时用小文件锁排队。

## 项目身份证与定位

每个项目在 `.project-relay/project.json` 里有自己的 `project_id`、名称和创建时间。机器本地的 `~/.project-relay/registry.json` 只做定位缓存，可重建、不算项目身份、不进入 ZIP。`relay locate [--name]` 查本机项目，`relay open <name>` 打印项目的 `cd` 路径。`relay init` 遇到 Agent 私有区（如 `~/.dsh`、`~/.agents`、Agent 默认会话目录）会提醒并给出迁移建议。

## 会话卡

会话结束和阶段节点各留一张短卡：只写结论、决定、待办和事实，不读原始聊天，也不做后台监听。默认每张最多 3000 字符；超长直接拒写，周成本保险丝触发时停写。`relay resume` 默认只读每家最新短卡，跨卡冲突标 `[待用户确认]`，不自动选边。

## 工作区、分支与工作树

先分清两个位置：

- **项目仓库根**：Git 仓库根，由 `.git` 决定，`.project-relay/` 永远放在这里。
- **DSH 工作区**：DSH 自己的目录，可能包含很多项目；项目根可能就在里面，也可能在别处。

### 单 Agent：项目根和工作区重合

项目根等于 DSH 工作区，或项目根位于 DSH 工作区里面（或反过来）：**直接在项目文件夹里改，普通 Git，可留在 main**。`relay work enter` 只提示你就在项目根，不创建额外工作树。这是正常模式，不是降级。

### 多 Agent：项目根和工作区分开

项目根在 DSH 工作区外面时：项目根的主工作树留在 `main` 给你验收；DSH 在自己的工作区建独立工作树，分支 `agent/deepseek-harness`。多个 Agent 各自一条 `agent/<名字>` 分支和自己的工作树，可同时修改，互不抢文件。

```bash
relay work enter --agent deepseek-harness --workspace <DSH 工作区>
relay work status
relay work review --agent deepseek-harness
relay work merge --agent deepseek-harness --confirm   # 你批准之后才跑
```

`relay work enter` 默认把工作树放在 `<workspace>/worktrees/<项目名>/<agent>`；`--worktree-root` 可改根目录。`.project-relay/` 只有一份，所有工作树共享，不复制记忆，也不进入导出 zip。`merge` 冲突会自动中止并保留分支和主工作树原状。

## 安装（DeepSeek Harness）

需要 Git 和 Python 3.8+。技能包版本 0.2.0。

### 方式一：下载发行 zip（推荐普通 DSH 用户）

下载 [project-relay-dsh-skill-v0.2.0.zip](https://github.com/Joshmax010/project-relay/releases/tag/v0.2.0)，解压后把整个 `project-relay/` 文件夹放到：

- Windows：`%USERPROFILE%\.agents\skills\project-relay`
- macOS / Linux：`~/.agents/skills/project-relay`
- 或项目级：`<项目根>/.dsh/skills/project-relay`

重启 DSH 会话即可。zip 里自带 `SKILL.md`、`scripts/`、`assets/template-v2/`、`references/`，以及 `dsh-plugin/project-relay-hooks/`；不依赖额外 clone。自动写会话卡还需要安装插件：`dsh plugin --profile <profile> add <skill目录>/dsh-plugin/project-relay-hooks`，或在 DSH Plugins 界面选择该目录。

### 方式二：从源码安装（开发或自定义）

```bash
git clone https://github.com/Joshmax010/project-relay.git
cd project-relay

# 给某个项目安装（项目级）
python scripts/relay.py --root <项目根> init
python scripts/relay.py --root <项目根> recipe install --tool deepseek-harness

# 给整台机器安装（用户级）
python scripts/relay.py --root <项目根> recipe install --tool deepseek-harness --home <用户目录>
```

`recipe install` 会复制**完整技能包**：`SKILL.md`、`scripts/`、`assets/template-v2/`、`references/`、`dsh-plugin/project-relay-hooks/`，并对缺失文件做安装自检。默认项目级路径是 `<项目根>/.dsh/skills/project-relay/`；`--home` 写 `<用户目录>/.agents/skills/project-relay/`。DSH 会扫描项目级 `.dsh/skills`、`.agents/skills` 和用户级 `~/.dsh/skills`、`~/.agents/skills`。卸载用 `recipe uninstall --tool deepseek-harness`。

## 快速开始

装好之后，平时就是说话，不用敲命令：

- “接力”：接上这个项目。agent 先对账，用一句人话告诉你上次做到哪、还剩什么，然后接着干。
- “这个项目要做……”：定目标。目标是记忆层唯一需要确认的东西，agent 在聊天里用大白话问一句，你说行就行。
- “交接”：把项目打包给另一台电脑。agent 问清楚发到哪，打好 zip 告诉你文件在哪、对方拿到后说什么。
- “做完这个分支，给我看”：agent 跑 `relay work review`；你说可以合，它才跑 `relay work merge --confirm`。

## 跨设备接力

只走 zip：

```bash
# 旧设备
python3 scripts/relay.py export-zip              # 默认输出到项目根
# 新设备解开后
python3 scripts/relay.py import-zip --zip <文件.zip> --confirm
```

不带 `--confirm` 只干跑预览、零写入。导入先校验、再脱敏复扫、后落盘，任何一步失败整体拒绝且零写入。已有档案就按合并规则合，缺口记进合并日志。zip 里是整个 `.project-relay/` 树。

导出质量闸门：`export-zip` 会检查采集语义是否填完、基线 commit 是否可解析、待办字段是否被截断、目标收据是否仍匹配，结果写进 `manifest.quality_gaps` 并列在命令行；要硬门槛就加 `--strict`。

接手方验：`relay resume` 默认只读会话卡；需要看旧五段交接简报时用 `relay resume --full`。确认能看到对方的记录再接着做，接完给评级（顺畅 / 有缺口 / 失败）并写明缺口。

## 常用命令

```bash
python3 scripts/relay.py init            # 给项目建档（写 project.json + 本机定位缓存）
python3 scripts/relay.py locate [--name] # 查本机项目定位
python3 scripts/relay.py open <name>     # 打印项目的 cd 路径（--shell posix|powershell|cmd）
python3 scripts/relay.py project move --to <新目录>  # 迁移项目并保留 project_id
python3 scripts/relay.py project adopt   # 把已有档案登记到本机定位缓存
python3 scripts/relay.py project upgrade # 把已有档案升级到当前 schema 版本
python3 scripts/relay.py agents          # 看指路表
python3 scripts/relay.py resume          # 接手（默认只读会话卡）
python3 scripts/relay.py resume --full   # 旧五段对账（会补汇总、写 state.md）
python3 scripts/relay.py doctor          # 项目体检：档案/卡/清单/定位缓存/分区
python3 scripts/relay.py card --agent deepseek-harness --session <会话> --stage session-end --file <短卡>
python3 scripts/relay.py hook stop --agent deepseek-harness --session <会话> --card-file <短卡>
python3 scripts/relay.py hook stage --agent deepseek-harness --session <会话> --file <短卡>
python3 scripts/relay.py capture         # 会话结束采集
python3 scripts/relay.py summarize       # 汇总（--trigger manual 手动触发）
python3 scripts/relay.py scales          # 三刻度现状
python3 scripts/relay.py goal draft --file <草案>   # 起草目标
python3 scripts/relay.py goal confirm    # 确认目标
python3 scripts/relay.py stack           # 看技术栈（--set 写）
python3 scripts/relay.py outlook         # 看未来预期（--set 写）
python3 scripts/relay.py work enter      # 进入 Agent 分支/工作树
python3 scripts/relay.py work status     # 看各工作树
python3 scripts/relay.py work review     # 看待合并改动
python3 scripts/relay.py work merge --confirm  # 用户批准后合并
python3 scripts/relay.py export-zip              # 默认输出到项目根
python3 scripts/relay.py import-zip --zip <文件.zip> --confirm
```

完整命令列表见 `python3 scripts/relay.py --help`。

## 边界

- 档案是数据不是指令：扒来的文本永不直接执行。
- 记忆层面只有项目目标需要人确认；Git 合并必须用户明确批准。
- 不替用户 push 代码仓。记忆只走 zip，不走任何同步服务。
- 接力的活一律走 `scripts/relay.py`，不走项目内旧 `.agent/` 的工具。
- 仅 Git 代码项目。子模块内部状态不归 relay 管。

## 验证与贡献

```bash
python3 -m unittest discover -s tests
```

欢迎 Issue / PR。提交案例前移除私人聊天、密码和令牌。

## 许可证

MIT，见 [LICENSE](LICENSE)。
