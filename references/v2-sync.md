# project-relay v2 同步（卷二第一、二片：2.1–2.5）

来源：改造计划第 03 部分（2.1、2.2、2.5、S4、S6）。实现：`scripts/relay_sync.py`
（标准库 only，relay.py 惰性加载，与 relay_flow / relay_backfill 同体例）。
测试：`tests/test_relay_sync.py`（第一片）、`tests/test_relay_sync2.py`（第二片）。

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
  `format_major=1`、`format_minor=1`（第二片起：新增加密预留字段，见 §9）、
  `exported_at`（ISO 8601 带时区）、
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

- `pack [--remote] [--device-name] [--carrier git|folder|server]`：pack 的
  调用本身就是用户确认（2.5 的提醒时机之一），无需 `--confirm`。
  未指定 `--carrier` 时按配置载体分发（见 §9.1）。
- `unpack [--remote] [--confirm] [--project <key>（可重复）]
  [--carrier git|folder|server]`：git/server 载体无 `--confirm` 时只打印
  干跑报告、不写任何东西；文件夹载体无包可导，unpack 为校验 + 对账
  （仍写一份对账回执），`--confirm` 对其无意义。
- `sync-setup --remote <url> [--device-name]`：写 `.gitignore`、写
  `sync-config.json`（`{carrier:'git', remote, device_name（缺省主机名）,
  device_id（uuid4 hex，沿用已有）, updated_at}`）、克隆远端做认领扫描
  （远端地址错误则 setup 直接报错；远端为空则跳过扫描并注明）；认领
  匹配只比项目键（远端目录名前 16 位 hex = 本地注册表行的 key，键相等
  即 remote 相等），输出「可认领 / 待认领区」两列清单，setup 本身不落
  盘任何项目负载（落盘走 unpack）。
- `sync-setup --carrier folder --folder <目录> [--device-name]`：见 §9.2
  （写 `{carrier:'folder'}` 配置 + `~/.project-relay-location` 指针 +
  一次冲突扫描；默认仓已有项目且非同一仓时拒绝）。
- `sync-setup --carrier server --server-type <webdav|sftp|https|local>
  --server-url <url> [--server-user <u>] [--server-token <t>]`：见 §9.3
  （token 只进 `sync-credentials.json`（0600），亦可用
  `RELAY_SERVER_TOKEN` 环境变量；`local:` URL 缺省推断 `--server-type
  local`）。
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
- `sync-rollback --bundle <包目录或 zip> [--reason]`：文件夹/服务器载体
  专用，见 §9.2（git 载体传 `--bundle` 会报错）。
- `sync-status`（`--root` 指定单个项目，缺省列全部已注册项目）：按项目
  读最新 pack 回执（`sync-receipts/pack-<key>-*.json` 按文件名时间戳取
  最新）、当前汇总记录数与原料条目数，算出「自 pack 后新增汇总数/
  新增条目数」，无回执则报「未 pack 过」；输出行含 `carrier` 字段
  （文件夹载体 seq/commit 为空时显示 `—`）；不催促。唯一例外：
  文件夹载体下 `sync-status` 会执行隔离式冲突扫描（见 D-sync-16），
  其余载体下纯只读。

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

## 8. 留给下一片（2.6–2.7）的接口与注意事项

- 2.3（同步文件夹载体）与 2.4（自托管服务器载体）已在本片后半段落地，
  见 §9；2.5 提醒时机已落地，见 §9 末尾。以下仍是下一片（2.6–2.7）的事。
- 2.6 冲突裁决 UI（分叉待裁、目标冲突的人工裁决入口）尚未做：
  本片只把冲突记进 merge-log 与 `goal-conflict.json` / 分叉待裁行；
  下一片给裁决命令时，裁决结果要写回档案并清掉对应标记，不能只改
  merge-log。裁决后若改了 `archive/` 下文件，须刷新校验清单
  （`relay.update_checksum_manifest`），否则下次 resume 会报清单不符。
- `sync-claim` 目前落盘不提交（下一次 pack 一并提交）；若 2.7 要求认领
  即提交，在 claim 成功后补一次 `_commit_if_changes` 即可，路径前缀用
  本地 slug（见 unpack 的 slug 教训：远端 slug ≠ 本地 slug）。
- 远端 slug 与本地 slug 可能不同（同一 remote、不同检出目录名）：
  任何按 slug 拼 store 路径的代码都必须用本地注册表行的 slug，不得用
  包内 slug；包内 slug 只用于显示与待认领目录名。
