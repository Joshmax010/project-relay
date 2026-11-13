# project-relay v3 流程片

> 实现：`scripts/relay_flow.py`（标准库 only），命令经 `scripts/relay.py` 分发。
> 地基片见 `references/v2-foundation.md`；本片不改地基片的命令与语义，
> 只在它上面长流程。v3 按 Agent 分家写记忆，汇总、接手、合并都按家走。

## 命令一览

`relay capture --agent <家> [--session] [--message]`、`relay queue [--agent]`、
`relay enforce --agent <家> [--session]`（未传 --session 时，若 stdin 是钩子 JSON
则取其中 session_id；否则沿用 default）、
`relay recipe install|uninstall --tool claude-code|workbuddy|codex [--target-root] [--home]`、
`relay recipe package`、`relay precompress`、`relay fingerprint`、
`relay scales [--agent]`、`relay summarize --agent <家> [--source] [--semantic-file] [--session]`、
`relay recover --agent <家>`、`relay inject [--budget] [--agent]`、
`relay resume`、`relay hook resume|stop`、`relay stack/outlook [--set]`、
`relay agent-goal --agent <家> [--set/--file]`、`relay log --agent <家>`。

已删的命令：`pack`、`unpack`、`sync-setup`、`sync-claim`、`sync-rollback`、
`sync-status`、`backfill`、`promote`、`accept`、`sync-accept`、`handoff`、
`migrate-key`、`split`、`fork`、`stats`、`where`。交接只走 `export-zip` /
`import-zip`（见 `references/v2-sync.md`）。

## 数据流

```
会话收尾：capture（确定性快照 + 语义占位） -> agents/<家>/raw/cap-NNNN.md（queue=pending）
   -> precompress（规则整理稿 state/digest.json + 数字白名单 state/numbers.json）
   -> scales（三刻度判定，任一到线触发）
   -> summarize（确定性拼装 + 语义增量 -> 事务日志 tx/ -> 原子落盘 agents/<家>/
      memory.md、decisions.md、lessons.md、backlog.md、changes.md
      -> 备份 backups/ -> 游标 state/cursor.json + 基线 state/baseline.json
      -> 条目 pending -> summarized，上一批 summarized -> raw/archived/）
会话开始：resume（五段：摘要 -> 对账 -> 积压 -> 兜底汇总 -> 冒烟）
   + 现算总记忆写外层 state.md + 跨 Agent 核对 + 目标对齐
```

## 接手质量补充（batch 10，v3 按家走）

`relay resume` 的五段契约不变，另加两块**只读**补充段（不写档案，
失败只降级为缺失，不影响接手）：

1. **接手对账·交接包指纹**（`_package_drift_section`）：取全家最新一条
   采集条目 header 的 `fingerprint.code`（即交接包导出时的代码指纹，注明
   agent），与当前工作区 `snapshot_v2` 逐文件比 SHA256，列出 changed /
   added / deleted。锚点条目没有指纹时整段省略（老包兼容）。
2. **目标对齐·各家目标与总目标**（`_goal_alignment_section`，batch 10 的
   承诺对照改成哈希比对）：各家 goal.md 首行记录的总目标哈希 vs 当前总
   目标哈希，分对齐 / 过期 / 未盖章 / 未写目标四档，另附各家目标一句话摘录。

其它同步修复（batch 10，仍有效）：

- `_merge_state_md`：空仓首次导入不再产生 state 噪音——块外文本仍是模板
  原文时采纳对方；本地值为空/未知/无时采纳对方；导入时清理上一次遗留的
  分叉待裁行。真实内容冲突仍保留本地并记分叉。
- `summarize` 待办解析：Owner 取到「，/；/。/空白」为止，验证取最后一处
  「验证：」之后的全文（不再被 `\S+` 截断成「- Owner：用户，验证：在」）；
  同事项重复汇总不再生成重复 T 编号。

## 关键约定

- 条目格式：JSON 头 + `---relay-body---` 分隔线 + Markdown 体；体部语义段占位符
  为 `<!-- relay:semantic pending -->`，加四个空小节标题。
