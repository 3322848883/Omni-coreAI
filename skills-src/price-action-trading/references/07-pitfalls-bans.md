# 陷阱与禁止清单

> 已知的分析陷阱（来自实战复盘）+ 禁止事项清单。**出手前扫一眼**。

# 陷阱与禁止清单（v35.0 合并版）

> 完整陷阱清单。SKILL.md 只保留摘要；分析前建议通读本文。
> 编号已去重合并（原稿曾出现重复的 11/18/19，以及缺口 6/7/12/13）。

## 文件协同

### Pitfall 1：workflow × strategy_workflow 必须配合

- **错**：只加载 workflow.md 走完 checklist，输出模糊建议。
- **对**：先读 references/SOUL.md → 同时用 workflow + strategy_workflow → Step 1 走 Q1-Q7 → 进入 T/R/V 规则引擎 → 输出具体规则 ID。

### Pitfall 2：两套 Step 编号并存

- 中文「步骤X」= 简化流程；英文「Step X.Y」= 完整生命周期（SOUL 流程图）。
- SKILL/SOUL 的 Step 3.2（信号棒）≠ workflow.md 步骤 3.2（反转建仓）。
- **执行顺序以 references/SOUL.md 流程图为准**，不要混用两套编号解释同一步。

### Pitfall 3：SOUL 流程图与映射表必须同步

已修复过的问题（勿回退）：

- Step 1.9 盘前/机构 → 阶段一（不是阶段三）
- Step 1.10 趋势日类型 → 不得缺失
- Step 3.2 不得重复；Step 3.3 不得在 3.2 前
- Step 4.0 止损校验必须在 Step 4.1 入场方式**之前**
- Step 7 必须拆成 7.1–7.4，不得合并
- 映射表三列：Step | workflow（查什么）| strategy_workflow（怎么决策）

修改 SOUL 后应用脚本检查 Steps 无重复、无逆序。

### Pitfall 4：必须读取实际文件

- **错**：凭记忆写分析。
- **对**：open strategy_workflow 对应章节 + memory/ 五文件；引用 T/R/V/SB/CT/BAN 等 ID；决策树逐条打勾。
- 详见 knowledge-file-reading.md。

### Pitfall 5：禁止子代理做行情分析

- 子代理无法访问技能目录下的 references/knowledge/memory/templates，且会自编规则。
- **对**：用行情工具取数（klines/indicators），一次取足所需根数，串行完整分析。
- 多品种：一品种一完整 26 步，宁可串行也不批量简化。
- 详见 pitfall-subagent-analysis.md、multi-instance-deployment.md。

### Pitfall 6：分析完整性 — 26 Steps 不得跳过

即使判定「不交易」：

- Step 4.1/4.2 必须写条件性方案（条件满足后的入场方式与仓位）
- Step 7.1–7.4 各自独立段落并引用章节
- Step 8 含失败处理；Step 9 含再决策与心理检查
- 每品种独立 26 步，禁止合并

标准示例：[assets/examples/](../assets/examples)。

### Pitfall 7：空壳分析（Hollow Analysis）

填满 26 步但无读文件、无规则 ID、无逐条评估 = 无效。

自查：

1. memory/ 五文件已读
2. Q1–Q7 逐条
3. SB/CT 逐条
4. 规则 ID ≥ 10
5. 多理由 ≥ 2/3

（v32 时代的旧 pitfalls 已并入本文件。）

### Pitfall 8：格式以 examples 为准

- 先读 `assets/examples/01-btc-*.md` 作为格式标准。
- 紧凑输出：决策树一行路径；SB/CT 表格一行一条；条件方案必填；引用清单合并。
- 禁止：大段「原文说明」、每步 `---` 分隔、重复打印、SB/CT 写成十八行空话。

### Pitfall 9：路径与数据加载可移植

- **禁止**在技能正文/脚本中写死已失效的个人绝对路径（如旧 hermes 目录）。
- **对**：
  - 技能根 = `PRICE_ACTION_SKILL_ROOT` 或 SKILL.md 所在目录
  - 数据 = `PRICE_ACTION_DATA_DIR`（默认 `<skill>/data`）
  - 输出 = `PRICE_ACTION_OUTPUT_DIR`（默认 `<skill>/logs/reports`）
- 一次把 4H/1H/5M 各 200 根取足（klines 带 limit），避免 10+ 次碎调用。

### Pitfall 10：知识库三系统分离

| 系统 | 存什么 |
|------|--------|
| references/knowledge/ | 分析引擎（workflow/strategy/theme/source） |
| memory/ | 动态交易记忆 |
| assets/templates/ | 日志与分析结果 |
| Obsidian | 人工笔记（分析时勿引用） |

详见 knowledge-architecture.md。

## 风险与市场状态

### Pitfall 11：加密货币止损校验（Step 4.0，强制）

原文规则：

- **SA-04**：止损距离 < 1 tick → 不交易
- **SA-05**：止损距离 > 日均波幅 × 20% → 减仓或放弃

流程：结构止损 → SA-04 → SA-05 → 波动率调整 → 不通过则不进方程。  
加密货币波动约为 ES 的 3–8 倍，结构止损可能过紧；参考 `instrument_crypto_specifics.md §5.1`。  
详见 [stop-loss-rules-consolidated.md](stop-loss-rules-consolidated.md)、[crypto-stop-loss-framework.md](crypto-stop-loss-framework.md)。

