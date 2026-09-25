---
feature: prelaunch-test
status: delivered
updated: 2026-10-20
branch: master
commits: 422c1ee..b2d37e4
---

# 上线前全量测试方案（模拟盘 + 实盘）

## Report

**What was built** — `scripts/prelaunch_runner.py`（矩阵网关 + `.prelaunch_test/report.json`）与分域模块 `prelaunch_orders/fault/live.py`，覆盖 M1–M20。已合入 `master`（`b2d37e4`）。

**最终上线前复验（合并后 master）** — **0 FAIL / 0 SKIP**：

| 域 | 结果 |
|----|------|
| unittest | 148 OK |
| readonly testnet + live（含 LLM Plan） | 各 27 PASS |
| testnet 下单矩阵 | 15 PASS |
| 故障注入 | 10 PASS |
| 实盘 ≤10U 闭环 | 4 PASS，无残留 |
| M20 regress | 148 OK |

**Verification** —
- `python -m unittest discover -s tests` → 148 OK
- `prelaunch --phase readonly --env testnet|live` → 27+27 PASS
- `prelaunch --phase orders --env testnet` → 15 PASS
- `prelaunch --phase fault` → 10 PASS（含 M16 部分成交）
- `prelaunch --phase live` → M19 entry+三腿+journal+flatten
- `prelaunch --phase regress` → M20 148 OK

**Journey log** —
1. M12 暴露 P1：`cancel_all` 无视 `order_scope=own` 误撤他 bot 单 → 按 label/`label_prefix` 过滤。
2. 市价回退曾挂 `gtc` 在 maker 侧 → 改为吃对手价 + IOC，并夹在 last±0.2% 公允价带。
3. review 抓出 M4/M5 未覆盖、M14 空断言 → 已补。
4. label `bot`/`bota` 前缀误匹配 → `_text_owned` 分段安全；`label_prefix` 命名空间防伪造。
5. EMA 复算需 SMA 播种；testnet 薄簿用例改为可成交限价。

### 真实问题清单

| ID | 现象 | 根因 | 级别 | 状态 |
|----|------|------|------|------|
| PRE-01 | 多 bot cancel_all 误撤他单 | 未按 label 过滤 | **P1** | **已修** |
| PRE-02 | 市价回退 gtc 挂住 / 追宽簿 | 回退挂 maker 侧 | P2 | **已修**（IOC+公允价带） |
| PRE-03 | testnet 薄簿无法成交 | 环境 | P2 ENV | 用例改限价后通过 |
| PRE-04 | 各币 min_notional 差异 | quanto | P2 | 快照 `contract` 已暴露 |
| PRE-05 | label 前缀误匹配 | startswith | P2 | **已修** |
| PRE-06 | 信号可伪造他 bot label | 无命名空间 | P2 | **已修** `label_prefix` |

## [S1] Problem

功能已在 testnet/实盘分项验证过（信号、指标、触发、LLM Plan、TP/SL、网格、多 bot），但缺少一份**上线前统一全量**矩阵：同一版本上把正常路径、风控拒绝、故障注入与恢复、多维度交叉一次性跑完，暴露「单测过、组合挂」的问题。需要可重复执行的测试方案 + 脚本 + 问题清单，作为放行依据。

**已确认边界**
- 实盘允许 **≤10 USDT 名义小额实单**，必带 TP/SL，成交后 **立即 flatten + cancel**
- 覆盖深度：**全维度含故障与恢复**（断网、部分成交、进程中断后对账、幂等重发）
- 工作区：`.worktrees/prelaunch-test`，分支 `feat/prelaunch-test`

## [S2] Design

### 2.1 环境与安全

| 项 | 约定 |
|----|------|
| 模拟盘 | Gate **testnet**，全量下单矩阵 |
| 实盘 | Gate **live**，只读矩阵 + ≤10U 小额实单（上限 **4 轮**，单轮 ≤10U） |
| 密钥 | 仅环境变量 `GATE_*` / `OPENAI_*`，不落盘 |
| 失败处理 | 记问题，**不盲目重下**；单测用例失败不阻断后续域 |
| 收尾 | 每轮实单后 flatten + cancel_all；结束时账户无残留挂单/仓位 |
| 禁止 | 大额、高杠杆、trail（资金密码）、长驻 plan-loop 实盘 |

### 2.2 测试矩阵（模拟盘全量 + 实盘只读/小额）

