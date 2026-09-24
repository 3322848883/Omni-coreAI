# 交易系统设计深度报告（Trading System Deep Review）

**范围**：gate-signal-bot + pa-data-source（monorepo）  
**视角**：生产级加密永续策略执行系统（多机器人 / 多策略）  
**证据**：代码结构、并发测试（29/29）、全链路与订单确认实测  
**日期**：2026-09-24

---

## 0. 结论摘要

| 维度 | 评级 | 一句话 |
|------|------|--------|
| **独立策略稳定性** | ★★★★☆ | 多 bot 并发 plan/执行/日志隔离已实测 29/29，可独立跑 |
| **下单正确性** | ★★★★☆ | 开仓/TP/SL 三腿回读确认 + 重试 + fail-closed |
| **行情与指标** | ★★★★☆ | 真 K 独立复算一致；EMA/RSI/ATR/MA/MACD/BOLL + 条件触发 |
| **组合级风控** | ★★☆☆☆ | **缺**：日内亏损上限、总敞口、回撤熔断、kill switch |
| **故障恢复** | ★★☆☆☆ | **缺**：启动对账、孤儿单清理、cycle 去重持久化 |
| **运维可观测** | ★★☆☆☆ | 有 trades JSONL；缺 metrics/告警/PnL 看板 |
| **执行质量** | ★★★☆☆ | 有滑点回退/限价收价；无拆单、无手续费核算、无部分成交策略 |

**总评**：作为「策略信号 → 程序执行」中台，**主链路生产可用**；要扛真金白银长期跑，必须先补 **P0 组合风控 + 崩溃对账 + 全局急停**。

---

## 1. 系统定位与已验证能力

```text
pa-data-source ──write──► kline.db（只读）
                              │
gate_bot strategist ──hybrid──┘
  快照(指标/资金费/OI/盘口) → LLM Plan → 风控 → inbox JSON
                                    │
                          watcher / executor
                                    │
                    开仓 + TP/SL 三腿确认 → Gate
                                    │
                          logs/trades/*.jsonl
```

### 1.1 独立策略机器人：已达标项

| 能力 | 证据 |
|------|------|
| 多 bot 并行 LLM plan | 3 线程 18.6s，cycle 各自独立 |
| 信号目录隔离 | `inbox/<bot_id>/` 互不写错 |
| 风控独立 | `max_notional` 20/40/50 各自拒绝超限 |
| 执行并发 | 3× `run_bot_once` 同时跑 failed=0 |
| 日志隔离 | `trend-a.jsonl` / `meanrev-b.jsonl` / `breakout-c.jsonl` |
| 标签隔离 | `ta` / `mb` / `bc` 进订单 text |
| 策略人格隔离 | 3 套 prompt_file |
| 持仓策略 | `free` / `strict`（修复了 `run_bot_once` 未传 policy） |
| 三腿订单确认 | entry/TP/SL 回读 + 重试 + 未挂上即失败 |

**结论：独立稳定运行 = 是（测试网多轮验证）。**

---

## 2. 架构分层评估

| 层 | 现状 | 问题 | 级别 |
|----|------|------|------|
| **L1 数据** | WS/REST 管道 + REST 现拉 + 指标 | 无数据质量评分；假突破/坏 tick 无过滤 | P2 |
| **L2 决策** | LLM Plan + 固定契约 + 条件触发 | 无组合约束；无 regime 过滤 | P1 |
| **L3 风控** | bot 级 notional/confidence/actions/chips | **无账户级/组合级** | **P0** |
| **L4 执行** | 三腿确认、滑点回退、限价收价 | 无部分成交、无拆单、无费用 | P1 |
| **L5 账务** | trades JSONL + archive | 无 PnL、无保证金监控、无对账 | **P0** |
| **L6 运维** | 文件归档、可选 pa watchdog | 无指标/告警/急停/审计看板 | **P0** |

---

## 3. 缺口清单（按交易系统设计原则）

### 3.1 P0 — 上真钱前必须有

