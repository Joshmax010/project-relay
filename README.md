# Project Relay · 项目接力

让 Git 代码项目携带可审核、可回退的记忆，让下一个 Agent 接得住。

Project Relay 包含普通 Markdown 项目协议和 Agent Skill。当前版本只适配 Git 代码项目，同一时间一个 Agent 工作。正式项目文档由人审核，和对应代码进入同一个提交；未审核草稿仅供恢复线索。

## 工作流程

| 阶段 | 行为 |
|---|---|
| 接手 | 阅读正式记录，核对实际代码、分支和未提交改动 |
| 任务中 | 完成代码任务与验证，可保存隔离的未审核工作草稿 |
| 一轮结束 | 汇报结果，先询问是否同步项目文档，暂不提交 |
| 同意同步 | 起草完整更新，展示逐文件差异和证据 |
| 批准具体内容 | 记录实际人工批准，应用获批文档，拒绝项不改 |
| 提交前 | 核对代码、正式文档、Git 基线及实际暂存区与批准一致 |
| 提交 | 用户已授权时，将代码与对应获批文档一并提交 |

同意同步、批准具体内容、授权提交、授权上传是不同动作。用户拒绝同步则保留未提交工作。任务完成、文档同步完成、已经提交必须分别报告。

## 快速开始

下载或克隆本仓库到独立 `project-relay` 目录。支持 Agent Skills 的工具可安装整个目录；安装位置由工具决定。其他 Agent 可直接读取 `SKILL.md`。

> 使用 project-relay 初始化这个 Git 代码项目，保留现有规则，先生成未审核骨架。任务结束先问是否同步文档；同意后展示差异并等待具体批准。

需要 Git 和 Python 3.8+；脚本仅使用标准库：

```bash
python3 /path/to/project-relay/scripts/init_project.py /path/to/git-project
```

Windows 可用 `python`，带空格路径加引号。目标须是 Git 仓库根目录；不自动执行 `git init`。重复运行只补缺，不覆盖已有协议、记录或工具。骨架不是正式项目理解结果，填充内容也要人审。

在项目根目录：

```bash
python3 .agent/tools/relayctl.py resume
python3 .agent/tools/relayctl.py draft --file /path/to/proposal.json --title "本轮文档同步"
python3 .agent/tools/relayctl.py review <完整批次ID>
# 看过差异且用户实际批准后，才记录批准：
python3 .agent/tools/relayctl.py review <ID> --approve state.md sessions/task.md sessions/index.md --reviewed-by "项目负责人"
python3 .agent/tools/relayctl.py apply <ID>
python3 .agent/tools/relayctl.py lint
# 用户已授权提交，且代码与获批文档已正确暂存后：
python3 .agent/tools/relayctl.py commit-check
```

批次 JSON、部分批准、拒绝、首次提交及恢复示例见 [人审操作说明](references/review-workflow.md)。无 Python 时仍可遵守 Markdown 协议，手动展示差异、核对与审核。

## 项目文件

| 路径 | 用途与信任状态 |
|---|---|
| `.agent/agent.md` | 正式执行协议、角色和按需读取索引 |
| `.agent/human.md` | 人如何审核、接手与提交 |
| `.agent/project.md` | 正式目标、范围、约束与验收标准 |
| `.agent/state.md` | 已审核状态、阻塞、下一步及最近会话 |
| `.agent/structure.md` | 代码结构、构建与测试入口 |
| `.agent/decisions.md` / `lessons.md` | 已审核决策、教训、来源与可信程度 |
| `.agent/changelog.md` | 已审核项目变更概览 |
| `.agent/sessions/` | 已审核的执行与交接记录，随代码版本控制 |
| `.agent/conversations/index.md` | 经审核的原文/摘要索引 |
| `.agent/conversations/raw/` | 可直接存档的原文，默认不提交、不上传 |
| `.agent/inbox/` | 隔离草稿与待审批次，默认不进 Git；只作线索 |
| `.agent/.local/` | 本地批准收据、应用事务和无损备份，默认不进 Git |
| `.agent/tools/relayctl.py` | 项目携带的独立辅助脚本 |

初始化文件标注未知或未初始化；它们属于待审核骨架。首次提交须把所有新增正式 Markdown 一起审核。已审核项目迁移时携带完整 Git 仓库；未完成工作的草稿、原文与本地事务需按授权另外携带。

## 版本绑定与恢复

- 草稿记录目标基础哈希、提案内容哈希和代码/正式文档/Git 基线快照。内容或基线变化后必须重新起草与审核，旧批准不能套用到新代码。
- 支持部分批准和拒绝。状态、新执行会话及会话索引必须获批，才能完成同步；其他被拒绝文件保持不变。
- 提交检查核对完整快照与实际暂存区，拒绝遗漏代码、新文件、未获批正式文档及私有草稿。首次提交和代码删除也在检查范围内。
- 多文档应用前保存恢复事务，逐文件原子替换；中断时先 `recover`，避免把半更新状态误当作完成同步。恢复发现外部修改则停止，保留冲突。
- 本地原始备份按完整批次 ID 保存，避免秒级文件名碰撞。正式可回退历史由代码和获批文档的 Git 提交提供。

## 兼容性与边界

- 核心是普通 Markdown，相对路径与可选 Python 命令。兼容读取文件不代表各工具都自动发现 Skill；后续可直接要求阅读 `.agent/agent.md`。
- 第一版只适配 Git 代码项目；子模块、多 Agent 并发和非代码项目暂不支持。
- 审核命令记录人工批准声明，不验证人的身份。Agent 必须先获得用户对具体内容的批准；不能自我批准。
- `commit-check` 是提交前流程守卫，不执行 commit、不安装 Git hook、不改全局配置。直接绕过命令的 Git 操作不受技术阻断；手动流程也不具有脚本阻断能力。
- 健康检查只检查结构、链接、大小参考与恢复状态，不证明内容真实或用户批准。文档真实性仍由来源、验证与人工审核共同判断。
- 本轮尚未实现工具聊天采集器；原文导入与 Codex、Claude Code 采集适配另行实现。摘要不冒充原文，原文存档不自动授权上传。
- 旧项目协议与工具保留，按审核差异升级，不自动迁移或覆盖原有规则。Skill 不监听打开文件夹。
- 不保存密钥；按授权存档、提交和上传。原文需要脱敏时保留明确标记。

## 验证与贡献

```bash
python3 -m unittest discover -s tests -v
```

测试使用临时 Git 仓库，覆盖拒绝同步、部分批准、旧草稿冲突、批准后变更、暂存不完整、首次提交、代码删除、路径保护、应用中断与恢复冲突。跨厂商 Agent 的实际接力仍需实测。

欢迎 Issue / PR；提交案例前移除私人聊天、密码和令牌。协议修改应保持来源可追溯、按需读取与人审定稿。

## 许可证

MIT，见 [LICENSE](LICENSE)。辅助命令重新实现，没有复制上传的 project-handoff 脚本。
