# project-relay v2 同步（卷二第一片：2.1 导出/导入协议 + 2.2 私有 Git 仓载体）

来源：改造计划第 03 部分（2.1、2.2、2.5、S4、S6）。实现：`scripts/relay_sync.py`
（标准库 only，relay.py 惰性加载，与 relay_flow / relay_backfill 同体例）。
测试：`tests/test_relay_sync.py`。

> 先读警告：S6 的目录/文件清单是规划层面描述，与卷一实际布局不一致。
> 本片所有合并语义都落在卷一实际布局上，不凭空发明 S6 的目录名。

## 1. S6 规划 → v1 实际布局映射

| S6 规划说法 | v1 实际布局 | 合并落点 |
|---|---|---|
| goal/project.md（目标文件） | `archive/goal.md` | 永不自动合并，见 §4 |
| state/state.md（当前值型档案） | `archive/state.md`（受管块 `<!-- relay:state-begin -->` … `<!-- relay:state-end -->`） | 仅合并受管块，见 §4 |
| 追加型档案（decisions/lessons） | `archive/decisions.md`（`## D<n>`）、`archive/lessons.md`（`## L<n>`） | 按块体哈希取并集，重编号 |
| backlog（条目级并集） | `archive/backlog.md`（`## T<n>`，键为「事项」行） | 同事项同体跳过，同事项不同体冲突 |
| sessions/、cursors/、meta/ | 不存在 | 游标 = `state/cursor.json`（`{entry_seq, anchor_message}`），合并永不动；基线 = `state/baseline.json`，合并永不动 |
| 采集条目（JSONL、内容哈希 ID） | `raw/cap-*.md`（JSON 头 + `---relay-body---` + 体），头含 seq、queue、fact_hash、session、tool、anchor、fingerprint | 合并键 = `(source, session, fact_hash)`，见 §4 |
| 目标确认收据 | `receipts/goal-*.json`（seq、goal_hash、confirmed_at、channel、prev_receipt_hash 链）+ 同名 `.md` 快照 | 包内收据仅作信息，永不拷入本地 `receipts/` |
| 仓根 config.json、projects/、staging/、receipts/ | 仓根 `registry.json`、`projects/`、`global/`、`trash/`；项目仓六区 raw/archive/state/tx/backups/receipts | 包内只有 `projects/`；其余一律不进包 |

## 2. 导出包格式（2.1）

- 形态：目录；单文件时用 zip，内部结构不变（`write_bundle_zip` /
  `read_bundle_zip`，读包时拒绝绝对路径、`..` 与符号链接条目）。
- 根部 `manifest.json`，字段固定：`format='project-relay-export'`、
  `format_major=1`、`format_minor=0`、`exported_at`（ISO 8601 带时区）、
  `source_device`（用户可读名）、`source_device_id`（随机设备 ID）、
  `tool_version='project-relay-v2'`、`entry_count`（原料条目总数）、
  `redacted_count`（只记数）、`files`（包根相对路径 → SHA256，一律带
  `projects/<key>-<slug>/` 前缀）、`projects[]`（每项含 key、slug、
  display_name、remote、key_type、pack_seq、files 路径列表）。
- Git 载体把每项目清单另存为 `sync-manifests/<key>-<seq>.json` 并提交；
  该清单另带 `source_device` / `source_device_id` / `exported_at` /
  `format_minor`，因为 unpack 重建包时只有它可读。

### 负载纳入清单（按项目仓相对路径）

纳入：`archive/**` 全部递归文件（五件套 + `goal-history/` + `handoffs/` +
`splits/`）；`state/split-index.json`、`state/goal-tentative.md` / `.json`、
`state/goal-draft.md` / `.json`、`state/last-summary.json`、
`state/handoff-index.md`、`state/summaries/*.json`；`raw/cap-*.md`、
`raw/archived/cap-*.md`；`receipts/goal-*.json` / `.md`（仅作信息）。

一律排除：`state/config.json`（阈值偏好）、`enforce.json`、`digest.json`、
`numbers.json`（预压缩工作文件，可再生）、`backlog-notice.json`、
`pack-seq.json`、`goal-conflict.json`、`goal-imported.json`、
`rollback-note.json`、`cursor.json`、`baseline.json`（本地度量状态）、
`raw-index.json`（本地索引）、`tx/`（恢复状态）、`backups/`（本地保险，
S6 明言不同步），以及仓根的 `registry.json`、`global/`、`trash/`。
`_is_payload_file` / `_collect_payload` 是唯一事实来源。

