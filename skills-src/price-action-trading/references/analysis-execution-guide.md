# 分析执行指南 — 如何正确执行一次完整分析

> 本文件记录实际执行分析时的正确流程、常见错误和注意事项。
> 基于实战经验总结，确保每次分析都严格按 references/SOUL.md 流程图执行。

## 核心原则

1. **按 references/SOUL.md 流程图逐步执行** — 不要凭记忆跳步
2. **每个 Step 必须读对应文件** — 按映射表的 workflow.md + strategy_workflow.md 列读取
3. **两套编号并存是设计** — 中文「步骤X」= 简化核心流程；英文「Step X.Y」= 扩展生命周期。不冲突，不需要统一
4. **加密货币止损必须校验** — Step 4.0 是卡点，不通过不计算方程

## 逐步执行清单

### 阶段零：盘前准备

| Step | 必须读的文件 | 输出 |
|------|------------|------|
| Step 0 | `memory/pattern_effectiveness.md`<br>`memory/error_patterns.md`<br>`memory/trader_profile.md`<br>`memory/strategy_hypotheses.md`<br>`memory/market_wisdom.md` | 有无活跃错误模式？有无已验证设置？有无待验证假设？ |
| Step 0.5 | `memory/trader_profile.md` + `memory/cross_instrument.md` | 当前市场是否在优势区间？ |
| Step 0.9 | `references/knowledge/workflow.md` 步骤0.9（认知偏差检查清单） | 逐项检查8种偏差，记录结果 |

### 阶段一：市场分析

| Step | 必须读的文件 | 输出 |
|------|------------|------|
| Step 1 | `references/knowledge/workflow.md` 步骤1.1（趋势定义+24条特征+20项区间特征）<br>`references/knowledge/strategy_workflow.md` 第2章（Q1-Q7决策树，逐条走查） | 趋势/区间/反转？强/弱？AIS/AIL？进入哪个规则引擎？ |
| Step 1.5 | `references/knowledge/workflow.md` 步骤1.5（5问清单）<br>`references/knowledge/theme13_reading_bar_by_bar.md` | 当前棒类型？与前棒关系？AIS变了吗？ |
| Step 1.8 | `references/knowledge/workflow.md` 步骤2.1（开盘模式）<br>`references/knowledge/theme14_trend_day_types.md` | 开盘区间大小？趋势日类型？概率数据？ |
| Step 1.9 | `references/knowledge/workflow.md` 步骤0.3<br>`references/knowledge/strategy_workflow.md` 第6.8章 AL-01~05 | 有无真空效应？Globex趋势？AL规则检查 |
| Step 1.10 | `references/knowledge/theme14_trend_day_types.md`<br>`references/knowledge/theme2_trends.md` Ch18-20 | 7种趋势日类型匹配 |
| Step 1.11 | `references/knowledge/source/V3_反转/V3_B05_P201-250.md` | 时间框架选择 |

### 阶段二：开盘专项

| Step | 必须读的文件 | 输出 |
|------|------------|------|
| Step 2.4 | `references/knowledge/workflow.md` 步骤2.4<br>`references/knowledge/source/V3_反转/V3_B06_P251-300.md` | 头10棒逐bar分析 |

### 阶段三：信号识别

| Step | 必须读的文件 | 输出 |
|------|------------|------|
| Step 3.1 | `references/knowledge/workflow.md` 步骤3.1-3.4（四大形态库）<br>`references/knowledge/strategy_workflow.md` 第6章（优先级表 P1-P5） | 匹配到哪些形态？优先级？ |
| Step 3.2 | `references/knowledge/workflow.md` 步骤3.6（信号棒评估规则）<br>`references/knowledge/strategy_workflow.md` 第6.5章 SB-01~18（逐条检查）<br>`references/knowledge/strategy_workflow.md` 第6.6章 CT-01~13（逆势筛选） | SB判定？CT是否否决逆势？ |
| Step 3.3 | `references/knowledge/workflow.md` 步骤3.7（多理由确认）+ 步骤3.8（交易者方程）<br>`references/knowledge/strategy_workflow.md` 第11章（TE-01~05） | ≥2个理由？方程为正？ |
| Step 3.5 | `references/knowledge/strategy_workflow.md` 第6.7章 BL-01~07<br>`references/knowledge/strategy_workflow.md` 第13章（冲突处理）<br>`references/knowledge/strategy_workflow.md` 第14-15章 | 有无决斗线？有无冲突？ |
| Step 3.9 | `references/knowledge/strategy_workflow.md` 第16章（P-01~P-76）<br>`references/knowledge/strategy_workflow.md` 第12章（BAN-001~BAN-015） | 概率评估？禁止清单通过？ |

