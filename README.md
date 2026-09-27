# gate-io monorepo（gate-signal-bot + pa-data-source）

> **新人 / AI 请先读 [`AGENTS.md`](AGENTS.md)**：系统心智模型、五分钟上手、关键规则、文档地图。

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

### 文档导航

| 我要… | 打开 |
|-------|------|
| **10 分钟图文上手** | [`docs/TUTORIAL-10min.md`](docs/TUTORIAL-10min.md) |
| 快速上手 / AI 代理入口 | [`AGENTS.md`](AGENTS.md) |
| 信号 JSON 字段与示例 | `templates/README.md` |
| 策略人格怎么写 | `prompts/README.md` |
| 可抄案例（12 信号 / 多 bot） | `examples/README.md` |
| 上线前全量测试 | `docs/compose/spec/prelaunch-test.md` |
| 各功能设计规格 | `docs/compose/spec/` |

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

AI 生成方案 → 程序风控 → 写 `inbox` → 现有执行器下单（多品种 chips）。

```bash
# 环境变量（密钥不落盘）
# $env:OPENAI_BASE_URL = "http://<host>:<port>/v1"
# $env:OPENAI_API_KEY  = "<key>"

# 单轮：采集快照 → LLM → 风控 → 写 inbox
.venv\Scripts\python.exe -m gate_bot plan --bot alpha

# 常驻：interval_sec 定时 + K线收盘事件（见「AI 触发时机」）
# 生产请用任务计划/systemd 保活，见 docs/OPERATIONS.md
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

### 全部可配参数（每 bot / 每策略独立）

| 层 | 参数 | 说明 |
|----|------|------|
| **触发** | `interval_sec` | 定时秒（300=5m，600=10m…） |
| | `event_on_kline_close` | K 收盘事件开关 |
| | `event_timeframe` | 事件周期 `1m/5m/15m/30m/1h/4h/1d` |
| | `check_interval_sec` | 条件检查频率 |
| **条件** | `price_vs_ema` | `symbol`, **`period` EMA 任意**, `side` |
| | `ema_cross` | `fast` / **`slow` 任意**, `dir` |
| | `ma_cross` | `fast`/`slow`, `ma` sma\|ema, `dir` |
| | `macd_cross` | `fast`, `slow`, `signal`, `dir` |
| | `boll_break` | `period`, `k`, `side` upper\|lower |
| | `atr_spike` | **`period` ATR 任意**, `mult`, `lookback` |
| | `rsi` | **`period` RSI 任意**, `op`, `level` |
| | `price_break` | `lookback`, `side` |
| | `volume_spike` | `mult`, `lookback` |
| | 公共 | `cooldown_sec`, `candles`, `key` |
| **快照指标** | `market.indicators` | **`emaN`/`rsiN`/`atrN`/`maN`/`smaN`**、`macd`/`macd_dea`/`macd_hist`/`macd12_26_9`、`bollN_K`/`boll_upper`/`boll_middle`/`boll_lower` |
| **行情** | `market.mode` / `pa_data_root` / `db` / `stale_factor` / `health_url` / `refresh` | |
| **策略** | `timeframe` / `candles` / `prompt_file` / `write_hold` | |
| **风控** | `min_confidence` / `max_notional_usd` / `max_chips` / `allow_actions` | 程序强制 |
| **LLM** | `model` / `temperature` / `timeout_sec` / `max_tokens` / env 名 | |
| **执行** | `position_policy` / `default_replace` / `label_prefix` / bot `max_notional_usd` | |

指标类 **周期任意** + MACD/BOLL/MA；**未知名启动即报错**（不静默 null）：

```yaml
strategist:
  market:
    indicators: [ema9, ma30, rsi7, atr10, macd, macd_dea, macd_hist, boll20]
  conditions:
    - {type: macd_cross, symbol: BTC_USDT, dir: up}
    - {type: boll_break, symbol: BTC_USDT, side: upper}
    - {type: ma_cross, symbol: BTC_USDT, fast: 7, slow: 30, ma: sma}
    - {type: volume_spike, symbol: BTC_USDT, mult: 2}
