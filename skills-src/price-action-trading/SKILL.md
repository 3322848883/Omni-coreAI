---
name: price-action-trading
version: "34.2"
tags: [trading, price-action, al-brooks, candlestick, trend, range, reversal, breakout, pullback, scalping, swing-trading, cryptocurrency, risk-management, trading-psychology]
description: "Al Brooks 价格行为交易辅助：按 26 步工作流做 K 线行情分析、交易计划、止损校验、交易复盘与规则检查。Use when 用户要求分析 BTC/ETH/SOL/XAU 等 K 线或图表截图、制定或检查交易计划、做交易复盘与自我改进、查询价格行为规则（Always In、H2/L2、交易者方程、信号棒、BAN 禁止清单），或提到 Al Brooks、price action、价格行为、26步分析。Do not use for 基本面选股、荐币、新闻面交易、量化回测或非 K 线策略。"
license: Proprietary
---

# Al Brooks 价格行为交易系统

基于 Al Brooks 三部曲 + *Reading Price Charts Bar by Bar* 的 AI-First 交易工作流技能。

## 核心能力

| 功能 | 结果 | 典型触发 |
|------|------|----------|
| 交易决策辅助 | 26 步分析 + 可执行计划（入场/止损/目标/仓位） | 「分析 BTC」「现在能做空吗」 |
| 交易复盘与自我进化 | 日志/错误模式/记忆更新 | 「复盘这笔交易」「我总在追突破」 |
| 规则检查清单 | 按规则 ID 逐条合规检查 | 「检查这个计划合规吗」 |
| 知识库查询 | 概念/概率/原文出处 | 「H2 是什么」「Always In 怎么判断」 |

## 执行入口（强制顺序）

1. **先读 [references/SOUL.md](references/SOUL.md)** — AI 角色、26 Steps 流程图、workflow×strategy 映射、执行规范、路径解析。
2. **同时使用** [references/knowledge/workflow.md](references/knowledge/workflow.md)（走到哪一步）+ [references/knowledge/strategy_workflow.md](references/knowledge/strategy_workflow.md)（这一步怎么做）。
3. **格式标准**：[assets/templates/price_action_analysis.md](assets/templates/price_action_analysis.md) + [assets/examples/](assets/examples)（BTC/ETH/SOL/XAU/XAG）。
4. **验收规范**：[references/analysis-acceptance.md](references/analysis-acceptance.md) — G0/G1/G2 门禁；完整分析文末必须粘贴验收表。
5. **导图对齐**：[references/mindmap-gap-list.md](references/mindmap-gap-list.md) — 导图↔规则映射；冲突 **Brooks 优先**。
6. **完整细则与陷阱**：[references/skill-pitfalls.md](references/skill-pitfalls.md)、[references/analysis-lifecycle-workflow.md](references/analysis-lifecycle-workflow.md)。

禁止：只跑 checklist 不引用规则 ID；用子代理做行情分析；多品种批量简化分析；凭记忆编造规则。

## 路径与数据（可移植）

| 配置项 | 环境变量 | 默认 |
|--------|----------|------|
| 技能根目录 | `PRICE_ACTION_SKILL_ROOT` | 本 SKILL.md 所在目录 |
| K 线数据目录 | `PRICE_ACTION_DATA_DIR` | `<skill>/data` |
| 分析报告输出 | `PRICE_ACTION_OUTPUT_DIR` | `<skill>/logs/reports` |

- 所有相对路径均基于**技能根目录**，不要写死个人用户目录。
- 外部行情源若存在，通过 `PRICE_ACTION_DATA_DIR` 指向其 `data` 目录（JSON：`{symbol}_usdt_{tf}.json`）。
- 数据加载：4H/1H/5M 各 **200 根** K 线；优先用脚本或 execute_code 读取。

```bash
python scripts/analyze_market_state.py btc 4h,1h,5m
python scripts/generate_analysis.py btc
```

## 知识与记录索引

### 决策引擎与流程

| 文件 | 用途 |
|------|------|
| [references/SOUL.md](references/SOUL.md) | 角色 + 26 Steps + 映射 + 执行规范 |
| [references/knowledge/workflow.md](references/knowledge/workflow.md) | 8 阶段 / 49 步流程驱动 |
| [references/knowledge/strategy_workflow.md](references/knowledge/strategy_workflow.md) | 23 章 / 133+ 规则引擎（T/R/V/SB/CT/BL/AL/BAN/CD/PS） |
| [references/knowledge/instrument_crypto_specifics.md](references/knowledge/instrument_crypto_specifics.md) | 加密货币：资金费率、止损适配 |

### 主题知识（按需读取，勿一次加载全部）

