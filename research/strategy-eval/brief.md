# Research Brief — 策略评估与模拟盘体系深度调研

**Date:** 2026-09-28  
**Mode:** standard（4 angles，1 轮补查）  
**Project:** OmniAlpha（Gate 永续 + LLM 策略人格模拟盘赛马）

## Question
在多套交易人格（价格行为/SMC/订单流等）于本地模拟盘并行赛马的场景下：  
**应如何设计一套「可信、可比、可运维」的策略评估与模拟盘体系？** 覆盖指标体系、风控、排行榜、运维观测、与实盘交接标准。

## Scope
- In：绩效指标与统计口径；回测/前向评估方法；策略组合与热管理；模拟盘 vs 实盘差异；LLM 策略可观测性；排行榜/评分板产品形态；上实盘门槛（prop-firm 风格）
- Out：具体下单 API 实现细节；税务/合规；跨所套利策略本身
- Assumptions：加密永续 7×24；本金模拟 10k/户；策略 15–20 个并行；已修复撮合双记账等引擎 bug

## Angles
1. **F1 绩效指标标准** — 行业/学术上评价交易策略的必备指标与陷阱（Sharpe/Sortino/MAR/期望值/样本量）
2. **F2 前向 vs 回测** — paper/walk-forward/组合回测常见失败；多策略比较的统计显著性
3. **F3 风控与资金管理** — 仓位公式、热管理、相关性、熔断、回撤控制（Ralph Vince / prop firm 规则）
4. **F4 产品与运维** — 策略评分板/排行榜形态；模拟→实盘交接 checklist；LLM 交易系统的观测与审计

## Deliverable
`research/strategy-eval/REPORT.md` — 中文报告 + 引用；结论可直接升级 docs/compose/spec/system-hardening-plan.md 与 P2 设计
