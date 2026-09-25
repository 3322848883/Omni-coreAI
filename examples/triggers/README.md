# L5 事件触发示例

## A. 定时触发（5 分钟策略）

```yaml
# config/bots/scalp5.yaml
strategist:
  interval_sec: 300
  timeframe: 5m
  event_on_kline_close: true
  event_timeframe: 5m
```

## B. 10 分钟 + 只看收盘

```yaml
strategist:
  interval_sec: 600
  timeframe: 10m
  event_on_kline_close: true
  event_timeframe: 10m
```

## C. 条件触发（EMA 金叉 / 突破 / 波动）

```yaml
strategist:
  interval_sec: 3600          # 几乎不靠定时
  event_on_kline_close: false
  event_timeframe: 5m
  check_interval_sec: 1
  conditions:
    - type: ema_cross
      symbol: BTC_USDT
      fast: 9
      slow: 21
      dir: up
      cooldown_sec: 120
    - type: price_break
      symbol: ETH_USDT
      lookback: 20
      side: high
      cooldown_sec: 60
    - type: atr_spike
      symbol: BTC_USDT
      period: 14
      mult: 1.5
      cooldown_sec: 300
    - type: rsi
      symbol: BTC_USDT
      period: 14
      op: gt
      level: 70
      cooldown_sec: 180
    - type: macd_cross
      symbol: ETH_USDT
      dir: any
    - type: boll_break
      symbol: BTC_USDT
      side: upper
    - type: volume_spike
      symbol: BTC_USDT
      mult: 2
```

## D. 组合条件（全满足才触发）

```yaml
conditions:
  - type: all
    cooldown_sec: 300
    children:
      - {type: rsi, symbol: BTC_USDT, period: 14, op: gt, level: 55}
      - {type: macd_cross, symbol: BTC_USDT, dir: up}
```

## E. 混合：定时兜底 + 条件加急

```yaml
interval_sec: 300
event_on_kline_close: true
conditions:
  - {type: price_break, symbol: BTC_USDT, lookback: 20, side: high, cooldown_sec: 30}
```
→ 突破瞬间立刻跑一轮，否则每 5 分钟 / 每根 5m 收盘跑。