| ID | 缺口 | 风险 | 建议 |
|----|------|------|------|
| **R1** | **无全局 Kill Switch** | 异常策略/行情黑天鹅无法一键停 | 配置/信号级 `halt: true`；watcher 读到即拒绝一切新单，只允许平仓 |
| **R2** | **无日内亏损 / 回撤熔断** | 连亏失控 | 账户级 `daily_loss_limit`、`max_drawdown_pct`；触线 → 全 bot halt |
| **R3** | **无总敞口控制** | 多 bot 叠加杠杆爆仓 | 账户 `max_total_notional`、`max_open_positions`；下单前算未实现敞口 |
| **R4** | **启动无对账** | 崩溃重启后孤儿挂单 / 裸仓（无 SL） | 启动时：扫 positions ↔ price_orders ↔ trades；缺 SL 则按策略补挂或告警 |
| **R5** | **cycle_id 去重仅内存** | 重启后重复下单 | 持久化 `history/last_cycle.json` 或 DB |
| **R6** | **无 PnL / 保证金监控** | 不知赚亏与强平距离 | 实时 `unrealised_pnl`、`margin_ratio`、`liq_price`；写 trades + 告警 |
| **R7** | **多 bot 同账户无协调预算** | 同时开仓打满保证金 | 账户预算池：bot 各自 `max_notional` 之和 ≤ 账户上限；或互斥 symbol 锁 |

### 3.2 P1 — 稳定盈利所需

| ID | 缺口 | 风险 | 建议 |
|----|------|------|------|
| **R8** | 部分成交不感知 | 20U 只成交 10U，TP/SL 仍按 20U | 用 `finish_as`/`left` 算真实成交；TP/SL 尺寸跟随 `filled` |
| **R9** | 无拆单 / TWAP | 大单冲击 | `size_usd > X` 时拆 N 片 |
| **R10** | 无手续费 / 滑点核算 | 账面假盈利 | 记录 `fill_price` vs `decision_price`、taker/maker 费 |
| **R11** | 无订单 amend | 只能 cancel+replace，短真空窗 | 支持 Gate amend（价改量改） |
| **R12** | LLM 无超时熔断 / 降级 | 网关挂导致整轮空转 | LLM 失败 → 用最近一次 Plan 或纯规则触发 |
| **R13** | 无策略置信度校准 | confidence 随意 | 历史 hit-rate 校准；hold 占比监控 |
| **R14** | 条件触发无防抖/毛刺过滤 | 假突破打止损 | 要求 N 根确认 / ATR 过滤 |
| **R15** | 无杠杆上限策略化 | 策略可设 100x | bot 级 `max_leverage` 强制 |

### 3.3 P2 — 运维与长期

| ID | 缺口 | 建议 |
|----|------|------|
| **R16** | 无 Prometheus/日志指标 | 下单延迟、成功率、拒单率、degraded |
| **R17** | 无告警通道 | 钉钉/TG/邮件：裸仓、熔断、LLM 连续失败 |
| **R18** | 无回测对齐 | 同一套 indicators/risk 做 backtest，防「实盘逻辑漂移」 |
| **R19** | pa aux Intel 不稳 | 已重试；可降级只用 cex REST |
| **R20** | 无配置热加载 | 改 risk 要重启 |
| **R21** | prompt 注入面 | snapshot 若含外部文本需转义/分隔 |
| **R22** | trail 仍搁置 | 资金密码/权限 |
| **R23** | 单机单进程假设 | 无分布式锁；多机部署会双下单 |

---

## 4. 关键设计问题深度分析

### 4.1 组合风险是当前最大空洞

```text
现状：botA max 20U + botB max 40U + botC max 50U = 理论 110U 同时开仓
      账户 3300U × 杠杆 10 → 名义敞口可达 1100U（33%）
      若再开 leverage=50 … 无程序拦截
```

**建议模型**：

```yaml
account_risk:
  max_total_notional_usd: 200
  max_open_positions: 5
  max_leverage: 10
  daily_loss_limit_usd: 50
  max_drawdown_pct: 10
  halt_on_breach: true      # 触线只允许 flatten/cancel
```

### 4.2 崩溃恢复（Exactly-once 语义缺口）