- `state/pack-seq.json` 的 next 只有推送/上传成功后才递增；新载体沿用同一
  规则（上传成功才消耗序列号），否则断线重 pack 会跳号。
- 本片未动 `SKILL.md`（仍是旧版人审流程）；v2 纪律的对外文档化等旧
  流程退役时统一重写，不要在本片中途改 SKILL。

## 9. 第二片：文件夹载体（2.3）与服务器载体（2.4）、提醒时机（2.5）

### 9.1 与第一片管线的衔接（两载体都不另写合并规则）

- 文件夹载体没有「远端包」：同步文件夹本身就是记忆仓，`pack` 不生成
  包、不提交、不消耗序列号，只做「隔离冲突副本 → 校验清单验证 →
  强制汇总 → 健康检查 → 一致性自检 → 脱敏复扫 → 刷新清单 → 写 pack
  回执」（回执 `seq`/`commit` 为空，`sync-status` 显示为 `—`）。
- 服务器载体复用同一导入管线：`pack` 第 ⓪ 步下载 current 包 →
  `import_bundle(confirm=True)` 合并；`unpack` 下载 current 包 → 同一
  `import_bundle`（干跑/落盘与 git unpack 完全一致）。`export_bundle` /
  `verify_bundle` / `merge_project` / `health_check` 一律复用。
- 三载体 pack 固定顺序相同（2.5.2/2.5.3），只有第 ⓪ 步（拉取合并的
  来源）与第 ⑤ 步（提交/上传/仅刷新清单）随载体而变；任一步失败停在
  该步并给出可执行的下一步提示（例如外来心跳锁活跃时 pack 中止并
  提示「等对方写完后再 pack」，而不是带着未汇总原料打包）。

### 9.2 文件夹载体（2.3）

- 启用：`relay sync-setup --carrier folder --folder <目录>`（或先
  `--store-root` 指向文件夹再 setup）。配置只含 `{carrier: 'folder',
  device_id, device_name, updated_at}`，不存文件夹绝对路径（两台设备
  的路径本就不同）；`~/.project-relay-location` 指针文件记录本机文件夹
  位置（`PROJECT_RELAY_HOME` 环境变量优先于指针，测试隔离不受影响）。
  设备身份只存本机 `~/.project-relay-device.json`，绝不进同步仓。
- 拒绝条件：本机默认仓已有项目且与目标文件夹不是同一仓时 setup 拒绝，
  提示先 pack 导出或手动迁移（防止无声启用第二个仓、旧档案被 stranded）。
- 写入纪律：一切写入仍走原子写（临时文件 + rename）。2.3.2 的「JSONL
  按日切分」不映射到 v1 布局：v1 条目已是每条目独立文件
  （`raw/cap-*.md`），爆炸半径天然限于单文件，不另造日切分机制（偏离
  D-sync-18）。
- 心跳锁（2.3.3）：实现在 `relay_flow.summarize` 的 wrapper 内（而非
  relay_sync 模块，避免 relay_flow ↔ relay_sync 循环 import，且第一片
  docstring 已声明 sync 模块不被同机流程调用）。文件夹载体下，汇总前
  写本设备心跳锁 `heartbeat-<device_id>.json`（`{device_id, device_name,
  at, at_epoch}`），写完删除；发现另一台设备 10 分钟内的活跃锁时，
  本次汇总降级为只采集不汇总：采集文件按设备分名
  （`cap-<seq>-<device_id 前 8 位>.md`，`list_entries` 的 `cap-*.md` glob
  与 `_next_seq` 天然兼容，同 seq 不同设备内容共存时以 fact_hash 为
  合并键），并写 `state/device-notice.json`，供下次 resume 第 3 段展示
  「另一台设备近期在写此项目」；下一次本机成功汇总后清除该文件。
  锁过期（>600 秒，如写崩溃残留）视为失效；时钟漂移会缩短有效窗口，
  属启发式而非锁保证（边界：两台设备同时长时间写同一项目不在支持
  范围内，损失只降到「双方都保留、人工裁决」）。
- 冲突副本（2.3.4）：`scan_folder_conflicts(store, isolate=True)` 递归
  扫描整个仓根（跳过 `.git`、`trash/`、`sync-receipts/`，避免隔离自己
  的隔离产物与合并日志证据），按 basename 匹配：`冲突副本`、
  `conflicted copy`、`.sync-conflict-`、` (conflict `、`（conflict`
  （大小写不敏感匹配拉丁部分）。合法文件 `state/goal-conflict.json`
  不命中（它含 `-conflict.` 但不含上述模式）。命中即移入
  `trash/conflicts/<时间戳>/`（相对路径 `/` 换 `__`，重名加 `-2` 后缀，
  永不删除）并写 `conflicts-<时间戳>.json` 原路径记录。扫描点：
  文件夹 pack 第 ⓪ 步（必须在收包前，否则冲突副本会随 `archive/` 被
  当成负载）、文件夹 unpack、`sync-status`（见 9.4 落点解释）。
