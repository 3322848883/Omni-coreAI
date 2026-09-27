---
feature: local-paper-trading
status: delivered
updated: 2026-09-27
branch: feat/multi-exchange
commits: 0fb7bec..HEAD
---

# 本地模拟盘（Local Paper Trading）— 复刻交易所语义

## Report

**What was built** — 一个复刻交易所语义的本地模拟盘：`env: paper` 即启用，行情/合约元数据/资金费率委托绑定的真实交易所，订单/持仓/资金/强平/费率结算全在本地 `data/bots/<id>/paper/account.db`。盘口价成交（买→ask 卖→bid），订单全类型（limit/market/stop_entry/TP-SL 触发/GTC/IOC/FOK/PO/POC），含精度校验（tick/lot/最小名义/价格带/杠杆）、保证金与强平引擎、8h 真实资金费率结算、实时权益估值。默认 10000 USDT / 20x 可配。独立进程 `python -m gate_bot paper-run --bot <id>`（含撮合 tick 线程），复用信号链/风控/LLM 策略/20 工具。

**Verification** — `python -m unittest discover -s tests`：**293 PASS**（含 24 个 paper 测试：精度校验、盘口价撮合、PnL/quanto、强平、资金费率、Executor 契约集成）。`tests.test_paper` + `tests.test_paper_executor` 覆盖 Gate 嵌套 body、`id` 字段、`price=0` 市价语义、`poc` 别名、available 重算、quanto 贯穿。

**Journey log**
- 首版只做了扁平 body 的撮合，Review 抓出 Executor↔Adapter 契约错配（C1–C4）：Gate 价格单是嵌套 `{initial,trigger}`、订单要 `id` 不是 `order_id`、市价=`price:"0"+tif=ioc`、post_only 别名 `poc`。教训：**新 adapter 必须跑 Executor 级集成测试**，单测 engine 会漏契约 bug。
- quanto_multiplier 只在手续费里用了，PnL/保证金/费率漏了 → 对真实 Gate 合约（BTC quanto=0.0001）会差 1/quanto 倍。教训：**面值系数要贯穿所有名义计算**。
- paper 一开始还走 `load_credentials`，与「脱离交易所账户」矛盾；移到 paper 分支前短路。
- PowerShell 逗号参数会把 `--intervals "1m,...,1d"` 截断、Python 三引号嵌套反复炸——改用脚本文件 + Edit 工具。

## [S1] Problem

需要一个**本地模拟盘**：行情、AI 分析、交易记录、盈亏计算全部与真实交易一致，只有资金是虚拟的。订单挂在本地而不是交易所，但价格到了要像交易所一样自动触发，并自动实时计算盈亏。**复刻一个模拟交易所**——合约精度、保证金、强平、委托有效期、价格带、订单状态机、错误码等交易所细节全部对齐。

## [S2] Design

### 总体结构

```
inbox/<bot>/ 信号 ──► executor（复用）──► PaperExchange（gate_bot/paper/）
                                              │
      行情/合约元数据/费率/精度 ────────────────┤ 委托给绑定的真实所（feed）
      订单/持仓/资金/成交/强平/费率 ────────────┤ 本地 paper_account.db
      撮合·强平·结算引擎（engine+risk）─────────┘  盘口价成交
```

- **独立 paper bot 进程** + **独立账户库**：`python -m gate_bot paper-run --bot <id>`
- 每个 paper bot **绑定一个 feed 交易所**（六所任选），合约元数据（精度/最小名义/杠杆上限）从该所读取并本地执行
- **不需要交易所 API 密钥**（paper 分支短路 load_credentials）

### [S2.1] PaperExchange adapter（`gate_bot/paper/exchange.py`）

实现 `ExchangeClient` 全接口 + Executor 扩展（`get_available_usdt` / `place_trailing_order` / `stop_trailing_orders`）。`name="paper"`。

| 方法组 | 行为 |
|---|---|
| 行情：`get_last_price`/`get_ticker`/`get_klines`/`get_orderbook_top`/`get_contract_stats` | 委托绑定 feed 所 |
| 合约：`get_contract` | 委托绑定所读精度/最小名义/杠杆上限 |
| 账户：`get_account`/`get_positions`/`get_available_usdt`/`get_position_mode`/`is_dual_position_mode` | 读本地库，返回 Gate 字段（含 `id`） |
| 交易：`place_order`/`place_price_order`/`cancel_*`/`list_*`/`get_order`/`get_price_order`/`close_position` | 写本地库；**Gate 形状 body/返回** |
| 杠杆/保证金：`set_leverage`/`set_margin_mode` | 校验 ≤ leverage_max |

