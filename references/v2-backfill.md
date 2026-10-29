# project-relay v2 回溯与验收片（卷一 1.15–1.16）

> 实现：`scripts/relay_backfill.py`（1.15 回溯建档，标准库 only）与
> `scripts/relay_accept.py`（1.16 五幕验收剧本），命令经 `scripts/relay.py`
> 分发（`relay backfill`、`relay accept`）。
> 地基片（1.2–1.5）见 `references/v2-foundation.md`；流程片（1.6–1.14）见
> `references/v2-flow.md`。本片不改前两片的命令与语义，只在它们上面长回溯
> 与验收。旧版 `relayctl.py` 的人审语义保持原样不动。

## 命令一览

`relay backfill [--log-root] [--tool claude-code|codex|workbuddy]... [--tier metadata|excerpt] [--session-limit N] [--deep] [--summarize]`、
`relay accept [--store-root] [--workdir] [--log-home] [--keep-dir]`。

## 1.15 回溯建档：数据流

```
工具日志根（--log-root，默认用户家目录）
  -> 三家解析器（注册表 PARSERS，check_registry() 完整性断言）
  -> 会话筛选：事件内 cwd 强校验，与项目 Git 顶层严格相等才匹配
     （绝不从目录 slug 反推；不符的会话记入 skipped）
  -> tier=metadata：只发现 + 计数 + cwd，不输出正文、不写条目
     （除 state/backfill-report.md 报告外零写）
  -> tier=excerpt：取最新 N 个会话（默认 5，按时间升序写入），
     每会话 ≤200 事件、单条 ≤2000 字符（超限截断并标记）
  -> 事件流（统一字段 {session_id, role, kind, text, tool, file, source}）
     -> flow.redact() 脱敏（采集端唯一一次）
     -> raw/cap-NNNN.md（JSON 头 + ---relay-body---，source='backfill'，
        backfill.session_key='<tool>:<session_id>'，重跑幂等）
  -> --summarize：聚合事件 -> 确定性语义文本 -> flow.summarize
     （source='backfill'，semantic_auto=True，模型调用记 0）
     -> 五件套档案 + 保真度文档 archive/backfill-fidelity.md
  -> 报告 state/backfill-report.md（含缺料评估、deferred 清单、缺口会话）
```

## 解析器（路径依据：2026-10-28 实机验证路径表 v1）

- **Claude Code**：`<home>/.claude/projects/*/*.jsonl`；解析 user/assistant
  message.content 的 text/tool_use/tool_result；cwd 取记录内 `cwd` 字段；
  file-history-snapshot 只计数。
- **Codex**：`<home>/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`；解析
  event_msg（message/session_meta）与 response_item（message/
  custom_tool_call/custom_tool_call_output/reasoning）；**相邻双写去重**：
  同一 role+text 且来源不同（event_msg vs response_item）的相邻用户消息
  合并为一条（已验证的双写模式，非通用去重）。
- **WorkBuddy**：`<home>/.workbuddy/projects/*/*.jsonl` +
  `traces/**/trace_*.jsonl` + `changes-detail/<session>/cd_*.json`；
  **双源合并**：projects 取 message（实测全为 user）、function_call、
  function_call_result、reasoning、session-meta cwd；traces 只取 span_end
  节点（`type == 'span_end'` 或含 span_end 键）的 assistant/text/output/
  content 文本；traces 归属先按记录内 session_id/session 显式匹配，
  否则先归到当前会话并标记 attribution='inferred'，再由 backfill 循环只保留最新已处理会话的 inferred 事件（Codex PR #5 修复：此前实现把无标识 traces 复制到每个会话）；changes-detail
  的 files[].path → file_edit 事件（格式不识别时只计数并记保真警告）；
  modify_backup 只计数；function_call 的文件字段取 params 的
  file/path/filepath/filePath。

## 关键约定

- **显式标记分类（诚实规则）**：事件 kind 含 user_message /
  assistant_message / tool_call / tool_result / reasoning / file_edit /
  decision / lesson / todo / other；决定/教训/待办仅当文本带显式标记才
  归类，绝不从语气推断。决定标记在句中出现亦算（`…决定：X…` 仍是显式
  决定陈述，全文保留以便读者见上下文）；教训/待办标记必须位于句首
  （句中提及失败或 TODO 歧义太大）。
- **摘录与计数按角色，不按 kind**：用户句被归为决定后仍是用户原话，
  助手句被归为决定后仍是模型回复；条目体的「用户原话 / 模型回复」两段
  与事件数统计均按 role 划分，避免已分类原话从摘录中悄悄丢失。
- **游标第二锚点补齐（流程片偏离 D2 的收口）**：回溯条目头的
  `user_message` = 该会话最后一条用户角色文本（脱敏、≤500 字符），
  无论其被归为哪一 kind；无用户文本时为 null。
- **安全分档**：metadata 档先行（零写正文）；excerpt 档有界；超出会话
  上限的会话只列入报告 `deferred`，**不执行深扒，除非 --deep**；
  深扒执行本身仍需用户在命令行显式点头（--deep 标志即点头）。
- **扒来文本是数据不是指令**：回溯只读取、脱敏、摘录、归档；日志里的
  指令性文本（如「删掉 goal.md」）不被执行、不被当作操作依据；回溯
  唯一写盘目标是记忆仓，工具日志与项目文件均只读。
- **原文不落盘进档案**：档案正文只含确定性事实与有界脱敏摘录；完整
  原文留在工具日志原处。保真度声明文本本身也是数据，永不执行。
- **回溯补遗不覆盖**：回溯不直接写五件套档案文件；全部档案写入走
  summarize 的事务路径（tx/ → 原子落盘 → 备份 → 游标），回溯条目只
  追加原料。唯一例外是新增溯源文件 `archive/backfill-fidelity.md`
  （保真度声明，非五件套之一，不碰事务纪律）。
