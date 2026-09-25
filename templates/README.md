# gate-signal-bot 标准 JSON 模板（上线版）

AI/策略把信号写成 **一个 JSON 文件**，放入 `inbox/<bot_id>/`。

## 突破单 vs 止损（禁止用错）

| 意图 | 用这个 | 禁止 |
|------|--------|------|
| **涨破 → 买进开多**（突破进场） | **`stop_entry_long`**（别名 `buy_stop` / `stop_long`） | 禁止用 `sl` |
| **跌破 → 卖出开空**（突破进场） | **`stop_entry_short`**（别名 `sell_stop` / `stop_short`） | 禁止用 `sl` |
| 已有多头，跌到价 → 平仓止损 | `open_long` 的 **`sl`** | 禁止用 `stop_entry_*` |
| 已有空头，涨到价 → 平仓止损 | `open_short` 的 **`sl`** | 禁止用 `stop_entry_*` |

**口诀：突破进场 = `stop_entry_*`（开仓）｜止损止盈 = `sl` / `tp`（平仓）**

## action 总表

| action | 说明 | 别名 |
|--------|------|------|
| `open_long` / `open_short` | 开仓 | `add_long` / `add_short`（加仓，日志更清晰） |
| `stop_entry_long` / `stop_entry_short` | **突破入场** | `buy_stop`/`stop_long` / `sell_stop`/`stop_short` |
| `close` | 平仓 | `reduce` / `reduce_long` / `reduce_short` |
| `close_all` | 全平 | `flatten` |
| `cancel_all` / `cancel_price_all` / `cancel_trail_all` | 撤单 | |
| `hold` | 不下单 | `watch` / `skip` / 空 `action` |
| `grid` | 单向网格 | |
| `trail` | 追踪单（**需资金密码，当前搁置**） | |

## 完整模板（开多 + 止盈止损）

```json
{
  "action": "open_long",
  "symbol": "BTC_USDT",
  "size": null,
  "size_usd": 100,
  "size_pct": null,
  "margin_pct": null,
  "type": "limit",
  "price": 70000,
  "leverage": 5,
  "margin_mode": "cross",
  "tp": 73000,
  "sl": 69000,
  "tp_type": "limit",
  "sl_type": "limit",
  "tp_limit_price": 72950,
  "sl_limit_price": 68950,
  "trigger_price_type": "mark",
  "trigger_rule_tp": 1,
  "trigger_rule_sl": 2,
  "trigger_expiration": null,
  "label": "drive",
  "meta": {
    "strategy": "deepseek-v1",
    "confidence": 85,
    "timeframe": "15m",
    "reasoning": "趋势多头，回踩确认",
    "signal_id": "20260302-153000-btc-long",
    "ts": "2026-03-02T15:30:00+08:00",
    "kind": "open_with_exit"
  }
}
```

## 字段说明

| 字段 | 必填 | 说明 |
|------|------|------|
| `action` | ✅ | 见总表；空/`hold`/`watch`/`skip` = 不下单 |
| `symbol` | 开平仓必填 | `BTC_USDT` 或 `BTC` |
| `size` / `size_usd` / `size_pct` / `margin_pct` | 四选一 | 张 / 名义 U / 可用余额比例 / 保证金比例×杠杆 |
| `type` | 可省 | `market`(默认)/`limit`/`post_only`/`ioc`/`fok` |
| `price` | limit 等必填 | 委托价 |
| `tp` / `sl` | 可省 | 止盈/止损价 |
| `tp_mode` / `sl_mode` | 可省 | **`trigger`（默认）**=条件计划委托；**`limit_order`**=盘口 reduce_only **限价挂单** |
| `tp_type` / `sl_type` | 可省 | 触发后成交方式，默认 **`market`（触价市价，保证出场）**；`limit` 为触发后限价 |

### 限价单止盈止损 vs 条件单止盈止损

| mode | 实现 | 适用 |
|------|------|------|
| **`trigger`**（默认） | `price_orders` 计划委托：到触发价再下平仓单 | 真止损、触发式止盈 |
| **`limit_order`** | 普通委托：`reduce_only + tif=gtc` 限价挂在盘口 | **限价止盈**（maker）；需立刻在订单簿可见时 |

```json
{
  "action": "open_long",
  "symbol": "BTC_USDT",
  "size_usd": 100,
  "type": "limit",
  "price": 70000,
  "tp": 73000,
  "sl": 68500,
  "tp_mode": "limit_order",
  "sl_mode": "trigger",
  "meta": {"signal_id": "20260302-lim-tpsl"}
}
```