**Gate 契约对齐**（Review 后补）：
- `place_price_order` 收嵌套 `{initial:{contract,size,price,tif,text}, trigger:{rule,price_type,price}}`
- 订单/触发单 dict 带 **`id`**（Executor 确认用）
- 市价 = `price:"0"` + `tif=ioc`，无 `type` 字段
- `tif=poc`（post_only 别名）接受；IOC/FOK 未立即成交即撤销（FOK 回滚成交）
- `close_position(contract, side=long|short)` 按 side 过滤（dual 模式）

### [S2.2] 账户库（`gate_bot/paper/store.py`，八表）

`data/bots/<bot_id>/paper/account.db`：config / account / positions / orders / price_orders / fills / funding_log / pnl_snapshot

**默认**：`initial_capital=10000`、`leverage=20`、`fee_rate=0.0005`、`funding_enabled=true`，bot yaml `paper:` 段可覆盖。`available = balance - position_margin - order_margin`，成交后自动重算。

### [S2.3] 合约精度与校验（`gate_bot/paper/validate.py`）

tick（order_price_round）/ lot（order_size_round）/ 最小名义 / 价格带（默认 ±5%）/ 杠杆上限。拒绝原因对齐交易所风格（`price out of band` / `insufficient available` / `min notional` / `leverage too high`）。

### [S2.4] 订单状态机（`store.py` 状态 + `engine.py` 生命周期）

`placed → open → partially_filled/filled | cancelled | rejected`；触发单 `untriggered → triggered → filled/cancelled`。`tif`：GTC / IOC / FOK / PO(POC)。

### [S2.5] 撮合引擎（`gate_bot/paper/engine.py`）

| 类型 | 触发 | 成交价 |
|---|---|---|
| `limit` | 买单：卖一 ≤ 限价；卖单：买一 ≥ 限价 | **买→ask，卖→bid** |
| `market`（price=0） | 立即 | 同上 |
| `stop_entry`/TP-SL 触发单 | 触及 trigger_price（latest/mark/index + rule 1/2） | 转限价/市价 |

### [S2.6] 保证金与强平（`gate_bot/paper/risk.py`）

初始保证金 = 名义(含 quanto)/杠杆；维持保证金率（默认 0.5%）反推强平价；mark/last 触及即强制平仓（role=liquidation，单条 fill）。`liquidation_price = entry × (1 ∓ 1/lev ± mmr)`。

### [S2.7] 盈亏与资金费率

- 未实现：`Σ(last - entry) × size × quanto`，实时写 pnl_snapshot
- 已实现：平仓按成交价 vs 均价 × quanto，入 balance
- 手续费：taker/maker 分开，含 quanto
- 资金费率：每 8h 边界取真实费率结算（`amount = -rate × |size| × quanto × price`），入 balance

### [S2.8] 接入与配置

```yaml
env: paper
paper:
  feed_exchange: gate
  initial_capital: 10000
  leverage: 20
  fee_rate: 0.0005
  maker_fee_rate: 0.0
  funding_enabled: true
  position_mode: single
  margin_mode: isolated
  price_band_pct: 5.0
  maintenance_margin_rate: 0.005
  trigger_price_type: latest
```

命令：`python -m gate_bot paper-run --bot <id>`（独立进程 + pid 锁 + 撮合 tick 线程）。

### [S2.9] 错误码与返回结构对齐

`place_order`/`cancel_*`/`get_*` 返回 Gate 字段名（含 `id`）；失败抛 `ExchangeError` 且 message 含交易所风格原因。

## [S3] Out of Scope

- 界面/仪表盘；多 paper bot 组合视图；跨所套利
- 额外滑点模型（只按盘口点差）；现货/期权
- 部分成交撮合、冰山单、做市商排队
- 自动减仓 ADL、保险基金；模拟盘与实盘资金互通

## Tasks

- [x] T1: PaperExchange adapter — 行情委托绑定所、交易/账户走本地库、接口全覆盖（covers: S2.1）
- [x] T2: paper_account.db 八表与读写层（covers: S2.2）
- [x] T3: 合约精度校验 — tick/lot/min_notional/价格带/杠杆上限（covers: S2.3; S2.9）
- [x] T4: 订单状态机 — GTC/IOC/FOK/PO/POC、触发单生命周期、id 字段（covers: S2.4; S2.9）
- [x] T5: 撮合引擎 — limit/market/stop_entry/TP-SL，盘口价成交（covers: S2.5）
- [x] T6: 保证金与强平引擎（covers: S2.6）
- [x] T7: 盈亏计算 — 未实现估值、已实现入账、手续费、quanto（covers: S2.7）
- [x] T8: 资金费率结算 — 8h 真实费率（covers: S2.7）
- [x] T9: paper-run 进程与配置（covers: S2.8）
- [x] T10: 测试 — 精度/状态机/撮合/强平/盈亏/费率/Executor 契约（covers: S2.3–S2.7）
- [x] T11: 文档 — AGENTS.md（covers: S2.8）