### 导出前三件事（缺一不可，顺序见 §6）

1. 同步前汇总（消化全部积压）；2. 健康检查；3. 全部负载脱敏复扫。
   脱敏用 `relay_flow.redact` 的 14 类正则（单一事实来源，不在 sync
   模块另写正则）；复扫命中不仅改包内副本，同时原子写回本地文件
   （否则下次 pack 会重新导出未脱敏原文、在对端制造合并冲突）；
   manifest 只记命中数，不记原文。包内永不含明文密钥。

## 3. 导入管线（2.1）

`import_bundle(bundle_dir, store, confirm=False)`：

1. 只读 manifest 校验（`verify_bundle`）：格式名、主版本不一致拒绝并
   说明「需要先升级/降级工具」；次版本不同只警告、未知字段忽略；
   SHA256 逐一比对，任一不符整体拒绝、零写入，并指明文件。
2. 不可信输入处理：路径监狱（拒绝绝对路径、`..`、符号链接分量、
   冒号盘符、未规范化路径）；文本字段重跑脱敏；条目 ID 与内容哈希
   重算比对（capture 类用头内 fingerprint 的 anchor/code/gaps 重算，
   backfill 类用头内 backfill.session_key 与 events 重算），无法验证
   或不符的条目丢弃并计数，不使整个包失败；体部含冲突标记行的条目
   同样丢弃。
3. 干跑报告先给人看（新增/跳过/冲突/目标状态变化/丢弃分类）；无
   `--confirm` 时 `import_bundle` 与 `unpack`/`sync-claim` 保证零写入。
4. 确认后事务落盘：合并先在影子目录完成并通过健康检查，才落盘；
   落盘前把所有将被改动文件的当前字节备份到临时目录，中途失败则
   原子写回备份并删除新增文件、重新抛出错误；影子目录在健康检查
   失败时保留供排查（错误信息中给出路径），成功后删除。
5. 项目身份以 git remote 规范化地址为键匹配（键相等或规范化地址
   相等即匹配）；对不上落入待认领区 `unclaimed/<key>-<slug>/` +
   `_claim.json`，并更新 `unclaimed/_index.json`；导入全程不写注册表。
   注意 `normalize_remote` 对已是规范形的 `host/path` 字符串（无冒号）
   返回 None：匹配时绝不能直接比 `normalize(a) == normalize(b)`
   （None == None 会让所有项目误匹配），必须先比原始字符串相等，
   再比「双方都非 None 的规范化结果」。
6. 导入完成后生成本地回执（merge-log，见 §5），不进同步范围。

## 4. 合并规则（S6 落到实际布局，`merge_project` 纯函数）

- **原料条目**：按 `(source, session, fact_hash)` 取并集；同键同体跳过；
  同键不同体 → 冲突：双方保留（incoming 以新 seq 落盘，头加
  `merge_note` 说明来源与原 seq），并记入 merge-log；新键条目按本地
  最大 seq 递增重编号（文件名是本地 seq artifacts，导入时忽略源文件
  名）；incoming 条目按源 seq 顺序处理（不是 fact_hash 顺序，否则空
  本地仓的文件名会被打乱）；queue 状态映射：summarizing 视为 pending，
  summarized / archived 保持 summarized；已归档的 incoming 条目落入本地
  `raw/`（不直接进 `raw/archived/`），走本地正常生命周期。枚举一律用
  `_parse_local_entries_all`：同键冲突会产生同键多文件，字典版枚举会
  互相覆盖（last wins），游标校验与条目计数必须用列表版。
- **decisions/lessons**：按块体哈希（`## D/L<n>` 头行除外，正文行去尾
  空白后规范化）取并集，新块按本地最大号递增重编号。
- **backlog**：按「事项」行定键；同事项同体跳过；同事项不同体 →
  冲突（保留本地块，incoming 块全文写入 merge-log，不污染待办真相源）；
  新事项重编号追加。
- **state.md**：仅合并受管块。「已完成且有证据」行按 `；` 切分取并集
  去重（`无` 视为无值）；其他受管行（当前分支@基线、正在做什么、
  下一动作、已改动但尚未验证、阻塞待确认、最近会话）两边不同时保留
  本地行，并在 `STATE_END` 之后追加 `- 分叉待裁：<key>（对方）：<值>` 行；
  块外正文两边不同时保留本地、只在 merge-log 记冲突；本地无受管标记
  时若两边不同则保留本地并记冲突。
