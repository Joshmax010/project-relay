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

## 2026-11-18 · 第 3 轮

### 范围

- 基线：第 2 轮提交 `067fe84`，全量 223 tests OK。
- 本轮审计完整性清单边界、配置读取健壮性、锁句柄生命周期、ZIP manifest 形状与统计配置来源。

### 已修复问题

| 优先级 | 问题 | 修复 |
|---|---|---|
| P1 完整性 | 正常 capture→summarize→capture 后 checksum 清单报 `agents/deepseek-harness/log.jsonl` 与 `state/fingerprint-cache.json` 不符：本机状态文件被纳入 manifest | 在 `relay.py` 增加 `LOCAL_ONLY_OUTER`、`LOCAL_ONLY_AGENT_STATE`、`LOCAL_ONLY_AGENT_FILES`、`LOCAL_ONLY_AGENT_DIRS` 与 `is_local_only_path`；清单与 `relay_sync` 共用同一组“本机文件”定义；capture/summarize/log/agent-goal/stack/outlook 写后刷新清单 |
| P2 可维护性 | `relay_sync` 自己维护 `_LOCAL_ONLY_*` 集合，容易与 manifest 规则漂移 | 改为 `relay.LOCAL_ONLY_*` 别名，`_is_payload_file` 统一调用 `relay.is_local_only_path` |
| P2 健壮性 | `load_config` 只校验 dict，`pending_cap` 等字段为字符串/列表时后续比较或除法抛 TypeError | 新增 `_is_int` / `_validate_config`，校验所有数值、列表、可空字符串配置项；非法 JSON 转 `RelayError` |
| P2 配置 | `relay_stats._read_config` 只读外层 `config.json`，文档与 `relay_flow` 的 knob 在 `state/config.json`，月度 token 上限不生效 | 先读外层再叠加 `state/config.json`（state 优先），非法 cap 置 None |
| P3 锁 | `_StoreLock.__enter__` 在 open/加锁失败时可能泄漏句柄，`__exit__` 对未初始化句柄不安全 | 局部 `fh` 成功后才赋 `self._fh`，异常时关闭并置 None；`__exit__` 先判空，解锁异常收窄为 `OSError/ValueError` |
| P3 完整性 | `stack --set` / `outlook --set` 写外层正式文件后不刷新 checksum，下一次 resume 会误报 | `write_stack` / `write_outlook` 写后调用 `update_checksum_manifest` |
| P3 ZIP | `verify_bundle` 只要求清单路径以 `projects/` 开头，`projects/other/` 文件会被静默丢弃；项目 slug 未在验证阶段约束 | 校验 slug 匹配 `_PROJECT_SLUG_RE` 且非 `.`/`..`，所有清单文件必须在 `projects/<slug>/` 前缀下 |

### 验证

- `python -m unittest tests.test_v3_manifest_config -v`：13 tests OK（manifest 本机状态、旧清单兼容、capture-after-summarize、log/stack/outlook 刷新、锁句柄、配置校验、stats 月度 cap）
- `python -m unittest tests.test_v3_zip -v`：28 tests OK（含新增 2 条其它项目/非法 slug 校验）
- `python -m unittest tests.test_v3_layout -v`：18 tests OK
- 定向回归（zip/merge/capture_summarize/stats_fork/redact）：124 tests OK（218.348s）
- 全量回归：`python -m unittest discover -s tests -v` → **238 tests OK**（323.600s，EXIT 0，日志 `D:\AAAAA\dsh\engineering-round3-final2-tests.log`）
- `git diff --check`：通过

### 说明与后续

- 本轮不改变 ZIP 包格式；只加严 manifest 校验与本机文件边界。
- 仍未处理的小项：`smoke_command` 仅执行本地 config（本地配置，不是远程 RCE）；zip 解包失败时可能保留部分临时目录；均属 P3，下一轮评估。

## 2026-11-18 · 第 4 轮

### 范围

- 基线：第 3 轮提交 `8a4365c`，全量 238 tests OK。
- 本轮审计 ZIP 导出/导入辅助函数的失败原子性，以及 `export_bundle` 的 strict 质量门执行顺序。
- 所有改动仍在 worktree `D:\AAAAA\dsh\worktrees\project-relay\deepseek-harness` 的分支 `agent/deepseek-harness/engineering-audit` 上；主工作树保持 `main` 干净。

