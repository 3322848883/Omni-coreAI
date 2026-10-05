# F0: 本地参考项目 nofx（VergeX）的设计优点

**来源类型**：本地代码阅读（`C:\Users\w6485\Desktop\测试\nofx`，Go 语言，非网络来源）
**调研方式**：直接精读 `kernel/` `trader/` `store/` + 3 个子代理并行分析
**日期**：2026-10-05
**置信度**：high（一手代码，可复验）

> 本文件不是网络调研结果，作为「已知参考基线」并入报告。所有引用为仓库内 `file:line`。

## Findings

### [1] 程序强制状态机，提示词只做镜像
- 描述：`applyVergexSignalPolicy` 在决策进执行器**之前**改写它——有持仓的 symbol，AI 说什么都不算，程序直接生成 `hold`（`Confidence: 100`）；flat 的 symbol 若方向与看板不符则进 `blocked` 并打日志。
- 位置：`trader/auto_trader_signal_policy.go:136-204`
- 提示词侧：`vergexHoldRules()` 的注释写明 `mirrors the direction state machine enforced by the trader`（`kernel/engine_prompt.go:259-270`）
- 价值：high。这正是我们缺失的一层——规则 14 让 AI 去撤孤儿，AI 犹豫了 6 小时。

### [2] 提示词显式标注「代码强制」vs「AI 引导」
- 描述：约束分节为 `## CODE ENFORCED (cannot be bypassed)` 与 `## AI GUIDED (recommended)`，模型清楚哪些是硬边界、哪些只是建议。
- 位置：`kernel/engine_prompt.go:530/534`（enforced）vs `:565/569`（guided）
- 价值：high，成本极低。

### [3] 不变量：开仓后必须挂上止损，失败即回滚平仓
- 描述：`SetStopLoss` 失败 → `closeUnprotectedPosition` 立刻平掉；回滚用 `emergencyClosePositionAndVerify`（3 次重试 → `CancelAllOrders` → `GetOpenOrders` 复查，非空报错）。**不假设清理成功**。
- 位置：`trader/auto_trader_orders.go:176-184`、`trader/auto_trader_signal_policy.go:57-108`
- 价值：high。我们 `_open` 有 precheck，但 `_stop_entry` 没有；且没有「SL 挂失败就回滚」这一步。

### [4] 能纠正就不拒绝（分层）
- 描述：杠杆超限 → 自动夹到上限 + 日志；名义超 `equity×ratio` → cap；超可用保证金 → 缩到 `maxAffordable×0.98`（留手续费缓冲）；**只有低于最小仓位才拒绝**。
- 位置：`kernel/engine_position.go:61-65`；`trader/auto_trader_risk.go:231-269`
- 价值：high。我们已做「名义钳制」，但缺 0.98 缓冲与「保证金不足时缩量」这一层。

### [5] 宽松解析 + 安全降级
- 描述：XML 标签硬隔离 `<reasoning>`/`<decision>`；解析四级回退（数组围栏→对象围栏→裸对象→任意数组）；清洗全角标点/零宽字符/公式/千分位；全失败降级为 `wait`。
- 位置：`kernel/engine_prompt.go:628-667`、`kernel/engine_analysis.go:458-559`
- 价值：high。我们 `schema.py` 只有单路径。

### [6] 外部真相优先于本地记忆（自愈对账）
- 描述：本地 OPEN 必须是交易所实盘持仓的子集，多余「僵尸行」关闭、超出量修剪，明确「不虚构 PnL」；快照重建打 `Source:"snapshot"` 标记；缓存带 TTL（Binance 15s）保证新鲜度上界。
- 位置：`store/position_reconcile.go:64-104`、`trader/position_snapshot.go:16-102`、`binance/futures.go:80`
- 价值：high。我们的共享订单库/台账没有与交易所对账的机制。

### [7] 决策按「先平仓、后开仓」排序
- 描述：`close_*` 优先级 1、`open_*` 优先级 2、`hold/wait` 优先级 3；先释放保证金再开新仓。
- 位置：`trader/auto_trader_loop.go:250-257`（另见 `docs/architecture/STRATEGY_MODULE.zh-CN.md:576-586`）
- 价值：medium，实现简单。

