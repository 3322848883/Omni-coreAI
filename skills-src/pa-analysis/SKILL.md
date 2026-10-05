---
name: pa-analysis
version: "v2.0"
description: "深度价格行为分析（低频、完整方案）：按七层分析 + 交易生命周期五阶段 + 退出规则，产出一个自洽可执行的 Plan JSON chip。只做纯价格行为（禁指标/基本面/其他学派）。Use when 需要完整深度分析、重分析对账、换会话后衔接旧方案，或查交易生命周期/退出规则/形态词表/交付门禁。"
---

# pa-analysis · 深度价格行为分析

你产出**完整、可追溯、可执行**的价格行为方案。与「每轮例行决策」不同，本技能面向**低频深度分析**：一次把方案想透，而不是快速给一个 chip。

**红线**：技能只影响你的观点与理由，**不改变可下单动作与风控约束**。执行闸门在 executor + yaml 风控。

## 输出契约

只输出一个 JSON 对象。**不能产出 HTML、不能写文件、不能跑脚本、不能派子代理。**
可用工具只有 35 个只读行情/账户工具（`klines` `indicators` `ticker` `orderbook` `contract` `stats` `account` `smc_map` `smc_events` `sqzmom` `taker_delta` `orderflow_*` `orderbook_*` `tv_*`）+ `skill` + `skill_ref`。

```json
{"cycle_id":"...","reasoning":"≤30字",
 "chips":[{"symbol":"BTC_USDT","action":"open_long","type":"limit","price":85800,
   "size_usd":423.35,"sl":85560,"tp":86150,"tp2":86600,"tp1_share":0.5,"leverage":50,
   "confidence":0.62,"reasoning":"≤30字",
   "region":"trend|range|reversal","invalidation":85560,"time_stop_bars":8,
   "give_back_pct":40,"risk_pct":1.4,"rule_ids":["SB-06","SA-05"],
   "scenarios":{"entry_pending":"...","position_open":"..."}}],
 "triggers":[]}
```

7 个决策字段（**必填**，它们进决策日志与订单上下文，下一轮你会看到本轮写了什么）：`region`（**`region=range` 时禁止给 `tp2`**）｜`invalidation`（前提失效价，触及即视为结构破坏）｜`time_stop_bars`（最大持仓轮数）｜`give_back_pct`（浮盈回撤阈值%）｜`risk_pct`（实际风险占权益比例）｜`rule_ids`（依据的规则/形态 ID）｜`scenarios`（入场后情形→应对动作）。

`action` 枚举与 `type` 取值见 `price-action-trading` 技能；`type=limit` 必须给 `price`。

## 反触发边界（Exclusions）

本技能**只做纯价格行为**，以下一律不分析、不纳入方案，出现即明确返回「非本技能范畴」：

- **技术指标**：MACD/RSI/布林带/KDJ/CCI/OBV/SAR/VWAP 及任何指标交叉、背离信号。（例外：EMA20 仅作趋势背景辅助、ATR14 仅用于止损距离/波幅度量，**二者均不构成入场信号**。）
- **基本面/新闻/政策**：CPI、GDP、利率决议、财报、监管公告、新闻情绪等事件驱动。
- **非价格行为学派**：缠论、波浪理论、江恩、谐波形态、量化模型、机器学习预测、数学评分（「强度 8/10」「置信度 0.85」）。
- **长期投资/资产配置**：只做短周期决策（scalp/波段），不做估值、定投、配置建议。
- **越界信息**：资金费率/多空比/清算热力图/链上数据/ETF 资金流等**不属于本技能知识库**，不进分析、不作任何入场/离场依据；与价格行为相关时以价格行为为准。
- **缺失输入**：没有具体品种或 OHLC 数据时先取数，不凭空分析。

## 强制原则

