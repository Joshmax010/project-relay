# ENGINEERING_LOG

项目：project-relay（D:\AAAAA\Josh Project\project-relay）
维护方式：工程审计与低风险修复，每轮记录在案。

## 2026-11-18 · 第 1 轮

### 范围

- 基线：主工作树 `main@3a68f89`，全量测试 203 tests OK（基线日志见 D:\AAAAA\dsh\engineering-baseline-tests.log）。
- 审计范围：`scripts/relay.py`、`scripts/relay_flow.py`、`scripts/relay_sync.py`、`scripts/relay_work.py`、`pyproject.toml`、相关测试。
- 本轮全部改动在 worktree `D:\AAAAA\dsh\worktrees\project-relay\deepseek-harness` 的分支 `agent/deepseek-harness/engineering-audit` 上；主工作树保持 `main` 干净。

### 已修复问题

| 优先级 | 问题 | 修复 |
|---|---|---|
| P1 安全 | `uninstall_deepseek_harness` 直接信任 `recipe.json` 的 `skill_dir`，篡改后可删除任意目录 | 新增 `_skill_dir_for_uninstall`，只接受 `.../skills/project-relay` 且父目录为 `.dsh` 或 `.agents` 的安装路径；新增篡改记录拒绝测试 |
| P2 打包 | `pyproject.toml` 只打包 6 个模板，实际 `assets/template-v2` 有 11 个；pip 安装后 `init` 写空模板，`_skeleton_bytes()` 可能 `FileNotFoundError` | 补齐 `agent-goal.md`、`changes.md`、`memory.md`、`outlook.md`、`stack.md` 等 11 个模板；新增 `_resolve_templates_dir()`，兼容源码、技能包与 pip 的 `share/project-relay/template-v2` |
| P3 版本 | `relay --version` 输出 `3.1.0`，与 `pyproject.toml` 的 `0.2.0` 不一致 | 新增 `VERSION = '0.2.0'`，`--version` 与测试统一读取该常量 |
| P3 死代码 | `recipe package` 分支不可达（parser 没有 `package` 子命令），且属于旧 WorkBuddy 产品面 | 删除不可达分支；`recipe` 安装/卸载只保留 DSH |
| P2 数据安全 | `import_bundle` 在 `confirm=False` 的干跑也会调用 `relay.init`，违反“零写入” | 只有 `confirm=True` 且 store 不存在时才初始化；干跑保持纯预览 |
| P3 可用性 | 默认分支 `agent/deepseek-harness` 已被已有子分支 `agent/deepseek-harness/dsh-only` 占用时，`relay work enter` 直接失败 | 新增 Git ref 前缀冲突检测；默认分支冲突时回退 `agent/deepseek-harness/work`，显式 `--branch` 冲突时给出清晰错误；review/merge 支持识别子分支并优先使用已登记 worktree |

### 验证

- `python -m unittest tests.test_relay_work -v`：11 tests OK
- `python -m unittest tests.test_relay_batch16 -v`：9 tests OK
- `python -m unittest tests.test_v3_zip -v`：18 tests OK
- 全量回归：`python -m unittest discover -s tests -v` → 210 tests OK（304.399s，EXIT 0）。
- `git diff --check`：通过，无空白错误。

### 说明与后续

- 旧 Claude Code / Codex / WorkBuddy 代码保留为读取旧档案的兼容层，不再进入新产品路径；本轮未删除历史函数，避免破坏旧档案读取与旧测试。
- `recipe.json`、`state/config.json` 属本地文件，不随 zip 导入；本轮只做路径结构校验，未改变同步包格式。
- 后续候选（尚未处理）：`read_bundle_zip` 尚无解压总大小/单文件大小上限；`smoke_command` 仅执行本地 config；这些属于 P3 健壮性，不影响本轮已修复问题。