- 队列状态机：pending → summarizing → summarized → archived，只许前进；
  summarizing 超时（默认 3600 秒）由写命令（capture/summarize/enforce）回退 pending
  并计数，超 3 次回退的条目在接手时点名。resume 不做回退（只报告超时条目；
  回退发生在随后的补汇总写动作中）。resume 的有界写入只两处：现算总记忆写
  外层 state.md、四段兜底汇总；其余段只读。
- 游标双锚点（按家）：`agents/<家>/state/cursor.json` = {entry_seq, anchor_message}；
  anchor_message 来自 capture 的 --message 参数（触发本次采集的用户消息原文），
  缺省为 null。锚点缺失或不匹配时汇总中止，不猜位置续跑。
- 配方状态按家存：`agents/<家>/recipe.json`（Codex PR #5 修复的旧病是所有项目
  共享同一文件，后装覆盖先装；v3 按家分文件，旧病不再有）。
- 脱敏：14 类正则（AWS key、GitHub/GitLab token、JWT、私钥块、数据库连接串、
  手机号、身份证号、Slack/OpenAI/Anthropic/Google key、密码赋值、Bearer），
  命中替换为 ``，计数记入条目头；只在采集端做一次。
- 执法链：Stop 时最新条目语义仍占位则拦截（退出码 2，指令只含条目编号与缺失小节名）；
  单条 3 次上限后放行并标记语义缺失 + 自白行；单会话硬顶 5 次，到顶停用并记录原因。
- 三刻度阈值（`state/config.json` 可改）：改动文件 ≥10 或净改动 ≥800 行；距上次汇总
  ≥7 天且有 pending；里程碑 = 目标档案已确认变更（新收据），无目标档案时该刻度
  显示「无锚点、未启用」。时间刻度永远附注「检查点顺带评估，非准点触发」。
- 注入预算：默认 2000 tokens（粗估 = 字符数 × 2/3），顺序导航 → 目标 → 状态三行 →
  待办 → 决策/教训（先砍细节段，状态三行与待办永不砍）。有 `--agent` 时再把该家的
  待办、决定、教训按预算拼进去。
- 档案预算（按家）：memory.md 100 行/8000 字、goal.md 300 行/15000 字、
  backlog/decisions/lessons 800 行/30000 字；超限自动分卷到该家 `splits/` 并登记
  `state/split-index.json`。
- 跨 Agent 核对：resume 取各家 memory.md 里 `## 事实` 小节的 `- key：value` 行，
  同一 key 在两家值不同即冲突，直接列出来，不悄悄选边。
- 事实合并：语义文本里 `## 事实` 小节的 `- key：value` 行并入 memory.md 的
  `## 事实` 小节；facts section 在受管块之外，state patch 冲不掉。

## 与旧版计划的偏离（仍有效的）

- 汇总超时回退不在 resume 时执行（保接手零写优先），只报告；回退由随后的
  写命令执行。理由：零写保证优先于回退及时性，超时状态可见不沉默。
- 游标的「用户消息原文锚点」在 capture 未传 --message 时为 null；完整双锚点
  需调用方在采集时传入触发消息。
- 锚点守卫移到 noop 返回之前——游标已坏、但游标之后无 pending 时，汇总也必须
  中止报锚点缺失，不能静默放过。
- capture 去重键改为仓库状态本身（anchor + code + gaps 的规范化哈希），不用
  渲染后的事实文本——「相对上条目的改动文件」一行天生相对，拿它哈希第二次
  采集永远对不上，去重恒失效。
- 语义段解析遇重复小节标题时合并而非覆盖；子模块目录本身不再进 code 扫描
  （只记缺口），否则 snapshot 在 gitlink 目录上报错。
- 对账纠正不再经汇总语义文本、直接记进现算的 state.md；无积压但有纠正时，
  先 mint 一条 session='resume-correction' 的采集条目作载体（去重键含 session，
  不会塌到已汇总条目上），再汇总。
