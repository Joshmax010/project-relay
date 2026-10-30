# 卷三 3.6：边界补全（boundaries）

计划依据：计划分卷 03 §3.6。本片只收口计划已列明的 7 个边界；新发现的
边界问题进待办与 lessons，不在本片扩围。实现不改核心语义。

## 3.6.1 子模块降级（submodule downgrade）

- 语义：旧 `snapshot()` 遇 mode 160000 的 gitlink 一票拒绝整仓；现改为
  指纹计算时跳过子模块路径及其子路径，记入快照 `gaps`（`{path, pointer}`，
  顺序取自 `ls-files --stage` 的确定性输出，快照相等性比较不受影响）。
  子模块内部状态不归 relay 管；子模块内想接力，须在子模块仓内单独建档
  （仓内联动不在本片范围，只在文档写明）。
- 标注位置（四处，措辞统一为计划原文）：
  1. 采集事实文本：`capture` 的缺口行后写「子模块 <路径> 未深入（指针 <sha>）」；
  2. 交接包保真度段：`handoff` 的 gaps 列表同款条目；
  3. 回溯保真度声明：`backfill` 两档（metadata / excerpt）都在 `assess_gaps`
     赋值后追加一条 `category=过程、impact=中` 的缺口，item 全文为
     「子模块 <路径> 未纳入接力范围，其内部状态不归 relay 管」，落盘进
     `archive/backfill-fidelity.md`（仅 summarize_after 且有新建条目时写）；
  4. 基线快照：`state/baseline.json` 的快照含 gaps。
- 结构说明：文档层见 `references/v2-foundation.md` 六区小节的子模块排除行；
  运行层即上述四处；不另造 per-archive 结构文件（避免无谓新产物与校验
  清单纠缠）。
- 命令：无新增命令。回归：`tests/test_relay_boundaries.py`
  `TestSubmoduleDowngrade`（旧 snapshot、新 snapshot_v2、回溯保真度文件）。

## 3.6.2 零档案启动（zero-archive start）

- 语义：`resume` 在本机记忆仓查无此项目时不报错，进「无档接手」分支。
  本片在保留既有行（“二选一：走回溯建档或从零 init”、“请回旧设备执行
  pack”）的前提下富化该分支：加只读缺料评估——提交数（`git rev-list
  --count HEAD`，无 HEAD 计 0）、旧 `.agent/` 是否存在、remote 是否存在；
  有旧材料或提交 >0 建议回溯建档，空仓建议从零 init。评估结果以
  `assessment` 字典返回，message 追加评估行与建议；**新文本不含“同步”
  二字**；全程零写（`projects/` 目录不得被创建）。
- 注意：全仓 grep 未发现旧测试断言“无档案必报错”（针对 resume），故无
  需同步更新的旧断言；既有三点（`no_archive` 为真、remote 提示存在与否、
  message 不含“同步”）全部保留并由回归测试钉死。
- 命令：`relay resume`（无新增命令）。回归：`TestZeroArchiveStart`。

## 3.6.3 无 git remote 的项目：弱身份与迁移（weak identity）

- 语义：项目身份键以 remote 为主；无 remote 的本地仓降级用弱身份键：
  `digest(解析后顶层路径 + '\0' + 排序后的全部根提交哈希)[:16]`。
  空仓无提交时根哈希部分为空串。祖父条款：旧公式（仅路径哈希）建的行
  按路径优先查找到后沿用既有键，绝不重 key（`register` 里 local→local
  键变化时沿用既有键并留注）。
- 标注位置：`state.md` 管理块**外**的文件尾一行，全文为
  「身份：弱身份（无 git remote；键为路径哈希 + 首提交哈希；加 remote
  后可迁移，见 relay migrate-key）」，由 `_ensure_weak_identity_line`
  在 init 时幂等追加（不碰管理块与用户内容）；迁移时
  `_strip_weak_identity_line` 按标记子串精确删行。
- 迁移（敏感区，可审计、可回查）：用户给仓加 remote 后，工具在下次
  pack 前**提示、不自动迁移**——`relay_sync.pack()` 调度器返回后调
  `pending_key_migration()`（只读），有待迁移时把提示挂到 pack 结果上
  （`key_migration_prompt`，绝不碰 manifest），CLI 打印提示文案（本次
  pack 仍用弱身份键、未自动迁移、迁移请运行 `relay migrate-key`）。
  显式迁移走共享 helper `_migrate_row_to_remote()`（`register` 的弱→强
  内联块已改调它，行为不变）：分仓目录改名（目标已存在时旧仓移入
  trash 兜底）+ tombstone 行（`status=migrated`、`migrated_to`）+
  `migrated_from` 追加旧键 + 新仓去弱身份行；全程留痕。
- 命令：`relay migrate-key`（无参，作用于 `--root` 指向的仓；拒绝情形：
  未登记 / 已是 remote 键 / 尚无 remote 或键已一致，文案明确）。