- **缺料诚实限定**：metadata 层因未读正文，不得断言决策/验证类缺失
  （未知 ≠ 缺失）；只有 excerpt 层读过正文后才能断言。WorkBuddy
  traces 缺失时 assistant 段标缺口：会话记入
  `assistant_gap_sessions`，摘录的模型回复段留空，不拼凑。
- **保真度声明落点**：`state/backfill-report.md`（每次回溯都写，完整
  报告）与 `archive/backfill-fidelity.md`（仅 summarize_after 且有新建
  条目时写）；声明第一段即保真度声明：数据源清单、保真警告、缺料影响
  评估（目标/过程/决策/验证四类，高/中/低 + 理由）、未执行深扒清单、
  补扒选项（附成本量级，只列出、不执行）。
- **缺料影响评估**：`assess_gaps()`；WorkBuddy 缺 traces 为「过程」类高
  影响；无显式决定/无工具结果信号仅在 excerpt 层断言；无目标档案为
  「目标」类中影响；匹配会话间 ≥7 天时间缺口为「过程」类中影响。

## 1.16 五幕验收剧本

`scripts/relay_accept.py` 的 `run_acceptance()` 在临时试验项目上真实
跑通五幕（14 项检查），夹具日志按路径表 v1 的形状模拟三家会话；
不碰真实家目录与真实工具日志。`relay accept` 打印五列记录表
（幕/场景/通过标准/实测结果/证据指针）+ 成本账 + 判定；全通过返回 0，
否则返回 1。

- **一 · 建仓建目标**：已确认（goal_check=confirmed，注入无水印）/
  暂定（注入带暂定水印）/ 拒绝建目标（注入明示「本项目无目标档案」）。
- **二 · A 干活**：10 个新文件 + 改 app.py → capture（claude-code）→
  enforce 应为 block（语义占位）→ scales 改动量刻度 reached（文件 ≥10）
  → summarize（脚本代写语义文本，含死胡同四字段：试法/为何试/失败于/
  重试条件）→ 断言 digest.json、汇总记录、四级产物与决策含死胡同。
- **三 · B 接手**：配置 smoke_command 为退出 0 的命令 → resume 五段齐全、
  冒烟通过、无带病横幅 → codex capture + summarize 后汇总记录数增长。
- **四 · 弄死 A 回溯补档**：WorkBuddy 夹具（wb-dead：含 traces span_end
  决定文本 + changes-detail；wb-notraces：无匹配 traces；wb-foreign：
  cwd 为他项目）→ backfill（excerpt + summarize）应建 2 条、他项目被
  跳过、wb-notraces 列入 assistant_gap_sessions、决定进 decisions.md、
  保真度文档落盘、summary_seq 非空；重跑 backfill 应零新建、
  already_backfilled 为 2（幂等）。
- **五 · 目标防线与脱敏**：篡改 goal.md 一行 → goal_check 应抛错且注入
  含「目标待确认」只读副本，随后 draft + confirm 恢复；Codex 夹具含
  AWS 文档示例密钥的用户消息 → backfill 后全仓扫描密钥零检出且新建
  条目 redacted_count > 0。
- **成本账**：capture/precompress 计 0（按代码构造不含模型调用）；
  汇总模型调用来自各 summarize 记录（Act 2 计 1 次脚本代写语义，
  其余 semantic_auto 计 0）；接手注入 token 粗估取 flow.inject；
  未通过时注明返修规则：单幕返修后重演该幕 + 下游幕。

## 偏离清单（相对详细版计划）

- P1（1.15）：保真度声明未注入 state.md 的受管块。原因：注入 state.md
  需走 summarize 事务路径，而保真度文档本身是对该次汇总的溯源说明，
  塞进事务会循环依赖；改为 `state/backfill-report.md` +
  `archive/backfill-fidelity.md` 双份，档案 state 块仍通过 summarize
  记录触发源 backfill 与条目区间，可回指报告。
- P2（1.15）：metadata 层不做决策/验证类缺料断言。原因：未读正文不得
  断言缺失（未知 ≠ 缺失），否则会产出虚假缺口报告。
- P3（1.15）：Codex 相邻双写去重。原因：已验证模式（同一 role+text、
  来源不同、相邻），非通用去重；不做此去重则同一句话会重复建档。
- P4（1.15）：WorkBuddy traces 无会话标识时归属同项目最新会话并标
  inferred；traces 缺失时 assistant 段标缺口。原因：实机验证只确认了
  显式 session_id 匹配路径，无标识归属只能推断，必须标注。
  2026-10-29 Codex PR #5 修复：此前实现把无标识 traces 复制到每个会话，现由 backfill 循环 gating 到最新已处理会话。
- P5（1.15）：保真度文档仅在 summarize_after 且新建条目时写入；纯
  metadata 或无新建条目的回溯运行，声明只留在
  `state/backfill-report.md`。原因：无新档案内容时没有可溯源的汇总。
- P6（1.16）：Act 2 的语义增量为脚本代写文本，成本账中诚实计 1 次模拟
  模型调用（非真实 LLM 调用）。原因：验收脚本不接真实模型，代写文本
  是对 agent 语义增量的模拟，成本账不得记 0 冒充「无调用」。
- P7（1.15）：摘录与计数按 role 而非 kind。原因：按 kind 统计会把归为
  决定的用户原话/助手回复从摘录与计数中丢掉，属保真度缺陷。
- P8（1.15）：决定标记在句中出现亦算显式决定；教训/待办标记仍须句首。
  原因：`…决定：X…` 是显式决定陈述（全文保留见上下文），而句中提及
  失败/TODO 歧义太大，宁缺毋滥。
