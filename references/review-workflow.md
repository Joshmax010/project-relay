# 人审操作说明

本说明用于 Git 代码项目。Python 3.8+、Git CLI；无需第三方包。命令在仓库根目录执行；其他目录用 `python3 <工具路径> --root <仓库根目录> <子命令>`。Windows 可用 `python`。

## 完整流程

1. 完成代码任务及验证，一轮结束询问“是否同步项目文档”。未同意时只保留未提交工作和隔离草稿。
2. 同意后起草 JSON 批次，每个键是 `.agent` 内相对 Markdown 路径，每个值是完整新内容。用程序或编辑器生成 JSON，保留实际换行，不拼接 shell 命令。输入文件存于仓库外或 `.agent/inbox/`；放在代码目录会被纳入快照，之后移动或删除会使批准失效。
3. 建立批次并展示逐文件差异，等待用户批准具体内容。同意同步不等于批准。
4. 记录用户批准或拒绝的条目。修改草稿内容、代码、正式文档或 Git 基线后重新建立批次并审核。
5. 应用获批文档。暂存对应代码及全部获批文档，提交前运行检查；仅在已经获得提交授权时 commit。

```json
{
  "state.md": "# 当前状态\n\n- 当前任务：修复登录校验\n- 结果：填写真实测试证据\n- 最近会话：[本次执行](sessions/20261009-login.md)\n",
  "sessions/20261009-login.md": "# 已审核执行记录\n\n目标、改动、验证结果、来源、未解决事项及下一步。此处是结构示例，不是已验证事实。\n",
  "sessions/index.md": "# 执行会话索引\n\n- [登录校验](20261009-login.md)\n"
}
```

```bash
python3 .agent/tools/relayctl.py draft --file /path/to/batch.json --title "登录校验后的文档同步"
python3 .agent/tools/relayctl.py review <完整批次ID>
```

`draft` 只写 `inbox/<ID>/batch.json`。它保存目标基础哈希、内容哈希及代码/正式文档/Git 基线快照。`review` 显示差异和内容哈希；不要把未经审核的内容当作事实。

## 批准、部分批准与拒绝

用户批准具体内容后才记录以下操作。`--reviewed-by` 记录实际审核者的称呼，不是身份认证，不能让 Agent 自己充当审核者。

```bash
python3 .agent/tools/relayctl.py review <ID> --approve state.md sessions/20261009-login.md sessions/index.md --reviewed-by "项目负责人"
python3 .agent/tools/relayctl.py review <ID> --reject lessons.md --reason "原因未经验证"
python3 .agent/tools/relayctl.py apply <ID>
```

所有条目须明确获批或被拒绝，才可应用。部分批准不会自动扩大授权；被拒绝文档保持不变。state、新会话及会话索引是每次同步必需条目，任一未获批则本次不能完成同步。修改已起草内容时建立新批次，旧批次保留为线索。

## 提交前检查

```bash
git add -- <本次代码路径> .agent/state.md .agent/sessions/ <其他获批文档路径>
python3 .agent/tools/relayctl.py commit-check
# PASS 且用户已授权提交时，才执行自己的 git commit 命令。
```

检查覆盖整个仓库的已跟踪文件、非忽略的新文件和正式文档；新增、删除、内容、可执行位、Git 基线变化都会使旧批准失效。不允许只暂存一部分代码；工作区须与暂存区一致。暂停其他无关任务，先分清提交范围，再起草审核；不要为通过检查删除他人的改动。

首次提交要审核所有新正式 Markdown（包括与初始化骨架内容相同的文件），并暂存 `.agent/.gitignore`、工具及项目产物。正式文档被全局忽略时明确检查原因，再由用户决定是否强制纳入。隔离草稿、收据和原文目录不得纳入正式提交。

批准后的文件变化需要新一轮同步。提交使 HEAD 变化，批准不可以复用于下一次提交。`commit-check` 不自行执行 commit，不安装 hook；直接绕开它的 Git 命令不受技术阻断。它防止流程遗漏，不证明文档语义真实或用户身份，也不是对不可信进程的隔离机制。

## 中断与恢复

- 任务中断：`resume` 展示正式 project/state、工作区和未审核批次列表。核对实际代码后继续；不得直接沿用草稿中的要求。
- 应用中断：脚本留下 `.local/transaction.json`，提交检查拒绝通过。运行 `recover` 回到应用前文档，保留批次；事务记录可重复恢复。
- 恢复发现外部修改：停止并保留事务，人工核对冲突后处理，绝不覆盖未知版本。
- 本地备份按完整批次 ID 保存全部原始字节与路径，不依赖秒级名称，也不作为正式审核历史。正式版本历史由代码和获批文档的 Git commit 提供。
- 迁移未完成工作须另外携带 `inbox/`、`.local/` 和未提交代码。它们默认不在 Git 中；批准绑定 Git 基线和文件快照，接手者仍须核对用户授权。

## 无 Python 与旧项目升级

无 Python 时按相同流程手动起草、展示、批准与核对，不声称存在脚本门禁。旧 `.agent` 不重置：保留有效规则，先提出协议 2.0 和缺失能力的完整差异，经批准合并；升级工具也供用户审查。没有 Git 的项目暂不适配，不自动改变用户的版本管理方式。