| 文件 | 内容 |
|------|------|
| [references/knowledge/theme1_basics.md](references/knowledge/theme1_basics.md) | 价格行为基础、K 线与信号棒 |
| [references/knowledge/theme2_trends.md](references/knowledge/theme2_trends.md) | 趋势、通道、趋势日、入场规则 |
| [references/knowledge/theme3_pullbacks.md](references/knowledge/theme3_pullbacks.md) | 回撤、H1-L4、缺口棒 |
| [references/knowledge/theme4_breakouts.md](references/knowledge/theme4_breakouts.md) | 突破、区间、三角形、铁丝网 |
| [references/knowledge/theme5_reversals.md](references/knowledge/theme5_reversals.md) | 反转、MTR、高潮、失败模式 |
| [references/knowledge/theme6_magnets.md](references/knowledge/theme6_magnets.md) | 磁铁与测量移动 |
| [references/knowledge/theme7_management.md](references/knowledge/theme7_management.md) | 止损/出场/仓位/交易风格 |
| [references/knowledge/theme8_daytrading.md](references/knowledge/theme8_daytrading.md) | 日内、Always In、关键时间 |
| [references/knowledge/theme9_psychology.md](references/knowledge/theme9_psychology.md) | 心理准备与认知偏差 |
| [references/knowledge/theme10_market_cycle.md](references/knowledge/theme10_market_cycle.md) | 市场周期 |
| [references/knowledge/theme11_probability.md](references/knowledge/theme11_probability.md) | 概率分层数据 |
| [references/knowledge/theme12_abbreviations.md](references/knowledge/theme12_abbreviations.md) | 187 缩写速查（术语正文在此） |
| [references/knowledge/theme13_reading_bar_by_bar.md](references/knowledge/theme13_reading_bar_by_bar.md) | 逐 bar 阅读 |
| [references/knowledge/theme14_trend_day_types.md](references/knowledge/theme14_trend_day_types.md) | 趋势日类型 |
| [references/knowledge/theme15_signal_bar_standards.md](references/knowledge/theme15_signal_bar_standards.md) | 信号棒评估标准 |
| [references/knowledge/theme16_price_action_basics.md](references/knowledge/theme16_price_action_basics.md) | 基础规则与纪律 |
| [references/knowledge/theme17_continuity_and_tracking.md](references/knowledge/theme17_continuity_and_tracking.md) | 分析连续性与价格行为跟踪 |

原文证据：`references/knowledge/source/V1_趋势篇/`、`V2_区间/`、`V3_反转/`（共 27 篇）。概念不确定时查 theme 或 source，并标注章节/页码。

### 记忆与模板

| 路径 | 用途 |
|------|------|
| [memory/](memory/) | 交易记忆（画像/错误/有效性/智慧/假设/跨品种/market_state） |
| [assets/templates/](assets/templates) | 分析与复盘模板（先看 `price_action_analysis.md`） |
| [references/analysis-acceptance.md](references/analysis-acceptance.md) | **分析验收规范**（G0 硬性 / G1 深度含 G1-13～16 / G2 闭环） |
| [references/mindmap-gap-list.md](references/mindmap-gap-list.md) | 思维导图对齐与未采纳项（NG-01～07） |
| [references/](references/) | 生命周期、连续性、止损整合、架构说明、Pitfall 细则 |

三系统分离：分析读 `references/knowledge/`，结果写 `assets/templates/`，沉淀进 `memory/`。不要把分析结果写入 Obsidian。

## 执行规范摘要（完整版见 references/SOUL.md）

1. **26 Steps 全执行** — 即使「不交易」，Step 4.0/4.1/4.2 也要写条件性方案。
2. **决策带规则 ID** — 如 `T-001`、`R-007`、`BAN-01`、`SA-05`；禁止空泛「建议观望」。
3. **Step 3.2 SB/CT 逐条** — SB 至少 01/02/06/08/10，CT 至少 03/05/07/09/13。
4. **Step 3.9 BAN 逐条** — 至少 BAN-01/02/07/09。
5. **Step 4.0 止损校验** — SA-04（≥1 tick）、SA-05（≤日波幅×20%）；加密货币必查 crypto 文件。
6. **交易者方程扣费** — 双向 0.05%；`实际盈利=目标-费用`，`实际风险=止损+费用`。
7. **计划衔接** — 分析前读 `memory/market_state.md`，Step 0.5b 走 D/A1/A2/B/C 路径。
8. **引用清单** — 文末输出 Step → workflow → strategy_workflow 映射表。
9. **实际读文件** — 按需 open 对应章节；规则 ID < 10 视为空壳分析风险。
10. **一品种一完整分析** — 禁止子代理分析、禁止多品种合并简化。
11. **交付前验收** — 按 [references/analysis-acceptance.md](references/analysis-acceptance.md) 勾选 G0/G1/G2，文末输出验收表（PASS / PASS-DEGRADED / FAIL / HOLLOW + 深度标签）。无验收表 = 未验收。
12. **完整分析含 G1-13～16** — 市场周期四选一、20根法则、四技能自检、MM/目标类型；导图与规则冲突时以 **Brooks / strategy_workflow** 为准（[mindmap-gap-list.md](references/mindmap-gap-list.md)）。

## 两套 Step 编号（互补，非冲突）

| 体系 | 含义 | 示例 |
|------|------|------|
| 中文「步骤X」 | 简化核心流程 | 步骤1 = 确定市场状态 |
| 英文「Step X.Y」 | 完整生命周期（以 SOUL 流程图为准） | Step 3.2 = 信号棒评估 |

注意：SKILL/SOUL 的 Step 3.2 ≠ workflow.md 的「步骤3.2」（后者是反转建仓形态）。执行顺序以 **references/SOUL.md 配合流程图** 为准。

## 核心原则（速记）

- 趋势中约 80% 反转尝试失败；区间中约 80% 突破失败。
- 二次入场通常比首次可靠；交易者方程必须为正。
- 高潮 ≠ 反转；紧凑区间与铁丝网慎入（BAN）。
- 主时间框架 5 分钟；唯一指标 EMA20。
- 概率多在 40%–60%；条件满足给精确价位，不满足指出未通过的规则 ID。

术语全文 → [references/knowledge/theme12_abbreviations.md](references/knowledge/theme12_abbreviations.md) 与 [references/knowledge/theme1_basics.md](references/knowledge/theme1_basics.md)。  
Pitfall 全文 → [references/skill-pitfalls.md](references/skill-pitfalls.md)。  
验收规范 → [references/analysis-acceptance.md](references/analysis-acceptance.md)。  
导图对齐 → [references/mindmap-gap-list.md](references/mindmap-gap-list.md)。  
升级说明 → [references/history/](references/history/)。
