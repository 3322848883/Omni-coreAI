# gate-io monorepo（gate-signal-bot + pa-data-source）

一个仓库两个组件，**文件级接口**（只读 `kline.db`），进程分离、互不 import。

| 目录 | 角色 |
|------|------|
| `gate_bot/` | 策略 JSON / LLM Plan → 风控 → Gate 下单（执行） |
| `pa-data-source/` | 行情/账户采集管道（WS+REST→SQLite），**零判断** |
| `contracts/` | 两组件唯一接缝：`kline.db` schema v1 |

```text
pa-data-source（独立进程/watchdog）
    └─ write → data/kline.db / kline_testnet.db
                    │ 只读
                    ▼
gate_bot strategist：快照 → LLM Plan → 风控 → inbox → executor → Gate → trades 日志
```

长期稳定约定见 **`contracts/KLINE_SCHEMA.md`**；bot 读不到合规库时自动 REST，不硬读。

### 跑数据管道（可选，独立进程）

```bash
cd pa-data-source
# 依赖：pyyaml + websocket-client（见 pa-data-source/requirements.txt）
.venv\Scripts\python.exe watchdog.py          # kline live/testnet + aux 整体守护
.venv\Scripts\python.exe status.py --json     # 先查后用
.venv\Scripts\python.exe query_kline.py -s BTC_USDT -i 15m -n 20
```

- 密钥：实盘 `GATE_API_KEY` / 测试网 `GATE_TESTNET_API_KEY`（环境变量，不落盘）
- 数据落 `pa-data-source/data/`；**bot 只读该目录**
- 备用源 `gate-cli.exe` 不进 git，需要时放到 `pa-data-source/` 下（aux 舆情类要用）
- bot 侧无需启动 pa：缺库自动 `rest_only`

---

## gate-signal-bot

Gate.io 策略 JSON 信号下单机器人：AI/策略把交易意图 JSON 写入指定文件夹，机器人自动识别并直接下单。

- **输入**：`inbox/<bot_id>/*.json`（策略意图层）
- **多机器人**：子目录 = 机器人；`config/bots/<bot_id>.yaml` 配密钥/白名单/风控
- **环境**：实盘 `live` + 模拟盘 `testnet` 双模式，密钥不混用
- **持仓模式**：按 API Key 自动识别 `single / dual / dual_long_short`
- **无 dry-run**：合法信号直接下单；失败文件归档到 `archive/failed/`
- **空仓 flatten/close_all**：视为 no-op 成功（`POSITION_EMPTY` 不算失败）

## LLM 策略层（strategist）

AI 生成方案 → 程序风控 → 写 `inbox` → 现有执行器下单（VergeX 式多品种 chips）。

```bash
# 环境变量（密钥不落盘）
# $env:OPENAI_BASE_URL = "http://<host>:<port>/v1"
# $env:OPENAI_API_KEY  = "<key>"

# 单轮：采集快照 → LLM → 风控 → 写 inbox
.venv\Scripts\python.exe -m gate_bot plan --bot alpha

# 常驻：interval_sec 定时 + K线收盘事件
.venv\Scripts\python.exe -m gate_bot plan-loop --bot alpha
# 另开进程执行
.venv\Scripts\python.exe -m gate_bot run --bot alpha
```

`config/bots/<id>.yaml` 片段（**风控属于策略，每 bot 独立**）：

```yaml
strategist:
  enabled: true
  interval_sec: 300
  timeframe: 15m
  event_on_kline_close: true
  symbols: [BTC_USDT, ETH_USDT]
  prompt_file: prompts/vergex_default.md
  candles: 120
  market:
    mode: hybrid          # hybrid | rest_only | local_only
    pa_data_root: pa-data-source/data
    stale_factor: 2.0
    refresh: [ticker, stats, orderbook]   # ticker 含 funding/mark/index/24h
  risk:
    min_confidence: 0.75
    max_notional_usd: 50
    max_chips: 3
    allow_actions: [open_long, open_short, reduce_long, reduce_short, close, hold, stop_entry_long, stop_entry_short]
  llm:
    base_url_env: OPENAI_BASE_URL
    api_key_env: OPENAI_API_KEY
    model: global:deepseek-v4.1-flash   # OpenAI 兼容任意模型 id
    temperature: 0.1
    timeout_sec: 90
    max_tokens: 4096        # reasoning 模型需较大额度
```

联调样例：`config/bots/_llm_test.example.yaml`（复制为 `llm-test.yaml`）。

LLM 输出 **Plan**（chips 数组），经风控后转 `orders[]` 写入 inbox；`hold`/低置信不产生下单。  
契约详见 `docs/compose/spec/llm-strategist.md` 与 `prompts/vergex_default.md`。

