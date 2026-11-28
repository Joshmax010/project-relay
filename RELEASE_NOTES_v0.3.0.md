# Project Relay v0.3.0

发布日期：2026-11-27

## 本次发行

- 技能包版本：0.3.0
- 发行文件：`dist/project-relay-dsh-skill-v0.3.0.zip`
- 包含完整技能：`SKILL.md`、`scripts/`、`assets/template-v2/`、`references/`
- 包含 DSH 插件：`dsh-plugin/project-relay-hooks/`
- 安装自检会检查上述文件是否齐全

## 主要变化

- DSH Stop / 阶段钩子：`relay hook stop` / `relay hook stage`，配套 `project-relay-hooks` 插件
- DSH 插件真实事件修复：`agent/turn-stopping` 用 `agent.id` + `ctx.sessions.get` 取 Session，用 `ctx.workingDirectory.get` 取当前项目目录
- `relay resume` 首屏「你的分区」，支持 `agent/<name>` 分支/工作树识别
- 会话卡默认上限 3000 字符，坏卡不阻断接手，成本保险丝和月度软上限（默认 200000）
- 项目身份证 `project.json` 增加 `schema_version`，新增 `relay project upgrade`
- `relay doctor` 项目体检：身份证、定位缓存、.gitignore、配置、校验清单、坏卡、分区
- `relay export-zip` 默认落项目根，`relay import-zip` 自动登记定位缓存
- 默认导出 zip 使用 `.git/info/exclude` 本地排除，不弄脏已跟踪 `.gitignore`

## 安装

1. 解压得到 `project-relay/`。
2. 放到 DSH 扫描的技能目录：
   - 项目级：`<项目根>/.dsh/skills/project-relay/`
   - 用户级：`~/.agents/skills/project-relay/`
3. 安装 DSH 插件：

   ```bash
   dsh plugin --profile <profile> add <技能目录>/dsh-plugin/project-relay-hooks
   ```

   或在 DSH Plugins 界面选择该本地插件目录。

## 升级已有档案

旧档案没有 `schema_version` 时，先跑：

```bash
relay project upgrade
```

它会补齐版本并刷新 checksum manifest；之后用 `relay doctor` 确认健康状态。

## 验证

- 全量：334 tests OK（skipped=3）
- 发行 zip：36 entries，含插件文件（插件已包含真实 DSH 事件修复）
