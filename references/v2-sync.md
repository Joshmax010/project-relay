# project-relay v3 同步：ZIP 交接

> 实现：`scripts/relay_sync.py`（标准库 only），命令经 `scripts/relay.py`
> 分发（`export-zip` / `import-zip` / `sync-resolve`）。
> v3 只走 ZIP：Git 打包（pack/unpack）、同步命令（sync-setup/sync-claim/
> sync-rollback/sync-status）、服务器载体、文件夹载体、待认领、Ed25519 签名、
> WIP 快照、sync accept 已全部删除。v2 的开发史（D-sync-1 – D-sync-42、
> 三批 Codex 修复）在 git 历史里（batch 9、batch 10、第 17 批），
> 本文件只写现行规格；仍有效的编号只有 D-sync-3（新设备首次导入只留
> 一条基线分叉记录）。

## 1. 模型

- 一个项目，一份档案树：`<项目根目录>/.project-relay/`。没有注册表、
  项目键、同步配置、载体选择。
- 搬运物是 zip：`export-zip` 打包整树，`import-zip` 解进本机项目根目录
  的 `.project-relay/`，已有就按合并规则合。
- 本地无档案时 import 先 `relay.init` 再合；不走待认领，不等人点认领。

## 2. 导出包格式 2.0

- zip 内：`manifest.json` + `projects/<slug>/` + 整个 `.project-relay/` 树
  （扣掉不进包的文件，见 §3）。slug 由项目目录名经 `relay.slug_for` 得。
- manifest：`format`（`project-relay-export`）、`format_major`（2）、
  `format_minor`（0）、`exported_at`、`source_device` / `source_device_id`、
  `tool_version`（`project-relay-v3`）、`entry_count`、`redacted_count`、
  `files`（包内路径 → sha256）、`projects`、`quality_gaps`。
- 1.x 包（旧 `archive/` 布局）拒绝导入，报错含「主版本」；次版本不符只警告。
- `verify_bundle`：验 manifest 在、文件齐、哈希对、无清单外文件；
  篡改字节、缺 manifest、清单外文件一律拒绝。

## 3. 进包过滤（`_is_payload_file`）

进包的是档案本身：外层文件、各家 memory/decisions/lessons/backlog/changes/
goal/log/raw/state（last-summary + summaries）、receipts、目标机件暂存
（goal-draft/tentative）。

不进包（本机文件）：锁文件（`*.lock`）、完整性清单（`checksum-manifest.json`，
落盘后重算）、rollback-trash、外层 `config.json` 与裁决标记
（`state/device-notice.json`、`state/goal-conflict.json`、
`state/goal-imported.json`、`state/rollback-note.json`）、各家 state 里的
测量文件（cursor/baseline/numbers/digest/enforce.json）、恢复态
（`tx/`、`backups/`）、各家 stats（`stats/`）、安装记录（`recipe.json`）。
`stats/` 只写在 `<store>/stats/`，记账行不进清单也不进包。

## 4. 导出管线（`export_bundle` / `export_zip`）

1. 收集负载（整树单项目，要求 `goal.md` 在；无档案拒绝导出）。
2. 脱敏复扫（`_redact_payload`）：每份负载文本当不可信输入重扫；原料条目
   字段级脱敏（体部全文、头部除验算字段外），其余文件全文脱敏；非 UTF-8
   文件拒绝导出；复扫后验算字段损坏即中止，什么都不改。
3. 导出质量闸门（batch 9，`_export_quality_gaps`，结果写进
   `manifest.quality_gaps` 并列在命令行）：采集语义未填、基线 commit 本地
   不可解析、待办 Owner/验证字段被截断、目标收据与 goal.md 不一致。
   `export-zip --strict` 遇 block 级缺口拒绝产包。
4. 写 zip（目标已存在拒绝覆盖）。

## 5. 导入管线（`import_bundle` / `import_zip`）

