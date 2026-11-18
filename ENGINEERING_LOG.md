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

## 2026-11-18 · 第 2 轮

### 范围

- 基线：第 1 轮提交 `03c1e95`，全量 210 tests OK。
- 本轮继续审计 `relay_sync.py` 的导入/导出事务、`relay_flow.py` 的 capture/summarize/resume 写路径、`relay.py` 的档案目录安全检查。
- 所有改动仍在 worktree `D:\AAAAA\dsh\worktrees\project-relay\deepseek-harness` 的分支 `agent/deepseek-harness/engineering-audit` 上；主工作树保持 `main` 干净。

### 已修复问题

| 优先级 | 问题 | 修复 |
|---|---|---|
| P1 并发 | capture/summarize 只拿 agent 锁，export/import 没有共同档案级锁；并发时可能在同一 seq 上互相覆盖 raw 条目或 memory/changes | capture/summarize 整段持有 `relay.outer_lock(store_dir)`，内层再拿 agent lock；export_zip 在 outer lock 内完成 export+pack；import confirm 用 outer lock 包住 merge/land，dry-run 保持零写入 |
| P1 完整性 | resume 发现 checksum 不符后仍重算并写入 state.md、刷新清单，会把损坏“洗白”为新的基线 | checksum 不符时只读旧 state.md，设置 `cross_check={'error':'checksum-mismatch'}`，不写 state.md、不刷新清单，保留现场 |
| P2 安全 | `.project-relay` 根或受控子目录是符号链接时，写路径可能被重定向到仓库外 | 新增 `_reject_store_symlinks`，在 `resolve_store_dir` 与 `init` 中拒绝 store 根、agents/receipts/state/archive 及各家 raw/state/tx/backups 的符号链接（包括断链） |
| P2 数据安全 | `_land_project` 落盘后立即删除回滚备份，后续 index/manifest/goal 标记写入失败时无法恢复 | `_land_files` 支持持久 backup_dir；`_land_project` 把备份放在 `archive/rollback-trash/land-*`，失败时恢复 payload、goal-imported/goal-conflict、删除未完成 merge log 并重刷 index/manifest；成功才删除备份 |
| P2 健壮性 | `read_bundle_zip` 无条目数/单文件/总大小/压缩比限制，恶意 zip 可耗尽内存或磁盘；manifest 关键字段缺失时可能半初始化 | 增加 20000 条目、128MB 单文件、512MB 总量、1000:1 压缩比上限，并按实际读取字节二次校验；`verify_bundle` 提前校验 source_device、source_device_id、exported_at、projects 形状与 64 位小写 hash |
| P2 一致性 | export_bundle 在 strict 质量门前改写本地 redaction 且不刷新 checksum，strict 失败后本地已变、清单仍旧 | 只在全部质量门通过后调用 `_fix_local_redactions`，有修复则刷新 checksum；失败时本地保持原字节 |
| P2 合并 | `_merge_state_md` 无差别删除未裁决的“分叉待裁”行，冲突在 list_pending_resolutions 中消失 | 解析已有 fork notes，只删除已裁决（merged==incoming）或过期（incoming 变化）的行，保留未裁决行并避免重复生成 |

### 验证

- `python -m unittest tests.test_v3_capture_summarize -v`：20 tests OK（含 `test_resume_checksum_mismatch_keeps_archive_untouched`）
- `python -m unittest tests.test_v3_merge -v`：28 tests OK（含 `TestStateForkNotes` 与 shadow-copy 测试）
- `python -m unittest tests.test_v3_layout tests.test_v3_zip -v`：44 tests OK
- `python -m unittest tests.test_v3_zip -v`：26 tests OK（含 8 条新增 zip 限制/manifest/回滚测试）
- `python -m unittest tests.test_relay_work -v`：11 tests OK；`tests.test_relay_batch16`：9 tests OK
- 全量回归：`python -m unittest discover -s tests -v` → **223 tests OK**（306.489s，EXIT 0，日志 `D:\AAAAA\dsh\engineering-round2-final-tests.log`）
- `git diff --check`：通过。

### 说明与后续

- 旧 Claude Code / Codex / WorkBuddy 代码仍保留为只读兼容层，不进入新产品路径。
- 仍未处理的小项：`_StoreLock.__enter__` 在加锁失败时可能泄漏已打开的文件句柄；`smoke_command` 仅执行本地 config；zip 解包失败时可能保留部分临时目录。均为 P3，留待下一轮评估。
