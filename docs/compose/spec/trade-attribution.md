---
feature: trade-attribution
status: designed
updated: 2026-10-07
branch: master
commits:
---

# 交易归属：画像只统计本 bot 自己的单

## Report

## [S1] Problem

`strategist-context-integrity` 给 live bot 接通了画像（`state/exchange_pnl.json`），但口径是
**「该账户上该 symbol 的所有平仓」**，不区分来源。用户指出「统计不是全部自己做的交易的话，
画像不真实，不能真实统计出策略效果」——实测证实。

**实测证据**（2026-10-07，`position_close` 全量，`BTC_USDT` 41 条）：

| 类别 | 条数 | 说明 |
|---|---|---|
| `ao-<id>` 且 id 命中本地条件单 | 8 | **可精确归属** |
| `ao-<id>` 但 id 不在本地 | 20 | 全部在 bot 启动前（09-07~09-30） |
| `api` / `-` | 10 | **无标记，无法归属**（启动后 5 条） |
| 别的 bot（`app`/`t-drive`/`t-l-close-*`） | 3 | 全部在启动前 |

三处失真：

1. **画像的 `worst` 不是本 bot 的** —— `-22.47465924` 对应 `t-l-close-f5ce3436-0`（09-07）。
2. **混入别的 bot** —— `app`、`t-drive` 各 1 条。
3. **混入启动前历史** —— 首次同步 `cursor=0` 拉了全部历史。

当前那份画像（41 笔 / 42% / -25.00U）**不能代表 brooks-btc 的策略效果**。

**为什么不能只用 `position_close`**：它没有 `order_id`，`text` 被交易所改写 —— bot 下单时的
`t-brk` 在这里变成 `ao-<id>`（条件单触发）或 `api`（其余）。

**关键发现**（决定了整个方案）：`ao-<id>` 的 id **就是条件单的 id**，而条件单 id 在本地
`trades.jsonl` 里（`detail.tp_placed.id` / `sl_placed.id` / `tp_orders[].id` / `sl_orders[].id`）。
实测修正收集口径后（369 → 681 个 id），`ao-` 与本地**有 8 个交集**——之前是 0，因为我漏了
`tp_placed` 这个容器。

## [S2] Design

### S2.1 两个数据源合并

没有任何单一来源能覆盖全部，所以按**平仓的发起方式**分两路：

| 平仓方式 | 数据源 | 归属依据 | pnl 来源 |
|---|---|---|---|
| **bot 主动平仓**（`close`/`flatten`/`reduce_*`） | 本地 `trades.jsonl` | 本来就是本地发起的 | `detail.realized_pnl`（executor 已回传） |
| **SL/TP 触发平仓** | 交易所 `position_close` | `text == ao-<id>` 且 id ∈ 本地条件单 id | `position_close.pnl`（官方，含手续费与资金费） |

**为什么不用「自己算 pnl」**：实测用 `my_trades` 的 `(close_size, price, fee)` 配合开仓均价复算，
与官方 pnl 差 0.04–0.09（手续费/资金费舍入），个别样本差 0.3（跨持仓周期时开仓均价失真）。
官方 `pnl` 是权威口径，没有理由自己复算。

### S2.2 本地订单 id 索引

来源 `data/bots/<bot_id>/logs/trades.jsonl`，每次 sync 重建（实测 344 行）。

收集容器（**四个都要**，漏一个就会让对应类型的单无法归属）：

- `steps[].detail.order.id` —— 普通单
- `steps[].detail.tp_placed.id` / `sl_placed.id` —— `modify_tp_sl` 路径
- `steps[].detail.tp_orders[].id` / `sl_orders[].id` —— 开仓时随单挂的保护腿

内存集合，不落盘（可从日志重建，避免两份真源）。

### S2.3 归属判据

对 `position_close` 的每条记录：

```
归属 ⟺ text 形如 "ao-<id>" 且 <id> ∈ 本地条件单 id 集合
```

**唯一判据**。不采用 `api` + 时间/数量的模糊匹配 —— 实测 (时间 ±180s, `accum_size == |size|`)
只能匹配 1/5，且 `accum_size` 与单笔成交的 `size` 语义不同（35 vs 18），不可靠。

### S2.4 主动平仓来源