| ID | 域 | 用例要点 | testnet | live | 验收 |
|----|----|----------|:-------:|:----:|------|
| **M1** | 行情 | last/kline/ticker/funding/OI/盘口；多 symbol；stale 标记 | ✓ | ✓只读 | 字段齐全、`t` 递增 |
| **M2** | 指标 | ema/rsi/atr/ma/macd/boll 真 K 复算；未知名拒绝 | ✓ | ✓只读 | 与独立复算一致 |
| **M3** | 合约元数据 | `contract.quanto/min_notional/round` 进快照；未知合约 degraded | ✓ | ✓只读 | 5+ 币含小数币 |
| **M4** | 触发条件 | 9 种叶子 + all/any；cooldown；interval；kline_close | ✓ | ✓只读 | 可 fired/不误火 |
| **M5** | AI 触发 | `triggers[]` TTL/白名单/max_active；命中跑 Plan 不下单 | ✓ | ✓只读 | 越权被拒 |
| **M6** | LLM Plan | 合法 JSON；hold；多 chip；四仓位模式（size_usd/size/pct 拒绝/smart hold） | ✓ | ✓只读 | 见 `test_llm_sizing_modes` |
| **M7** | 订单类型 | market/limit/post_only/ioc/fok + trigger 市价/限价 | ✓ | 小额 | 全类型回读 status |
| **M8** | TP/SL 三腿 | 入场+TP+SL 同时挂；`trigger_price` 按 tick 取整；归属 own | ✓ | 小额 | 三腿 confirmed |
| **M9** | 网格 | long/short/dual；`tp_scope` per_level/shared；每档 SL | ✓ | ✗ | expand 继承正确 |
| **M10** | 仓位换算 | size_usd→张；不足 1 张拒绝；size 张数；size_pct；margin_pct | ✓ | 小额 | 与 `contract` 一致 |
| **M11** | 风控拒绝 | require_sl；max_notional；halt；max_leverage；allow_actions；符号白名单 | ✓ | ✓只读 | 每条可复现拒绝 |
| **M12** | 多 bot 隔离 | 并行 plan；inbox/label/risk 分离；互不误撤 | ✓ | ✗ | 目录与订单归属 |
| **M13** | 日志审计 | trades.jsonl 字段；cycle_id；write_hold | ✓ | ✓ | 可追溯 |
| **M14** | 幂等/重发 | 同 cycle 重复投递不双开；确认失败不盲目重下 | ✓ | ✗ | 仅 1 笔有效单 |
| **M15** | 故障：超时 | REST/LLM 超时不自动重试；降级标记 | ✓ | ✗ | 不重复下单 |
| **M16** | 故障：部分成交 | 部分 fill 后 reduce/close 数量正确 | ✓ | ✗ | 剩余仓位正确 |
| **M17** | 故障：中断恢复 | 挂单中 kill 进程 → 启动对账 `reconcile_protection` | ✓ | ✗ | TP/SL 补齐或告警 |
| **M18** | 故障：参数边界 | 非法 symbol/action/price round/负 size | ✓ | ✗ | SchemaError 拒收 |
| **M19** | 实单闭环 | 小额 entry filled + TP/SL + journal + flatten | ✗ | ✓ | 无残留 |
| **M20** | 回归门禁 | `unittest discover` 全绿 | ✓ | — | 无 FAIL |

### 2.3 实盘小额协议（M19）

| 规则 | 值 |
|------|-----|
| 单笔名义 | ≤10 USDT（不足 1 张的币自动跳过并记录 min_notional） |
| 轮数上限 | 4 轮（含失败重试） |
| 必带 | `sl` + `tp`，trigger 默认 mark |
| 生命周期 | 成交确认 → journal → **立即 flatten + cancel_all** |
| 品种 | 优先 BTC/ETH/DOGE/BNB（min_notional ≤10U 或接近） |
| 演练字段 | 含 trigger 价取整（历史 LIVE bug）、order_scope=own |

### 2.4 故障注入方法（M15–M18）

| 手段 | 适用 |
|------|------|
| 不可达 base_url / 超时 timeout_sec=1 | LLM、REST 超时 |
| 半填订单 mock 或 testnet 限价远离盘口再部分吃单 | 部分成交 |
| `process_file` 中途 `os._exit` 子进程模拟 kill，再重启 `reconcile_protection` | 中断恢复 |
| 同一 signal 文件复制两次 / 同 cycle_id | 幂等 |
| 直接调用 schema.parse_* 喂非法 JSON | 参数边界 |

### 2.5 报告格式

每个用例记录：`ID / 环境 / 结果 PASS|FAIL|SKIP / 证据(order_id|日志) / 问题ID`。  
问题表：`id / 现象 / 复现 / 根因 / 级别 P0-P3 / 建议`，写入本文件 `Report`。

### 2.6 执行顺序

1. T0 门禁：unittest
2. T1 只读链（M1–M3, M6, M11, M13）testnet+live
3. T2 testnet 下单链（M7–M10, M12, M14）
4. T3 故障注入（M15–M18）
5. T4 实盘小额闭环（M19）+ 回归（M20）
6. T5 汇总问题清单 + 更新本 Report

## [S3] Out of Scope

- 大额/杠杆/压力性能测试
- trail 追踪单（资金密码）
- Intel 舆情稳定性
- 策略收益/回测/实盘盈利验证
- 未在文档中的新功能开发

## Tasks

- [x] T0: 测试网关与汇总器（结果 JSON + 问题表收集）— acceptance: `scripts/prelaunch_runner.py` 可按矩阵 ID 过滤执行并输出汇总 (covers: S2.5)
- [x] T1: 只读域 M1–M3/M6/M11/M13 — acceptance: testnet+live 只读项全 PASS 或已知 SKIP，无假数据 (covers: S2.2)
- [x] T2: testnet 下单域 M7–M10/M12/M14 — acceptance: 全类型/网格/多 bot/幂等 PASS，清理无残留 (covers: S2.2; depends: T0)
- [x] T3: 故障注入 M15–M18 — acceptance: 每类至少 1 条 PASS，恢复路径可复现 (covers: S2.4; depends: T2)
- [x] T4: 实盘小额闭环 M19 + 回归 M20 — acceptance: ≤4 轮实单 confirmed+journal+flatten，账户无残留 (covers: S2.3; depends: T2)
- [x] T5: 问题清单与 Report — acceptance: 问题表含真实发现，status→delivered (covers: S2.5; depends: T1,T3,T4)
