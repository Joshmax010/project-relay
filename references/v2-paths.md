# 路径表 v3（DeepSeek Harness 专用）

> 本版只记 DeepSeek Harness 的安装落点、技能扫描根与工作区规则。
> 旧版其他 Agent 的配方代码保留为只读兼容层，不在产品面暴露。
> **证据等级**：官方路径、实测、未验证必须分开记。

## 证据等级

| 等级 | 含义 |
|---|---|
| 官方口径 | 厂商官方文档原话，但未经本机实机验证 |
| 实测 | 在实机上用真实会话验证过可读性与格式 |
| 未验证 | 只有社区/第三方口径，或完全没证据 |

## DeepSeek Harness（唯一现役 Agent）

- **技能扫描根（从 DSH 本体确认）**：
  - 项目级：`<项目根>/.dsh/skills`（rank 100，最高优先）
  - 项目级：`<项目根>/.agents/skills`（rank 200）
  - 用户级：`~/.dsh/skills`（rank 400）
  - 用户级：`~/.agents/skills`（rank 500）
- **默认安装落点**：`<项目根>/.dsh/skills/project-relay/SKILL.md`，由
  `recipe install --tool deepseek-harness` 写入，注入 `agent_created: true`。
- **用户级安装**：`recipe install --tool deepseek-harness --home <用户目录>`
  写 `<用户目录>/.agents/skills/project-relay/SKILL.md`。
- **安装记录**：项目档案 `agents/deepseek-harness/recipe.json`（含 `skill_dir`）。
- **卸载**：`recipe uninstall --tool deepseek-harness` 按安装记录删技能目录。
- **证据等级：本机安装流程已实测**（脚本单测覆盖）；DSH 宿主侧真实会话
  仍需真机验证。

## Git 工作区规则（与 DSH 同一套 Skill）

- **项目仓库根**由 Git 的 `.git` 决定；`.project-relay/` 永远放在项目仓库根。
- **单 Agent（项目根 = DSH 工作区，或两者互相包含）**：直接在项目根用普通
  Git，可留在 main；`relay work enter` 不创建额外工作树。
- **多 Agent（项目根在 DSH 工作区之外）**：主工作树留 main 给用户验收；
  DSH 在 `<DSH 工作区>/worktrees/<项目名>/deepseek-harness` 建独立工作树，
  分支 `agent/deepseek-harness`；其他 Agent 同理各用 `agent/<名字>`。
- **合并**：只有用户明确批准后才跑 `relay work merge --agent <名字> --confirm`；
  冲突自动中止并保留原状。
- **共享记忆**：`.project-relay/` 只有一份，所有工作树通过 Git common dir
  共享；不要把记忆复制进工作树，也不要让工作树进入导出 zip。