- 完整性自检（2.3.5）：`state/checksum-manifest.json`，scope 仅
  `archive/**`（排除 `archive/rollback-trash/**`；清单外的多余文件一律
  忽略，只断言清单内文件存在且哈希一致）。不变量：一切档案写入方都
  刷新清单——summarize（全部载体，非 noop 时）、`_land_project` 落盘后、
  `goal_confirm`/`goal_decline`、git/包回退后、`recover` 后；capture /
  enforce 只写 `raw/` 区，不影响清单。resume 第 2 段验清单（不符时
  列出文件），第 4 段遇不符则带病横幅并跳过汇总（「先从其他设备或
  最近导出包恢复，恢复前不汇总、不写档案」）。注意：清单一旦存在就
  会被 resume 检查，因此「只在文件夹载体刷新」是不够的——这是第二片
  实测踩到的坑（验收 Act 3 曾因此 FAIL），见 D-sync-20。
- 删除传播防护（2.3.6）：工具从不真正删除档案文件。包回退把被替换的
  当前负载文件改名（非删除）到 `archive/rollback-trash/<时间戳>/`，
  且该目录已排除出负载与清单；`sync-receipts/rollback-<时间戳>/` 另存
  一份备份。整个记忆仓目录被误删只能靠 2.1 导出包兜底（文案见 2.7，
  留给下一片）。
- 回退（2.3 边界）：文件夹载体无 git 语义，`sync-rollback` 必须带
  `--bundle`（目录或 zip），走 `rollback_from_bundle`：先
  `verify_bundle`（不可信输入全套校验，失败零写入），按包内项目键
  匹配本地项目（slug 不同时以包内 slug 定目录），当前负载备份 +
  改名进 rollback-trash，写入包内负载，写 `state/rollback-note.json`，
  刷新清单，无提交。无 `--bundle` 时报错并给出路径（先 pack 留导出
  包，或用 `sync-receipts/rollback-*` 备份恢复）；git 载体传 `--bundle`
  时报错（请用 `--to ref`）。

### 9.3 服务器载体（2.4）

- 后端接口（字节存取，`get/put/exists/list_dir/rename/remove_tree`，
  相对路径一律过 `_jail_check`）：`LocalDirBackend`（本地目录，
  `local:` scheme，测试主力）、`FaultBackend`（包装任意后端，可注入
  写 tmp 途中失败与读失败）、`CurlBackend`（WebDAV MOVE 改名 /
  HTTPS PUT/GET/HEAD，Bearer 头；`allow_move=False` 的纯 HTTPS 端点在
  换位时拒绝）、`SftpBackend`（rsync over ssh 上传下载 + ssh mv 改名，
  端点形如 `user@host:/base/path`，认证走 ssh 密钥/agent）。
  注意：Curl/SFTP 后端未接真实服务器实测（沙箱内无真实端点），代码为
  直接的 curl/rsync 调用，留待真机验证时再验。
- 远端布局：`relay/<项目键>/current/` 一套完整包文件（manifest.json +
  `projects/...`），外加 `relay/<项目键>/current-index.json` 快速索引
  （换位之后写；`current/manifest.json` 是权威比对源，index 写崩溃不
  影响比对）。上传先写临时目录
  `relay/<key>/.tmp-<seq>-<device8>/` 并逐文件读回验哈希，再做目录
  换位（现存 current/ 改名 `.old-<seq>-<时间戳>/`，临时目录改名
  current/，删 `.old`）；换位中途崩溃导致 current/ 缺失且 `.old` 存在
  时，下一次读先把 `.old` 恢复为 current/（见 `_read_server_current`）。
  换位不是单文件原子改名，是两步目录换位 + `.old` 恢复 + manifest
  权威比对（偏离 D-sync-19）。
- 防覆盖比较键 `(seq, device_id)`：允许上传当且仅当无现存包，或现存包
  哈希与待上传包相同（幂等重传），或现存键严格小于待上传键；拒绝当
  且仅当现存 seq 更大，或 seq 相等但 device_id 不同（拒绝文案用计划
  原话「先在本机拉取最新包合并，再重新导出」）。同设备同 seq 不同
  哈希允许覆盖（属本设备自己的重试；若拒绝会因「上传成功但本地 seq
  未递增」的崩溃窗口而永久 brick）。现存包 seq 取自 manifest 的
  `projects[0].pack_seq`，独立导出包该字段为空时回退读
  `current-index.json` 的 seq（否则比较键失效）。
