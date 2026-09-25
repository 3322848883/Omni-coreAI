---
feature: live-prod-test
status: designed
updated: 2026-09-25
branch: master
commits: # 交付后填
---

# 实盘全功能生产测试方案

## Report

## [S1] Problem

现有测试多在 **testnet** 完成；实盘仅做过行情/触发评估。需要在**实盘**跑全功能生产测试，暴露真实问题（行情差异、账户、滑点、触发、执行、日志），并用**小额实单**验证执行链。

**边界（已确认）**：实盘允许小额实单（名义 ≈5–10U）；禁止大额。

## [S2] Design

### 2.1 测试矩阵

| # | 域 | 项 | 环境 | 验收 |
|---|-----|----|------|------|
| L1 | 行情 | last/K/ticker/funding/OI/盘口 | 实盘 REST | 字段齐全、时间戳递增 |
| L2 | 指标 | EMA/RSI/ATR/MA/MACD/BOLL 真K复算 | 实盘 K | 与独立复算一致 |
| L3 | 触发 | 11 种条件实盘评估 | 实盘 K | 返回 reason，可 fired |
| L4 | 策略 | 真实 LLM Plan（hold/下单意图） | 实盘快照 | 合法 Plan JSON |
| L5 | AI 触发 | Plan `triggers[]` 落盘/校验/TTL | 实盘 | 白名单/上限生效 |
| L6 | 风控 | require_sl / notional / halt | 实盘 | 拒单正确 |
| L7 | **执行** | **小额实单** 入场+TP+SL 三腿 | **实盘** | 回读确认，随后平仓 |
| L8 | 归属 | 先挂后撤、order_scope=own | 实盘 | 不误撤他单 |
| L9 | 日志 | trades jsonl 字段 | 实盘 | ts/plan_cycle/ok/steps |
| L10 | 多 bot | 并行 plan 隔离（不下实单或各 1 笔） | 实盘 | 目录/label 隔离 |

### 2.2 小额实单协议

| 规则 | 值 |
|------|-----|
| 单笔名义 | ≤ 10 USDT |
| 必带 | `sl` + `tp`（trigger 市价） |
| 生命周期 | 入场确认 → 记日志 → **立即 flatten + cancel** |
| 品种 | BTC 或 ETH（流动性好） |
| 失败 | 记问题，不盲目重下 |

### 2.3 真实问题记录

每条问题记：`id / 现象 / 复现 / 根因 / 级别 / 建议`，写入测试报告表。

### 2.4 安全

- 密钥仅环境变量
- 不在实盘跑网格/多腿大单
- 不开启 plan-loop 长驻（仅单轮）

## [S3] Out of Scope

- 大额实盘、杠杆压力测试
- trail 资金密码
- Intel 舆情稳定性专项
- 回测

## Tasks

- [ ] T1: 实盘行情+指标+触发（L1–L3） — acceptance: 汇总表无 FAIL (covers: S2.1)
- [ ] T2: 实盘 LLM Plan + AI 触发（L4–L5） — acceptance: Plan 合法、triggers 校验生效 (covers: S2.1)
- [ ] T3: 风控拒绝路径实盘（L6） — acceptance: SL/限额拒绝 (covers: S2.1)
- [ ] T4: 小额实单 L7–L9 三腿+日志 — acceptance: confirmed + journal + 清理 (covers: S2.2)
- [ ] T5: 问题清单 + 文档 — acceptance: 真实问题表 (covers: S2.3)
