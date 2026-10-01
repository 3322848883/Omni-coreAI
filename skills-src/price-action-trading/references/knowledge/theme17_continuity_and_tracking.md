# 主题十七：连续性规则与价格行为跟踪

> 本主题整合分析连续性规则和价格行为跟踪方法，确保每次分析都基于完整的历史上下文，避免独立分析问题。
> 所有规则引用自已有知识文件，不创造新规则。

---

## 目录

- [一、连续性规则](#一连续性规则)
  - [1.1 分析前执行清单（Step 0）](#11-分析前执行清单step-0)
  - [1.2 分析后更新清单](#12-分析后更新清单)
- [二、时间间隔规则](#二时间间隔规则)
- [三、Bar 类型阅读指南](#三bar-类型阅读指南)
  - [3.1 K线基础分类](#31k线基础分类)
  - [3.2 Signal Bar 类型与特征](#32signal-bar-类型与特征)
- [四、形态检测方法](#四形态检测方法)
  - [4.1 H1/L1→H2/L2 计数](#41h1l1h2l2-计数)
  - [4.2 双顶/双底](#42双顶双底)
  - [4.3 楔形（三推）](#43楔形三推)
  - [4.4 突破确认与失败](#44突破确认与失败)
- [五、趋势状态跟踪](#五趋势状态跟踪)
  - [5.1 趋势强度分类](#51趋势强度分类)
  - [5.2 Always In 方向判断](#52always-in-方向判断)
- [六、step_checklist 使用说明](#六step_checklist-使用说明)
  - [6.1 加载模板](#61加载模板)
  - [6.2 逐步骤标记](#62逐步骤标记)
  - [6.3 完成率检查](#63完成率检查)
- [七、记忆触发条件表](#七记忆触发条件表)

---

## 一、连续性规则

> 来源：[references/analysis-continuity.md](../analysis-continuity.md)

### 1.1 分析前执行清单（Step 0）

每次分析开始前，必须依次执行以下 4 步：

```
Step 0-1: 读取 market_state.md
   - 检查分析时间
   - 获取上次市场状态
   - 获取持仓状态（如有）
   - 获取关键价格位

Step 0-2: 读取最近分析
   - 读取最近 3 次分析记录（logs/analyses/）
   - 获取历史分析结论
   - 识别市场状态变化
   - 检查是否有持仓

Step 0-3: 读取记忆文件
   - pattern_effectiveness.md: 设置有效性
   - error_patterns.md: 错误模式
   - trader_profile.md: 个人画像
   - market_wisdom.md: 市场智慧

Step 0-4: 间隔判断
   - 根据时间间隔决定复用模式或完整模式
   - 见第二节 [时间间隔规则](#二时间间隔规则)
```

### 1.2 分析后更新清单

每次分析完成后，必须依次执行以下 4 步：

```
Step 1: 更新 market_state.md
   - 更新分析时间
   - 更新当前价格
   - 更新市场状态
   - 更新 Always In 方向
   - 更新持仓状态（如有）
   - 更新关键价格位

Step 2: 写入分析日志（logs/analyses/）
   - 文件名格式: YYYY-MM-DD_HHMM_品种.md
   - 内容: 简化版分析结论

Step 3: 写入订单日志（logs/orders/）（如有交易）
   - 文件名格式: YYYY-MM-DD_HHMM_品种_方向.md
   - 内容: 订单详情

Step 4: 更新记忆文件（如有新数据）
   - pattern_effectiveness.md: 更新设置胜率
   - error_patterns.md: 记录错误模式
   - market_wisdom.md: 记录市场规律
```

---

## 二、时间间隔规则

> 来源：[references/analysis-continuity.md](../analysis-continuity.md) 第三节

本规则将原 analysis-continuity.md 的 4 级时间矩阵简化为 **2 级模式**，便于快速决策：

| 时间间隔 | 模式 | 处理方式 |
|---------|------|---------|
| **< 1 小时** | **复用模式** | 复用上次 market_state 和形态判断，仅更新价格和重新评估当前 signal bar |
| **≥ 1 小时** | **完整模式** | 加载 step_checklist.md，重新执行 Step 1-6 完整分析流程 |

### 复用模式要点（< 1h）

- 市场状态直接复用上次分析结论
- Always In 方向不变，除非有明显信号
- 仅检查是否有新的 signal bar 形成
- 更新价格数据即可写入分析日志

### 完整模式要点（≥ 1h）

- 必须重新判断市场状态和趋势方向
- 加载 assets/templates/step_checklist.md 逐步骤标记
- 重新评估 SB 规则和交易者方程式
- 如有持仓，重新校验止损位置

---

## 三、Bar 类型阅读指南

> 来源：[references/knowledge/theme13_reading_bar_by_bar.md](theme13_reading_bar_by_bar.md)

### 3.1 K线基础分类

| 类型 | 特征 | 含义 |
|------|------|------|
| **趋势K线**（Trend Bar） | 实体较大，方向明确 | 一方控制市场，有紧迫感 |
| **暂停K线**（Pause Bar） | 实体中等，有上下影线 | 趋势中短暂停顿，方向待定 |
| **十字星**（Doji） | 实体极小或不存在 | 市场犹豫不决，多空平衡 |
| **反向K线**（Reversal Bar） | 收盘与开盘方向相反，且与前一根方向相反 | 可能标志着短期方向转变 |
| **外包K线**（Outside Bar） | 高点高于前高，低点低于前低 | 波动扩大，可能突破或反转 |
| **内包K线**（Inside Bar） | 高点低于前高，低点高于前低 | 波动收缩，蓄力待突破 |

### 3.2 Signal Bar 类型与特征

**反转K线（Reversal Bars）**：
- **多头反转K线**：收盘在最高 1/4 区域，实体占 K 线高度的 2/3 以上
- **空头反转K线**：收盘在最低 1/4 区域，实体占 K 线高度的 2/3 以上
- 尾部（影线）越短，信号越可靠

**信号可靠度判断**：
- A 级：大实体 + 几乎无影线 + 成交量放大
- B 级：中等实体 + 小影线 + 成交量正常
- C 级：实体偏小或影线偏长（需 ≥ 3 个额外理由才入场）
- D 级：禁止入场

**第二次入场（Second Entry）**：
- 第一次入场失败后的第二次尝试更可靠
- 形成微型双重顶/底结构时信号更强

---

## 四、形态检测方法

### 4.1 H1/L1→H2/L2 计数

> 来源：[references/knowledge/theme3_pullbacks.md](theme3_pullbacks.md) Ch17

**定义**：计算当前K线高点（或低点）超过前一根K线高点（或低点）的次数，用于识别回撤结束位置。

| 计数 | 含义 |
|------|------|
| **High 1** | 下跌或横盘中第一根高点高于前一根的K线，结束第一腿 |
| **High 2** | 下一根高点超过前高的K线，结束第二腿（更可靠的入场信号） |
| **High 3** | 第三次出现，功能上等同于楔形 |
| **High 4** | 之后市场很可能不再是回撤而是反向趋势 |
| **Low 1/Low 2/Low 3/Low 4** | 空头趋势中的对应概念，方向相反 |

**关键规则**：
- 多头趋势中的 High 2：通常在均线处或上方，买入持续模式
- 交易区间中的 High 2：通常在均线下方，买入反转
- High 1 仅在强趋势中可靠，在区间中通常是陷阱
- High 2 失败后演变为 High 3（楔形）或 High 4

### 4.2 双顶/双底

> 来源：[references/knowledge/theme3_pullbacks.md](theme3_pullbacks.md) Ch12

**双底多头旗（Double Bottom Bull Flag）**：
- 多头趋势中的回撤以小空头趋势结束
- 本质上是双腿下跌的 High 2 买入设定
- 最可靠的 High 2 类型

**双顶空头旗（Double Top Bear Flag）**：
- 空头趋势中的回撤以小多头趋势结束
- 本质上是双腿上涨的 Low 2 卖出设定

**检测方法**：
1. 识别趋势方向
2. 寻找两次测试同一支撑/阻力水平
3. 第二次测试时观察反转信号 K 线
4. 确认信号后执行入场

### 4.3 楔形（三推）

> 来源：[references/knowledge/theme5_reversals.md](theme5_reversals.md) 第5章

**特征**：
- 三次推动（Three Push），每次推动力度递减
- 通常伴随趋势通道线穿越（Overshoot）
- 在趋势末端出现时是可靠的反转信号
- 在趋势中作为回撤出现时是顺势建仓形态（楔形旗）

**检测流程**：
1. 标记三次推动的高低点
2. 检查推动力度是否递减（收缩阶梯）
3. 确认趋势通道线穿越
4. 等待反转信号 K 线确认

**变体**：
- 楔形多头旗（Wedge Bull Flag）：上升趋势中的三推回撤
- 楔形空头旗（Wedge Bear Flag）：下降趋势中的三推回撤
- 扩张楔形：通道线发散的三推形态

### 4.4 突破确认与失败

> 来源：[references/knowledge/theme4_breakouts.md](theme4_breakouts.md) 第2章、第5章

**高概率突破的 5 条核心判定规则**：
1. **突破K线体大** — 实体占K线高度的大部分
2. **收在极值** — 几乎没有影线
3. **离之前的区间足够远** — 突破幅度显著
4. **被突破的K线数量多** — 突破穿越了多层价格
5. **突破有急迫感** — 没有犹豫

**突破失败的识别**：
- 突破K线有小的实体和大的顶部影线
- 下一根K线是反向K线
- 回撤很快跌破突破点
- 突破仅以 1-2 跳突破阻力位后反转

**突破回撤（Breakout Pullback）**：
- 突破成功后回测突破点，是最安全的顺势入场方式
- 如果回撤不触及突破点，说明突破强劲
- 如果回撤跌破突破点但形成更高低点（多头），仍是有效入场

---

## 五、趋势状态跟踪

> 来源：[references/knowledge/theme2_trends.md](theme2_trends.md) 第19章

### 5.1 趋势强度分类

| 状态 | 特征 | 交易策略 |
|------|------|---------|
| **强趋势** | 连续趋势K线、小的回撤、收盘接近极值、20+ 根不触及 EMA | 只做顺势摆动，不做逆势刮头皮 |
| **弱趋势** | 回撤加深、出现重叠K线、趋势K线实体变小 | 顺势为主，可谨慎逆势刮头皮 |
| **区间** | 双向交易、突破频繁失败、多空K线大致均衡 | 双向刮头皮，在区间边界入场 |
| **反转临界** | 趋势线被突破、对极值的测试、二次入场信号出现 | 等待确认，不预测反转 |

**趋势走弱信号**：
- 多头开始在前期高点获利了结
- 空头开始在高点上方做空
- 初始急速被通道取代
- 最终演变为交易区间

### 5.2 Always In 方向判断

**Always In（总在场内）** 是判断当前市场主要方向的核心概念：

- **Always In Long (AIL)**：市场总体向上，回撤是买入机会
- **Always In Short (AIS)**：市场总体向下，反弹是卖出机会
- **Always In 切换**：当主要趋势线被强势突破且反向运动持续时，AI 方向可能切换

**判断依据**：
1. **趋势线突破**：主要趋势线被强势突破是 AI 切换的首要信号
2. **趋势极端测试**：趋势线突破后测试旧趋势极值（更高高点或更低低点）
3. **二次入场确认**：在极值测试后的二次入场确认 AI 方向切换
4. **HTF 一致性**：与更高时间框架方向一致时，AI 方向更可靠

---

## 六、step_checklist 使用说明

> 来源：[assets/templates/step_checklist.md](../../assets/templates/step_checklist.md)

### 6.1 加载模板

在 **完整模式**（≥ 1h 间隔）下，分析开始时加载 `assets/templates/step_checklist.md`：

```
分析开始时加载:
  assets/templates/step_checklist.md
  → 填入品种和时间
  → 开始逐步骤标记
```

### 6.2 逐步骤标记

清单涵盖 26 个步骤，分为以下阶段：

| 步骤范围 | 阶段 | 说明 |
|:--------:|:----:|------|
| Step 0 - 0.9 | 准备 | 加载历史上下文、确认品种、市场环境快检 |
| Step 1 - 1.11 | 市场状态 | 趋势方向、当前阶段、日类型、区间类型、多TF验证、Always In |
| Step 2.4 | 结构分析 | 支撑阻力标定、磁铁区域 |
| Step 3.1 - 3.9 | 规则检查 | SB 规则、CT 规则、交易者方程式、信号 K 线质量、BAN 禁止清单 |
| Step 4.0 - 4.2 | 交易计划 | 方向与设置、入场方案、仓位计算 |
| Step 5 | 风险管理 | 止损与目标设定 |
| Step 6 | 执行准备 | 最终确认（盈亏比、风险 ≤ 2%） |
| Step 7.1 - 7.4 | 入场执行 | 等待信号、确认信号、执行、设置止损 |
| Step 8 | 持仓管理 | 持仓监控、追踪止损 |
| Step 9 | 复盘记录 | 写入日志、更新 market_state、复盘归档 |

每个步骤标记状态：
- ⬜ 待执行
- ✅ 已完成
- ❌ 不适用/跳过

### 6.3 完成率检查

分析完成后计算完成率：

```
完成率 = 已完成步骤数 / 26 Steps
```

- 100%：完整分析
- < 100%：标记未完成步骤，说明原因

---

## 七、记忆触发条件表

> 来源：[memory/README.md](../memory/README.md) 第七章

AI 按以下条件自动触发记忆文件更新，无需用户手动操作：

| 触发事件 | 更新文件 | 更新内容 | 已有规则来源 |
|---------|---------|---------|------------|
| 26 步分析完成 | memory/market_state.md | 更新分析时间/状态/持仓/bar序列/形态/趋势 | analysis-continuity.md |
| 交易关闭（止损/止盈） | memory/pattern_effectiveness.md | 更新对应设置的胜率/笔数/盈亏比 | trading_journal.md |
| 交易关闭 + 触犯 BAN | memory/error_patterns.md | 更新错误频率 | trade_review.md |
| 交易关闭 + 新发现 | memory/market_wisdom.md | 追加新条目 | trade_review.md |
| 交易关闭 | memory/improvement_tracker.md | 更新改进计划进度 | trade_review.md |
| 交易关闭 | memory/strategy_hypotheses.md | 更新验证进度 | trade_review.md |
| 每日 UTC 00:00 | logs/analyses/ + memory/L2_daily/ | 生成日复盘 | daily_deep_review.md |
| 周日 | memory/L3_weekly/ | 周度提炼 | memory/README.md |
| 月末 | memory/L4_monthly/ | 月度审查 | memory/README.md |

---

> **引用索引**：
> - 连续性规则 → [references/analysis-continuity.md](../analysis-continuity.md)
> - Bar 类型分类 → [references/knowledge/theme13_reading_bar_by_bar.md](theme13_reading_bar_by_bar.md)
> - H1/L1→H2/L2 计数 & 双顶/双底 → [references/knowledge/theme3_pullbacks.md](theme3_pullbacks.md)
> - 突破确认与失败 → [references/knowledge/theme4_breakouts.md](theme4_breakouts.md)
> - 楔形（三推）→ [references/knowledge/theme5_reversals.md](theme5_reversals.md)
> - 趋势状态与 Always In → [references/knowledge/theme2_trends.md](theme2_trends.md)
> - Step Checklist → [assets/templates/step_checklist.md](../../assets/templates/step_checklist.md)
> - 记忆触发条件 → [memory/README.md](../memory/README.md)

---

## v33 新增：计划跟踪与执行写入规则

### 8.1 5路径分流规则

> Step 0.5b 检查结果直接决定后续路径。

| 路径 | 触发条件 | 执行步骤 | 跳过步骤 |
|------|---------|---------|---------|
| D | 有持仓 | Step 1 + Step 7 | Step 2-6 |
| A1 | 计划完全有效 | Step 1 快速更新 | Step 2-6 |
| A2 | 计划需调整 | Step 1+3 调参 | Step 2/4/5 |
| B | 计划已失效 | 完整 Step 1-6 | 无 |
| C | 无活跃计划 | 完整 Step 1-6 | 无 |

### 8.2 执行后写入规则（先执行后写入）

> 所有写入操作都是对已发生事件的记录，绝不预先写入。

| 路径 | 执行后写入文件 | 写入内容 |
|------|--------------|---------|
| D | market_state.md + logs/analyses/ + order_log.md + trade_review.md + memory/*.md | 市场状态+分析归档+修改记录+复盘+记忆更新 |
| A1 | market_state.md + logs/analyses/ | 仅更新价格+简短记录 |
| A2 | market_state.md + logs/analyses/ | 更新调整参数+调整原因 |
| B | market_state.md + logs/analyses/ + logs/orders/ | 标记旧计划失效+新计划+订单详情 |
| C | market_state.md + logs/analyses/ + logs/orders/ | 新计划+订单详情 |

### 8.3 记忆触发条件表扩展

在第七节触发条件表基础上，新增以下 Step 6.5 相关触发：

| 触发事件 | 更新文件 | 更新内容 |
|---------|---------|---------|
| Step 6.5 场景B下单成功 | logs/orders/ + memory/market_state.md | 订单详情+活跃计划→已触发 |
| Step 6.5 场景C挂单管理 | logs/orders/ | 更新订单记录 |
| Step 6.5 场景D止损/止盈调整 | assets/templates/order_log.md（阶段三） | 修改记录 |
| Step 6.5 场景D平仓执行 | order_log.md（阶段四）+ trade_review.md + memory/*.md | 平仓+复盘+记忆更新 |

### 8.4 交易闭环监控规则（强制）

> **核心规则：有交易计划就必须持续监控，直到成交或失效。这不是可选行为，是强制要求。**

#### 监控强制性声明

1. 一旦 market_state.md 活跃计划状态 = "等待触发"，后续每次分析必须走路径A1/A2
2. 一旦计划状态变为"已触发"（有持仓），后续每次分析必须走路径D
3. 只有计划状态变为"已失效"/"已过期"/"已取消"/"已成交"时，才允许走路径B/C生成新计划
4. 禁止在计划有效期间跳过 Step 0.5b 直接做完整分析

#### 阶段二强制监控场景（8个）

| # | 场景 | 检查内容 | 规则来源 |
|---|------|---------|---------|
| 1 | 等待触发条件触及 | 价格是否触及计划入场价/信号棒突破 | workflow §交易闭环 |
| 2 | 等待建仓形态 | 是否出现High 2/Low 2等建仓形态 | SKILL §Step 4.1 |
| 3 | 报告后等待 | 经济报告发布后是否已过1-3小时 | workflow §经济报告 |
| 4 | 等待回撤 | 趋势中是否出现H1-L4回撤 | workflow §回撤交易 |
| 5 | 等待二次入场 | 首次信号失败后是否出现新设置 | workflow §二次入场 |
| 6 | 限价单状态 | 挂单是否仍有效/需调整/需撤销 | workflow §Step 4.1 |
| 7 | BOM回测 | 开盘突破是否回测 | workflow §开盘突破 |
| 8 | 通道过冲 | 陡峭通道是否强突破 | workflow §趋势通道 |

#### 阶段四强制监控场景（7个）

| # | 场景 | 检查内容 | 规则来源 |
|---|------|---------|---------|
| 1 | 动态监控Q1-Q6 | AI方向/反转信号/磁铁/目标/铁丝网/方程式 | workflow §Step 5.2 |
| 2 | 追踪止损TS-01~05 | 创新高低/1R保本/2R缩仓 | strategy §20.3 |
| 3 | 仓位缩放 | 是否到达加仓/减仓位 | strategy §20.4 |
| 4 | 入场K线评估 | 首根K线质量是否需调整止损 | strategy §20.1 |
| 5 | 止损止盈调整 | 是否需要修改触发单 | strategy §TS系列 |
| 6 | 突破测试 | 入场后1-20棒是否回测入场点 | workflow §入场后测试 |
| 7 | 高潮确认 | 是否出现高潮K线需评估 | workflow §高潮交易 |

#### 闭环各阶段禁止行为

| 闭环阶段 | 强制行为 | 禁止行为 |
|---------|---------|---------|
| 等待触发 | 每次分析走A1/A2，15项检查 | 跳过监控直接生成新计划 |
| 已触发(持仓) | 每次分析走D，15项检查 | 忽略持仓做独立分析 |
| 平仓后 | CD-01~05再决策 | 立即反向开仓（需冷却期） |