```

合计约 **50 项**可配（触发 5 + 条件约 20 + 指标名任意 + 行情 6 + 策略 4 + 风控 4 + LLM 5 + 执行 4）。

### AI 触发时机（plan-loop，可按策略配置）

| 触发 | 配置 | 行为 |
|------|------|------|
| **定时** | `interval_sec: 300`（5m）/ `600`（10m）/ 任意秒 | 每 N 秒跑一轮 Plan |
| **事件：K 线收盘** | `event_on_kline_close: true` + `event_timeframe: 5m` | 该周期 K 线 `t` 前进时立刻跑 |
| **AI 自设触发** | `ai_triggers.enabled` + Plan `triggers[]` | AI 分析后登记条件，命中再跑 Plan（不下单） |
| **事件：条件** | `conditions[]` | EMA / ATR / RSI / 价格突破 等满足时跑 |
| 手动单轮 | `plan --bot <id>` | 不进 loop |

```yaml
strategist:
  interval_sec: 300              # 5 分钟策略；10 分钟用 600
  timeframe: 5m                  # 快照 K 周期（可与事件周期不同）
  event_on_kline_close: true
  event_timeframe: 1m            # 收盘/条件事件用 1m（默认= timeframe）
  check_interval_sec: 1.0        # 条件检查频率
  conditions:                    # 可选，多条任一命中即触发（带冷却）
    - type: price_vs_ema         # 价格相对 EMA
      symbol: BTC_USDT
      period: 20
      side: above                # above | below
      cooldown_sec: 60
    - type: ema_cross            # 快慢 EMA 交叉
      symbol: BTC_USDT
      fast: 9
      slow: 21
      dir: up                    # up | down | any
    - type: atr_spike            # ATR 突然放大
      symbol: BTC_USDT
      period: 14
      mult: 1.5                  # 当前 ATR > mult × 近 lookback 均值
      lookback: 20
    - type: price_break          # N 根高低点突破
      symbol: ETH_USDT
      lookback: 20
      side: high                 # high | low
    - type: rsi
      symbol: BTC_USDT
      period: 14
      op: gt                     # gt | lt
      level: 70
```

```text
每 check_interval_sec 检查
  1) interval 到点？     → run_once(trigger=interval)
  2) K 线收盘？          → run_once(trigger=kline_close)
  3) 任一 condition 命中？ → run_once(trigger=cond[...])
```

- 优先级：定时 > 收盘 > 条件（同刻不重复跑）
- 条件有 **cooldown_sec**（默认 60s），防连打
- `cycle_id` 去重；重入锁；账户失败 abort
- 日志：`plan[interval|kline_close|cond[...]]`

| 策略场景 | 建议 |
|----------|------|
| 5 分钟短线 | `interval_sec: 300`, `event_timeframe: 5m` |
| 10 分钟波段 | `interval_sec: 600`, `timeframe: 10m` |
| 跟 1m 事件 + 5m 快照 | `event_timeframe: 1m`, `timeframe: 5m` |
| 只追突破 | `event_on_kline_close: false`, `conditions: [price_break…]` |
| EMA 金叉进场 | `conditions: [ema_cross dir=up]` |
| 波动率放大加关注 | `conditions: [atr_spike]` |

### 策略提示词：可换 vs 固定

| 部分 | 可否更换 | 位置 |
|------|----------|------|
| **策略人格**（决策风格/进出场偏好） | ✅ 可换 | `strategist.prompt_file` 指向的 md |
| 输出契约（Plan JSON、action 枚举、必填字段） | ❌ 固定 | `gate_bot/strategist/prompt.py` |
| 安全规则（开仓必须 sl、不确定 hold、reasoning 从简） | ❌ 固定 | 同上 |
| 行情/账户快照 | 每轮自动生成 | 不写在策略文件里 |
| 风控（min_confidence / max_notional / allow_actions） | 按 bot 配置 | yaml `strategist.risk`（**程序强制**） |
| 模型 / 温度 / max_tokens | 按 bot 配置 | yaml `strategist.llm` |

组装（每轮相同）：

```text
system = 固定输出契约与安全规则（无角色/风格）
       + 【策略人格】= prompt_file（角色 + 决策风格）