### 已修复问题

| 优先级 | 问题 | 修复 |
|---|---|---|
| P1 数据完整性 | `write_bundle_zip` 直接写目标 zip，打包中途失败会留下半成品；`export_zip` 又拒绝覆盖已存在目标，半成品会成为永久障碍 | 改为先写同目录 `tempfile.mkstemp` 临时文件，全部条目写完后 `os.replace` 原子发布；异常时删除临时文件并重新抛出 |
| P1 数据安全 | `read_bundle_zip` 直接向目标目录逐条解压，校验通过但后面某条解压损坏时会留下前面的文件 | 改为在同级 `.relay-unzip-*` 临时目录解压，全部成功后再替换目标目录；任何异常清理临时目录并重新抛出，保留原有条目数/大小/压缩比限制 |
| P2 质量门 | `export_bundle` 先创建目标目录并写入 payload，最后才检查 strict 阻断项；直接调用时 strict 拒绝会留下部分导出文件 | 把 strict 阻断检查提前到 payload 写入之前，阻断时只可能留下空目标目录，不写任何文件 |

### 验证

- `python -m unittest tests.test_v3_zip -v`：31 tests OK（新增 3 条失败原子性/空目标回归）。
- 全量回归：`python -m unittest discover -s tests -v` → **241 tests OK**（325.913s，EXIT 0，日志 `D:\AAAAA\dsh\engineering-round4-tests.log`）。
- `git diff --check`：通过。

### 说明与后续

- 本轮不改变 ZIP 包格式，也不改变 manifest 结构；只修失败路径上的临时文件/半成品清理。
- `export_bundle` strict 拒绝时目标目录可能为空目录（`export_zip` 使用临时目录，不受影响）；后续可考虑连空目录也不创建。
- 仍未处理的小项：`smoke_command` 仅执行本地 config（本地配置，不是远程 RCE）；`relay_work.merge` 在无 Git 身份的宿主机上会因 `Author identity unknown` 失败；均属环境/低风险项，下一轮评估。

## 2026-11-18 · 第 5 轮

### 范围

- 基线：第 4 轮提交 `4acbf88`，全量 241 tests OK。
- 本轮审计 `relay_sync.py` 导出失败清理与 `relay_stats.py` 统计行容错，补对应回归。

### 已修复问题

| 优先级 | 问题 | 修复 |
|---|---|---|
| P2 可靠性 | `export_bundle` 在 strict 质量门前创建目标目录，strict 拒绝会留下空目录 | 把 `dest.mkdir` 后移到 strict 检查通过后、payload 写入前；测试改为断言目标目录不存在 |
| P2 稳定性 | `relay_stats.compute_fuse` 直接读取 `r['llm_tokens']`，旧版/手工 stats 行缺失字段会 KeyError，resume/保险丝崩溃 | 新增 `_token_int`、`_row_llm_tokens`、`_row_epoch`；过滤非 dict 行；缺失 total 回退 `llm_in_tokens + llm_out_tokens`；异常 epoch 归零 |

### 验证

- `python -m unittest tests.test_v3_zip tests.test_v3_stats_fork -v`：61 tests OK（45.867s）。
- 全量回归：**243 tests OK**（329.321s，EXIT 0，日志 `D:\AAAAA\dsh\engineering-round5-tests.log`）
- `git diff --check`：通过。

### 说明与后续

- 本轮不改变导出包格式或 manifest 结构。
- 仍未处理的小项：`relay_work.merge` 在无 Git 身份宿主机上会因 `Author identity unknown` 失败；`smoke_command` 仅执行本地 config（本地配置，不是远程输入）；下一轮评估。

## 2026-11-18 · 第 6 轮

### 范围

- 基线：第 5 轮提交 `9193092`，全量 243 tests OK。
- 本轮审计 `relay.py` 的 `init` 档案目录边界，并复核 `relay_work.py` 的 enter/status/review/merge 分支逻辑。

### 已修复问题