- pack-seq 规则沿用第一片：`state/pack-seq.json` 的 next 只有上传成功
  后才递增，断线重 pack 不跳号（`upload_server_bundle` 本身不消耗
  序列号，调用方成功后才 bump）。
- 下载与导入合一：`download_server_bundle` 按 manifest 逐文件下载并
  验哈希（缺失或不符报错），再进 `import_bundle` 全套校验，不提供跳过
  校验直接覆盖的捷径。服务器 unpack 落盘后不提交（无 git 语义），
  只做只读对账 + 就绪门。
- 凭据：地址与用户名进 `sync-config.json`，口令/令牌只进独立文件
  `sync-credentials.json`（`save` 后 `chmod 0600`，配置中只记
  `credential_file` 指针，不记口令原文），凭据永不进导出包与同步内容；
  CLI 也可用 `RELAY_SERVER_TOKEN` 环境变量供 token（`--server-token`
  优先）。`local:` 后端无需凭据。
- 加密预留（2.4.6）：manifest 增加 `encryption: 'none'` 与
  `encryption_salt: None` 字段；`FORMAT_MINOR` 由 0 升为 1（次版本差异
  只警告不拒绝，minor-0 旧包仍可导入，测试已覆盖）。整体加密本身
  推迟到卷三，但字段定义已定死：未来加密包须把 `encryption` 置为
  方案名、`encryption_salt` 置为派生盐（格式预留）。

### 9.4 提醒时机（2.5）落地清单

- 唯一提醒时机只有三个，落点如下（其余时机一律不提醒）：
  ① 用户主动执行 pack 命令：pack 本身就是提醒与执行合一的入口，
  各载体 pack 输出含步骤与回执；
  ② 用户在对话中明说要换设备/换电脑：Agent 侧话术模板——先跑
  `relay sync-status` 看未打包情况（上次 pack 时间、载体、包序列号、
  自 pack 后新增汇总/原料数），再提示「请回旧设备执行 pack」；话术
  中禁止出现「换工具前请先同步」一类表述（同机换工具与同步彻底
  解耦，见下）；
  ③ 新设备上首次接手某项目却发现本机无此项目记忆仓：resume 无档案
  分支在项目有 git remote 时追加「若这是新设备上 clone 下来的项目：
  此项目的档案可能还在旧设备上，请回旧设备执行 pack。」（纯本地
  项目无 remote 时不追加；该提示不含「同步」二字）。
- pack 固定顺序沿用第一片（⓪ 拉取合并 → ① 强制汇总 → ② 健康检查 →
  ③ 一致性自检（仅报告）→ ④ 脱敏复扫 → ⑤ 生成包/提交/上传 →
  ⑥ 本地 pack 回执），三载体同顺序，任一步失败停在该步并给出可执行
  的下一步提示。pack 前积压先汇总后打包（有 pending 才跑确定性最小
  增量汇总，失败中止）；文件夹载体外来锁活跃时 pack 直接中止（绝不
  带着未汇总原料打包）。
- unpack 后对账（2.5.4）：三载体 unpack 落盘后都跑只读对账
  （`_reconcile_note`：目标状态、pending 数、分支/HEAD）+ 就绪门
  （`_ready_gate`：健康检查通过 + 目标无 tampered + 校验清单无不符
  即 ready；分支漂移等正常现象只列为差异说明，不阻塞就绪，见
  D-sync-17）。CLI 在 ready 时打印「接力就绪」，否则逐条打印对账
  差异，不宣布就绪。对账通过才宣布就绪；标注修正（以现状为准修正
  档案标注）留给随后的 resume 或人工，不在 unpack 内写档案（偏离
  D-sync-21）。
- 同步状态可见（2.5.5）：落点为 `sync-status` 命令（已含上次 pack
  时间、序列号、新汇总/新原料数，本片补 `carrier` 字段；文件夹载体
  seq/commit 为空时显示 `—`），不放进 resume——因为 resume 是同机
  流程，放同步状态行会与 2.5.6「同机流程无同步字样」的验收标准冲突
  （偏离 D-sync-15）。resume 里只允许两处不含「同步」二字的本地
  状态提示：无档案 + 有 remote 时的回旧设备 pack 提示（9.4③），以及
  文件夹载体第 3 段的 device-notice 展示（「另一台设备近期在写此
  项目」）。冲突副本报告同理不进 resume，落点为 `sync-status`（文件
  夹载体，扫描并隔离后报告「发现 N 个同步冲突副本，已隔离，未自动
  合并」）与文件夹 pack 输出（偏离 D-sync-16）。