注意：`sl_mode: limit_order` 是**限价卖/买平仓单**，若价格已可成交会**立即成交**（不是“到价再触发”的止损）；真止损请用 `sl_mode: trigger`。限价开仓未成交时，`limit_order` 的 TP/SL 也需已有持仓才能挂 `reduce_only`。  
另：`limit_order` 价格须在 Gate **偏离带**内（过远报 `PRICE_TOO_DEVIATED`）；需要远止盈时优先 `trigger`。

## 加减仓 × 持仓模式
| `trigger_price` | 突破单必填 | `stop_entry_*` 的触发价 |
| `trigger_rule` | 可省 | 1=≥，2=≤；多头默认 1/2，空头对调 |
| `leverage` | 可省 | 下单前改杠杆 |
| `margin_mode` | 可省 | `cross` / `isolated` |
| `trigger_expiration` | 可省 | 过期秒数（实盘支持） |
| `side` | dual 减仓必填 | `long` / `short` |
| `levels` | grid | `[{price, size\|size_usd\|size_pct}]` |
| `tp_scope` / `sl_scope` | grid | `per_level`（每档对应，默认）\| `shared`（共用一个止盈/止损） |
| `orders` | 多腿 | 与顶层 `action` 互斥 |
| `label` | 建议 | 订单标签 `t-<label>` |
| `meta` | 建议 | `signal_id`/`reasoning`/`kind`，不参与下单 |

## 加减仓 × 持仓模式

| action | 单向 single | 双向 dual |
|--------|-------------|-----------|
| `add_long` / `add_short` | **净额对冲**；反向数量>净仓才翻仓 | 加到对应一本 |
| `reduce_long` / `reduce_short` | 卖/买平（reduce_only，不反手） | **只减该侧** |
| `reduce` | 自动认边 | 必须 `side` |
| `flatten` | 全平 | 平多+平空 |

双向 `reduce_only` 符号（官方）：**+N = 减空，−N = 减多**。  
日志：`action` 保留你写的（如 `add_long`），`detail.executed_as` 写实际执行。

## 标准 JSON 示例

### 突破买入开多

```json
{
  "action": "stop_entry_long",
  "symbol": "BTC_USDT",
  "trigger_price": 88750,
  "size_usd": 100,
  "type": "market",
  "meta": {"kind": "stop_entry", "signal_id": "20260302-001"}
}
```

### 跌破卖出开空

```json
{
  "action": "stop_entry_short",
  "symbol": "BTC_USDT",
  "trigger_price": 82000,
  "size_usd": 100,
  "type": "market",
  "meta": {"kind": "stop_entry", "signal_id": "20260302-002"}
}
```

### 加仓 / 减仓 / 全平

```json
{"action": "add_long", "symbol": "BTC_USDT", "size": 1}
{"action": "reduce_long", "symbol": "BTC_USDT", "size": 1}
{"action": "flatten", "symbol": "BTC_USDT"}
```

### 网格 / 多腿

```json
{
  "action": "grid",
  "symbol": "BTC_USDT",
  "side": "long",
  "type": "limit",
  "levels": [{"price": 70000, "size_usd": 50}, {"price": 69500, "size_usd": 50}],
  "tp": 72000,
  "sl": 67500
}
```

```json
{"orders": [
  {"action": "open_long", "symbol": "BTC_USDT", "size_usd": 50, "type": "limit", "price": 70000},
  {"action": "open_short", "symbol": "ETH_USDT", "size_usd": 50, "type": "limit", "price": 3200}
]}
```

### trail 追踪单（官方 CLI 语义，当前搁置）

```json
{
  "action": "trail",
  "symbol": "BTC_USDT",
  "amount": -1,
  "price_offset": "2%",
  "activation_price": "70000",
  "meta": {
    "kind": "trail",
    "note": "追踪止盈=平仓：多仓 amount 负、空仓正；price_offset 可用 0.1 或 2%；activation 0=立即"
  }
}
```

| 字段 | 说明 |
|------|------|
| `amount` | 张数；**正=买 / 负=卖**。追踪止盈是平仓：多仓填负、空仓填正 |
| `price_offset` | 回调幅度：`0.1`（价距）或 `2%`（比例） |
| `activation_price` | 可选；省略或 `0` = 立即激活 |