| 优先级 | 问题 | 修复 |
|---|---|---|
| P2 安全 | `relay.init` 只拒绝 `.project-relay` 根 symlink，未拒绝已存在的 `agents`/`receipts`/`state`/`archive` 子目录 symlink；仓库预置 `.project-relay/agents` 指向外部时，init 会写入仓库外 | 在 init 写骨架前，若 store 已存在则调用 `_reject_store_symlinks(store_dir)`；补 `test_init_rejects_symlinked_agent_dir`（宿主不支持 symlink 时 skip） |

### 验证

- `python -m unittest tests.test_v3_layout -v`：19 tests OK（1 skipped：宿主不支持创建 symlink）。
- 全量回归：**244 tests OK**（332.140s，EXIT 0，skipped=1，日志 `D:\AAAAA\dsh\engineering-round6-tests.log`）
- `git diff --check`：通过。

### 说明与后续

- `relay_work.py` 的 enter/status/review/merge 本轮未发现高严重度问题；`relay_work.merge` 仍依赖宿主机 Git identity（本地已配置 `DSH Agent <dsh@local.invalid>`）。
- 下一轮继续审计统计/分叉模块和 CLI 错误路径；发现低风险问题直接修复并补回归。

## 2026-11-18 · 第 7 轮

### 范围

- 基线：第 6 轮提交 `7430eaa`，全量 244 tests OK（skipped=1）。
- 本轮继续审计 `relay_sync.py` 的并发导出路径与目标文件保护。

### 已修复问题

| 优先级 | 问题 | 修复 |
|---|---|---|
| P2 并发 | `export_zip` 的目标 zip 已存在检查在获取 `relay.outer_lock` 之前；另一个导出进程可在检查与真正写入之间创建目标，随后 `write_bundle_zip` 的 `os.replace` 会静默覆盖已有 zip | 在 `with relay.outer_lock(store_dir):` 内再次检查 `zip_path.exists()`，不存在才继续写；新增 `test_export_zip_rechecks_target_under_lock` 覆盖竞争窗口 |

### 验证

- `python -m unittest tests.test_v3_zip -v`：32 tests OK（28.365s）。
- 全量回归：**245 tests OK**（346.266s，EXIT 0，skipped=1，日志 `D:\AAAAA\dsh\engineering-round7-tests.log`）
- `git diff --check`：通过。

### 说明与后续

- 本轮仅收紧导出目标文件保护，不改变 ZIP 包格式。
- 下一轮继续审计分叉/统计/CLI 错误路径。

## 2026-11-18 · 第 8 轮

### 范围

- 基线：第 7 轮提交 `5bc4c47`，全量 245 tests OK（skipped=1）。
- 本轮继续审计 `relay_fork.py` 的分叉链读取容错。

### 已修复问题

| 优先级 | 问题 | 修复 |
|---|---|---|
| P2 稳定性 | `_package_records` 对 `state/chain-archives/*.json` 只捕获 JSON 解析错误；合法 JSON 但顶层为列表或 `records` 非列表时会在 `pkg.get(...)` / 迭代时崩溃，导致 `resume` / `detect_forks` / `list_summaries` 失败 | 读取后校验顶层为 dict、`records` 为 list；不合法包安全跳过。新增 `test_malformed_chain_package_does_not_crash` |

### 验证

- `python -m unittest tests.test_v3_stats_fork -v`：31 tests OK（23.289s）。
- 全量回归：**246 tests OK**（353.152s，EXIT 0，skipped=1，日志 `D:\AAAAA\dsh\engineering-round8-tests.log`）
- `git diff --check`：通过。

### 说明与后续

- 分叉链包格式不变；只是损坏数据不再阻断 resume。
- 下一轮继续审计 CLI 错误路径与统计/分叉的剩余边界。

## 2026-11-20 · 第 9 轮（会话卡）

### 范围

- 基线：`main@7be4240`（目标落档与定位文档已提交）。
- 实现「项目是宿主、Agent 是住户」的会话卡：会话结束和阶段节点写短卡，默认 resume 只读卡，冲突只标 `[待用户确认]`。

### 已实现