`trades.jsonl` 里 `detail.realized_pnl != null` 的步骤即一笔主动平仓，**直接采用**。

**它与 S2.3 不重叠**：主动平仓是本地发起的（有 `realized_pnl`），而 SL/TP 触发在
`position_close` 里是 `ao-<id>`（本地日志没有 `realized_pnl`，因为 executor 没参与）。
两条路径按 **`(cycle_id, 时间)`** 去重 —— 同一时刻两者都有值时以 `position_close` 为准
（它含资金费）。

### S2.5 时间窗

只统计 `time >= bot_first_run_ts`。

**时间源**：`state/first_run.json` 的 `first_run_ts`，首次 sync 时写入（文件不存在就写当前时间，
之后永不改）。

**为什么不从 `memory_journal.jsonl` 首条取**：journal 会被遗忘 GC 按 TTL 归档
（`memory/forget.py`），首条会随归档前移 —— 那会让时间窗**逐月放宽**，历史污染悄悄回流。
独立文件不受 GC 影响。

**为什么还需要时间窗**：S2.3 只能识别「本地有记录的条件单」，而启动前的历史平仓其条件单
不在日志里（实测 20 条 `ao-` 未命中全部在启动前）。时间窗把它们排除，也顺带排除
`app`/`t-drive` 那些别的 bot 的记录。

### S2.6 排除留痕

无法归属的记录（`api` / `-` / 未命中的 `ao-`）**不计入统计**，但：

- 计数写入 sync 返回值 `skipped_unattributed`
- 落 `state/exchange_pnl.json` 的 `excluded` 数组（最多 200 条，含 `text`/`pnl`/`time`）

**为什么留痕**：`api` 那部分（启动后 5 条）是 bot 的主动平仓，本应由 S2.4 覆盖 —— 若
S2.4 有遗漏，`excluded` 是唯一能发现的地方。

### S2.7 重置

现有 `exchange_pnl.json` 的 totals 是污染的（S1），**部署时删除**，由首次 sync 重建。

## [S3] Out of Scope

- **不改 symbol 白名单**：`contracts` 仍是有效粗筛（ETH 那批曾让胜率虚低 9 个百分点）。
- **不追求 100% 覆盖**：`api`/`-` 那部分如果 S2.4 也没覆盖到，就明确排除并留痕，
  不做模糊匹配 —— 宁可少算，不算错。
- **不改 persona 路径**：`persona/runner.py` 的 `record_trade` 独立。
- **不做跨 bot 归属**：同账户同 symbol 的多个 bot 之间无法区分（交易所不提供），
  时间窗只能排除「启动前」，排除不了「同期并行」。这是已知边界，写入 docstring。

## Tasks

- [ ] T1: 本地订单 id 索引 — acceptance: 四个容器（`order`/`tp_placed`/`sl_placed`/`tp_orders`/`sl_orders`）的 id 都被收集；对现有 `trades.jsonl` 收集数 ≥600；文件缺失/行损坏不抛异常 (covers: S2.2)
- [ ] T2: 归属判据 — acceptance: `ao-<id>` 且 id 命中本地集合时判为归属；`api`/`-`/未命中一律判为未归属 (covers: S2.3)
- [ ] T3: 主动平仓来源 — acceptance: 从 `trades.jsonl` 提取 `realized_pnl != null` 的步骤；与 T2 结果按 `(cycle_id, 时间)` 去重，冲突时以 `position_close` 为准 (covers: S2.4; depends: T2)
- [ ] T4: 时间窗 — acceptance: `time < first_run_ts` 的记录被排除；`first_run_ts` 落在 `state/first_run.json`，首次 sync 写入且之后不再改变（journal 归档不影响它） (covers: S2.5; depends: T2)
- [ ] T5: 排除留痕 — acceptance: `skipped_unattributed` 出现在 sync 返回值；`excluded` 数组落盘且不超过 200 条 (covers: S2.6; depends: T2)
- [ ] T6: 重置与真机验证 — acceptance: 删除服务器 `exchange_pnl.json` 后重新同步，totals 只含归属成功的记录，`worst` 不再是 -22.47；主动平仓与 SL/TP 触发两类都有样本 (covers: S2.7; depends: T1 T2 T3 T4 T5)