### [8] AI 连续失败 3 次进安全模式（只平不开，自动恢复）
- 描述：`consecutiveAIFailures>=3` 触发，过滤掉所有 `open_long/short`，保留平仓能力；AI 恢复后自动退出。
- 位置：`trader/auto_trader_loop.go:177-223, 274-288`
- 价值：high。我们只有 `error_streak` 告警，没有降级动作。

### [9] 每轮完整留痕（prompt + CoT + 原始响应 + 执行日志）
- 描述：`DecisionRecord` 存 system/user prompt、CoT、原始响应、逐条执行日志与结果，出事能精确复盘「为什么下了这单」。
- 位置：`trader/auto_trader_go:1235-1256`
- 价值：medium-high。我们的 `thinking.json` 存了 CoT 但 `content_head` 截断 500 字符，讨论轮的调用完全不落盘。

### [10] 独立回撤保护（价格基，与杠杆解耦）
- 描述：盈利 >5% 后从峰值回吐 40% 即平仓；用**价格基**而非保证金基，避免高杠杆下阈值被动收紧。
- 位置：`trader/auto_trader_risk.go:34-141`
- 价值：medium。我们只有 `daily_loss_limit_usd`。

### [11] 接口分层 + 兜底适配 + 一套测试跑所有实现
- 描述：核心能力在 `Trader` 接口，可选能力（网格）拆扩展接口；不支持的交易所用 `GridTraderAdapter` 兜底；`CancelOrder` 用类型断言探测，不支持时**报错而非降级成「取消全部」**这种破坏性替代；`testutil/test_suite.go` 一套黑盒契约测试跑所有交易所。
- 位置：`trader/types/interface.go:43-160`、`trader/interface.go:22-88`、`trader/testutil/test_suite.go:42-68`
- 价值：medium。我们 `exchanges/` 无契约测试。

### [12] 配置三档模式 + 覆盖时仍保留机器输出契约
- 描述：官方模板 / 模板+自定义 / 完全覆盖三档（`override_base_prompt`）；即使完全覆盖，也追加 `Machine Output Contract` 保证 JSON 可解析。
- 位置：`kernel/engine_prompt.go:18-34`；`docs/prompt-guide.zh-CN.md:36-92`
- 价值：medium。

### [13] 提示词回归测试（断言必备短语、禁 CJK）
- 描述：测试断言必备短语存在、遗留/违禁短语不存在、输出无 CJK 字符；校验用表驱动带 `wantLeverage`/`wantError`。
- 位置：`kernel/engine_prompt_test.go:129-136`、`kernel/validate_test.go:8-146`
- 价值：medium。可为我们 `prompt.py` 加同类护栏。

### [14] 三层状态分离（对话助手侧）
- 描述：`chatHistory`（内存短期）/ `TaskState`（持久长期摘要）/ `ExecutionState`（执行态，支持中断恢复）三层职责严格区分，并**写明什么不该进哪一层**——「实时余额、当前持仓、当前行情」明确禁止进长期记忆。
- 位置：`docs/architecture/AGENT_MEMORY_AND_PLANNING.zh-CN.md:21-100`
- 价值：high（对记忆系统设计）。与 `omnialpha` 现有四层记忆可对照。

### [15] 孤儿归属按交易所账户而非本地 ID
- 描述：账号被多次「重启」会生成新 `trader_id`，若按 `trader_id` 对账，旧行会变成永不关闭的孤儿；改为按 exchange 账户对账 + 唯一部分索引幂等去重。
- 位置：`store/position.go:350-354, 163-177, 523-567`
- 价值：medium。

## Dead ends

- `trader/position_rebuild.go` 是「从成交历史重建已平仓记录」（FIFO + 加权成本），用于 PnL 统计，**不是**处理保护单对齐——保护单问题不在这个文件。

## Suggested follow-ups

- nofx 的 `auto_trader_throttle.go` 里「平仓后 4h 冷却、最小持仓 90m」的具体阈值是怎么回放标定的？是否可移植到 Gate 品种？
- `store/position_reconcile.go` 的对账循环触发频率与幂等键设计细节。