### 阶段四：交易执行

| Step | 必须读的文件 | 输出 |
|------|------------|------|
| **Step 4.0** ⚠️ | `references/knowledge/strategy_workflow.md` 第7章 SL-01~SL-10 + SA-01~SA-08<br>`references/knowledge/instrument_crypto_specifics.md` 第五章（品种止损参考值） | **止损距离 ≥ 品种最小值？≤ SA-05上限？不通过则拒绝交易** |
| Step 4.1 | `references/knowledge/strategy_workflow.md` 第17章（限价单规则） | 止损/限价/市价？ |
| Step 4.2 | `references/knowledge/strategy_workflow.md` 第9章 PM-01~10 | 仓位计算 |
| Step 5 | `references/knowledge/strategy_workflow.md` 第11章 | 方程最终验证 |
| Step 6 | `references/knowledge/strategy_workflow.md` 第18章（10条核心规则） | 计划合规检查 |

### 阶段五-七：管理/复盘/再决策

| Step | 必须读的文件 | 输出 |
|------|------------|------|
| Step 7 | `references/knowledge/strategy_workflow.md` 第20章（Q1-Q6 + TS-01~05） | 入场后管理决策树 |
| Step 8 | `references/knowledge/strategy_workflow.md` 第21章（FB-01~04 + FL-01~03） | 失败处理决策树 |
| Step 9 | `references/knowledge/strategy_workflow.md` 第22章（CD-01~05）+ 第23章（PS-01~07） | 再决策树 + 心理检查 |

## 常见错误（Pitfalls）

### ❌ 错误1：凭记忆分析，不读文件

**表现**：写出"Q1: 有趋势 ✓"但没有打开 strategy_workflow.md 第2章逐条对照 Q1 的6个条件。

**正确做法**：每个 Step 打开映射表指定的文件，逐条检查。

### ❌ 错误2：止损太紧（加密货币）

**表现**：用价格结构止损（信号棒极值+1 tick）但远小于典型止损距离，容易被噪音扫掉。

**正确做法**：
- 参考 `instrument_crypto_specifics.md §5.1` 的典型止损距离作为合理性检查
- 原文规则：SA-04（止损 < 1 tick → 不交易）、SA-05（止损 > 日均波幅 × 20% → 减仓或放弃）
- 高波动环境（日波幅 > 正常 1.5 倍）需放宽 1.5 倍并缩小仓位 50%

### ❌ 错误3：跳过"不紧急"的步骤

**表现**：Step 3.5/3.10/3.11/4.5/5.5 经常被跳过。

**正确做法**：即使当前不适用，也要记录"不适用"及原因。

### ❌ 错误4：混合两套编号

**表现**：把 workflow.md 的"步骤3.2（反转交易建仓形态）"和 SKILL.md 的"Step 3.2（信号棒评估）"搞混。

**正确做法**：
- workflow.md 用「步骤X.Y」（49个细粒度步骤）
- SKILL.md 用「Step X.Y」（扩展生命周期）
- 它们是不同的编号体系，通过 references/SOUL.md 映射表关联

### ❌ 错误5：映射表只看 theme，不看 workflow + strategy_workflow

**表现**：分析中引用 theme15 但没有引用 strategy_workflow 第6.5章 SB 系列。

**正确做法**：映射表的每一行都有3列 — workflow.md（查什么）+ strategy_workflow.md（怎么决策）+ theme（补充知识）。三者都要参考。

## 高波动环境规则

当今日波幅 > 品种日均波幅参考值上限时（instrument_crypto 5.3）：

| 调整项 | 规则 |
|--------|------|
| 止损 | 放宽 1.5 倍 |
| 仓位 | 缩小 50% |
| R:R 要求 | ≥ 3:1 |
| 交易形态 | 只做高概率（P1-P2） |

## 分析输出格式

每次分析必须包含：
1. 数据源 + 时间戳
2. 每个 Step 的实际执行记录（读了什么文件、查了什么规则）
3. 决策树完整路径（Q1→Q2→...→结论）
4. 规则编号引用（SB-02、CT-03、BAN-01、TE-01 等）
5. Step 4.0 止损校验结果
6. 交易计划（入场/止损/目标/仓位/理由）
7. 入场后管理预设（第20章 Q1-Q6）
8. 失败处理预设（第21章）
9. 再决策预设（第22/23章）