### 数据来源（market hybrid + P1）

快照按数据特性分源，**不改 pa-data-source**（只读它的库）：

| 字段 | 来源 |
|------|------|
| `candles` + `ema20`/`atr14` | 默认 REST；`hybrid` 读 `kline.db`（过期/schema 不符/缺库自动 REST） |
| `ema50` / `rsi14` | 本地计算 |
| `last`、持仓、余额 | **强制实时 API**（账户失败则本轮 abort，不写 inbox） |
| `ticker` | 实时：funding_rate / mark_price / index_price / 24h 高低量 |
| `stats` | 实时：OI、多空比（lsr_taker/account）、爆仓量 |
| `orderbook` | 实时：买卖各 5 档 |
| 下单报价 | executor 实时 `get_last_price`（不用库价） |

`market.refresh` 可裁剪：`[ticker]` 最省；默认 `[ticker, stats, orderbook]`。

`mode`：

- `rest_only`（默认）— 每轮 REST，零外部依赖
- `hybrid` — 本地优先，末根年龄 > `stale_factor × 周期` 则回退 REST
- `local_only` — 只读本地库，不打 K 线 REST

路径：`GATE_BOT_PA_DATA` 环境变量 > `market.pa_data_root` > 默认 `pa-data-source/data`（monorepo 内）。  
`env: testnet` 自动读 `kline_testnet.db`，与实盘 `kline.db` 隔离。  
可选 `health_url: http://127.0.0.1:18080/health`，非 200 时视本地库不可信。  
契约见 **`contracts/KLINE_SCHEMA.md`** 与 `docs/compose/spec/market-data-hybrid.md`。

## 交易日志（trades）

每笔执行追加写入 **`logs/trades/<bot_id>.jsonl`**（JSONL，一行一条）。

```bash
# 查看最近 N 条
.venv\Scripts\python.exe -m gate_bot trades --bot alpha --tail 50
```

| 字段 | 说明 |
|------|------|
| `ts` | UTC 时间 |
| `type` | `execution`（下单执行）/ `plan`（LLM 规划轮） |
| `bot_id` / `source` | 机器人、来源 |
| `plan_cycle` / `strategy` | 计划周期、策略名 |
| `ok` / `steps` | 成败与每步 action/order_id |

与 `archive/**/result.json`（按信号文件）互补：**trades 按时间流水**，适合审计回看。

## 测试（全套 harness）

```bash
# 单元测试（契约/schema/执行/策略/指标）
.venv\Scripts\python.exe -m unittest discover -s tests

# 行情读取 + hybrid + 指标
.venv\Scripts\python.exe scripts\test_market_read.py

# 订单全类型 testnet（market/limit/post_only/ioc/fok、TP-SL、突破、grid…）
.venv\Scripts\python.exe scripts\testnet_all_orders.py

# 策略提示词 + LLM Plan（需 OPENAI_BASE_URL / OPENAI_API_KEY）
.venv\Scripts\python.exe scripts\test_strategy_prompt.py

# 策略 → 交易所全链路（快照→LLM→风控→inbox→执行→挂单→日志）
.venv\Scripts\python.exe scripts\test_full_chain.py
```

| 套件 | 参考规模 |
|------|----------|
| unittest | 95 |
| market_read | 36 |
| testnet_all_orders | 57 |
| strategy_prompt | 8 |
| full_chain | 10（策略→交易所） |

## 上线准备（Checklist）

1. **密钥**：环境变量 `GATE_API_KEY` / `GATE_API_SECRET`（或 `GATE_TESTNET_*`），LLM 用 `OPENAI_BASE_URL` / `OPENAI_API_KEY`，**不要写进 yaml / 不要进 git**
2. **配置**：复制 `config/bots/_example.yaml` → `config/bots/<bot_id>.yaml`，设 `env`、`symbols`、`max_notional_usd`
3. **自检**：`python -m gate_bot status`；先 `once --bot <id>` 小文件试跑
4. **策略联调**：复制 `config/bots/_llm_test.example.yaml` → `llm-test.yaml`，先 `plan --bot llm-test`
5. **信号源**：AI 只写 `inbox/<bot_id>/`，模板见 **`templates/README.md`**
6. **建议**：`max_notional_usd` 从小开始；确认持仓模式 single/dual 与策略一致
7. **已知限制**：
   - `trail` 追踪单需资金密码 / 测试网不支持，当前搁置
   - `limit_order` 模式 TP/SL 价须在 Gate 偏离带内（过远 `PRICE_TOO_DEVIATED`）；真止损用 `sl_mode: trigger`
   - pa aux 新闻/宏观走 Gate Intel，偶发 TLS 超时（与密钥无关）；盘口/成交/OI 不受影响