user   = 【品种宇宙】+【策略风控】+【市场与账户快照】→ 请输出 Plan JSON
```

自定义策略步骤：

1. 新建 `prompts/my_strategy.md`（只写**判断风格**：决策顺序、进出场、禁止项）  
2. bot yaml：`prompt_file: prompts/my_strategy.md`  
3. 需要时单独调该 bot 的 `risk` / `symbols` / `max_notional_usd`  
4. `plan --bot <id>` 看 reasoning 是否符合风格  

多策略 = 多个 `prompt_file` + 多个 bot 配置 + 各自 inbox（见「多机器人怎么加」）。  
**提示词改不了动作语义**：突破只能用 `stop_entry_*`，止损用 `sl`；超名义上限仍会被风控拒绝。  
模板与示例见 `prompts/README.md`。

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

# 上线前全量矩阵 M1–M20（推荐，见 docs/compose/spec/prelaunch-test.md）
.venv\Scripts\python.exe scripts\prelaunch_runner.py --phase readonly --env testnet
.venv\Scripts\python.exe scripts\prelaunch_runner.py --phase orders --env testnet
.venv\Scripts\python.exe scripts\prelaunch_runner.py --phase fault
.venv\Scripts\python.exe scripts\prelaunch_runner.py --phase live          # ≤10U 小额实单
.venv\Scripts\python.exe scripts\prelaunch_runner.py --phase regress

# 行情读取 + hybrid + 指标
.venv\Scripts\python.exe scripts\test_market_read.py

# 订单全类型 testnet（market/limit/post_only/ioc/fok、TP-SL、突破、grid…）
.venv\Scripts\python.exe scripts\testnet_all_orders.py

# 策略提示词 + LLM Plan（需 OPENAI_BASE_URL / OPENAI_API_KEY）
.venv\Scripts\python.exe scripts\test_strategy_prompt.py
.venv\Scripts\python.exe scripts\test_llm_sizing_modes.py

# 策略 → 交易所全链路（快照→LLM→风控→inbox→执行→挂单→日志）
.venv\Scripts\python.exe scripts\test_full_chain.py

# 生产级：触发配置 + 真实 AI + 全订单类型 + 交易所
.venv\Scripts\python.exe scripts\test_production.py
```

| 套件 | 参考规模 |
|------|----------|
| unittest | **148** |
| **prelaunch 矩阵** | **M1–M20**（testnet+live，含故障注入） |
| market_read | 36 |
| testnet_all_orders | 57 |
| strategy_prompt / llm_sizing | 8 / 7 场景 |
| full_chain | 10（策略→交易所） |
| production | 24（触发+AI+订单+交易所） |
| examples/testnet | 25（全部案例含双向网格/两种止盈） |

## 上线准备（Checklist）

> **放行基线**：`master` `b2d37e4` 已通过 prelaunch 全量（0 FAIL），见 `docs/compose/spec/prelaunch-test.md`。

1. **密钥**：环境变量 `GATE_API_KEY` / `GATE_API_SECRET`（或 `GATE_TESTNET_*`），LLM 用 `OPENAI_BASE_URL` / `OPENAI_API_KEY`，**不要写进 yaml / 不要进 git**
2. **配置**：复制 `config/bots/_example.yaml` → `config/bots/<bot_id>.yaml`，设 `env`、`symbols`、`max_notional_usd`、**`label_prefix`**（多 bot 隔离命名空间）
3. **自检**：`python -m gate_bot status`；先 `once --bot <id>` 小文件试跑
4. **策略联调**：复制 `config/bots/_llm_test.example.yaml` → `llm-test.yaml`，先 `plan --bot llm-test`
5. **信号源**：AI 只写 `inbox/<bot_id>/`，模板见 **`templates/README.md`**
6. **上线首日**：`max_notional_usd` 从小开始（建议 ≤10U）；确认持仓模式 single/dual 与策略一致；跑一轮 `prelaunch_runner --phase live`
7. **仓位口径**：策略优先 `size_usd`（名义 U）；用 `size`（张）前查快照 `contract.min_notional_usd`
8. **已知限制**：
   - `trail` 追踪单需资金密码 / 测试网不支持，当前搁置
   - `limit_order` 模式 TP/SL 价须在 Gate 偏离带内（过远 `PRICE_TOO_DEVIATED`）；真止损用 `sl_mode: trigger`
   - 市价单若遇价格偏离，自动回退为 **公允价带内 IOC**（不成即撤，不挂死）
   - pa aux 新闻/宏观走 Gate Intel，偶发 TLS 超时（与密钥无关）；盘口/成交/OI 不受影响
   - 多 bot 归属靠 `label_prefix` 命名空间，**不是**加密鉴权；勿把不可信 JSON 放进生产 inbox

## 多机器人怎么加（账户 × 信号源 可自由组合）

> **逐步照抄请看 [`docs/HOWTO-add-bot.md`](docs/HOWTO-add-bot.md)**（6 步 + 验收清单 + FAQ）。

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

> 换机器 / 上服务器见 **`AGENTS.md` §8**（`--root` / `GATE_BOT_ROOT` / `GATE_BOT_PA_DATA`；Linux 用 `.venv/bin/python`）。