### Pitfall 12：极端波动日（日波幅 > 5%）

| 波动率比率（波幅/价） | 止损 | 仓位 | 最低 R:R |
|----------------------|------|------|----------|
| < 4% 正常 | 1x | 100% | 按设置 |
| 4–6% 高 | 1.5x | ×50% | ≥3:1 |
| ≥6% 极端 | 2x | ×25% | ≥5:1 |

极端日额外：不追突破；不在区间中部；CT 全否决；止损过 Step 4.0。  
加密参考：BTC 常规 1.5–4%，ETH 2–5%，SOL 3–8%（各自极端阈值更高）。

### Pitfall 13：手续费必须进入交易者方程

```
手续费 ≈ 入场价 × 0.1%（双向 0.05%×2）
实际盈利 = 目标盈利 - 手续费
实际风险 = 止损距离 + 手续费
方程：P(win)×实际盈利 > P(loss)×实际风险
```

小止损交易 R:R 会被费用显著拉低。

## 计划闭环

### Pitfall 14：独立分析陷阱

每次从零分析、不看上次计划 → 计划互相矛盾。  
**解**：Step 0.5b 计划衔接 + 五路径分流。

### Pitfall 15：计划不一致陷阱

无故推翻仍然有效的旧计划。  
**解**：路径 A1/A2 优先管理现有计划；仅路径 B 生成新计划。

### Pitfall 16：写入遗漏陷阱

下单/平仓后不写日志与记忆 → 复盘断链。  
**解**：Step 6.5 执行后按写入清单更新；**先执行后写入**。  
详见 analysis-lifecycle-workflow.md。

## 验证清单（每次完整分析后）

**验收**：bot 无「文末」，验收并入决策自检 —— 把未通过的规则 ID 写进 rule_ids 的说明里。 导图冲突见 [mindmap-gap-list.md](mindmap-gap-list.md)。以下为交付前速查：

- [ ] SOUL 流程图 26/26，无跳步
- [ ] 规则 ID ≥ 10，含 SB/CT/BAN 逐条
- [ ] Step 4.0 止损校验完整（加密必做）
- [ ] 方程已扣手续费
- [ ] 不交易时 4.1/4.2 有条件方案
- [ ] G1：多 TF ≥3 档 + 主 TF 逐 K ≥10 + 波段 SH/SL
- [ ] 文末引用清单（Step | workflow | strategy）

- [ ] market_state.md 已更新
- [ ] 无硬编码失效路径

## 相关文档

- [mindmap-gap-list.md](mindmap-gap-list.md)
- analysis-continuity.md
- complete-analysis-workflow.md
- efficient-analysis-pattern.md
- data-loading-workflow.md
- trading-execution-guide.md


---

### 附录D：禁止事项清单

#### 绝对禁止

- [ ] 不使用止损
- [ ] 在铁丝网（Barb Wire）中交易
- [ ] 在紧密交易区间（Tight Trading Range）中用止损入场
- [ ] 在强趋势中逆势交易（初学者）
- [ ] 基于新闻交易
- [ ] 过度交易（一天超过合理数量）
- [ ] 在单笔交易上冒超过2%的风险
- [ ] 追逐市场（FOMO入场）
- [ ] 在1分钟图上交易（大多数交易者会亏钱）
- [ ] 使用心理止损（不使用实际在市场中的止损）
- [ ] 在亏损后将交易转换为投资（不遵守止损）
- [ ] 在连续亏损后加大仓位（报复性交易/Martingale）
- [ ] 在经济报告发布期间交易
- [ ] 不遵守交易计划
- [ ] 在没有至少2个理由的情况下交易
- [ ] 十字星信号K线入场（除非极端位置+第二个理由）→ 禁止交易（BAN-05）
- [ ] 在区间中间用止损入场
- [ ] 在不明确的图表中交易
- [ ] 试图预测顶部和底部（初学者）
- [ ] 在交易区间日做摆动交易（初学者）

#### 强烈不建议

- [ ] 在第一小时之后才开始交易（错过最佳机会）
- [ ] 在日波幅中部交易（新手）
- [ ] 使用限价入场（初学者应只用止损入场）
- [ ] 加仓（初学者）
- [ ] 在没有趋势线突破的情况下逆势交易
- [ ] 在交易区间中做摆动交易
- [ ] 在没有测量移动目标的情况下做摆动交易
- [ ] 在连续亏损后继续交易（应停止当天交易）
- [ ] 在盈利后放松纪律
- [ ] 在不记录交易日志的情况下交易
- [ ] 在市场状态不明确时强行交易
- [ ] 使用超过2点的止损（Emini）而不增加目标
- [ ] 在收盘前最后一小时逆势加仓

#### 需要特别注意

- [ ] 在11:30 PST附近交易（强趋势日可能有止损扫荡）
- [ ] 在区间顶部做多（除非有强突破信号）
- [ ] 在区间底部做空（除非有强突破信号）
- [ ] 在高潮后立即反转交易（等待确认）
- [ ] 在第一根K线收盘前入场
- [ ] 在连续3次或更多失败交易后继续交易
- [ ] 在没有更高时间级别确认的情况下做大方向决策
- [ ] 在交易计划之外的时间段交易

---