1. **方案完整**：chip 的每个要素（方向/入场/止损/目标/仓位/触发/失效）都要能追溯到具体分析结论与形态 ID；说不清来源的要素不得进方案。
2. **判断可追溯**：把依据写进 `rule_ids`（形态名取自 `sequence-vocabulary.md` 的 63 条词表，**写错会被拒**）。
3. **账户联动**：先调 `account` 取余额/持仓/挂单/保护单；无资金、持仓冲突、风控锁定 → 不开新仓。仓位 = 权益 × 单笔风险 ÷ 止损距离。
4. **真实性**：所有数值来自行情工具或账户接口；**禁止编造**。
5. **术语双层表达**：经典术语 → 普通话解释 → 动作含义；禁止「涨不动/必然/大概率/看上方」等模糊词。
6. **交易链 7 段**：当前判断 → 交易假设 → 触发证据 → 执行动作 → 失败/止损 → 目标磁体 → 删除条件 → 不交易条件。**缺任一段就降级为观察**，不输出入场。
7. **交易生命周期完整**：方案必须覆盖五阶段 —— 触发前 / 触发确认（信号K→入场K→跟进K）/ 入场后（≥8 种情形）/ 失效（fatal/warning 分级）/ 止损后（禁追回+二次机会+复盘+冷却）。**任一阶段缺失 → 不给 `open_*`**。规范见 `references/trade-lifecycle.md`。
8. **方案交接对账（重分析强制）**：同品种存在旧方案时（**从订单上下文读，不靠会话记忆** —— 换会话仍可执行），产出逐方案对账：已触发 / 已失效 / 继续等待（注明「延续自 YYYY-MM-DD」）/ 结构已变（supersede + 一句话归因：行情演化还是判断修正）。**任何旧方案不得静默丢弃**；首份标注「无前版」。
9. **输出即完整**：方案在生成时必须带全分析位 + 执行位 + 生命周期五阶段 + 组合边界 + 离场规则；**禁止先出骨架、事后补字段** —— 补出来的字段没经过分析流程，等于让程序做判断。
10. **形态类条件必须带 params**：`signal_bar` / `sequence` / `momentum_check` / `risk_reward_check` 类条件**必须携带非空 params**；`signal_bar` 名须在 11 种登记枚举内、`sequence` 名须在 63 种词表内（见 `references/sequence-vocabulary.md`、`references/contract-enums.md`）。语境类形态（尖峰暂停 / 二次入场 / 最终旗形 / 区间陷阱 / SPS）**必须标注 `ai_judged: true`**。
11. **数值引用口径三律**：①量能倍数统一「前 20 均」口径（`vol_x_avg20`），逐处标注周期；确需「相对前一根」口径必须显式标注，**同段禁混用**。②关键价位来源必须含**精确 bar 时间戳**（「日期+bar 时刻+周期」）。③同词同义全文一套口径；两套口径并存时首次出现处必须声明。

## 分析工作流（无脚本版）

节点2 原先靠 `scripts/` 做预检与特征表；bot 没有代码执行，**改为用工具取数 + 自己判读**：

0. **预检**：调 `account`（持仓/挂单/保护单/权益）+ `klines`（主周期 ≥200 根）+ `contract`（精度/最小名义）。**先确认数据齐了再分析。**
1. **交接对账**：读订单上下文里的旧方案，按强制原则 8 四选一定案。
2. **走势分段**：把最近 ≥10 根逐根读（`klines` 拉足够根数 + `indicators` 取 EMA20/ATR14），标出摆动高低点。
3. **市场状态**：趋势 / 区间 / 反转 → 填 `region`。
4. **证据层**：形态识别（`pattern-catalog.md`）+ 信号K质量（`03_signal_confirmation.md`）+ 信号×背景（`32_signal_context_matrix.md`）。
5. **交易计划**：入场带 / 止损 / 目标（磁铁与测量移动）/ 仓位 / 触发条件 / 失效条件。
6. **交易生命周期**：按强制原则 7 补全五阶段。
7. **仓位风控**：按 2% 风险反推 `size_usd`；写 `risk_pct`。
8. **自检**：见下。

## 知识路由（按需 `skill_ref` 读取）

**每份 ≤12,000 字符，一次能读完**（超限的已分片，`_part1` 优先）。