| 项 | 说明 |
|---|---|
| 会话卡写入 | `relay card --agent ... --session ... --stage session-end|milestone --file ...`；默认 3000 字符上限，超长拒写，脱敏后校验；不读 raw 条目、不做后台监听 |
| 默认接手 | `relay resume` 只读每家最新短卡并显示成本；旧五段对账保留为 `relay resume --full`（会补汇总、写 state.md） |
| 冲突处理 | 同一事实跨卡/记忆不一致时并列显示并标 `[待用户确认]`，不自动选边；同会话同阶段的分叉卡双方保留 |
| 成本 | 会话卡 LLM 成本进入同一周熔断与月度软上限；resume 显示本月用量/上限；周熔断时停写卡，月度上限只告警 |
| ZIP | 会话卡作为 payload 进入导出；导入按内容并集，同事件不同内容保留双方，不覆盖 |

### 验证

- `python -m unittest tests.test_v3_session_cards -v`：14 tests OK。
- `python -m unittest discover -s tests -v`：260 tests OK（382.242s，EXIT 0，skipped=1，日志 `D:\AAAAA\dsh\session-cards-full-tests-final.log`）。
- `git diff --check`：通过。

### 说明与后续

- 本项目 `.project-relay/state/config.json` 已设 `monthly_llm_token_cap = 200000`，resume 成本段会显示；默认未配置项目仍为 None（只统计不告警）。
- 下一步：接入 DSH 的 Stop/阶段钩子自动写卡；再统一 README/SKILL 与 references 的旧五段说明。

### 复审修复（2026-11-21）

- 里程碑卡分组改按 `event_id`：同一会话的旧 milestone 不再进入 resume / 对账；只有同 event_id 的导入分叉副本双方保留。
- `inject --budget N`：卡片与跨 Agent 核对合计后按总 token 预算截断，输出不超过 N。
- `llm_usage`：calls / input_tokens / output_tokens 三字段缺一不可，缺失或非法直接拒写，不再标成「已实测」。
- 卡头 `session` 也走脱敏；纯密钥形式的 session 脱敏为空时拒写。
- Claude / Codex 安装说明补 `relay card` 步骤，和默认 resume 读卡对齐。
- `RELAY_SKILL_TEXT` 读取根目录 SKILL.md 时增加缺失回退；新增 SKILL 两份逐字节一致测试。
- 验证：`python -m unittest tests.test_v3_session_cards -v`：19 tests OK；全量 **265 tests OK**（344.462s，EXIT 0，skipped=1，日志 `D:\AAAAA\dsh\session-cards-review-full-tests.log`）；`git diff --check` 通过。

### 第二轮复审修复（2026-11-22）

- 正文脱敏后为空也拒写：全密钥形状的卡正文不会生成新卡、不会顶掉上一张有效卡。
- 导入包内部两张同 event_id、不同正文的卡也会记为 `session-card-same-event-different-body` 冲突，两张都保留。
- 默认 `resume_cards` 也记一笔 handoff 统计；统计失败不阻断接手。
- `resume_cards` / `inject` 只解析一次 `latest_session_cards`，cross check 与渲染共用。
- `resume_cards` 增加 checksum-manifest 校验；卡被改过时挂【档案完整性】提示。
- 验证：`tests.test_v3_session_cards`：24 tests OK；全量 **270 tests OK**（377.803s，EXIT 0，skipped=1，日志 `D:\AAAAA\dsh\session-cards-review2-full-tests.log`）；`git diff --check` 通过。

## 2026-11-21 · 项目身份证与定位

### 范围

- 基线：`main@7be4240`（目标落档与定位文档已提交）。
- 实现项目身份证、机器本地定位缓存，以及 init 遇到 Agent 私有区的提醒。

### 已实现

| 项 | 说明 |
|---|---|
| 项目身份证 | `relay init` 写 `.project-relay/project.json`：稳定 `project_id`、名称、创建时间；幂等，不改已有 ID |
| 本机定位缓存 | `~/.project-relay/registry.json` 记录 project_id、名称、路径、last_seen；可重建、不进 ZIP、不算身份 |
| 查找命令 | `relay locate [--name] [--json]` 查项目；`relay open <name>` 打印可执行的 `cd` 路径 |
| 私有区提醒 | `relay init` 检测 `~/.dsh`、`~/.agents`、`~/.claude`、`~/.codex`、`~/.workbuddy`、`DSH_PROFILE_DIR`，提示迁移到用户项目区 |
| ZIP | `project.json` 进入 payload；导入保留本地 project_id，ID 冲突只记 note，不覆盖本地身份 |