- 同机换工具彻底解耦（2.5.6）：resume / capture / inject 全程不得出现
  「同步」二字（测试覆盖：健康 resume、inject、以及校验清单不符时
  的 resume 文案——后者曾含「同步截断」字样，已改写为「截断或冲突
  副本污染」）。文档与话术中禁止「换工具前请先同步」一类表述。
  唯一例外是 `sync-status` / pack / unpack 等同步命令自身的输出，
  它们本来就是同步入口。

## 10. 第二片偏离清单（续 D-sync-1 – D-sync-14）

- **D-sync-15**：2.5.5 的「接手输出中的同步状态行」落在 `sync-status`
  而非 resume。原因：resume 是同机流程，2.5.6 验收标准要求同机流程
  全程无同步字样；状态行放 resume 会直接违反该标准。`sync-status`
  本就是「状态可见」的本机入口，信息完全相同（上次 pack 时间、载体、
  序列号、未打包增量），只展示、不催促。主聊 2026-10-29 确认：该取舍成立，2.5.6 的字面验收标准优先于 2.5.5 的落点字面表述，信息完整且只展示不催促，意图未损。
- **D-sync-16**：2.3.4 的「每次启动检查时扫描」与「接手输出中报告」
  落点为 `sync-status`（文件夹载体）与文件夹 pack/unpack，而非
  resume。原因同上：报告文案含「同步冲突副本」字样，进 resume 即破
  2.5.6；且 `sync-status` 的扫描是隔离式扫描（命中即移入
  `trash/conflicts`），不是只读报告——只报告不隔离会让下一次 pack 把
  冲突副本当成 `archive/` 负载运走，所以状态命令在这里例外地写（可逆
  移动，非删除），docstring 已注明。主聊确认：同理成立。
- **D-sync-17**：unpack 就绪判定口径为「健康检查通过 + 目标无 tampered
  + 校验清单无不符」即 ready；分支漂移、档案后又有新提交、引用文件
  缺失等对账差异只列出、不阻塞。原因：漂移是正常现象（第一片
  D-sync-7 已定一致性自检只报告不失败），就绪门若把它当阻塞项，新
  设备永远无法宣布就绪。主聊确认：就绪口径合理，漂移类差异只列出不阻塞，与 D-sync-7 一致。
- **D-sync-18**：2.3.2 的「JSONL 按日切分」不直接映射 v1 布局。v1 条目
  已是每条目独立文件，爆炸半径天然限于单文件；已汇总的历史文件永不
  被改写（汇总只追加新记录、旧条目归档改名），与日切分追求的效果
  相同，故不另造日切分机制。
- **D-sync-19**：服务器换位不是单文件原子改名。目录换位是两步
  （current/ → `.old-*/`，tmp/ → current/），中途崩溃由 `.old` 恢复 +
  manifest 权威比对兜底；`current-index.json` 写在换位之后，写崩溃不
  影响比对。单文件原子改名在「一套完整包文件」形态下不可行（多文件
  须整体换位），目录换位 + 恢复是等价的崩溃安全语义。主聊确认：多文件包形态下单文件原子改名不可行，远端端点更无此原语；两步换位 + .old 恢复 + manifest 权威比对 + 下载侧哈希校验，等价保障了“失败后正式版本仍是上一完整包”这一不变量；换位中途 current/ 短暂缺失的窗口由下载侧校验兜住。
- **D-sync-20**：校验清单刷新不变量推广到全部载体。起因是实测失败：
  第二片初期只在文件夹载体汇总时刷新，但 `goal_confirm` 在任何载体
  都会写清单；随后非文件夹载体的汇总改了档案却不刷新，验收 Act 3
  的 resume 正确地报了清单不符导致 FAIL。修复：summarize wrapper
  非 noop 时一律刷新（全部载体），`recover` 恢复后也刷新；规则是
  「清单一旦存在，一切档案写入方都必须刷新」，与载体无关。
- **D-sync-21**：2.5.4 的「以现状为准修正档案标注」在 unpack 侧只做
  只读对账、不写档案。原因：第一片 D-sync-10 已定「unpack 的写必须
  保持显式」（resume 第 4 段可写，unpack 不可）；差异逐条列出、对账
  通过宣布就绪后，标注修正留给随后的 resume 或人工。