| 分析步骤 | 读取资产 |
|---|---|
| 走势分段 / 七状态 | `assets/knowledge/01_concept_recognition.md`、`assets/knowledge/19_playbook.md`、`assets/knowledge/31_market_cycle.md` |
| 状态决策树 | `assets/knowledge/02_decision_tree.md` |
| 信号K质量 | `assets/knowledge/03_signal_confirmation.md`、`assets/knowledge/24_bar_by_bar.md` |
| 信号×背景 | `assets/knowledge/32_signal_context_matrix.md` |
| 数推 / 回调计数 | `assets/knowledge/62_bar_counting_pullback_part1.md`、`assets/knowledge/35_two_leg_move.md` |
| Always In | `assets/knowledge/20_always_in.md` |
| 多时间框架 | `assets/knowledge/12_multi_timeframe.md`、`assets/knowledge/60_timeframe_matrix.md` |
| 入场规则 | `assets/knowledge/51_entry_rules.md`、`assets/knowledge/55_stop_order_rules.md`、`assets/knowledge/58_limit_vs_stop.md` |
| 形态/结构/入场速查（全表） | `references/pattern-catalog_part1.md` |
| 形态识别 | `assets/knowledge/15_pattern_recognition.md`、`33_climax.md`、`38_bop.md`、`39_dbl_dbs.md`、`40_final_flag.md`、`45_breakout.md`、`43_ii_iii_ioi.md`、`46_trading_range.md`、`47_reversal_mtr.md` |
| 通道/区间/反转结构 | `assets/knowledge/44_channel.md`、`46_trading_range.md`、`47_reversal_mtr.md`、`35_two_leg_move.md`、`36_pullback_rally.md`、`34_good_trend.md`、`59_failed_low1_low2.md` |
| 特殊结构（缺口/高潮/急速/开盘/收缩/铁丝网） | `assets/knowledge/09_gap_analysis.md`、`33_climax.md`、`29_surprise_bar.md`、`48_spike_channel.md`、`26_opening_analysis.md`、`41_shrinking_stairs.md`、`42_barb_wire.md`、`49_oscillation_bar.md`、`57_trend_day_types.md`、`56_battle_line_pbt.md`、`63_buying_selling_pressure.md` |
| 磁体 / 测量 | `assets/knowledge/37_magnet.md`、`10_mm_retracement.md`、`15_pattern_recognition.md` |
| 概率数据 | `assets/knowledge/52_probability_data.md`、`30_80_20_rule.md` |
| 仓位 / 风控 | `assets/knowledge/65_position_scaling.md`、`64_actual_risk.md`、`05_position_management.md`、`27_anti_loss_rules.md`、`13_risk_automation.md` |
| 加密/合约适配（环境差异/概念映射/结构止损） | `assets/knowledge/67_crypto_market.md` |
| 交易生命周期 | `references/trade-lifecycle.md` |
| 多空逻辑 / 风格 | `assets/knowledge/16_bull_bear_logic.md`、`22_scalping_vs_swing.md`、`21_traders_equation.md` |
| 心理 / 复盘文化 | `assets/knowledge/25_psychology.md`、`61_sentiment_scale.md`、`14_case_studies.md`、`17_three_school_integration.md`、`23_three_layer_model.md`、`18_knowledge_hub.md` |
| 复盘 / 失败 | `assets/knowledge/11_failure_handling.md`、`06_review_evolution.md` |
| 自检（数值溯源/关系断言/计数复核） | `references/verification-checklist.md` |
| 交付门禁（44 项，当自检清单用） | `references/delivery-gates.md` |
| 序列词表 / 信号→订单意图 / 契约枚举 | `references/sequence-vocabulary.md`、`signal-intent-matrix.md`、`contract-enums.md` |
| 形态与结构全表 | `references/pattern-catalog_part1.md` |
| 计划字段定义 | `references/plan-schema_part1.md` |

`assets/knowledge/` 共 67 个模块（编号 01–67），上面没列到的按编号规律猜不到就**先别读**，优先读已列出的。

## 交付前自检（无报告版）

bot 没有「文末」，所以验收并入决策自检 —— **把未通过项写进 `rule_ids` 的说明里**。

逐条核对（证据来自你已取到的数据）：

1. **数值溯源**：每个关键价位都能指到「日期 + bar 时刻 + 周期」。
2. **链式不变式**：多头 `SL < 入场带下沿 < 带上沿 < T1`（严格不等），空头镜像 —— **相等即断裂**（历史事故：SL == 带下沿被整版拒绝）。
3. **RR 自洽**：用带中值入场实算的盈亏比，与 `rule_ids` 里声明的形态基准一致。
4. **失效对应**：每条失效条件都有对应处置（漏接 = 触发后无人接管）。
5. **前提失效位**：`invalidation` 必须是**数值**；**空头方案的语义是「收盘站上」**（写反方向 = 永不失效或秒失效）。
6. **止损距离**：`|SL − 入场带中值| ≥ 0.5 × ATR`（ATR 取方案执行周期：swing→1h、scalp→5m）；不足即「噪声扫损侧」。
7. **时间口径**：`time_stop_bars` 与风格自洽（swing 不用分钟级叙事；scalp 时间止损应短）。
8. **占位清零**：任何字段都不许留「X点 / 待填 / TBD / TODO」。

## 事件触发

`triggers[]` 可选（命中后重新分析，**不直接下单**）。类型与参数范围见 `price-action-trading` 技能；同时最多 5 个生效。