### 验证

- `python -m unittest tests.test_v3_identity -v`：6 tests OK。
- 全量回归：**252 tests OK**（333.387s，EXIT 0，skipped=1，日志 `D:\AAAAA\dsh\project-identity-full-tests.log`）。
- `git diff --check`：通过。

### 说明与后续

- 私有区只提醒不阻止，迁移目前是建议路径；自动迁移命令留给下一轮。
- 定位缓存只做机器本地索引；跨设备仍只走 zip。

### PR13 复审修复（2026-11-23）

- fresh import 采用包内 `project.json` 的 project_id，并刷新本机定位缓存，不再现造新 ID。
- `relay open` 使用 shell 安全引用，新增 `--shell posix|powershell|cmd`；CMD 输出 `cd /d`。
- 导入的 `project.json` 非对象时忽略并记 note，不再 AttributeError。
- 注册表读改写加 `~/.project-relay/registry.lock` 排他锁。
- `relay open` 对不存在路径报错退出；`locate` 默认标出不存在行。
- 注册表 version 非数字时回退 1；`locate --name` 支持子串匹配。
- 新增 fresh-import / existing-import 身份测试、shell 注入测试、坏版本/坏身份测试。
- 验证：`tests.test_v3_identity`：12 tests OK；全量 **258 tests OK**（477.677s，EXIT 0，skipped=1，日志 `D:\AAAAA\dsh\project-identity-review-full-tests.log`）；`git diff --check` 通过。

### 项目迁移命令（2026-11-23）

- 新增 `relay project move --to <新目录>`：移动项目目录、保留 project_id，并更新本机 registry 路径。
- 新增 `relay project adopt`：把已有 `.project-relay` 登记到本机定位缓存。
- init 私有区提醒现在指向 `relay project move --to`。
- 验证：`tests.test_v3_identity`：16 tests OK；全量 **262 tests OK**（442.910s，EXIT 0，skipped=1，日志 `D:\AAAAA\dsh\project-identity-final-full-tests.log`）；`git diff --check` 通过。

## 2026-11-21 · 第 10 轮（DSH Stop / 阶段钩子）

### 范围

- 基线：`session-cards@1e35dc7`（会话卡 PR #11 未合并，本轮以栈式分支开发）。
- 给 DSH 的 Stop / 阶段钩子提供写卡入口，不改变 capture/enforce 既有行为。

### 已实现

| 项 | 说明 |
|---|---|
| Stop 钩子 | `relay hook stop --agent deepseek-harness --session <会话> --card-file <短卡>`：先采集、再执法，最后写 `session-end` 卡；卡片失败只返回可见错误，不阻断采集/执法 |
| 阶段钩子 | `relay hook stage --agent deepseek-harness --session <会话> --file <短卡>`：写 `milestone` 卡，不重复 capture |
| 兼容 | 原 `hook stop` 仍默认 `claude-code`、不传卡片时行为不变；Claude 现有 hook recipe 不受影响 |
| 文档 | README / SKILL / references 补 Stop/阶段钩子命令与失败语义 |
| DSH 插件 | `dsh-plugin/project-relay-hooks`：监听 `workspace/session-stop` 和 `session/event`，自动调用 hook stop/stage；通过 Plugins > Add plugin 安装 |

### 验证

- `python -m unittest tests.test_v3_dsh_hooks -v`：10 tests OK。
- `python -m unittest tests.test_v3_dsh_hooks tests.test_v3_hooks_recipe tests.test_v3_session_cards -v`：56 tests OK。
- 全量回归：**280 tests OK**（435.017s，EXIT 0，skipped=1，日志 `D:\AAAAA\dsh\dsh-stop-cards-plugin-full-tests.log`）。
- `git diff --check`：通过。

### 说明与后续

