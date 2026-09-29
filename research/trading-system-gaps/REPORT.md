# AI 提示词策略交易系统 — 功能缺口分析报告

> 日期: 2026-09-28 | 调研: 5 角度 61 条发现 | 对象: gate-signal-bot

---

## 一、我们已有什么

| 层 | 已有功能 | 覆盖度 |
|---|---|---|
| **LLM 策略** | 21 人格 / 3 融合模式 / 讨论模式 | ★★★★☆ |
| **记忆** | 四层记忆 / 事件溯源 / top-K 事件 | ★★★★☆ |
| **工具** | 20 LLM 工具 / 23 族指标 / 2 套 SMC | ★★★★★ |
| **数据** | 6 所 / hybrid / WS+REST | ★★★★☆ |
| **执行** | 市价/限价/突破/TP-SL/grid/追踪 | ★★★☆☆ |
| **风控** | min_conf / max_notional / require_sl | ★★★☆☆ |
| **模拟盘** | paper（交易所语义复刻）| ★★★★☆ |
| **运维** | PidLock / 日志 / 崩溃恢复 | ★★★☆☆ |

---

## 二、缺口矩阵（按优先级排序）

### P0 — 核心缺失（不补会出事）

| # | 缺口 | 来源依据 | 现状 | 建议 |
|---|------|----------|------|------|
| 1 | **组合级风控** | TradingAgents 风控团队 [F1-1]、OpenPM typed constraints [F4-1]、相关性控制 [F4-4] | 只有单 bot 风控 | 跨 bot 敞口/相关性/日内亏损限制 |
| 2 | **回测验证体系** | DSR+PBO+SPA+MinTRL [F2-3]、walk-forward 三段协议 [F2-11]、vs B&H 基准 [F2-6] | 无系统化回测 | LLM 策略回测框架 + 统计显著性 |
| 3 | **策略衰减检测** | alpha decay 三路径 [F5-3]、7×24 监控 [F5-5]、performance decay [F5-2] | 无 | 滚动窗口监控 + 自动告警 |
| 4 | **订单状态机完整覆盖** | OKX mmp_canceled / IOC 部分成交 [F3-5] | 基础状态机 | 补终态分支 + 部分成交处理 |

### P1 — 重要缺失（影响竞争力）

| # | 缺口 | 来源依据 | 现状 | 建议 |
|---|------|----------|------|------|
| 5 | **智能执行（TWAP/VWAP）** | VWAP 直接优化 [F3-3]、Hyperliquid TWAP [F3-4]、OKX slippagePct [F3-7] | 市价/限价 | 大单拆分 + 滑点保护 |
| 6 | **自我批判/信念更新** | FinCon conceptual verbal reinforcement [F1-6] | 讨论模式只修正决策 | 周期性 self-critique → 投资信念 |
| 7 | **监控告警** | 全链路埋点→Kafka→Flink→AlertManager [F5-1] | 日志+PidLock | 延迟/成交率/滑点/PnL 异常告警 |
| 8 | **策略评分** | MinervaScore [F2-5]、CLQT 五轴 [F2-7] | metrics 有记分板 | DSR/PBO 集成 + 多维评分 |
| 9 | **仓位管理** | 波动率目标 [F4-7]、Kelly+vol-regime [F4-8] | 固定 size_usd | 波动率目标缩放 |
| 10 | **交易所限频管理** | OKX 按成交率动态配额 [F3-6] | 无限频控制 | 限频跟踪 + 自适应 |

### P2 — 锦上添花（长期竞争力）

| # | 缺口 | 来源依据 | 现状 | 建议 |
|---|------|----------|------|------|
| 11 | **多模态**（K 线图识别） | FinAgent 视觉 [F1-4] | 纯文本 | K 线图输入 LLM |
| 12 | **在线自适应重训** | FreqAI self-adaptive retraining [F1-7] | 固定 prompt | prompt 自动优化 |
| 13 | **DCA/GRID/COMBO bot** | 3Commas/Bitsgap [F1-10,11] | 有 grid | DCA/COMBO 扩展 |
| 14 | **交易日志/复盘** | 3Commas Trading Journal [F1-10] | journal（记忆） | 可视化复盘 |
| 15 | **组合再平衡** | Bitsgap portfolio rebalance [F1-11] | 无 | 跨 bot 资金再平衡 |
| 16 | **A/B 测试** | 多目标 post-validation [F5-6] | 无 | 策略对比框架 |
| 17 | **防 MEV/防夹** | Flashbots Protect [F3-8] | CEX 为主 | DEX 场景预留 |
| 18 | **快/深思考混合** | TradingAgents 双模型 [F1-3] | 单模型 | 摘要用快模型、决策用深模型 |

---

## 三、优先级路线图

```
Phase 1（P0，2-3 周）:
  ① 组合级风控（跨 bot 敞口/相关性/日内亏损）
  ② 订单状态机补全（mmp_canceled/部分成交）
  ③ 策略衰减检测（滚动监控+告警）
  ④ 回测验证框架（LLM 策略 OOS + 统计显著性）

Phase 2（P1，3-4 周）:
  ⑤ TWAP/滑点保护
  ⑥ self-critique 信念更新
  ⑦ 监控告警（延迟/成交率/滑点/PnL）
  ⑧ 仓位管理（波动率目标）
  ⑨ 策略评分（DSR/PBO 集成）

Phase 3（P2，按需）:
  ⑩ 多模态 / ⑪ 在线重训 / ⑫ DCA/COMBO / ⑬ 组合再平衡
```

---

## 四、关键结论

1. **最大的缺口是「组合级」**：我们单 bot 做得不错，但 21 个 bot 跑在一起时**没有组合风控**（相关性/集中度/总敞口）——这是 TradingAgents 等系统的核心能力

2. **回测是硬伤**：LLM 策略无法直接回测，但可以「prompt 固化→OOS 模拟」[F2-1]。40 个 LLM 策略仅 2 个跑赢 B&H [F2-6]——没有回测就无法知道策略是否真有 alpha

3. **策略衰减比过拟合更隐蔽**：「曾经有效但失灵」需要 7×24 监控 [F5-5]，我们目前完全没有这层

4. **执行层有提升空间**：大单拆分（TWAP）、滑点保护（slippagePct）、完整订单状态机是商业系统的标配 [F3]

5. **我们领先的部分**：四层记忆（论文级设计）、多人格讨论模式、事件溯源审计——这三块**超过多数开源/商业系统**

---

## Open Questions
- 加密永续的杠杆/保证金优化一手证据不足（F4 标记 medium）
- LLM token 成本归因的行业实践尚薄（F5 标记 low）
- A/B 测试框架的 LLM 策略专门方案未找到成熟参考

## Sources
完整来源见 `research/trading-system-gaps/findings/F1-F5.md`（61 条 sourced findings，25+ 一手来源）。

---

*报告由 deep-research 流程生成：5 个独立 sub-agent 并行调研，覆盖学术/开源/商业三线。*
