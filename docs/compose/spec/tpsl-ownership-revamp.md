---
feature: tpsl-ownership-revamp
status: designed
updated: 2026-09-24
branch: master
commits: # 交付后填
---

# 止盈止损挂撤与订单归属改造计划

## [S1] Problem（核实结论）

| # | 现状（已实测） | 用户目标 | 为什么现在不对 |
|---|----------------|----------|----------------|
| 1 | TP/SL 条件单触发后挂 **限价**（如 trigger 100 → limit 99.9，仅 0.1%） | **触价即市价成交** | Gate `price-trigger create` 支持 `price=0` 表示市价；我们代码写死限价并 `raise market forbidden` → **跳空不成交** |
| 2 | `replace` **先撤后挂**：`cancel_all_orders` + `cancel_all_price_orders` 再下新单 | **先挂后撤** | 保护真空：撤掉旧 SL 后下单失败 → 裸仓 |
| 3 | `cancel_all_price_orders(contract)` / `cancel_all_orders(contract)` 按 **整币种** 全撤 | **策略只管自己的单** | `t-trend-a-sl` 与 `t-meanrev-b-sl` 同 symbol 时会互删（实测 text 前缀已有 `t-{label}-`，但撤单不按前缀过滤） |

证据：
- `_place_trigger`：`initial.price = limit`，`tif=gtc`；`close trigger market forbidden`
- `sizing.default_trigger_limit_price`：仅 0.1% 价差
- `_apply_replace`：先 `cancel_all_*` 再执行 intent
- `gate_client.cancel_all_price_orders(contract)`：无 label/text 过滤
- CLI：`price-trigger create --price` **「0 for market」**

## [S2] Design

### A. 默认 TP/SL = 条件单 + **市价**成交（触价即出）

| 项 | 现 | 改为 |
|----|----|------|
| 触发 | price_order trigger | 不变（触价语义） |
| 触发后 initial | 限价 99.9 / 100.1 | **`price: "0"` 市价**（或 `tif: ioc` 市价） |
| `tp_type`/`sl_type` | 默认 limit | 默认 **`market`**（仍允许显式 limit） |
| `sl_mode` | trigger / limit_order | 默认仍 **trigger**；`limit_order` 保留为「限价挂单」选项 |

规则：
- `sl_mode: trigger` + `sl_type: market` → **触价市价平仓**（保证出场）
- `sl_mode: trigger` + `sl_type: limit` → 旧限价语义（可配）
- 移除「trigger 禁止 market」限制（Gate 支持 `price=0`）
- 归属 text：`t-{bot|label}-sl` 不变，便于 B/C

验收：mock/联调下 body `initial.price == "0"`；testnet 跳价场景能成交（可选 live 不做）。

### B. 先挂后撤（TP/SL 管理顺序）

**只对「自己的」TP/SL/入场挂单** 操作：

```text
新计划 open_* with sl:
  1) place 入场
  2) place 新 TP/SL（保护先建立）
  3) 确认三腿
  4) cancel 本策略旧 TP/SL（同 symbol、同 label 前缀、非本单 id）
```

`replace` 语义改为：
- `replace: none` — 不动旧单
- `replace: symbol` / `all` — **先挂新保护，再撤本策略旧单**；撤单范围见 C

失败策略：
- 新 SL 未确认 → **不撤旧 SL**（保命优先）
- 新单失败 → 保留旧保护

### C. 订单归属：策略只管自己的

**归属键**：`text` 前缀 `t-{label}-`（已有）+ 可选 `meta.strategy` 写入 `text` 第二段，如 `t-{label}-{strategy}-sl`。

| 操作 | 现 | 改为 |
|------|----|------|
| list | 全 symbol | `list_price_orders(contract)` 后 **过滤 `text` startswith `t-{label}`** |
| cancel | `cancel_all_price_orders(contract)` | 只 `cancel_price_order(id)` **本前缀命中的 id** |
| cancel_all / cancel_price_all action | 全撤 | 默认只撤 **本 bot** 的；可选 `scope: all` 显式全撤 |
| replace | 全撤 | 只撤本 bot 旧单 |

配置：
```yaml
label_prefix: ta          # 归属前缀
order_scope: own          # own（默认）| all
```

多策略共账户：`ta` 永不碰 `mb`/`bc` 的单。

## [S3] Out of Scope

- 部分成交后 SL 数量动态对齐（另项）
- 部分成交 TWAP
- 跨 bot 资金预算池
- trail 追踪单

## Tasks

- [x] T1: `_place_trigger` 默认市价 initial（`price=0`）+ `tp_type/sl_type` 默认 market；允许 market close — acceptance: 单测 body.price=="0"；旧 limit 仍可配 (covers: S2.A)
- [x] T2: `default_trigger_limit_price` 仅在显式 limit 时使用；文档说明 gap 风险 — acceptance: market 路径不调用 (covers: S2.A)
- [x] T3: 归属过滤 helper（`owned_price_orders` / `cancel_owned_price_orders`）— acceptance: 只撤 `t-{label}` 前缀 (covers: S2.C)
- [x] T4: `_apply_replace` 改为先挂后撤 + 只撤 owned；`cancel_*` action 默认 own — acceptance: 顺序与范围单测 (covers: S2.B, S2.C; depends: T3)
- [x] T5: `_open` 流程：入场 → 新 TP/SL → 确认 → 撤旧 owned — acceptance: 新 SL 失败不撤旧 (covers: S2.B; depends: T1, T4)
- [x] T6: testnet 实测：市价触发 SL、双策略同 symbol 互不撤单、先挂后撤 — acceptance: 脚本 PASS (covers: S2; depends: T1–T5)
- [x] T7: README/templates + 全量 unittest — acceptance: 124 全绿；prod_e2e 23/23 (covers: S2)