- **goal.md**：永不自动合并。本地无目标（缺失、空、等于骨架字节）→
  采纳 incoming 并写 `state/goal-imported.json` 待确认标记；两边字节
  相同且本地已确认（收据哈希匹配）→ 跳过；字节相同但本地未确认 →
  写待确认标记；字节不同 → 冲突（保留本地，写
  `state/goal-conflict.json`，记 merge-log）。收据文件本身永不拷入本地
  `receipts/`。
- **cursor.json 与 baseline.json**：合并永不触碰（见偏离 D-sync-2/3）。
- **splits/**：按内容哈希取并集；冲突按该文件下一个空闲号重命名，
  `split-index.json` 只追加新入分卷的条目。
- **goal-history/**：按内容哈希取并集；冲突重命名加 `-imported` 后缀。
- **handoffs/**：按内容哈希取并集；冲突重命名加 `-imported` 后缀并
  同步改索引行；`handoff-index.md` 按行取并集。
- **summaries/**：按内容哈希去重；文件名冲突但内容不同时重命名到
  下一个空闲序号（内部 seq 字段不动）；`last-summary.json` 本地已存在
  则保留、缺失则采纳。
- **goal-tentative.\***：仅当 goal 被采纳且其 `content_hash` 等于采纳后
  目标字节哈希时才采纳；**goal-draft.\***：仅当本地无草案时采纳，
  否则保留本地并记 merge-log。
- **冲突标记文件**：导入时整文件排除出合并（本地保留）；被排除的文件
  保存在 merge-log 同名目录的 `conflicted/` 下（文件名中 `/` 换成 `__`），
  双方保留，不丢。注意：pack 端健康检查也会拦截本地含冲突标记的档案
  （见 §6 步骤 ②），所以正常 pack 出来的包不应含标记文件；导入侧的
  排除是对外来/手工包的防线。
- **未知相对路径**：忽略并记 merge-log（对应次版本差异的未知字段规则）。
- **raw-index.json**：合并永不触碰（incoming 已归档条目落入本地 `raw/`，
  故本地索引继续有效）。

## 5. 目标导入态与回执（S4 落到 v1）

- `goal_status` 新增 `imported` 态：判定在收据哈希匹配与暂定记录匹配
  之后、`tampered` 之前，条件为 `state/goal-imported.json` 存在且当前
  目标字节哈希等于记录的 `source_goal_hash`。
- `goal_inject` 对 `imported` 不硬拦截（沿用 v1 暂定水印先例）：返回
  醒目横幅「以下目标未经本机确认，不可当作已确认目标执行；请先运行
  relay goal confirm --imported 一键确认：」+ 目标原文；
  `goal_check` 对 `imported` 放行（仅 tampered 拦截）。
- `goal confirm --imported`（`_confirm_imported`）：校验目标字节哈希与
  记录一致（不一致则报错、走正常草案确认流程），写本地收据（seq =
  本地最新收据 seq + 1，`channel='import'`，`prev_receipt_hash` 链向本地
  上一份收据，`imported_from` 记源设备/设备 ID/源收据 seq/源哈希），写
  同名 `.md` 快照，删除 `goal-imported.json`；正常确认成功时也删除残留
  的 `goal-imported.json`。
- `resume` 第一节的目标状态显示映射：`imported` 显示为「待本机确认」
  （目标词汇，不出现同步/导入字样）；resume 全程不调用 sync 模块。
- 回执与日志位置（仓根，不进同步范围）：`sync-config.json`（配置）、
  `sync-receipts/`（pack/unpack/merge/rollback 回执；merge-log 即
  `merge-<key>-<时间戳>.json`，含双方来源、适用规则、结果、冲突全文
  （backlog 冲突的 incoming 块全文）、计数、目标动作、丢弃清单；
  本地有 `state/rollback-note.json` 时 merge-log 作高优先级注记呈现）、
  `sync-manifests/<key>-<seq>.json`（每 pack 的清单文件，需提交）、
  `unclaimed/`（待认领区）。时间戳文件名用 UTC 紧凑格式（取 ISO 字符串
  中前 14 位数字），重名加 `-2`、`-3` 后缀。

## 6. 命令与 pack 固定顺序（2.2、2.5）

命令（relay.py 分发体例，relay_sync 惰性加载；sync 分支自己包
`except Exception`，因 RelayError 类身份问题）：

- `pack [--remote] [--device-name]`：pack 的调用本身就是用户确认
  （2.5 的提醒时机之一），无需 `--confirm`。
- `unpack [--remote] [--confirm] [--project <key>（可重复）]`：无
  `--confirm` 时只打印干跑报告、不写任何东西。
- `sync-setup --remote <url> [--device-name]`：写 `.gitignore`、写
  `sync-config.json`（`{carrier:'git', remote, device_name（缺省主机名）,
  device_id（uuid4 hex，沿用已有）, updated_at}`）、克隆远端做认领扫描
  （远端地址错误则 setup 直接报错；远端为空则跳过扫描并注明）；认领
  匹配只比项目键（远端目录名前 16 位 hex = 本地注册表行的 key，键相等
  即 remote 相等），输出「可认领 / 待认领区」两列清单，setup 本身不落
  盘任何项目负载（落盘走 unpack）。
- `sync-claim --key <key> [--confirm]`：用户须先对代码仓跑过 `relay init`
  （未建仓则 claim 内部自动 init）；按 `--key` 找到待认领目录，走同一
  合并预览/落盘（无 `--confirm` 只干跑），成功后删除待认领目录与索引
  条目。认领落盘不提交（文件进入本地仓，下一次 pack 一并提交）。
- `sync-rollback --to <ref> [--reason]`：要求活动仓是 Git 仓且 `--to` 的
  ref 可解析；用 `git archive <ref> projects/<key>-<slug>` 经 tarfile
  （`filter='data'`）解到临时目录；把当前已纳入包的文件集合备份到
  `sync-receipts/rollback-<时间戳>/`；删除当前包内文件集合、写入 ref
  的文件（本地文件如配置、执法计数等一律不动）；写本地文件
  `state/rollback-note.json`（`{ref, at, reason}`，reason 缺省为空字符串）；
  在活动仓提交（只暂存包内文件，note 文件不进提交）。
- `sync-status`（`--root` 指定单个项目，缺省列全部已注册项目）：只读；
  按项目读最新 pack 回执（`sync-receipts/pack-<key>-*.json` 按文件名
  时间戳取最新）、当前汇总记录数与原料条目数，算出「自 pack 后新增
  汇总数/新增条目数」，无回执则报「未 pack 过」；不写任何东西、不催促。

pack 固定顺序（2.5.3 + ⓪）：

1. ⓪ 拉取合并（远端已配置且可达时；远端为空则跳过；拉取失败则 pack
   中止并提示检查远端/网络）：先拉取远端并对目标项目做导入式合并
   （确定性规则、双方保留，目标冲突时保留本地并报告），不经过该步
   不提交。这是防覆盖纪律（2.2.5），不是可选优化。
2. ① 强制汇总（有 pending 才跑，`flow.summarize(source='pack',
   semantic_text=None)` 走确定性最小增量、零模型调用；失败则中止并
   指明环节）。
3. ② 健康检查（`health_check`，纯只读；problems 非空则中止）。
4. ③ 一致性自检（仅报告、不失败：`measure_change` 的分支/HEAD/漂移
   计数、受管块分支与当前分支不一致警告、工作区脏文件清单上限 20 条
   警告；漂移本身是正常现象）。
5. ④ 脱敏复扫（命中则原子修复本地文件并计数）。
6. ⑤ 生成包/提交/上传（包序列号取自 `state/pack-seq.json` 的 next，
   推送成功后才递增；提交信息由工具生成，含设备名、时间、条目计数；
   无 staged 变化时跳过提交但仍尝试 push；push 失败则中止并提示，且
   序列号不消耗）。
7. ⑥ 本地 pack 回执（时间、载体 git、远端、设备名/ID、项目键/slug、
   序列号、提交 SHA（或空）、条目总数、文件总数、脱敏命中数、汇总序号
   （或 noop）、拉取合并摘要、一致性警告、各步状态）。

unpack 流程：读同步配置（缺失则报错先运行 sync-setup）→ 临时克隆远端
（克隆失败报错；远端无提交则返回「远端无内容」；克隆后显式检出远端
分支——裸仓 HEAD 可能仍指向建仓默认分支而非 main，`git clone` 默认检出
会落空）→ 对每个远端项目拼出临时包目录（manifest.json + projects/
负载，清单只取 sync-manifest 列出的文件——git 不删旧文件，目录里可能
有旧 pack 的残留文件）→ 走同一导入管线（无 `--confirm` 只干跑）→
落盘后在活动仓提交合并结果（消息含源设备与序列号，不 push；push 留给
下一次 pack）→ 写聚合 unpack 回执 → 只读对账摘要（分支/HEAD 与基线
对比、目标状态、pending 数，不调用 resume——resume 第 4 段可写，
unpack 的写必须保持显式）。

Git 载体形态（计划字面 + 两处收紧）：记忆仓根目录自身就是 Git 工作区
（`sync-setup` 时 `git init -b main`，失败则退回普通 init；
`git remote add origin`，已存在则 set-url）；`.gitignore` 忽略
`registry.json`、`global/`、`trash/`、`sync-config.json`、
`sync-receipts/`、`unclaimed/`、`*.pyc`、`__pycache__/`（`sync-manifests/`
必须提交，故不忽略）；pack 只显式暂存包内文件清单 + 清单文件（不整
目录 add，避免把本地文件误提交）；unpack 永不直接 pull 进活动仓——先
`git clone` 到临时目录读远端内容，走导入管线合并，合并结果在活动仓
提交但不 push；pack 前必须先 pull 并合并（见上 ⓪）。

历史收敛（`_converge_history`，实现细节）：每台设备的 store 仓都从
自己独立的根提交起步，直接 push 第二台设备的提交会被远端以非快进
拒绝。内容合并（导入管线，双方保留）与历史合并是两回事：pack 提交后、
push 之前，以及 unpack 提交合并结果后，都先 `git fetch` 远端分支再
`git merge --allow-unrelated-histories -X ours`——工作树已经是内容合并
后的结果，`-X ours` 让它在每个冲突点都胜出，合并后的树与合并前完全
一致，只收敛历史、不改内容。远端已包含于本地时跳过；本地尚无提交时
跳过；fetch 失败时 pack 照常走到 push（push 报错会明说），unpack 记
`history_merge='fetch-failed'`。最新清单按 `exported_at`（而非本机
pack_seq）选取：pack_seq 是每设备自增计数，两台设备的 seq=1 会打平。

Git 树是各设备快照的并集：pack 按本地 slug 提交
`projects/<key>-<本地 slug>/`，所以远端树里同一项目会有多个 slug
目录（每台设备一个）。这是有意的：各设备快照互不覆盖。unpack 与
setup 的扫描必须按 key 去重（同一 key 只导入/列出一次，清单解析已
按 `exported_at` 取最新）；本机真正有效的目录永远是本地注册表行
slug 对应的那一个，不要直接读其他 slug 目录。

健康检查（`health_check(store_dir)`，纯只读，接受项目仓目录）：档案
五件套存在且 UTF-8 可读；`state.md` 受管标记成对出现（若存在）；全部
原料条目可解析（头 version==1 且含 seq、体部分隔线）；仅最新 tx 日志
做恢复预检（见 D-sync-8）；`cursor.json` 可解析且 entry_seq 为 0 或
指向现存条目；收据 JSON 可解析；档案文件不含冲突标记行。返回
`(problems, warnings)`；pack 在 problems 非空时中止。

## 7. 偏离清单（与计划不一致处，原因）

- **D-sync-1**：S6 规划目录与 v1 实际布局的映射（见 §1）。规划是
  目标描述，代码事实优先。
- **D-sync-2**：游标合并时永不推进。S6「取分界较晚者」的前提是全局
  事件时间游标；v1 游标是本地 seq 锚点，跨设备 seq 不可比；且合并把
  新条目排在本地最大 seq 之上、已汇总的 incoming 条目按 queue 过滤
  跳过，因此游标不动也不会丢活。
- **D-sync-3**：`baseline.json` 合并时永不覆盖。它描述本地代码状态
  （分支/HEAD/文件快照），跨设备覆盖会 corrupt 对账。
- **D-sync-4**：条目标识为 `(source, session, fact_hash)` + 体部比对。
  v1 条目是 Markdown + JSON 头，不是 JSONL；ID 哈希重算用头内
  fingerprint / backfill 字段。
- **D-sync-5**：目标导入交付带横幅的原文而非硬拦截。沿用 v1 暂定水印
  先例；「不可当作已确认用」由横幅 + `imported` 态 + 一键确认路径保证。
- **D-sync-6**：S4 goal_version 映射为 v1 收据 seq，跨设备版本连续性只
  记录在 `imported_from` 中，不强制 seq 相等（本地 seq 链保持本地连续）。
- **D-sync-7**：pack 第 ③ 步只报告不失败。漂移是正常现象（用户改代码
  不汇总是常态），失败项只属于 ②/④。
- **D-sync-8**：健康检查的「未恢复事务」指最新 tx 日志呈现部分应用：
  至少一个文件仍在事务前字节、同时另一个文件已离开事务前字节（事务
  开始应用却没写完）。tx 日志存在本身不算未恢复（summarize 成功后不
  删日志，它同时是回退记录）；已完成事务之后档案再演进（新汇总、合并
  使当前字节既非事务前也非事务后）是正常现象，不拦截——否则任何一次
  合并都会永久 brick 掉后续 pack/unpack（双向冒烟实测到的误报）。
  只有最新日志有意义（vol-1 recover 本身也只看最新日志）；旧日志被
  新汇总取代后不再作数。
- **D-sync-9**：unpack 提交合并结果但不 push（push 留给下一次 pack）。
  计划 2.2.4 只要求提交、未要求 push。
- **D-sync-10**：unpack 后对账为只读摘要，不调用 resume（resume 第 4 段
  可写，unpack 的写必须保持显式）。
- **D-sync-11**：脱敏复扫同时修复本地文件。只改包内副本会在下次 pack
  重新制造对端冲突。
- **D-sync-12**：同步回执用仓根 `sync-receipts/`，不用 S6 的仓根
  `receipts/` 名，避免与 v1 项目仓内目标收据区混淆。
- **D-sync-13**：回退注记为本地文件 + merge-log 注记，防覆盖的真正机制
  是 pack 的拉取合并前置（回退后旧设备若未 pull 就 pack，仍会被拉取
  合并挡下并报告冲突，而非静默覆盖）。
- **D-sync-14**：各载体清单路径统一为包根相对路径（带 `projects/` 前缀），
  Git 载体把每项目清单另存为 `sync-manifests/<key>-<seq>.json` 提交
  （单仓根清单会被多项目 pack 互相覆盖）。

## 8. 留给下一片（2.3–2.7）的接口与注意事项

- `import_bundle` / `merge_project` / `export_bundle` / `verify_bundle` /
  `health_check` 已是载体无关的纯管线：2.3（同步文件夹载体）与 2.4
  （自托管服务器载体）只需提供「取远端包目录」的方式，复用同一导入
  管线；不要为新载体另写合并规则。
- 2.3 注意：文件夹载体没有 git 的提交/回退语义——回退（`sync-rollback`）
  目前依赖活动仓是 Git 仓；文件夹载体需另给回退方案（例如用
  `sync-receipts/rollback-*` 备份做反向恢复），不要假装 git 可用。
- 2.5 提醒时机（仅在用户表达换设备意图时提醒）尚未接任何触发器：
  本片只做了 `sync-status`（只读可见）与 pack/unpack 命令；提醒文案与
  触发点是 2.5 的事，不得塞进 resume/capture 等同机流程。
- 2.6 冲突裁决 UI（分叉待裁、目标冲突的人工裁决入口）尚未做：
  本片只把冲突记进 merge-log 与 `goal-conflict.json` / 分叉待裁行；
  下一片给裁决命令时，裁决结果要写回档案并清掉对应标记，不能只改
  merge-log。
- `sync-claim` 目前落盘不提交（下一次 pack 一并提交）；若 2.7 要求认领
  即提交，在 claim 成功后补一次 `_commit_if_changes` 即可，路径前缀用
  本地 slug（见 unpack 的 slug 教训：远端 slug ≠ 本地 slug）。
- 远端 slug 与本地 slug 可能不同（同一 remote、不同检出目录名）：
  任何按 slug 拼 store 路径的代码都必须用本地注册表行的 slug，不得用
  包内 slug；包内 slug 只用于显示与待认领目录名。
- `state/pack-seq.json` 的 next 只有推送成功后才递增；新载体沿用同一
  规则（上传成功才消耗序列号），否则断线重 pack 会跳号。
- 本片未动 `SKILL.md`（仍是旧版人审流程）；v2 纪律的对外文档化等旧
  流程退役时统一重写，不要在本片中途改 SKILL。
