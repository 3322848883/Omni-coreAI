# L6 机器人类型示例

## 1. 普通单订单机器人（趋势 / 一笔一仓）

```yaml
# config/bots/single-trend.yaml
bot_id: single-trend
env: testnet
api_key_env: GATE_TESTNET_API_KEY
api_secret_env: GATE_TESTNET_API_SECRET
symbols: [BTC_USDT]
max_notional_usd: 30
label_prefix: st
position_policy: strict      # 一账户一策略
require_sl: true
account_risk: {max_leverage: 10, max_total_notional_usd: 30}

strategist:
  prompt_file: prompts/vergex_default.md
  interval_sec: 300
  timeframe: 15m
  risk: {min_confidence: 0.75, max_chips: 1, max_notional_usd: 30}
```

信号：`examples/signals/01-open-long-tpsl.json`

## 2. 网格机器人（多单挂在多价位）

```yaml
# config/bots/grid-bot.yaml
bot_id: grid-bot
symbols: [BTC_USDT]
max_notional_usd: 90          # 3 档 × 30
label_prefix: gb
position_policy: free
require_sl: true              # grid 建议带总止损

strategist:
  prompt_file: prompts/multi_meanrev.md
  interval_sec: 600
  timeframe: 10m
  conditions:
    - {type: rsi, symbol: BTC_USDT, period: 14, op: lt, level: 35, cooldown_sec: 600}
```

信号：`examples/signals/06-grid-long.json` 或 `07-orders-multi.json`

## 3. 突破机器人

```yaml
# config/bots/breakout-bot.yaml
position_policy: free
strategist:
  prompt_file: prompts/multi_breakout.md
  event_on_kline_close: false
  interval_sec: 3600
  conditions:
    - {type: price_break, symbol: BTC_USDT, lookback: 20, side: high, cooldown_sec: 60}
    - {type: atr_spike, symbol: BTC_USDT, mult: 1.4, cooldown_sec: 300}
```

信号：`examples/signals/05-breakout-entry.json`

## 对比

| | 单订单 | 网格 | 突破 |
|--|--------|------|------|
| 同时挂几笔 | 1 | 多档 | 1（触发后） |
| 典型 action | open_long | grid / orders[] | stop_entry_* |
| 触发 | 定时/K线 | RSI 超卖等 | price_break / atr |
| 风控重点 | 单笔 notional | 总敞口 | 突破假信号 |
