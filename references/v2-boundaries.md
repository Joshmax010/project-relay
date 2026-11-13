# v3 边界补全

本片只收口计划已列明的边界；新发现的边界问题进待办与 lessons，不在本片
扩围。实现不改核心语义。已删的边界（弱身份与迁移、多项目同仓拆分、回溯、
晋升）随功能删除，下文只留 v3 仍有的；删掉的在每节末尾写明去向。

## 3.6.1 子模块降级（submodule downgrade）

- 语义：指纹计算时跳过子模块路径及其子路径，记入快照 `gaps`（`{path, pointer}`，
  顺序取自 `ls-files --stage` 的确定性输出，快照相等性比较不受影响）。
  子模块内部状态不归 relay 管；子模块内想接力，须在子模块仓内单独建档。
- 标注位置（两处，措辞统一）：
  1. 采集事实文本：`capture` 的缺口行后写「子模块 <路径> 未深入（指针 <sha>）」；
  2. 基线快照：`agents/<家>/state/baseline.json` 的快照含 gaps。
- 命令：无新增命令。回归：v3 套件暂无子模块专项测试（代码在 snapshot_v2
  里跳过 gitlink，见 relay_flow.py 约 943 行）；补测试是后续工作。

## 3.6.2 零档案启动（zero-archive start）

- 语义：`resume` 在本项目查无档案时不报错，进「无档接手」分支。分支内容：
  只读缺料评估——提交数（`git rev-list --count HEAD`，无 HEAD 计 0）、
  旧 `.agent/` 是否存在；有 remote 的 clone 项目提示档案可能在旧设备上，
  回旧设备 export-zip 再 import-zip；没有就 `relay init` 从零建档，不猜不编。
- 注意：全程只读，不创建 `.project-relay/` 目录；message 里不含“同步”二字。
- 命令：`relay resume`（无新增命令）。回归：`tests/test_v3_cli.py`
  `TestErrorPaths`。

## 3.6.3 弱身份与迁移：已删除

v2 的项目键、注册表、弱身份（无 remote 时路径哈希键）与 `migrate-key`
已随 v3 删除。项目身份就是项目根目录本身，不存在需要迁移的键。

## 3.6.4 非 Git 项目：如实拒绝

- 语义：指纹、对账、快照都建立在 Git 之上；非 Git 目录明确不在支持
  范围。`init` 首行即调 `git_toplevel()`，非 Git 路径抛错，文案为
  「请指定 Git 仓库内的路径；非 Git 项目不在本版范围」。
- 回归：`tests/test_v3_layout.py`
  `TestInitLayout.test_init_refuses_non_git_directory`。

## 3.6.5 超大仓指纹成本：增量缓存与阈值

- 语义：`snapshot_v2(toplevel, store_dir=None, threshold_s=None)`。
  缓存文件 `<store>/state/fingerprint-cache.json`（原子写回）：
  键为 lstat 的 mtime_ns / ctime_ns / ino / size；symlink 另以 readlink
  目标计哈希，不缓存可执行位（可执行位每次从 stat 现算）；写回时剪掉
  不再列出的路径与被排除路径的条目。
  取舍（须知）：同尺寸且全 stat 字段不变的覆写会被漏判——与 git
  racy-clean 同款取舍，写入本文档，不藏着。
- 排除：配置 `fingerprint_excludes`（posix 前缀列表，精确等于或
  `前缀/` 开头即排除），来自 `state/config.json`。被排除路径出现在快照
  `excluded`（排序后），不进 code；采集事实文本追加「已排除（指纹排除配置，
  不计入指纹）」行。
- 耗时与阈值：快照返回 `timing`（`elapsed_s`、`files`、`cache_hits`、
  `cache_misses`、`threshold_s`、`exceeded`、`first_snapshot`，其中
  `first_snapshot` = 本次运行前缓存文件不存在）。阈值默认取配置
  `fingerprint_threshold_s`（默认 30.0 秒），显式传参优先；首次建档时
  `relay fingerprint` 输出耗时行；超阈值时输出提示行（写明去
  `state/config.json` 的 `fingerprint_excludes` 配置）。
- 回归：`tests/test_v3_stats_fork.py` 的 `TestFingerprintCache`
  （二次快照命中缓存、排除配置、阈值注入 `threshold_s=0` 不真造大仓、
  CLI 指纹耗时行）。

## 3.6.6 多项目同仓拆分：已删除

v2 的 `relay split`（按 scope 拆子仓、子键、注册表）已随注册表删除。
默认一份档案管整仓；子项目想分开记，在子目录另建一份档案。

## 3.6.7 平台差异表：见 references/v2-platforms.md

- 本片以核实 + 落表为主，不重复造轮子；核实结论：第三批已覆盖，
  未发现需补代码的缺口（审计表见 v2-platforms.md）。
