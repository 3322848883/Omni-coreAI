# gate-signal-bot

Gate.io 策略 JSON 信号下单机器人：AI/策略把交易意图 JSON 写入指定文件夹，机器人自动识别并直接下单。

- **输入**：`inbox/<bot_id>/*.json`（策略意图层）
- **多机器人**：子目录 = 机器人；`config/bots/<bot_id>.yaml` 配密钥/白名单/风控
- **环境**：实盘 `live` + 模拟盘 `testnet` 双模式，密钥不混用
- **持仓模式**：按 API Key 自动识别 `single / dual / dual_long_short`
- **无 dry-run**：合法信号直接下单；失败文件归档到 `archive/failed/`

## 目录

```text
gate-signal-bot/
├── config/bots/<bot_id>.yaml   # 机器人配置（_example.yaml 为样例）
├── inbox/<bot_id>/*.json       # AI 投递信号
├── archive/done/<bot_id>/      # 成功（+ *.result.json）
├── archive/failed/<bot_id>/    # 失败（+ *.error.json）
├── gate_bot/                   # 核心库 + CLI
├── tests/
└── docs/compose/spec/          # 设计规格
```

## 安装

```bash
cd gate-signal-bot
pip install -r requirements.txt   # pyyaml

# 密钥（推荐环境变量，不要写进 yaml）
# 实盘
setx GATE_API_KEY "..."
setx GATE_API_SECRET "..."
# 模拟盘
setx GATE_TESTNET_API_KEY "..."
setx GATE_TESTNET_API_SECRET "..."
```

复制 `config/bots/_example.yaml` → `config/bots/alpha.yaml`，改 `bot_id` / `env` / 白名单。

## 运行

```bash
python -m gate_bot status              # 看 bots 与 inbox 积压
python -m gate_bot once --bot alpha    # 扫一轮后退出（可挂计划任务）
python -m gate_bot run                 # 常驻守护，全部 enabled bots
python -m gate_bot run --bot alpha     # 只跑一个
python -m gate_bot process path\to\sig.json --bot alpha
```

工作目录请在项目根（或加 `--root`）。

## 信号 JSON

### 单意图

```json
{
  "action": "open_long",
  "symbol": "BTC_USDT",
  "size_usd": 100,
  "type": "market",
  "leverage": 5,
  "tp": 75000,
  "sl": 72000,
  "label": "drive",
  "meta": {"strategy": "deepseek-v1", "confidence": 85}
}
```

### 多意图 / 多腿

```json
{
  "orders": [
    {"action": "open_long", "symbol": "BTC_USDT", "size_usd": 50, "type": "limit", "price": 70000},
    {"action": "open_long", "symbol": "ETH_USDT", "size_usd": 50, "type": "limit", "price": 3200, "tp": 3400, "sl": 3100}
  ]
}
```

### 网格

```json
{
  "action": "grid",
  "symbol": "BTC_USDT",
  "side": "long",
  "levels": [
    {"price": 70000, "size_usd": 50},
    {"price": 69500, "size_usd": 50},
    {"price": 69000, "size_usd": 50}
  ],
  "type": "limit",
  "tp": 72000,
  "sl": 68000
}
```

### action 一览

| action | 说明 |
|--------|------|
| `open_long` / `open_short` | 开仓；`size`（张）/ `size_usd`（名义 U）/ `size_pct`（可用余额比例）/ `margin_pct`（保证金比例×杠杆） |
| `close` | 平仓；dual 模式需 `side` |
| `close_all` | 市价全平（可省 symbol） |
| `cancel_all` / `cancel_price_all` / `cancel_trail_all` | 撤普通挂单 / 计划委托 / 追踪止损 |
| `hold` | 无操作，直接归档 |
| `grid` | 网格多档限价开仓 |
| `trail` | 追踪止损（Gate `autoorder/v1/trail/create`） |

### 资产比例仓位

```json
{"action": "open_long", "symbol": "BTC_USDT", "size_pct": 0.1, "type": "market"}
```

- `size_pct`：名义价值 = 可用余额 × 比例（0.1 = 10%）
- `margin_pct`：保证金 = 可用余额 × 比例，名义 = 保证金 × `leverage`

### 追踪止损 trail

```json
{
  "action": "trail",
  "symbol": "BTC_USDT",
  "amount": -1,
  "price_offset": "0.5%",
  "activation_price": "0"
}
```

- `amount`：张数，正=买入（平空/做多腿），负=卖出
- `price_offset`：回撤比例或价距（`0.5` 或 `0.5%`）
- `activation_price`：激活价，`0` 表示立即生效

### 订单类型 `type`

| type | 行为 |
|------|------|
| `market` | 市价（TIF=ioc） |
| `limit` | 限价 GTC |
| `post_only` | 仅 Maker |
| `ioc` | 立即成交剩余撤销 |
| `fok` | 全部成交或取消 |

### 止盈止损

- `tp` / `sl`：开仓成功后自动挂 **平仓触发单**（禁止 market，强制 limit）
- `trigger_price_type`: `latest` | `mark` | `index`
- 规则自动推导：多头止盈 rule=1、止损 rule=2；空头对调
- `tp_limit_price` / `sl_limit_price` 可显式指定；缺省按触发价 ±0.1%

### 张数换算

```text
contracts = floor( size_usd / (price * quanto_multiplier) )
```

`price`：限价用委托价，市价用最新价。不足 1 张会失败归档，不会下 0 张。

## 安全约定

1. 启动每单打印 **实盘/模拟盘横幅**，密钥变量名明确。
2. `max_notional_usd` 超限拒单；`symbols` 白名单外拒单。
3. 网络超时 **不自动重试**（防重复下单），AI 下轮可重发。
4. 同文件顺序执行；部分成功不回滚，`error.json` 列出 partials。

## 与 quick_order.py 关系

签名、TIF 映射、触发单 body（`{initial, trigger}`）、持仓模式识别均对齐 `pa-data-source-v2.11/quick_order.py`。本项目专注「文件夹信号 → 自动下单」，不采集行情。
