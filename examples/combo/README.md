# 多机器人组合（同账户 / 分账户）

## 组合 A：趋势 + 网格 + 突破（共用 testnet 账户）

```text
single-trend  label=st  30U  strict  vergex_default
grid-bot      label=gb  90U  free    multi_meanrev
breakout-bot  label=bc  50U  free    multi_breakout
```

| 隔离 | 机制 |
|------|------|
| 信号 | 各自 `inbox/<bot_id>/` |
| 订单 | `label_prefix` + `order_scope: own` |
| 风控 | 各自 `max_notional_usd` |
| 日志 | `logs/trades/<bot_id>.jsonl` |
| 账户总敞口 | `account_risk.max_total_notional_usd`（如 200） |

```powershell
.venv\Scripts\python.exe -m omnialpha plan-loop --bot single-trend
.venv\Scripts\python.exe -m omnialpha plan-loop --bot grid-bot
.venv\Scripts\python.exe -m omnialpha plan-loop --bot breakout-bot
# 另开执行
.venv\Scripts\python.exe -m omnialpha run   # 扫全部 enabled bot
```

## 组合 B：分账户（更安全）

```text
bot-a  api_key_env: GATE_TESTNET_API_KEY   # 账户 1
bot-b  api_key_env: GATE_API_KEY           # 账户 2（实盘）
```

## 组合 C：同策略多账户 / 同账户多周期

| 场景 | 做法 |
|------|------|
| 同策略 2 账户 | 两份 yaml，`prompt_file` 相同，密钥不同 |
| 同策略 5m+1h | 两份 yaml，`timeframe` 不同，label 不同 |
| 一策略只做 BTC | `symbols: [BTC_USDT]` |

**原则**：一个 bot = 一套边界（品种/额度/触发/人格）；账户资金可共享，订单与风控不共享。
