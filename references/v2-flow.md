# project-relay v2 流程片（卷一 1.6–1.14）

> 实现：`scripts/relay_flow.py`（标准库 only），命令经 `scripts/relay.py` 分发。
> 地基片（1.2–1.5）见 `references/v2-foundation.md`；本片不改地基片的命令与语义，
> 只在它上面长流程。旧版 `relayctl.py` 的人审语义保持原样不动。

## 命令一览

`relay capture [--tool] [--session] [--message]`、`relay queue`、
`relay enforce [--tool] [--session]`、
`relay recipe install|uninstall --tool claude-code|codebuddy|workbuddy|codex [--target-root] [--home]`、
`relay precompress`、`relay fingerprint`、`relay scales [--session]`、
`relay summarize [--source] [--semantic-file] [--session]`、`relay recover`、
`relay handoff`、`relay inject [--budget]`、`relay resume [--tool]`。

## 数据流

```
会话收尾：capture（确定性快照 + 语义占位） -> raw/cap-NNNN.md（queue=pending）
   -> precompress（规则整理稿 state/digest.json + 数字白名单 state/numbers.json）
   -> scales（三刻度判定，任一到线触发）
   -> summarize（确定性拼装 + 语义增量 -> 事务日志 tx/ -> 原子落盘 archive/
      -> 备份 backups/ -> 游标 state/cursor.json + 基线 state/baseline.json
      -> 条目 pending -> summarized，上一批 summarized -> raw/archived/）
会话开始：resume（五段：摘要 -> 对账 -> 积压 -> 兜底汇总 -> 冒烟）
```

## 关键约定

- 条目格式：JSON 头 + `---relay-body---` 分隔线 + Markdown 体；体部语义段占位符
  为 `<!-- relay:semantic pending -->`，加四个空小节标题。
- 队列状态机：pending → summarizing → summarized → archived，只许前进；
  summarizing 超时（默认 3600 秒）由写命令（capture/summarize/enforce）回退 pending
  并计数，超 3 次回退的条目在接手时点名。resume 不做回退（1.14 零写规则优先，
  仅报告超时条目；回退发生在随后的补汇总写动作中）。偏离说明见交付报告 D3。
- 游标双锚点：`state/cursor.json` = {entry_seq, anchor_message}；anchor_message
  来自 capture 的 --message 参数（触发本次采集的用户消息原文），缺省为 null。
  锚点缺失或不匹配时汇总中止，不猜位置续跑。
- 脱敏：14 类正则（AWS key、GitHub/GitLab token、JWT、私钥块、数据库连接串、
  手机号、身份证号、Slack/OpenAI/Anthropic/Google key、密码赋值、Bearer），
  命中替换为 ``，计数记入条目头；只在采集端做一次。
- 执法链：Stop 时最新条目语义仍占位则拦截（退出码 2，指令只含条目编号与缺失小节名）；
  单条 3 次上限后放行并标记语义缺失 + 自白行；单会话硬顶 5 次，到顶停用并记录原因。
- 三刻度阈值（state/config.json 可改）：改动文件 ≥10 或净改动 ≥800 行；距上次汇总
  ≥7 天且有 pending；里程碑 = 目标档案已确认变更（新收据），无目标档案时该刻度
  显示「无锚点、未启用」。时间刻度永远附注「检查点顺带评估，非准点触发」。
- 注入预算：默认 2000 tokens（粗估 = 字符数 × 2/3），顺序导航 → 目标 → 状态三行 →
  待办 → 决策/教训（先砍细节段，状态三行与待办永不砍）。
- 档案预算：state.md 100 行/8000 字、goal.md 300 行/15000 字、
  backlog/decisions/lessons 800 行/30000 字；超限自动分卷到 archive/splits/ 并登记。

## 偏离清单（相对详细版计划）

- D1（1.10）：旧 snapshot() 未原样复用。原因：它硬编码 .agent 耦合且遇子模块
  直接报错，计划 1.10 本身要求去掉这两点；本片按旧算法改写扫描，复用其
  changed()/anchor()/digest()/git() 原语，并以等价测试证明无子模块仓输出一致。
- D2（1.12）：游标的「用户消息原文锚点」在 capture 未传 --message 时为 null；
  完整双锚点需调用方在采集时传入触发消息。1.15 回溯事件流将补齐该字段的来源。
- D3（1.7 vs 1.14）：summarizing 超时回退不在 resume 时执行（保 1.14 零写），
  只报告；回退由随后的写命令执行。理由：零写保证优先于回退及时性，超时状态可见不沉默。
- D4（1.8）：Claude Code 受管小节写在用户级 ~/.claude/CLAUDE.md 而非项目内，
  避免向项目仓写 relay 文件（地基片「项目仓零 relay 文件」原则）；Codex 配方
  按计划写项目内 AGENTS.md 受管小节（计划明文要求）。
- D5（1.14）：resume 对账发现档案与现状不一致时，纠正不得直改档案正文，应走
  增量通道：纠正文本以 semantic_text（semantic_auto=True，模型调用记 0）传入
  兜底汇总，使纠正本身带来源留痕入档；无积压但有纠正时，先 mint 一条
  session='resume-correction' 的采集条目作载体（去重键含 session，不会塌到已
  汇总条目上），再汇总。
- D6（1.12，测试期修正）：锚点守卫移到 noop 返回之前——游标已坏、但游标之后
  无 pending 时，汇总也必须中止报锚点缺失，不能静默放过。
- D7（1.6，测试期修正）：capture 去重键改为仓库状态本身（anchor + code +
  gaps 的规范化哈希），不用渲染后的事实文本——「相对上条目的改动文件」一行
  天生相对，拿它哈希第二次采集永远对不上，去重恒失效。
- D8（1.9/1.12，测试期修正）：语义段解析遇重复小节标题时合并而非覆盖；
  子模块目录本身不再进 code 扫描（只记缺口），否则 snapshot 在 gitlink 目录
  上报错。分支沿用旧引擎的 refs/heads/ 全量形式，不改短，以保等价测试可比。

## 留给 1.15/1.16 的接口

- 1.15 回溯解析器按 `discover/sessions/events` 注册表接口实现；三家路径见
  《实机验证/路径表v1.md》；WorkBuddy 解析器必须实现双源合并（projects 取用户消息
  与 function_call，traces/ span_end 取 assistant 文本），缺失 traces 时 assistant 段
  标注缺口。回溯产出的事件流可直接喂 precompress（类型表与聚合已就绪），
  其 user_message 字段将补齐游标的第二锚点。
- 1.16 验收剧本的五幕：建仓建目标 / A 干活 / B 接手 / 弄死 A 回溯补档 / 目标防线与
  脱敏；本片各命令的验收方法见详细版计划 1.6–1.14 各节。
