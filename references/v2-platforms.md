# Windows / macOS / Linux 差异表（卷三 3.6.7）

随代码发布。第三批已做 autocrlf 钉死与换行统一；本片以核实 + 落表为主，
核实出缺口才补代码——核实结论：未发现需补代码的缺口。

## 1. 已知差异与处理方式

| 维度 | Windows | macOS | Linux | relay 的处理 |
|---|---|---|---|---|
| 路径分隔 | `\`（Git 内部仍用 `/`） | `/` | `/` | 所有仓内相对路径统一用 posix（`/`）；Windows 下 `relative_to` 产物经 `as_posix()` 归一化后才进快照、注册表与包内路径；claim 键只认正斜杠前缀 |
| 换行（CRLF） | `core.autocrlf=true` 是常见默认；checkout 会把 LF 转 CRLF | 默认 LF，个别工具写 CRLF | LF | 两层：① relay 的 git() 封装对所有 git 调用强制 `-c core.autocrlf=false`（观测用户自有 git 产物除外，见 §2）；② `sync-setup` 建仓时提交 `.gitattributes`（`* -text`），relay 自营 store 仓不受用户 autocrlf 影响；③ relay 写文件一律 LF；④ 不动用户全局/系统 autocrlf |
| 文件模式位 | executable 位不可靠（NTFS 不存 Unix 模式位；Git 常报模式抖动） | 可靠 | 可靠 | 指纹缓存不缓存可执行位（每次从 stat 现算）；快照比较以内容哈希为准，不以模式位为准；Windows 下模式位抖动不触发误报 |
| 文件名大小写 | 不区分大小写（NTFS 默认） | 默认不区分（APFS 默认） | 区分 | 仓内路径以 Git 的记录为准；仅大小写不同的两个文件在 Windows/macOS 上 checkout 即冲突，relay 不另做合并或改名，冲突由 Git 如实报出 |
| 符号链接 | 需开发者模式/管理员权限，默认常被拒 | 可用 | 可用 | 快照对 symlink 以 readlink 目标计哈希（不跟随、不缓存）；relay 写盘路径本身拒绝符号链接目的地（`atomic()` 的 is_symlink 检查） |
| 换行符与文件名编码 | GBK 终端常见；PowerShell 默认输出编码非 UTF-8 | UTF-8 | UTF-8 | 所有 relay 读写显式 `encoding='utf-8'`；Windows 真机提示词要求每个 PowerShell 进程重设 `PYTHONIOENCODING=utf-8` 与 `GIT_CONFIG_*` 的 autocrlf=false 覆盖 |
| 换行过滤（CRLF）判据 | 见 §2：过滤后哈希 | 同左 | 同左 | 所有哈希判据在三平台一律走过滤后哈希（LF 字节），不存在“Windows 上按 CRLF 字节算、Linux 上按 LF 字节算”的双口径 |

## 2. 哈希判据审计表（核实结论）

| 判据 | 字节口径 | Windows 走法 | 结论 |
|---|---|---|---|
| 包 manifest、Ed25519 签名、目标哈希、受管小节哈希 | relay 写入的 `\n` 稳定字节 | 与 Linux 同口径（relay 自营写盘一律 LF） | 无缺口 |
| git 操作（快照、包验签、store 提交） | 经 git() 封装，`-c core.autocrlf=false` | 强制钉死，用户 autocrlf=true 不影响 | 无缺口（第三批 A1） |
| 快照自比较（快照对快照） | 工作区字节自比较，不与 git blob 比 | 同口径 | 无缺口 |
| 用户自有 git 产物观测（`index_truth`） | 裸 `subprocess` 调 git，按用户自己的 git 配置做过滤哈希 | 与用户 git 同口径（这是 A1 收口 doctrine：观测用户产物须与用户 git 一致） | 不是缺口，不得“修复”为钉死口径 |
| 指纹增量缓存键 | mtime_ns / ctime_ns / ino / size（见 v2-boundaries.md §3.6.5） | 同口径；可执行位不缓存 | 无缺口；同尺寸全 stat 不变的覆写漏判是与 git racy-clean 同款的已知取舍 |

## 3. 真机验证挂起项（与本表相关，仍待用户电脑重跑）

第三批 Windows 重跑验证 4 项仍挂起（unittest 全量、claim 路径、回滚、
init 模板换行），与 3.6.7 的核实结论无关——核实是代码审计结论，真机
重跑是运行结论，两者不互相代替。重跑时红线：只跑 `relay.py` 不跑
`relayctl.py`；先试验项目；同一项目别两套流程同跑。
