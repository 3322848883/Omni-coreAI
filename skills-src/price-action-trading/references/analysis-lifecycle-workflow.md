# 分析生命周期工作流

> 基于 workflow.md 阶段结构 + strategy_workflow 第22章再决策树。
> 每个事件对应具体的文件写入操作。

## 触发 → 写入 对照表

| 触发事件 | 写入文件 | workflow 步骤 | 时机 |
|----------|----------|---------------|------|
| 26步分析完成 | assets/templates/trading_journal.md | 阶段三结束 | 分析后 |
| 所有入场条件满足 | assets/templates/order_log.md | 步骤 4.1.1 | 下单前 |
| 挂单成交 | assets/templates/order_log.md（更新） | 步骤 5.1 | 成交后 |
| 持仓状态变化 | assets/templates/trading_journal.md（更新） | 步骤 5.5 | 持仓中 |
| 止损/止盈触发 | assets/templates/trade_review.md | 阶段六 | 平仓后 |
| 每日收盘 | assets/templates/daily_deep_review.md | 步骤 6.1 | UTC 00:00 |
| 复盘发现新错误 | memory/error_patterns.md | 步骤 6.1 | 复盘后 |
| 胜率统计更新 | memory/pattern_effectiveness.md | 步骤 6.1 | 复盘后 |
| 个人画像更新 | memory/trader_profile.md | 步骤 6.1 | 复盘后 |
| 可改进项 | memory/improvement_tracker.md | 步骤 6.4 | 复盘后 |
| 每周日 | assets/templates/weekly_review.md + memory/L3_weekly/ | 步骤 6.2 | 周日 |
| 每月末 | assets/templates/monthly_review.md + memory/L4_monthly/ | 步骤 6.3 | 月末 |
| 发现新规律 | memory/market_wisdom.md | 随时 | 分析中 |
| 多品种对比 | memory/cross_instrument.md | 随时 | 分析中 |
| Step 6.5 场景B下单成功 | logs/orders/ + memory/market_state.md | Step 6.5 | 三单全部提交后 |
| Step 6.5 场景C挂单管理 | logs/orders/（更新） | Step 6.5 | 操作完成后 |
| Step 6.5 场景D止损/止盈调整 | assets/templates/order_log.md（阶段三） | Step 6.5 | 操作完成后 |
| Step 6.5 场景D平仓执行 | assets/templates/order_log.md（阶段四）+ assets/templates/trade_review.md + memory/*.md | Step 6.5 | 平仓确认后 |

## 再决策分支（strategy_workflow 第22章）

| 分支 | 条件 | 动作 |
|------|------|------|
| CD-01 冷却期 | 止损平仓后 | 不写入，等待 15-30 分钟 |
| CD-02 评估 | 冷却期结束 | 更新 trading_journal.md |
| CD-03 同向再入场 | 趋势继续+新设置+方程正 | 重新开始阶段零 |
| CD-04 反向交易 | 趋势线突破+≥3理由 | 新建 trading_journal.md |
| CD-05 观望 | 区间中部/铁丝网/情绪 | 不写入 |

## 全自动模式

条件满足 → 自动下单（止损/限价/市价），按 strategy_workflow 第17章选择。
持仓管理按第20章 Q1-Q6 + TS-01~05 自动执行。
止损/止盈按第7/8章规则自动管理。
每个生命周期事件自动写入对应文件。

## v33 新增：先执行后写入原则

> 所有写入操作都是对已发生事件的记录，绝不预先写入。
> 先有交易行为/分析行为 → 后有文件更新。

### Step 6.5 执行后写入清单

| 路径 | 执行操作 | 写入文件 | 写入时机 |
|------|---------|---------|---------|
| 路径D | 止损/止盈调整 | order_log.md 阶段三 | 操作完成后 |
| 路径D | 平仓 | order_log.md 阶段四 + trade_review.md + memory/*.md | 平仓确认后 |
| 路径A1 | 快速更新 | market_state.md + logs/analyses/ | 检查完成后 |
| 路径A2 | 参数调整 | market_state.md + logs/analyses/ | 调整完成后 |
| 路径B/C | 下单 | logs/orders/ + market_state.md | 三单提交后 |

## 交易闭环生命周期（强制监控）

> 有交易计划就必须持续监控直到成交或失效。共35个监控等待场景，分布在6个闭环阶段中。

### 闭环阶段与监控场景数

| 闭环阶段 | 监控场景数 | 强制路径 | 写入文件 | 状态变更 |
|---------|:---------:|---------|---------|---------|
| 发现机会 | 3 | C | market_state.md + logs/analyses/ | → 等待触发 |
| 等待触发 | **8**（强制） | A1/A2 | market_state.md + logs/analyses/ | 不变/调参 |
| 执行交易 | 1 | B→D | logs/orders/ + market_state.md | → 已触发 |
| 持仓管理 | **7**（强制） | D | market_state.md + order_log.md + logs/analyses/ | 不变 |
| 平仓闭环 | 4 | D | 全部记忆文件 | → 已成交 |
| 再决策 | 10 | CD-01~05 | trading_journal.md | → 等待触发/无计划 |

> **注意**：阶段二（8个场景）和阶段四（7个场景）共15个场景为强制检查项，每次分析必须逐一评估。
> 完整场景清单见 `references/knowledge/workflow.md` §交易闭环各阶段涉及的监控等待场景。