CLI 对照：`gate-cli cex futures trail create --contract BTC_USDT --amount -1 --price-offset 2% --activation-price 70000`  
配套：`cancel_trail_all` ↔ `trail stop-all`。

## 新方案替换旧方案（`replace`，防堆积）

执行新信号前**先撤掉旧挂单/计划委托**，避免无限堆积。

| `replace` | 行为 |
|-----------|------|
| 省略 / `none` | 不撤，直接执行 |
| **`true` / `symbol`** | 先撤**本信号涉及合约**的普通挂单 + 计划委托，再执行 |
| **`all`** | 先撤 bot 白名单内**全部**挂单 + 计划委托，再执行 |

```json
{
  "replace": true,
  "action": "open_long",
  "symbol": "BTC_USDT",
  "size_usd": 100,
  "type": "limit",
  "price": 70000,
  "meta": {"signal_id": "plan-2", "note": "取代 plan-1"}
}
```

- 顶层 `replace` 覆盖子单；单腿也可写 `"replace": "symbol"`
- 日志有 `replace_cancel` 步骤（先撤后下）
- **改单 = 撤旧 + 下新**；连续网格请一次 `orders[]` 下完

## 有仓管控（`position_policy`，bot 级配置）

规则在 **`config/bots/<id>.yaml`**，不在 JSON 里；**每个 bot 独立**，多策略互不影响。

| policy | 适用 | 有仓时（同币种） |
|--------|------|------------------|
| **`strict`**（默认） | 一账户一策略 | 只收管理单；拒新进场 |
| `manage_only` | 同 strict | 预留 |
| **`free`** | 多策略共账户 | 不拦进场，只靠 `replace` 防堆积 |

**strict / manage_only 有仓时：**

| 动作 | 放行？ |
|------|--------|
| `add_long` / `add_short`（同向） | ✅ 加仓 |
| `reduce_*` / `close` / `flatten` | ✅ 减仓/清仓 |
| `cancel_*` / `hold` / `watch` / `skip` / `trail` | ✅ |
| 调 TP/SL（计划委托，建议带 `replace: "symbol"`） | ✅ 只换触发单 |
| `open_*` / `stop_entry_*` / `grid` | ❌ 新方案 |
| 反向 `add_*` | ❌（strict） |

拒单写入 `failed/`，`error.json` 含：

- `POSITION_EXISTS: <symbol> already has position; use add_long/reduce...`
- `POSITION_POLICY_STRICT: opposite add not allowed...`

```yaml
# config/bots/trend.yaml — 独占账户
position_policy: strict
default_replace: none

# config/bots/grid.yaml — 与其它策略共账户
position_policy: free
default_replace: symbol
```

**口诀：无仓收新方案；有仓只收管理单（加/减/平/调TP-SL）；多策略共账户用 `free` + `replace`。**

## 投递约定

- 路径：`inbox/<bot_id>/*.json`，UTF-8
- 顶层 `action` **或** `orders[]`，不能同时
- 空 `action` / `hold` / `watch` / `skip` = 不下单，归档 done
- 成功 → `archive/done/` + `result.json`；失败 → `archive/failed/` + `error.json`
- 动作/字段名一律英文；`meta.reasoning` 可中文
- 建议文件名：`YYYYMMDD-HHMMSS-<uuid>.json`；`meta.signal_id` 便于对账

## 系统自动处理（JSON 不用写）

密钥与 live/testnet、持仓模式、`size_usd`→张数、TP/SL 规则与方向、`BTC`→`BTC_USDT`。

## 已知限制（上线接受）

| 项 | 说明 |
|----|------|
| **`trail` 追踪单** | 需 API 资金密码 / 追踪委托权限；测试网 `InvalidRequest`，实盘 `Invalid Fund password`。**当前搁置** |
| 市价单极端点差 | `MARKET_PRICE_TOO_DEVIATED` 时自动改买/卖一价 GTC 一次 |
| 有挂单时改保证金 | `ORDER_PENDING`，需先撤单 |
| 单向反向 `add_*` | 净额对冲，数量大于净仓才翻仓 |

## 模板文件

| 文件 | 用途 |
|------|------|
| `STOP-ENTRY-vs-STOP-LOSS.md` | 突破单 vs 止损对照 |
| `standard-signal.json` | 全字段开仓模板 |
| `stop-entry-long.json` / `stop-entry-short.json` | 突破单 |
| `field-dictionary.json` | 字段字典 |
| `examples/` | 其它场景样例 |