- **D-sync-22**：心跳锁与降级实现在 `relay_flow.summarize` wrapper 内，
  而非 relay_sync 模块。原因：避免 relay_flow ↔ relay_sync 循环
  import，且第一片 docstring 已声明 sync 模块不被同机流程调用；心跳
  与 notice 只是本机文件操作，不属于 sync 模块。
- **D-sync-23**：新增测试集中放在 `tests/test_relay_sync2.py`，未按原
  计划拆一部分进 `tests/test_relay_flow.py`。原因：旧测试文件零改动
  更安全（不得悄悄弱化既有测试）；flow 侧用例（降级、notice、resume
  文案）通过加载的 flow 模块同样覆盖。
- **D-sync-24**：Curl/SFTP 后端未接真实服务器实测。沙箱内无真实
  WebDAV/NAS/SFTP 端点，测试以 `LocalDirBackend` + `FaultBackend`
  覆盖全部语义（原子换位、防覆盖、断线、篡改拒绝）；网络后端代码为
  直接的 curl/rsync 调用，留待真机验证时再验，文档已明示。

## 11. 第三片：2.6 未提交改动搬运 + 2.7 README + 卷二总验收

- 2.6 定型方案（specweave 式 git 私有快照，先提交再换设备是正道，
  patch 导出仅为降级路径，不实现）：pack 时工作区非干净（已跟踪修改
  或未跟踪文件），用独立临时 `GIT_INDEX_FILE` 把当前工作区（含未跟踪
  文件、尊重 `.gitignore`）写入一棵树，`commit-tree` 生成快照提交——父
  提交为当前 HEAD，提交信息含固定 trailers（`Relay-Snapshot: true`、
  `Relay-Source-Device`、`Relay-Source-Device-Id`、`Relay-Pack-Seq`、
  `Relay-Snapshot-At`）；全程不动用户的真实 index、工作区与分支。
  函数：`create_wip_snapshot`（干净返回 None；超 100 MB 拒绝并列出
  最大 20 个文件）、`push_wip_snapshot`、`restore_wip_snapshot`、
  `mark_wip_consumed`。
- 快照传输只走私有 Git 载体：快照提交 force-push 到记忆仓远端的保留
  ref `refs/relay/wip/<分支名>`；代码仓为他人/公开仓时，禁止往代码仓
  推此 ref，必须走独立记忆仓的远端——没有记忆仓远端配置时 pack 收据
  wip 记 `skipped`（未配置记忆仓远端，快照未推送），绝不退回代码仓
  远端。接线点在 `_pack_git` 步骤 ⑤：快照在包推送**前**创建（体积超限
  时 pack 在任何推送前失败），快照 ref 在包推送**成功后**才推（失败的
  pack 不留快照）；收据 `wip` 字段含状态/sha/文件清单/被忽略清单/
  是否覆盖未消费快照。
- 忽略文件：`.gitignore` 命中的文件默认不进快照，但 pack 回执的
  `wip.ignored_recent` 必须列出「被忽略且最近 7 天修改过」的文件
  （上限 100 条，超了记 `ignored_truncated`）；不列清单的静默跳过
  不允许。CLI pack 输出同步打印该清单。
- 接方还原：unpack 时（仅 git 载体、仅当调用方给出 `toplevel` 代码
  检出时）若存在未消费的快照 ref，先校验目标代码仓工作区干净（不
  干净则 `refused-dirty` 拒绝还原并报告，绝不覆盖新设备上已有的改动；
  CLI 打印「快照未还原：本机工作区不干净，你的改动无损」）；干净时
  fetch 快照，以 `cherry-pick --no-commit` + `reset` 把快照内容还原为
  「未暂存的未提交改动」，还原后推消费标记
  `refs/relay/wip-consumed/<分支名>`（失败仅记 `consumed_push: false`，
  下次 pack 会警告，不撤销还原）并写 `state/wip-consumed.json`、跑
  `_consistency_audit` 对账（档案所述状态 vs 还原后的工作区，不吻合
  按缺料评估流程报告）。还原失败/拒绝绝不 brick 已落盘的档案合并
  （只报告，不抛错）。快照只搬工作区文件内容，不搬运行状态；跨操作
  系统换设备时文件模式/换行差异按 Git 既有机制处理、只做如实报告
  （不在搬运层另造轮子）。
- 生命周期与上限：每个分支只保留最新一份未消费快照；新快照覆盖旧
  快照前，若旧快照未被消费，pack 必须警告「上一份未提交改动快照
  尚未在任何设备还原，将被覆盖」并要求显式确认
  （`pack --overwrite-unconsumed`，缺旗标即中止并报警；强制覆盖后
  收据记 `overwritten_unconsumed: true`）。文件夹/服务器载体无快照
  通道（其唯一回退路径是 `sync-rollback --bundle`），不得假装有；
  `pack --overwrite-unconsumed` 在这两载体上被忽略。