## 多机器人怎么加（账户 × 信号源 可自由组合）

**一个 bot = 一份配置 + 一个 inbox 目录。**  
账户和信号源是两个正交维度，可任意组合（不只两种）。

| 维度 | 怎么区分 | 配置字段 |
|------|----------|----------|
| **账户** | API 密钥 / 资金 / 持仓 | `env`、`api_key_env`、`api_secret_env` |
| **信号源** | 谁往哪个目录写 JSON | 目录 `inbox/<bot_id>/`、`symbols`、`label_prefix`、风控 |

### 模式 1：一个账户 = 一个机器人（可共用信号源）

同一账户一个 bot；若有多个策略源，都写**同一个** `inbox/<bot_id>/`（靠 `meta.strategy` 区分），或拆成模式 2。

```yaml
# config/bots/acct-a.yaml
bot_id: acct-a
env: live
api_key_env: GATE_API_KEY          # 账户 A
api_secret_env: GATE_API_SECRET
symbols: [BTC_USDT, ETH_USDT]
label_prefix: a
```

```text
信号源1 ──┐
信号源2 ──┼──► inbox/acct-a/*.json ──► bot acct-a ──► 账户 A
信号源3 ──┘
```

### 模式 2：一个信号源 = 一个机器人（可共用账户）

一个策略源一个 bot；密钥可相同（共用账户），只分开目录/白名单/标签。

```yaml
# config/bots/strat-grid.yaml   ← 同一账户的网格策略
bot_id: strat-grid
env: live
api_key_env: GATE_API_KEY        # 与 strat-trend 相同 = 共用账户
api_secret_env: GATE_API_SECRET
symbols: [BTC_USDT]
label_prefix: grid
max_notional_usd: 50
```

```yaml
# config/bots/strat-trend.yaml  ← 同一账户的趋势策略
bot_id: strat-trend
env: live
api_key_env: GATE_API_KEY        # 共用账户
api_secret_env: GATE_API_SECRET
symbols: [ETH_USDT]
label_prefix: trend
```

```text
网格 AI  ──► inbox/strat-grid/  ──► bot strat-grid  ──┐
趋势 AI  ──► inbox/strat-trend/ ──► bot strat-trend ──┴──► 同一账户
```

### 模式 3：混合（账户 × 策略 矩阵）

```text
                账户 A              账户 B
趋势        strat-trend-a       strat-trend-b
网格        strat-grid-a        strat-grid-b
```

每个格子一份 `config/bots/<id>.yaml` + `inbox/<id>/`，密钥用不同环境变量即可。

### 添加步骤（任一模式相同）

```bash
# 1) 写配置
cp config/bots/_example.yaml config/bots/<bot_id>.yaml
#    改 bot_id / env / api_key_env / symbols / max_notional_usd / label_prefix

# 2)（多账户）准备密钥环境变量
setx GATE_KEY_B "..."
setx GATE_SECRET_B "..."

# 3) 目录会自动创建；信号源写入
#    inbox/<bot_id>/20260302-100000-xxx.json

# 4) 核对并启动
python -m gate_bot status
python -m gate_bot once --bot <bot_id>
python -m gate_bot run              # 全部 enabled
python -m gate_bot run --bot <bot_id>
```

### 组合速查

| 你要什么 | 做法 |
|----------|------|
| 多账户 | 一份 bot 配一个 `api_key_env` |
| 多信号源 | 一份 bot 配一个 `inbox/<bot_id>/` |
| 同账户多策略 | 多份 bot 配置，**相同** `api_key_env` |
| 同策略多账户 | 多份 bot 配置，**相同**策略、**不同**密钥 |
| 隔离风控 | 每 bot 独立 `symbols` / `max_notional_usd` |
| 日志区分 | 每 bot 不同 `label_prefix`；JSON 里 `meta.strategy` |

## 多机器人模型（账户 × 信号源，可自由组合）

**一个 bot = 一份 `config/bots/<bot_id>.yaml` + 一个 `inbox/<bot_id>/`。**  
两个维度正交，可任意组合：

| 维度 | 怎么区分 | 配置字段 |
|------|----------|----------|
| **账户** | 不同 API Key / 资金 | `env`、`api_key_env`、`api_secret_env` |
| **信号源** | 谁往哪个 inbox 写 JSON | 目录名 `inbox/<bot_id>/`、`symbols`、`label_prefix` |

### 模式 1：一个账户 = 一个机器人（可共用信号源）

同一账户一个 bot；多个信号源都写进**同一个** `inbox/<bot_id>/`。