```bash
cd gate-signal-bot
pip install -r requirements.txt   # pyyaml

# 密钥（推荐环境变量，不要写进 yaml）
# 实盘
setx GATE_API_KEY "..."
setx GATE_API_SECRET "..."
# 交易所测试网
setx GATE_TESTNET_API_KEY "..."
setx GATE_TESTNET_API_SECRET "..."
# 本地模拟盘（env: paper）不需要交易所密钥
# LLM 策略另需 OPENAI_API_KEY
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

## 本地模拟盘（paper）— 复刻交易所，只虚拟资金

除资金外一切真实：**真实行情/精度/费率触发，本地订单/持仓/盈亏**。订单挂本地，价格到了自动触发；实时计算盈亏；触及强平价自动强平；每 8h 按真实资金费率结算。

```yaml
# config/bots/my-paper.yaml
bot_id: my-paper
env: paper                     # 不需要交易所密钥
symbols: [BTC_USDT]
label_prefix: mp
paper:
  feed_exchange: gate          # 行情/费率/合约元数据来源（六所任选）
  initial_capital: 10000       # 虚拟资金（可任意配）
  leverage: 20
  fee_rate: 0.0005
  funding_enabled: true
  tick_interval_sec: 2
```

```bash
python -m gate_bot paper-run --bot my-paper   # 独立进程 + 撮合/强平/费率 tick
python -m gate_bot once --bot my-paper        # 手工信号单次执行
```

**能力**：盘口价成交（买→ask 卖→bid）｜全订单类型（limit/market/stop_entry/TP-SL 触发/GTC/IOC/FOK/PO）｜精度校验（tick/lot/最小名义/价格带/杠杆）｜保证金与强平引擎｜实时盈亏（`account` 工具直接可读）｜资金费率 8h 结算。账户库 `data/bots/<id>/paper/account.db`（八表）。

配套 LLM 策略（`strategist:` 段）即可让 AI 自动分析→Plan→paper 执行。详见 `docs/compose/spec/local-paper-trading.md`。

## 信号广播（一信号 → 多所）

一条信号同时在**多个交易所账户**执行，目标由**系统配置**决定（AI 写的信号碰不到目标列表，防乱输入）。

```yaml
# config/broadcast.yaml
routes:
  - name: dual-gate-okx
    from: signal-feed          # 源 bot_id（信号投这里的 inbox）
    to: [gate-btc, okx-btc]    # 目标 bot 列表：可 1 个 / 2 个 / N 个任意子集
    enabled: true
```

```bash
python -m gate_bot broadcast           # 常驻自动分发
python -m gate_bot broadcast --once    # 单轮退出
```

**特性**：
- **可选目标**：`to` 列表任意写 1 个、2 个或 N 个 bot
- **可靠**：逐个校验写入，任一失败报错；原子落盘防半读；同名幂等跳过
- **隔离**：目标 bot 各自独立执行（密钥/风控/持仓/盈亏互不影响）
- **安全**：信号里 `targets`/`exchanges` 字段一律忽略；目标 bot 不存在拒绝启动
- **审计**：`data/broadcast/log.jsonl` 留痕；源信号归档 `archive/broadcast-done/`

## LLM 工具（20 个）与指标（23 族）

AI 策略层按需调用：`klines` `indicators` `ticker` `orderbook` `contract` `stats` `account` `smc_map` `smc_events` `sqzmom` + 10 个 aux。

- **两套 SMC**：`smc_map`（市场地图：趋势/估值区/关键位）与 `smc_events`（结构事件：BOS/CHoCH/扫荡/Breaker）互补
- **指标 23 族**（任意周期）：EMA/SMA/RMA/WMA/VWMA、ATR（Pine 平滑）、RSI、MACD、BOLL、Stoch、CCI、WR、MFI、ADX、VWAP、OBV、SuperTrend、SQZMOM

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

**优先写 `size_usd`（名义 U）**，机器人按下列公式换算张数；`size` 是合约张数，各币 1 张名义不同（BTC≈0.0001 BTC、ETH=0.01 ETH、SOL=1 SOL…），写错会开错仓。

```text
contracts = floor( size_usd / (price * quanto_multiplier) )
```

`price`：限价用委托价，市价用最新价。不足 1 张会失败归档，不会下 0 张。

AI 快照 `market[symbol].contract` 暴露换算所需元数据：

| 字段 | 含义 |
|------|------|
| `quanto_multiplier` | 1 张 = 多少标的（张数换算） |
| `min_notional_usd` | 1 张 ≈ 名义 U（`last * quanto`） |
| `order_size_round` / `order_price_round` | 数量/价格精度 |
| `leverage_max` | 该合约最大杠杆 |

## 安全约定

1. 启动每单打印 **实盘/模拟盘横幅**，密钥变量名明确。
2. `max_notional_usd` 超限拒单；`symbols` 白名单外拒单。
3. 网络超时 **不自动重试**（防重复下单），AI 下轮可重发。
4. 同文件顺序执行；部分成功不回滚，`error.json` 列出 partials。

## 与 quick_order.py 关系

签名、TIF 映射、触发单 body（`{initial, trigger}`）、持仓模式识别均对齐 `pa-data-source-v2.11/quick_order.py`。本项目专注「文件夹信号 → 自动下单」，不采集行情。