- 2.6 冲突裁决命令（第二片留下的接口）：`relay sync-resolve`。
  `--list` 只列待裁项（goal-conflict.json / state.md 分叉待裁行 / 最新
  合并日志中未裁决的待办冲突）；`--goal` / `--fork <键>` /
  `--backlog <事项>` 配 `--decision keep-local | adopt-incoming` 落盘
  裁决并清标记；改了 archive/ 文件后必刷新校验清单（否则下次 resume
  报清单不符）。目标采纳对方版须 `--file` 提供目标文本（合并日志只
  记哈希，不保留对方原文；采纳后同时暂存为草案，走正常
  `relay goal confirm` 完成本机确认）。裁决记入最新合并日志的
  `resolutions` 数组（无合并日志时记 resolve 回执），证据不删除。
  注意非粘性（设计如此）：合并是确定性的，keep-local 的目标/分叉裁决
  在重导同包时会重现标记（分歧本身还在）；adopt 裁决会粘住，因为采纳
  后内容已一致。命令本身不做自动选边以外的事——分叉待裁内容由用户在
  裁决时定夺，命令只负责落盘与清标记。
- 2.7 README：独立小节《换设备前必读：有一件事事后补不了》，位置在
  `README.md` 安装说明（`## 快速开始`）之前，不是文末附注；第一句直
  说原始日志只存旧设备本地的结论；三方事实边界对照表（原始日志在旧
  设备 / relay 记忆仓可同步 / 未提交代码可快照搬运）；习惯引导落到
  流程（换设备意图第一句先问/先执行 pack 检查；新设备无档案时第一条
  建议是回旧设备 pack 而非从零回溯，且回溯产物是重建、保真度低必须
  明示）；反例写具体损失（代码与 Git 在，对话脉络与决策理由没了）；
  文案禁区两端都不碰（不写成产品缺陷式道歉，不夸大成「不 pack 就全
  丢」）；正向锚点是「拔 U 盘前点弹出」一句话类比；pack 耗时量级为
  秒级到分钟级（取决于汇总积压）。**真实 fresh-reader 实测未做**，留待
  真机步骤：通读 README 后两分钟内复述「换设备前要在旧设备做什么、
  为什么、不做会丢什么」三点。
- 卷二总验收：`relay sync-accept`（`run_sync_acceptance` /
  `render_sync_record`），十一幕：① 两台设备完整 loop（档案逐条一致，
  除 state.md 基线行；块文件按条目比对，见 D-sync-26 的空行说明）；
  ② 双方改同一条；③ 篡改包；④ 目标导入；⑤ Git 冲突标记；⑥ 文件夹
  载体；⑦ 服务器载体（中断后正式版本不变、陈旧序列号拒绝覆盖并给
  指引、更新序列号接受为新版）；⑧ WIP 快照往返；⑨ 同机换工具无同步
  字样；⑩ pack 前积压先汇总后打包；⑪ 卷一验收重演。验收在临时目录
  内重定向 HOME 与 PROJECT_RELAY_HOME，绝不碰真实记忆仓与真实服务
  器；卷一 accept 与卷二验收各连续两遍 PASS 方算通过。

## 12. 第三片偏离清单（续 D-sync-1 – D-sync-24）

- **D-sync-7 最终裁决**：pack 第 ③ 步档案与代码一致性自检**保持只报告
  不失败**，不再留待确认，收紧方案被否决。理由有三：① 漂移是常态
  （用户改代码不汇总是正常工作方式），失败即停会把正常 pack 变成
  brick——用户每改一次代码不汇总就 pack 不动，与「无感接续」意图直接
  冲突；② WIP 流程本身要求脏工作区可 pack（2.6 快照就是为脏工作区
  而生），③ 步若对漂移失败，脏工作区 pack 在健康门之前就被 ③ 步拦
  下，快照通道名存实亡；③ 真正的失败门已经在 ② 健康检查（标记、
  清单、事务）与 ⑤ 推送，③ 步的报告完整落进 pack 回执的 `audit`
  字段并由 unpack 对账复述，没有信息被隐藏。计划原话「任一步失败停
  在该步」在此被修正为「任一**失败门**失败停在该步，报告步只报告」。
