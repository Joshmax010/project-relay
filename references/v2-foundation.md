# project-relay v3 地基

> 实现：`scripts/relay.py`（标准库 only）。v3 只留三家 Agent 与 ZIP 交接；
> v1 人审工具（`relayctl.py`、`init_project.py`）、Git 打包、服务器载体、
> 文件夹载体、回溯建档、晋升通道、Cursor 解析器、插件市场、pip 打包、
> 一键安装脚本、accept/sync-accept 验收脚本已全部删除，不在本片范围。

## 记忆仓

- 根目录：`<项目根目录>/.project-relay/`（`init` 时建，`.gitignore` 兜底，
  默认不进 git；换设备只走 ZIP）。不再有全局注册表、项目键、`~/.project-relay/`。
- 外层（总目标与总记忆，要精简）：`goal.md`、`state.md`、`backlog.md`、
  `decisions.md`、`stack.md`、`outlook.md`、`index.md`（指路表）、`receipts/`、
  `state/`（目标机件暂存）、`checksum-manifest.json`（整树完整性清单）。
- 各家目录 `agents/<agent>/`：`memory.md`、`decisions.md`、`lessons.md`、
  `backlog.md`、`changes.md`、`goal.md`、`log.jsonl`、`raw/`、`state/`、`tx/`、
  `backups/`。三家有全套骨架：`codex/`、`claude-code/`、`workbuddy/`；
  `deepseek-honeycomb/` 只留目录位置，适配器以后再做。
- **子模块排除**：指纹计算跳过子模块路径（mode 160000 的 gitlink），缺口
  记在快照 `gaps`；子模块内部不在本仓档案范围内，想接力须在子模块仓内
  单独建档。缺口标注位置见 `references/v2-boundaries.md`。
- 全部档案为纯文本（JSON 规范序列化、Markdown 文档），不引入数据库引擎。
- **写入纪律**：全部写入必须经 `atomic()`（同目录临时文件 → fsync →
  os.replace，新文件 0644、已有文件保原 mode）。任何直接 open 写档案文件
  的代码路径视为缺陷。
- **锁**：同一家开两个会话同时写自己那份，用该家目录下的小文件锁排队
  （阻塞 flock）；外层写（目标确认、新 Agent 加入、resume 写 state.md）
  拿外层小锁。跨设备合并不上走分叉检测与合并兜底。

## 项目身份

- 身份就是项目根目录本身：给项目根目录就指向根目录下的 `.project-relay/`，
  不再有 remote 键、弱身份键、注册表与迁移。
- `relay init` 在项目根目录建档；`relay agents` 打印指路表（哪个 Agent 的
  记忆文件在哪）。
- 项目仓目录只在 init / import-zip 时创建；日常命令查不到档案只报错并提示 init。

## 目标层（唯一需要人确认的内容）

- 流程：`goal draft --file <草案>` → `goal show`（diff）→ `goal confirm`
  （确认收据）或 `goal confirm --tentative`（暂定，无收据）。
- 确认收据存 `receipts/goal-<序号>.json`：目标文件哈希、确认时间、确认渠道、
  上一份收据文件哈希（收据链）；`receipts/goal-history/` 留目标变更历史。
- 注入前验哈希：`goal check` / `goal inject`。目标被改动时注入停止，只展示
  上一版已确认目标的只读副本（标注「可能已过期」），并用 `goal show` 摆出差异。
- 暂定目标注入时带水印，不可当硬约束执行；复访触发：下次里程碑到达时 /
  下次交接时 / 用户主动提起时。
- 用户明确不建目标：`goal decline`，目标文件只留一行决定记录；此后里程碑刻度
  自动失效、注入明示无目标档案，不反复追问。若此前已有已确认收据或暂定态，
  拒绝时一并退役（旧目标归档、收据与暂定态删除），否则状态会误判 tampered、
  注入仍吐旧目标。
- 各家目标必须与总目标一致：写某家目标时盖上当前总目标的哈希，resume 比对，
  过期或没盖章的都列出来。
- **诚实边界**：本地哈希收据防的是「目标被静默改动后继续当已确认版本用」，
  不是签名，不防有本机写权限者的蓄意伪造；用户未逐字看过的自动生成目标
  只能算暂定，不能算已确认。

## 命令一览

`relay init`、`relay agents`、`relay goal draft/show/confirm/check/inject/decline/status`、
`relay agent-goal --agent <名> [--set/--file]`、`relay stack/outlook`、`relay log`。
全局选项放在子命令之前：`--root <项目路径>`。