```yaml
# config/bots/acct-a.yaml   # 账户 A 的机器人
bot_id: acct-a
env: live
api_key_env: GATE_KEY_A
api_secret_env: GATE_SECRET_A
symbols: [BTC_USDT, ETH_USDT]
label_prefix: A
```

```text
信号源1 ─┐
信号源2 ─┼─► inbox/acct-a/*.json ─► bot acct-a ─► 账户 A
信号源3 ─┘
```

### 模式 2：一个信号源 = 一个机器人（可共用账户）

同一密钥多个 bot；不同策略写各自 inbox，风控/标签分开。

```yaml
# config/bots/trend.yaml
bot_id: trend
env: live
api_key_env: GATE_API_KEY      # 与 grid 相同 = 共用账户
api_secret_env: GATE_API_SECRET
symbols: [BTC_USDT]
label_prefix: trend
max_notional_usd: 50
```

```yaml
# config/bots/grid.yaml
bot_id: grid
env: live
api_key_env: GATE_API_KEY      # 共用账户
api_secret_env: GATE_API_SECRET
symbols: [ETH_USDT]
label_prefix: grid
max_notional_usd: 50
```

```text
趋势信号 ─► inbox/trend/ ─► bot trend ─┬─► 同一账户
网格信号 ─► inbox/grid/  ─► bot grid  ─┘
```

### 添加步骤（两种通用）

```bash
# 1) 建配置（账户=改 api_key_env；信号源=改 bot_id/目录）
cp config/bots/_example.yaml config/bots/<bot_id>.yaml

# 2)（多账户）准备不同环境变量
setx GATE_KEY_A "..."
setx GATE_SECRET_A "..."
setx GATE_KEY_B "..."
setx GATE_SECRET_B "..."

# 3) 信号源往对应目录丢 JSON
#    inbox/<bot_id>/20260302-100000-xxx.json

# 4) 核对并启动
python -m gate_bot status
python -m gate_bot once --bot <bot_id>
python -m gate_bot run          # 全部 enabled；或 --bot 只跑一个
```

### 组合一览

| 场景 | 做法 |
|------|------|
| 一账户一 bot | 1 份配置，1 个 `api_key_env` |
| 一信号源一 bot | 1 份配置 + 独立 `inbox/<id>/` |
| 同账户多策略 | 多份配置，**相同** `api_key_env` |
| 多账户同策略 | 多份配置，**不同** `api_key_env`，信号写多个 inbox |
| 混合矩阵 | 账户 × 策略 矩阵，一格一份配置 |

已测：同账户双 bot（alpha/beta）目录、白名单、归档隔离 ✅；双账户待有第二套 Key 后补测。

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

### 突破单 vs 止损（禁止用错）

| 意图 | 用这个 | 禁止 |
|------|--------|------|
| **涨破 → 买进开多**（突破进场） | **`stop_entry_long`**（别名 `buy_stop` / `stop_long`） | 禁止用 `sl` |
| **跌破 → 卖出开空**（突破进场） | **`stop_entry_short`**（别名 `sell_stop` / `stop_short`） | 禁止用 `sl` |
| 已有多头，跌到价 → 平仓止损 | `open_long` 的 **`sl`** | 禁止用 `stop_entry_*` |
| 已有空头，涨到价 → 平仓止损 | `open_short` 的 **`sl`** | 禁止用 `stop_entry_*` |

口诀：**突破进场 = `stop_entry_*`（开仓）｜止损保护 = `sl`（平仓）**。  
完整模板见 **`templates/README.md`**（含字段字典与加减仓×持仓模式）。

**已知限制**：`trail` 追踪单需 API 资金密码（`X-Gate-Password`）或追踪委托权限，当前搁置。官方 CLI 语义见 `templates/README.md`。

```json
{
  "action": "stop_entry_long",
  "symbol": "BTC_USDT",
  "trigger_price": 88750,
  "size_usd": 100,
  "type": "market",
  "meta": {"kind": "stop_entry", "reasoning": "breakout entry"}
}
```

### action 一览

| action | 说明 |
|--------|------|
| `open_long` / `open_short` | 开仓；`size`（张）/ `size_usd`（名义 U）/ `size_pct`（可用余额比例）/ `margin_pct`（保证金比例×杠杆） |
| `close` | 平仓；dual 模式需 `side` |
| `close_all` | 市价全平（可省 symbol） |
| `cancel_all` / `cancel_price_all` / `cancel_trail_all` | 撤普通挂单 / 计划委托 / 追踪止损 |
| `hold` / `watch` / `skip` | **不执行下单**，直接归档 done（可写 meta.reasoning） |
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