- 回归：`TestWeakIdentity`（弱身份建档、pack 提示且未自动迁移、迁移后
  目录改名/tombstone/`migrated_from`/档案零丢失/弱身份行清除、二次迁移
  拒绝、祖父条款）。

## 3.6.4 非 Git 项目：如实拒绝

- 语义：指纹、对账、快照都建立在 Git 之上；非 Git 目录明确不在支持
  范围。`init` 首行即调 `git_toplevel()`，非 Git 路径抛错，文案为
  「请指定 Git 仓库内的路径；非 Git 项目不在本版范围」——文案已存在，
  本片无需改代码，只补回归测试。
- 回归：`TestNonGitRefusal`。

## 3.6.5 超大仓指纹成本：增量缓存与阈值

- 语义：`snapshot_v2(toplevel, store_dir=None, threshold_s=None)`。
  缓存文件 `store_dir/state/fingerprint-cache.json`（原子写回）：
  键为 lstat 的 mtime_ns / ctime_ns / ino / size；symlink 另以 readlink
  目标计哈希，不缓存可执行位（可执行位每次从 stat 现算）；写回时剪掉
  不再列出的路径与被排除路径的条目。
  取舍（须知）：同尺寸且全 stat 字段不变的覆写会被漏判——与 git
  racy-clean 同款取舍，写入本文档，不藏着。
- 排除：配置 `fingerprint_excludes`（posix 前缀列表，精确等于或
  `前缀/` 开头即排除），来自 `state/config.json`（默认值 `[]`，经
  `DEFAULT_CONFIG` 合并）。被排除路径出现在快照 `excluded`（排序后），
  不进 code；采集事实文本与交接包 gaps 各追加「已排除（指纹排除配置，
  不计入指纹）」行；排除项从而写入保真度声明。
- 耗时与阈值：快照返回 `timing`（`elapsed_s`、`files`、`cache_hits`、
  `cache_misses`、`threshold_s`、`exceeded`、`first_snapshot`，其中
  `first_snapshot` = 本次运行前缓存文件不存在）。阈值默认取配置
  `fingerprint_threshold_s`（默认 30.0 秒），显式传参优先；首次建档时
  `relay fingerprint` 输出耗时行；超阈值时输出提示行（写明去
  `state/config.json` 的 `fingerprint_excludes` 配置、排除项写入保真度
  声明）。
- 调用点：6 处 `snapshot_v2` 已全部接上 `store_dir`（`relay_flow` 5 处 +
  `relay_backfill` 1 处）；旧无参调用仍兼容（无缓存、无排除，但仍返回
  timing/excluded 键）。
- 回归：`TestFingerprintCache`（命中/未命中/剪枝、阈值注入 `threshold_s=0`
  不真造大仓、排除配置、CLI 三行输出）。

## 3.6.6 多项目同仓：范围分区与显式拆分

- 语义：默认共用仓级一份档案，子项目以「范围」字段在状态卷内分区：
  配置键 `scope`（默认 `None` = 整仓），经 `DEFAULT_CONFIG` 合并。
- 显式拆分命令：`relay split --scope <仓内子目录> --name <新名>`。
  校验 scope 为仓内真子目录（拒绝 `.` 与越界）；拆分点取父最新汇总
  序号并格式化为 `sum-%04d`；子键 = 父键 + `'\0'` + scope 的哈希前 16 位，
  子 slug 由新名经 `slug_for` 生成，子行 `key_type='split'`，附
  `split_from`（父键、父 slug、scope、拆分序号、拆分点 id）；整仓目录
  复制到新仓目录（基线与 last-summary 原样复制，零丢失）；子配置写
  `scope` 与 `split_from`；父仓不动；注册表追加子行。
- 链指针语义对齐（3.3）：子仓首个汇总的 based_on 自然指向拆分点——
  因为 `_summarize_impl` 的链基取自 `fork.latest_summary(store_dir)`，
  而子仓克隆了父仓的 summaries，链头即拆分点；无需新指针格式。
- **寻址局限（须知）**：按路径解析仍返回父行（`find_row_by_path` 取首个
  匹配行），子仓目前经 key 寻址（`relay where --key`）与直接函数调用；
  完整的 `--scope` CLI 路由是后续工作，不得藏着。拆分 CLI 打印时已
  明示这一点。
- 回归：`TestMonorepoSplit`（默认 scope 为 None、拆分后子配置与
  `split_from` 正确、父仓不动、子仓链头为拆分点、父后续汇总不进子仓、
  `.` 与越界拒绝）。

## 3.6.7 平台差异表：见 references/v2-platforms.md

- 本片以核实 + 落表为主，不重复造轮子；核实结论：第三批已覆盖，
  未发现需补代码的缺口（审计表见 v2-platforms.md）。