| 阶段 | 失败 | 现状 | 目标 |
|------|------|------|------|
| Plan 写 inbox | 崩溃 | 可能重复文件 | 文件名含 cycle_id + 幂等 |
| 开仓成功 / TP 失败 | 崩溃 | 重启后裸仓 | **R4 对账**自动补 SL |
| 下单超时未知结果 | 崩溃 | 可能重复单 | client order id / 本地 id 去重 |

### 4.3 多策略共享账户

已测隔离是「逻辑隔离」；**资金是共享的**。缺：
- 账户预算租约（budget lease per bot）
- 同 symbol 策略互斥或 netting 规则文档化
- 保证金冲突：`strict` 只挡「同 symbol 已有仓」，不挡「保证金不足」

### 4.4 执行价格与估值

- 决策用 `last`，下单市价可能滑；TP/SL 用 trigger 价 vs mark
- 应统一 **mark_price** 作强平参考，`price_type: mark` 已有但未强制
- 未监控 `liquidation_price` 与当前价距离

### 4.5 LLM 策略层可靠性

| 优点 | 风险 |
|------|------|
| 固定契约、风控在程序、hold 默认 | 输出不确定；重试成本高 |
| cycle 去重 | 内存态 |
| 温度 0.1 | 仍可能输出非法 JSON → 本轮 skip（安全） |

**建议**：规则信号（条件触发）与 LLM 双通道；LLM 只在触发时「加强」而不是唯一入口。

---

## 5. 与业界对照（简化）

| 子系统 | 标准实践 | 本系统 | 差距 |
|--------|----------|--------|------|
| OMS | 订单状态机 + 对账 + amend | 下单+确认+撤 | **对账/amend** |
| Risk | pre-trade / 账户 / 日损 | pre-trade bot 级 | **账户级** |
| Execution | 路由 / 拆单 / 滑点控制 | 单笔 + 回退 | 拆单/费用 |
| Market data | 多源校验 / 时钟同步 | 单源 REST/WS | 多源 |
| Observability | metrics / 告警 / 审计 | JSONL 日志 | 告警 |
| Kill switch | 硬件/配置双通道 | 无 | **P0** |

---

## 6. 升级路线图（建议）

### Sprint A — 熔断与对账（P0，~2–3 天）
1. `halt` 开关 + 只允许平仓模式  
2. 账户 `max_total_notional` / `daily_loss_limit` / `max_leverage`  
3. 启动对账：裸仓补 SL、孤儿单列出  
4. `cycle_id` 持久化  

### Sprint B — 账务与执行质量（P1，~3–5 天）
5. PnL / margin / liq_price 进 trades 与快照  
6. 部分成交 → TP/SL 尺寸跟随  
7. 大单拆分 + 手续费/滑点统计  

### Sprint C — 策略稳健（P1–P2）
8. 条件触发防抖 + regime 过滤  
9. LLM 失败降级到规则通道  
10. 回测同构 harness  

### Sprint D — 运维（P2）
11. metrics + 告警  
12. 配置热加载、审计只读端点  

---

## 7. 独立策略机器人稳定性结论

| 问题 | 答案 |
|------|------|
| 各策略 bot 能独立跑吗？ | **能**。并发 plan/执行/日志/标签/风控已测 29/29 |
| 互相干扰吗？ | 逻辑层不干扰；**资金层会叠加敞口**（见 R7） |
| 重启后还安全吗？ | **尚无对账**（R4/R5）——P0 |
| 能直接扛大资金吗？ | **不建议**。先完成 Sprint A |

**一句话**：独立策略机器人 **运行层已稳定**；**资金安全层未闭环**。补上 Kill Switch、账户风控、启动对账后，才算「交易系统」而不仅是「自动下单器」。

---

## 8. 附录：现有防护清单（已具备）

- 白名单 symbols、max_notional、max_chips、allow_actions  
- position_policy（free/strict/manage_only）  
- replace 防堆积、TP/SL 三腿确认 + 重试  
- account 失败 abort、空仓 flatten no-op  
- live/testnet 密钥与库隔离、无 dry-run 明示  
- trades JSONL + archive done/failed  
- 指标计算与 Gate CLI 交叉验证（真 K）  