1. 读包并校验（`verify_bundle`；任何一步失败整体拒绝且零写入）。
2. 脱敏复扫（`_redact_incoming`）：同导出复扫，包里带进来的秘密不落地。
3. 合并（`merge_project`，见 §6）。不带 `--confirm` 只干跑预览、零写入。
4. 落盘（`_land_project`）：影子校验 → 落盘 → 重算 `index.md` → 刷新
   `checksum-manifest.json` → 合并日志写 `receipts/merge-<时间戳>.json`。
   目标冲突标记写 `state/goal-conflict.json`；待决目标信息写
   `state/goal-imported.json`。

## 6. 合并规则（`merge_project`）

- 原料条目：按 key 并集；对方条目落入自家 `raw/`，用自家下一 seq
  （cap-0002 起，不进另一家）。同 key 不同 body：双方保留并记 conflict。
- decisions/lessons/backlog：按 `## ID` 块合（外层 + 各家）；待办同事项
  不同正文记 conflict。
- memory.md：受管块经 `_merge_state_md`（模板值不赢、空白采纳对方、
  真实冲突保留本地记分叉）+ `## 事实` 小节按键合（incoming 胜出并记一笔）。
- 目标：外层 goal.md 走收据合并（`_merge_goal`，目标永不自动合并，只认
  收据）；各家 goal.md 按记录的总目标哈希挑（对齐采纳、不对齐保留本地）。
- stack.md/outlook.md：本地非模板内容在、对方不同时留本地并记分歧；
  模板/空白时采纳对方。
- changes.md：按 `## 汇总 N` 并集；log.jsonl：按行并集；
  receipts/：按文件名并集，同名不同内容改名 `-imported`。
- 外层 state.md 与 index.md：永不采纳（记 note；state.md 由本机 resume
  现算，index.md 由落盘重算）。
- 各家 state/：last-summary 取 seq 高的一方、summaries/ 按文件名并集。
- tx/ 与 backups/：不进包故不合（本机恢复/保险，影子校验里 tx 门只看本机日志）。
- 未知文件：记 note「忽略未纳入合并的文件」；格式损坏与 id-hash 不符：
  进 discarded 并被 quarantined。
- 新设备首次导入（D-sync-3）：基线留本机值「未知」、对方值记一条分叉记录，
  其余零噪音。

## 7. 目标导入态与收据

- 目标确认只针对总目标；导入的目标先看收据：收据匹配采纳，收据不匹配
  留本地并记目标冲突（`state/goal-conflict.json`），目标注入随即冻结，
  直到正常 `goal confirm` 或 `sync-resolve --goal` 清掉标记。
- 采纳的目标文本同时暂存为草案，走一遍正常 `goal confirm` 完成本机确认。

## 8. 健康检查（`health_check`）

外层 7 文件 + 各家 6 文件（跳过 deepseek-honeycomb）+ 全家条目解析 +
各家 tx 门（tx 日志 files 键相对 agent 目录）+ 各家游标检查 + 收据解析。
memory.md 写冲突标记后报问题。

## 9. 待裁项（`list_pending_resolutions` / `sync-resolve`）

- 待裁来源三处：`state/goal-conflict.json`、各家 memory.md 里的分叉行、
  最新合并日志里未裁的 backlog 冲突。
- `sync-resolve --list` 只打印不写；`sync-resolve --goal/--fork/--backlog
  --decision keep-local|adopt-incoming` 裁决，写进档案、清掉标记、刷新
  清单、记入裁决记录。分叉 key 在多家 memory.md 里都有时须指定 `--agent`。
- 目标 adopt-incoming 须带 `--file`：目标正文故意不留在合并日志里，
  由用户从另一台设备拷来。

## 10. 与 resume 的接线

- resume 的五段之外另有两块只读补充（batch 10）：交接包指纹漂移
  （包导出后代码漂移了哪些文件）与目标对齐（各家目标哈希 vs 总目标哈希）。
- 导入后第一次 resume 跑四段兜底汇总（各家有 pending 的逐家汇总），
  简报里能看到对方的记录才算接上；接完给评级（顺畅 / 有缺口 / 失败）
  并写明缺口。
