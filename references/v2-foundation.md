# project-relay v2 地基（卷一 1.2–1.5）

> 实现：`scripts/relay.py`（标准库 only）。旧版 `relayctl.py` / `init_project.py`
> 与人审流程保持原样，供旧测试与旧工作流使用；v2 命令不依赖它们的人审语义，
> 只复用其可靠性基质（atomic/safe/digest/序列化/Git 封装/错误约定）。

## 记忆仓

- 根目录：`~/.project-relay/`（代码用 `Path.home()` 派生，测试可用
  `PROJECT_RELAY_HOME` 覆盖；绝不写死任何用户目录）。
- 顶层：`projects/`（按项目分仓）、`registry.json`（项目注册表）、
  `global/`（跨项目偏好与配方状态）、`trash/`（删除中转，不直接真删）。
- 每项目仓六区：`raw/`（采集原料）、`archive/`（正式档案：goal/state/backlog/
  decisions/lessons 五件套）、`state/`（游标、草案、暂定记录等机器状态）、
  `tx/`（事务日志）、`backups/`（不可变备份）、`receipts/`（目标确认收据）。
- 全部档案为纯文本（JSON 规范序列化、Markdown 文档），不引入数据库引擎。
- **写入纪律（评审红线）**：全部写入必须经 `atomic()`（同目录临时文件 →
  fsync → os.replace，新文件 0644、已有文件保原 mode）。任何直接 open 写
  档案文件的代码路径视为缺陷。

## 项目身份

- 身份键 = remote origin URL 规范化（去协议/用户信息差异、去末尾 .git、
  主机与路径小写化）后的 SHA256 前 16 位；仓目录名 = 键 + 可读 slug。
- 无 remote 的纯本地项目用 Git 顶层绝对路径哈希作弱键，标记为本地键；
  补上 remote 后下次登记自动迁移为 remote 键并留痕（旧行标记 migrated）。
- `relay where` 只读：按路径或键查询，不写注册表、不自动登记新路径。
- 项目仓目录只在 init / 回溯建档时创建；日常命令查不到项目仓只报错并提示 init。

## 目标层（唯一需要人确认的内容）

- 流程：`goal draft --file <草案>` → `goal show`（diff）→ `goal confirm`
  （确认收据）或 `goal confirm --tentative`（暂定，无收据）。
- 确认收据存 `receipts/goal-<序号>.json`：目标文件哈希、确认时间、确认渠道、
  上一份收据文件哈希（收据链）；同名 `.md` 为确认时刻的目标快照。
- 注入前验哈希：`goal check` / `goal inject`。目标被改动时注入停止，只展示
  上一版已确认目标的只读副本（标注「可能已过期」），并用 `goal show` 摆出差异。
- 暂定目标注入时带水印，不可当硬约束执行；复访触发：下次里程碑到达时 /
  下次交接时 / 用户主动提起时。
- 用户明确不建目标：`goal decline`，目标文件只留一行决定记录；此后里程碑刻度
  自动失效、注入明示无目标档案，不反复追问。
- **诚实边界**：本地哈希收据防的是「目标被静默改动后继续当已确认版本用」，
  不是签名，不防有本机写权限者的蓄意伪造；用户未逐字看过的自动生成目标
  只能算暂定，不能算已确认。

## 命令一览

`relay init`、`relay where [--path | --key]`、
`relay goal draft/show/confirm/check/inject/decline/status`。
全局选项放在子命令之前：`--root <项目路径>`、`--store-root <记忆仓根>`。

## 下一步（1.6 起）的接口交代

- 采集（1.6/1.7）落点：项目仓 `raw/`；条目链与队列状态机在 1.7 定稿。
- 汇总（1.12）写档案时复用本片的 `atomic()` 与收据思想；事务日志落 `tx/`。
- 回溯（1.15）解析器按注册表接口 `discover/sessions/events` 实现；三家路径
  见《实机验证/路径表v1.md》（WorkBuddy 双源规则：assistant 文本只在 traces/ span_end）。
- 旧 `.agent/` 迁移执行（内容搬迁）不在本片范围：本片只做检测与预览。