- **D-sync-25**：服务器防覆盖口径以「序列号是否更新」为准，而非「设
  备是否相同」：陈旧或相等序列号的异设备包被拒绝（须先拉取最新包合
  并再导出），严格更新的序列号（无论哪台设备）被接受为新版。原因：
  pack 序列号是各设备本地递增的，设备轮流 pack 时新序列号即新内容；
  按设备身份拒绝更新包会让轮流 pack 的第二台设备永远推不上去。
  卷二验收第七幕同时覆盖拒绝与接受两面。
- **D-sync-26**：合并层块头格式统一为汇总的规范格式 `## D-001` /
  `## T-001`（带连字符、三位补零），解析同时兼容旧式 `## D1`。
  起因是第三片验收实测到的真 bug：汇总写 `## D-001`，而合并解析
  只认 `## D1`，导致汇总产生的决策/待办在 unpack 合并时被静默丢弃
  （第一、二片测试只 capture 不汇总，故未发现）。修复：`_parse_blocks`
  正则改为 `^## <前缀>-?(\d+)$`；`_merge_id_blocks`、`_merge_backlog`、
  `sync_resolve` 的待办采纳追加一律发出规范格式（否则合并后的条目
  会被本机 relay_flow 的 `^## T-\d+` 计数漏掉）。附带说明：合并
  追加时以本地 ID 重排并归一化块前空行，因此跨设备档案比对中块文件
  按「条目集合 + 前言」比对，不按字节比对（逐条一致，不逐字节一致）。

## 13. P0 修复：脱敏复扫三层修复（2026-10-29）

背景：卷二真机前置实测（本机真实仓库 science-online-guide，手工模拟
原料）挖出的 P0——pack 步骤 ④ 对全部负载文件全文跑脱敏，手机号
正则命中原料 header 里 64 位哈希中被 hex 字母夹住的 11 位数字串并
将其删除（64 位变 53 位），对端验指纹全丢（4/4 id-hash-mismatch），
且复扫写回污染源设备、重 pack 无法自愈。修复分三层，缺一不可，
顺序固定为 redact → verify → 写回/装包：

- **D-sync-27**：raw 原料条目不再全文脱敏。body 全文脱敏；header
  按字段脱敏：`fingerprint`、`fact_hash`、`backfill` 一律不动（它们
  是验算输入，动了就是改证据）；`anchor` 同样不动——它是
  `fingerprint.anchor` 的副本，只动其一会造成两份锚点不一致。
  header 其余字段逐叶脱敏（字符串脱敏，容器递归）。其他负载文件
  （archive/state 等散文型内容）维持全文脱敏。零命中条目按原格式
  重序列化，字节级无变化。
- **D-sync-28**：`mobile_cn` 边界收紧。旧边界 `(?<!\d)` / `(?!\d)`
  只挡数字相邻，挡不住 hex 字母夹住的数字串；新边界要求数字串
  前后均非 ASCII 字母或数字。真实手机号在文本中通常由空格、标点、
  CJK 字符分隔（皆非 ASCII 字母数字），仍被命中；串首、串尾的
  手机号同样命中；粘连 ASCII 字母的数字串（如 user13812345678）
  被视为更长 token 的一部分，不再命中（属有意取舍，已有测试
  钉死）。`id_cn` 评估结论：理论上存在同类风险，但需 18 位结构
  化数字串（特定年月日结构），概率极低且无实测命中，本次未动，
  保持改动最小。
- **D-sync-29**：验门 fail-closed，落在 `_redact_payload` 内——全部
  四处调用点（export_bundle 与三载体 pack 的步骤 ④）共用同一顺序，
  不存在绕开验门的复扫路径。验门两项：① 全部条目（含降级条目）
  的保护字段脱敏前后必须一致；② 非降级条目脱敏后必须验过
  `fact_hash`——脱敏前验过而脱敏后失配，报错指明「脱敏复扫改动
  了验算字段」；脱敏前已失配的手工改坏指纹，同样中止（报错不
  甩锅给脱敏）。降级条目（capture 失败路径按设计写 `fact_hash:
  None`）不拦 pack，仍走对端既有丢弃行为——否则一次采集降级
  会导致此后所有 pack 都被 brick。D-sync-11 的「复扫写回本地」
  在新顺序下依然成立：验门通过后才写回，验门失败则不写回、不
  装包、不推送、不留成功收据。P0 的次生问题（步骤 ④ 写坏文件、
  步骤 ⑤ 失败无收据、第二次 pack 以干净收据掩盖坏包）亦随之消
  失：步骤 ④ 自身先失败。已污染的 scratch 仓自行废弃即可，无需
  修复工具；真实用户仓此前未被污染（v2 未合并发布，前置实测只
  污染了 /tmp 下的 scratch 仓）。
