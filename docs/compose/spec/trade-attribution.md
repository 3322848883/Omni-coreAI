---
feature: trade-attribution
status: delivered
updated: 2026-10-07
branch: master
commits: 00755fd..38eeabd
---

# 交易归属：画像只统计本 bot 自己的单

## Report

**What was built** — 画像不再统计「账户 + symbol 的全部平仓」，改为只统计本 bot 自己的交易。
按**平仓的发起方式**分两个来源合并：

1. **SL/TP 触发** —— 交易所 `position_close`，归属判据是 `text == ao-<id>` 且 `<id>` 命中
   本地订单 id 集合（`is_ours`）。用官方 `pnl`（含手续费与资金费）。
2. **bot 主动平仓** —— 本地 `trades.jsonl` 的 `detail.realized_pnl`（executor 回传）。

外加一道 `first_run_ts` 时间窗（排除启动前历史）与一条去重（同一笔平仓两条路径都命中时
以 `position_close` 为准）。无法归属的记录不计入，但计数进返回值、明细落 `excluded` 数组留痕。

**决定性发现**：`ao-<id>` 的 id **就是条件单 id**，而条件单 id 在本地 `trades.jsonl` 里。
之前交集为 0 是因为收集口径漏了容器 —— 条件单在 `modify_tp_sl` 路径下记在
`detail.tp_placed.id`；补上后本地 id 369 → 681、交集 0 → 8。

**Verification** —

- `python -m unittest discover -s tests` → **1975 项，1 个失败**。唯一失败是
  `test_skill_sizes.TestSkillSizes.test_every_file_within_ref_limit`（**PRE-EXISTING**）：
  扫 `skills-src/price-action-trading/data/` 下被 gitignore 的本地生成市场数据。
- `tests.test_exchange_pnl` → 45 项，OK。
- 服务器实测（`38eeabd` 部署后，删除 `exchange_pnl.json` 重建）：
  - 本地订单 id `681`；`first_run_ts=1790763364`（2026-09-30 10:16，取自 journal 首条）
  - `trades=11, wins=6, pnl=-1.90, worst=-2.17`；来源分布 `{'trigger': 8, 'active': 3}`
  - `excluded=5`（全是 `api`）；`prompt_summary='历史表现: 11笔交易, 胜率55%, 均盈亏-0.17u'`
  - **`worst` 不再是 -22.47**（那条来自另一个 bot）

**对比修复前**：41 笔 / 42% / -25.00U / worst -22.47 → **11 笔 / 55% / -1.90U / worst -2.17**。
原先的 -25U 里 92% 是别人的亏损。

**Journey log** —

1. **归属信息只在 `my_trades` 里，不在 `position_close`**：后者的 `text` 被交易所改写
   （`t-brk` → `ao-<id>` 或 `api`）且没有 `order_id`。但 `my_trades` 又没有 `pnl` ——
   所以必须两个端点合起来用。
2. **`ao-<id>` 一度看起来无法归属**：第一次算交集得 0，据此差点否掉整个方案。真因是
   收集口径漏了 `tp_placed` 容器（`modify_tp_sl` 路径），补上后交集 8。**交集为 0 时先
   怀疑自己的收集口径，别急着否定方案**。
3. **自己算 pnl 不可行**：用 `my_trades` 的 `(close_size, price, fee)` 配合开仓均价复算，
   与官方差 0.04–0.09，个别样本差 0.3（跨持仓周期时开仓均价失真）。且第一次算时把
   `close_size` 的符号乘了两次导致双重负号 —— 官方值是权威口径，没理由复算。
4. **独立评审抓到 2 个 critical**：`active` 路径绕过了 `first_run_ts`（本地日志含启动前
   记录，会被静默计入）；去重条款被我在实现时以「两条路径天然互斥」为由跳过，而互斥
   只是**今天的巧合**（`close_position` 走普通下单 → text 是 `api`），不是不变量。
   **spec 写了的要求不能在实现时凭「应该不会发生」跳过**。
5. **`excluded` 满了会永久冻结**（在 append 前判断长度）—— 它本该是「发现遗漏」的窗口，
   冻结后这个作用就没了。凡是「留痕/观测」用的环形缓冲，都要保留**最新**的。

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

- [x] T1: 本地订单 id 索引 — acceptance: 四个容器（`order`/`tp_placed`/`sl_placed`/`tp_orders`/`sl_orders`）的 id 都被收集；对现有 `trades.jsonl` 收集数 ≥600；文件缺失/行损坏不抛异常 (covers: S2.2)
- [x] T2: 归属判据 — acceptance: `ao-<id>` 且 id 命中本地集合时判为归属；`api`/`-`/未命中一律判为未归属 (covers: S2.3)
- [x] T3: 主动平仓来源 — acceptance: 从 `trades.jsonl` 提取 `realized_pnl != null` 的步骤；与 T2 结果按 `(cycle_id, 时间)` 去重，冲突时以 `position_close` 为准 (covers: S2.4; depends: T2)
- [x] T4: 时间窗 — acceptance: `time < first_run_ts` 的记录被排除；`first_run_ts` 落在 `state/first_run.json`，首次 sync 写入且之后不再改变（journal 归档不影响它） (covers: S2.5; depends: T2)
- [x] T5: 排除留痕 — acceptance: `skipped_unattributed` 出现在 sync 返回值；`excluded` 数组落盘且不超过 200 条 (covers: S2.6; depends: T2)
- [x] T6: 重置与真机验证 — acceptance: 删除服务器 `exchange_pnl.json` 后重新同步，totals 只含归属成功的记录，`worst` 不再是 -22.47；主动平仓与 SL/TP 触发两类都有样本 (covers: S2.7; depends: T1 T2 T3 T4 T5)