- 本 PR 交付 DSH hook 命令入口、DSH 插件包和测试；插件通过 DSH Plugins > Add plugin 安装后监听 Stop/阶段事件。
- 卡片正文由调用方（DSH Stop/阶段事件）提供，不在 hook 内读取原始聊天。

### PR12 复审修复（2026-11-23）

- CLI `hook stop` 的 `--card-file` 读取失败或 `--llm-usage` 解析失败不再中断 capture/enforce；警告后继续。
- CLI `hook stage` 同样兜底坏输入；失败只丢阶段卡。
- SKILL 的 DSH 钩子说明补“接了 Stop 钩子就不用再手动 card/capture”。
- 分支已 rebase 到 PR11 新 head `1f24906`。
- 验证：`tests.test_v3_dsh_hooks`：9 tests OK；全量 **279 tests OK**（421.298s，EXIT 0，skipped=1，日志 `D:\AAAAA\dsh\dsh-stop-cards-review-full-tests.log`）；`git diff --check` 通过。

### PR12 插件事件修复（2026-11-23）

- `agent/turn-stopping` 从 `input.agent.session` 取 session/id/cwd，修掉真实 Stop 载荷下 hook stop 不触发的问题。
- `session/disposed` 改为独立 `ctx.on` 清理 `counts` / `lastText`，不再放在 `session/event` 里。
- 新增 Node 事件形状测试：真实 `{agent:{id,session:{id,header:{cwd}}},turn,signal}` 断言触发 hook stop；空会话只警告不崩。
- 验证：`tests.test_dsh_plugin` 1 test OK；全量 **301 tests OK**（421.049s，EXIT 0，skipped=2，日志 `D:\AAAAA\dsh\pr12-plugin-followup-full-tests.log`）；`git diff --check` 通过。
### PR13 落地后清理（2026-11-24）

- 坏卡容错：`list_session_cards` 跳过损坏卡并收集错误，`resume_cards` 在完整性横幅列出，不再因一张坏卡崩掉接手。
- `adopt_project` / `move_project` 在 `ensure_project_file` 前调用 `_reject_store_symlinks`，防止符号链接档案目录写到仓库外。
- PowerShell `relay open` 改用 `Set-Location -LiteralPath`，方括号路径不再被当通配符。
- `pyproject.toml` data-files 增加 `share/project-relay/SKILL.md`，`_load_skill_text` 增加 pip 安装位置回退；新增 packaged-layout 测试。
- 新增坏卡、PowerShell 方括号、packaged SKILL 回退测试；符号链接测试在本机因缺少权限跳过。
- 验证：全量 **304 tests OK**（424.756s，EXIT 0，skipped=3，日志 `D:\AAAAA\dsh\pr13-cleanup-full-tests.log`）；`git diff --check` 通过。
### 自动交接（2026-11-24）

- `relay export-zip` 的 `--zip` 改为可选；不传时自动生成 `<项目根>/<项目名>-project-relay-<时间>.zip`，默认落在项目根。
- `import-zip` 落盘后无论新/旧档案都登记本机定位缓存；fresh import 仍采用包内 project_id。
- 新增默认导出路径、fresh import 注册、existing import 注册测试。
- 验证：`tests.test_v3_handoff_auto`：3 tests OK；全量 **303 tests OK**（398.058s，EXIT 0，skipped=2，日志 `D:\AAAAA\dsh\auto-handoff-full-tests.log`）；`git diff --check` 通过。
### resume 分区首屏 + 月度上限校准（2026-11-25）

- `resume` 首屏新增「你的分区」：当前分支为 `agent/<name>` 时显示 Agent、分支、工作树和只写 `agents/<name>/` 的边界；项目根显示单 Agent 模式。
- `monthly_llm_token_cap` 默认值从 None 校准为 200000（软告警，不拦截）；项目可在 `state/config.json` 调整或显式设 null 关闭。
- 更新 `references/v2-stats.md`；新增 Agent 分区/单 Agent 首屏测试，更新默认 cap 断言。
- 验证：全量 302 tests OK（387.091s，EXIT 0，skipped=2，日志 `D:\AAAAA\dsh\resume-partition-cap-full-tests.log`）；`git diff --check` 通过。
