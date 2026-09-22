# 突破单 vs 止损 — 禁止用错

## 一张表

| 你的意图 | 正确写法 | 别名 | 触发后 | **禁止** |
|----------|----------|------|--------|----------|
| **涨破某价 → 买进开多**（突破进场） | **`action: "stop_entry_long"`** | `buy_stop` / `stop_long` | **开多仓** | 禁止用 `sl` |
| **跌破某价 → 卖出开空**（突破进场） | **`action: "stop_entry_short"`** | `sell_stop` / `stop_short` | **开空仓** | 禁止用 `sl` |
| 已有多头，跌到某价 → **平仓止损** | `open_long` 的 **`sl`** 字段 | — | **平多** | 禁止用 `stop_entry_*` |
| 已有空头，涨到某价 → **平仓止损** | `open_short` 的 **`sl`** 字段 | — | **平空** | 禁止用 `stop_entry_*` |
| 已有仓，到价止盈 | `tp` 字段 | — | **平仓** | — |

## 口诀

```text
突破进场  →  stop_entry_long / stop_entry_short   （开仓）
止损止盈  →  sl / tp                               （平仓）
```

> **Stop-Entry（突破单）≠ Stop-Loss（止损单）**  
> 中文「停损单」一般指止损，本项目**不用「停损单」三字**。

## 标准 JSON

### 突破买入开多

```json
{
  "action": "stop_entry_long",
  "symbol": "BTC_USDT",
  "trigger_price": 88750,
  "size_usd": 100,
  "type": "market",
  "meta": {
    "kind": "stop_entry",
    "signal_id": "20260302-stop-entry-long",
    "reasoning": "breakout entry, NOT stop-loss"
  }
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
  "meta": {
    "kind": "stop_entry",
    "signal_id": "20260302-stop-entry-short"
  }
}
```

### 开仓 + 止损/止盈（出场保护，不是突破）

```json
{
  "action": "open_long",
  "symbol": "BTC_USDT",
  "size_usd": 100,
  "type": "market",
  "tp": 75000,
  "sl": 72000,
  "meta": {
    "kind": "position_exit_protection",
    "note": "sl/tp 是平仓保护；突破进场请用 stop_entry_long/short"
  }
}
```

## stop_entry_* 字段

| 字段 | 必填 | 说明 |
|------|------|------|
| `action` | ✅ | 推荐全称 `stop_entry_long` / `stop_entry_short` |
| `symbol` | ✅ | 合约 |
| `trigger_price` | ✅ | 突破/跌破触发价 |
| 仓位 | ✅ 四选一 | `size` / `size_usd` / `size_pct` / `margin_pct` |
| `type` | 可省 | 触发后成交：`market`(默认)/`limit`/`post_only`/`ioc`/`fok` |
| `price` | limit 等必填 | 触发后委托价 |
| `trigger_rule` | 可省 | long 默认 `1`(≥)；short 默认 `2`(≤) |
| `trigger_price_type` | 可省 | `latest`(默认)/`mark`/`index` |
| `meta.kind` | 建议 | 固定 `"stop_entry"`，审计好区分 |

## 相关模板

| 文件 | 用途 |
|------|------|
| `stop-entry-long.json` | 突破买入开多 |
| `stop-entry-short.json` | 跌破卖出开空 |
| `standard-signal.json` | 全字段开仓 + tp/sl |
