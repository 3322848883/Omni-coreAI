# Account Watcher — Gate.io 合约实时监控 & 快速下单系统

通过 WebSocket 实时订阅 Gate.io 合约 K线数据与账户状态，支持持仓、余额、挂单、计划委托、成交记录的全量推送。同时提供 CLI 快速下单工具，支持所有订单类型和持仓模式。

**实盘 + 测试网双实例**：同一份代码同时常驻运行实盘与测试网（模拟盘）两个实例，各自独立数据库、独立健康端口、独立密钥，数据完全隔离，一比一复刻实盘规范（含 EMA20/ATR14）。

---

## 功能概览

### K线数据采集

| 功能 | 说明 |
|------|------|
| 实时K线采集 | WebSocket 订阅 `futures.candlesticks`，毫秒级延迟 |
| 实时价格推送 | WebSocket 订阅 `futures.tickers`，获取最新成交价 |
| 历史数据回填 | 启动时通过 REST API 拉取 2000 根历史K线 |
| EMA20 计算 | 基于收盘价本地计算，TradingView 标准公式 `ta.ema(src, 20)` |
| 数据存储 | SQLite 主库，统一表结构，连接设置 `timeout=30` 防并发超时 |
| 断线自动重连 | 连接断开后自动重连，通过 REST API 补全缺失数据（支持 1m/5m/15m/1h/4h/1d 全周期） |
| ATR14 计算 | TradingView 标准公式 `ta.rma(ta.tr(true), 14)` |
| 数据保留 | 可配置保留天数，超期自动清理 |
| **测试网双实例** | `--env testnet` 独立运行模拟盘实例，独立库 `kline_testnet.db`，数据与实盘完全隔离 |
| **测试网 REST 兜底** | 测试网 WS 端点当前不可达（10054，2026-08-29 复测仍不通），由 REST 轮询主采集兜底（10s 间隔 `TESTNET_POLL_INTERVAL`，60s 实测过慢已缩短）保证数据持续新鲜 |
| **合约存在性校验** | 启动时查询当前环境合约列表，不存在的品种自动跳过（如测试网无 XAU/XAG），不阻塞其他品种 |

### 容错与监控

| 功能 | 说明 |
|------|------|
| 结构化日志 | 日志输出到终端和文件，按日期轮转保留 7 天 |
| 数据更新监控 | 检测数据停滞，超过 2 分钟触发告警 |
| CLI 备用数据源 | WebSocket 数据异常时自动切换到 CLI |
| 自动重启守护进程 | 脚本崩溃后 5 秒内自动重启，最多 5 次/小时 |
| HTTP 健康检查 | 实盘端口 18080 / 测试网端口 18081，返回系统状态 JSON |
| 断线重连补全 | 断线后自动补全缺失数据并验证完整性 |
| 单实例锁 | watchdog/kline_watcher 各自 PID 锁 + launcher.pyw TCP 端口锁（18081），孤儿锁自动接管，绝不双实例写库 |
| 运行状态查询 | `python status.py`（`--json` 供 AI）：进程/健康端点/数据新鲜度/近 2h 日志错误，退出码 0=healthy 1=degraded 2=stopped |

### 账户状态监控

| 功能 | 说明 |
|------|------|
| 持仓推送 | WebSocket `futures.positions` 实时推送持仓变动 |
| 余额推送 | WebSocket `futures.balances` 实时推送余额变动 |
| 挂单推送 | WebSocket `futures.orders` 实时推送限价单状态 |
| 计划委托推送 | WebSocket `futures.autoorders` 推送止盈止损单、计划委托 |
| 成交记录推送 | WebSocket `futures.usertrades` 推送每笔成交明细 |
| 定时汇总 | 守护线程定时输出账户全景（余额+持仓+挂单+计划委托） |
| 持仓持久化 | 持仓数据实时写入 `position_current`，重启自动恢复 |
| **当前状态表** | `balance_current`、`position_current`、`order_current`、`price_order_current` — AI 友好查询 |
| **历史流水表** | `balance_history`、`position_history`、`order_history`、`price_order_history`、`trade_history` — 完整记录 |

### 快速下单工具 (quick_order.py)

| 功能 | 说明 |
|------|------|
| 查询 | 持仓、挂单、计划委托、余额、持仓模式 |
| 开仓 | 限价/市价/PostOnly/IOC/FOK，支持逐仓/全仓 |
| 平仓 | 单向/双向模式平仓，指定数量或全平 |
| 止盈止损 | 价格触发订单，支持市价/限价执行，可设触发规则 |
| 撤单 | 撤销普通挂单、计划委托/止盈止损单，支持单个或批量撤销 |
| 持仓模式自动识别 | 自动检测单向/双向/分仓模式，下单时自动适配 |
| 订单标签与对账 | `--order-label` 给订单打标签；下单网络超时后按标签查询挂单/最近成交自动对账，防止重复下单（无标签时提示手动核对） |
| **实盘/模拟盘隔离** | `--env testnet` 用独立测试网密钥（GATE_TESTNET_API_KEY）与测试网 REST 地址，操作前打印环境横幅防误下单 |

---

## 目录结构

```
pa-data-source/
├── kline_watcher.py      # 主程序（K线采集 + 账户监控）
├── fetch_aux.py          # 辅助信息采集（旁路，只读展示用，非分析输入）
├── aux_monitor.py        # 辅助流一致性判定
├── test_purge.py         # 辅助信息滚动缓存清理测试
├── quick_order.py        # CLI 快速下单工具
├── query_kline.py        # K线统一查询（本地优先，非监控品种自动转交易所）
├── status.py             # 运行状态一键查询（--json 供 AI）
├── launcher.pyw          # GUI 启动器（桌面双击启动）
├── launcher.vbs          # VBScript 启动入口（自动检测 Python，无黑窗）
├── watchdog.py           # 自动重启守护进程（守护 kline-live + kline-testnet + fetch_aux，整体重启）
├── watchlist.yaml        # 实盘配置文件（品种、周期、API Key）
├── watchlist_testnet.yaml # 测试网配置文件（模拟盘，独立密钥）
├── gate-cli.exe          # Gate CLI 工具（备用数据源）
├── logger.py             # 结构化日志系统
├── data_monitor.py       # 数据更新监控
├── backup_source.py      # CLI/MCP 备用数据源
├── health_check.py       # HTTP 健康检查端点
├── server_setup.sh       # Linux 服务器部署脚本
├── install.bat           # Windows 一键安装（建 .venv + 装依赖）
├── requirements.txt      # Python 依赖清单
├── autostart.vbs         # 开机自启脚本（Startup 快捷方式指向）
├── _ws_probe.py          # WS 连通性探针（诊断：连接/订阅/推送计数，复核测试网 WS 恢复用）
├── agents/               # 接口配置（openai.yaml）
├── logs/                 # 日志目录
│   ├── kline_watcher.log
│   └── watchdog.log
├── data/                 # 核心数据（K线 + 账户，与 aux-data 严格隔离）
│   ├── kline.db          # 实盘 K线数据库（统一表）
│   ├── account.db        # 实盘账户数据库（余额、持仓、订单、成交）
│   ├── kline_testnet.db  # 测试网 K线数据库（模拟盘，与实盘隔离）
│   └── account_testnet.db # 测试网账户数据库（模拟盘，与实盘隔离）
└── aux-data/             # 辅助信息运行时数据（旁路，详见 aux-data/README.md）
    ├── DESIGN.md         # 辅助信息流设计文档
    ├── README.md         # 区分说明（与 data/ 的边界）
    ├── snapshot/         # 落盘 JSON 快照（panel 只读，含 events/overview/macro/sentiment/reserves/stats/liquidations/orderbook/trades/rankings/announcements/social/coin_info/tech_analysis/onchain/event_signals）
    ├── aux_cache.db      # 独立滚动缓存（唯一自建、唯一自用）
    ├── seen_ids.json     # 事件去重状态
    ├── aux_status.json   # 采集状态
    └── logs/             # 辅助信息独立日志
```

---

## 环境要求

- Python 3.9+
- `gate-cli.exe`（已内置在项目目录）

### 安装依赖

一键安装（推荐，自动建 `.venv` 并装依赖，与 Linux `server_setup.sh` 对称）：

- Windows：双击 `install.bat`
- Linux：`bash server_setup.sh`

手动安装：

```bash
pip install -r requirements.txt
# 等价于: pip install websocket-client pyyaml
```

---

## 快速开始

### 1. 配置 watchlist.yaml

```yaml
market_type: futures          # futures 或 spot

symbols:
  - name: BTC_USDT
    intervals: [1m, 5m, 15m, 1h, 4h]
  - name: ETH_USDT
    intervals: [1m, 5m, 15m, 1h, 4h]

indicators:
  - ema: [20]

output_dir: "./data"
retention_days: 30            # 数据保留天数（0=永久保留）
max_candles: 2000             # 每个周期最大K线数

# === 账户推送配置 ===
api_key: "YOUR_API_KEY"       # 从 Gate.io API Keys 获取
api_secret: "YOUR_API_SECRET"
account_push: true            # 启用账户推送
account_push_interval: 60     # 汇总打印间隔（秒）
```

### 2. 启动采集

```bash
cd pa-data-source

# 方式 1：桌面双击启动（Windows 推荐）
# 双击 launcher.vbs，自动检测 Python 后打开 GUI 启动器，点击"启动"按钮即可

# 方式 2：使用守护进程启动（命令行，同时拉起实盘+测试网+辅助信息三实例）
python watchdog.py

# 方式 3：直接启动（调试用）
python kline_watcher.py                # 实盘
python kline_watcher.py --env testnet  # 测试网（模拟盘）
```

> **首次在新电脑运行？** 先双击 `install.bat` 一键装依赖（或 `pip install -r requirements.txt`），
> 再双击 `launcher.vbs` 启动 GUI（需已安装 Python 3.9+ 并勾选 Add to PATH）。

启动后自动执行：
1. 加载 `watchlist.yaml` 配置
2. 初始化 SQLite 数据库表（kline.db 统一表 + account.db 账户表）
3. 通过 REST API 加载历史K线
4. 重新计算所有 EMA20 和 ATR14 指标（确保与 TradingView 对齐）
5. 获取初始账户状态（余额、持仓）
6. 连接 WebSocket 实时接收 K线 + 账户数据

按 `Ctrl+C` 优雅退出。

### 2.1 测试网（模拟盘）配置

测试网与实盘完全隔离：独立配置文件、独立数据库、独立健康端口、独立密钥。

```yaml
# watchlist_testnet.yaml
market_type: futures
env: testnet          # 模拟盘标识

symbols:
  - name: BTC_USDT
    intervals: [1m, 5m, 15m, 1h, 4h, 1d]
  - name: ETH_USDT
    intervals: [1m, 5m, 15m, 1h, 4h, 1d]
  - name: SOL_USDT
    intervals: [1m, 5m, 15m, 1h, 4h, 1d]
  - name: DOGE_USDT    # XAU/XAG 测试网无合约，用热门品种补位
    intervals: [1m, 5m, 15m, 1h, 4h, 1d]
  - name: XRP_USDT
    intervals: [1m, 5m, 15m, 1h, 4h, 1d]

output_dir: "./data"
retention_days: 180
max_candles: 2000

# === 账户推送配置（模拟盘） ===
# 测试网密钥独立（不要复用实盘密钥）：
#   环境变量 GATE_TESTNET_API_KEY / GATE_TESTNET_API_SECRET
#   或本文件 api_key / api_secret（当前已配置测试网密钥）
account_push: true
account_push_interval: 60
```

> **当前状态**：测试网密钥已配置（用户级环境变量 `GATE_TESTNET_API_KEY`/`GATE_TESTNET_API_SECRET`，2026-08-30 从 yaml 明文迁出，yaml 不再存密钥），`account_push: true` 已开启，模拟盘账户推送正常。

**测试网实例参数**（由 watchdog 自动配置）：

| 参数 | 实盘 | 测试网 |
|------|------|--------|
| 配置文件 | `watchlist.yaml` | `watchlist_testnet.yaml` |
| K线库 | `data/kline.db` | `data/kline_testnet.db` |
| 账户库 | `data/account.db` | `data/account_testnet.db` |
| 健康端口 | 18080 | 18081 |
| 锁文件 | `kline_watcher.lock` | `kline_watcher_testnet.lock` |
| 密钥 | `GATE_API_KEY` | `GATE_TESTNET_API_KEY` |

> **测试网数据说明**：测试网为模拟盘数据，与实盘完全隔离。测试网 WS 官方支持实时推送（`wss://ws-testnet.gate.com/v4/ws/futures/usdt`），但当前端点不可达（10054，2026-08-29 复测仍不通），由 REST 轮询主采集兜底（10s 间隔），EMA20/ATR14 等指标与实盘同规范计算。

### 3. 验证数据

```bash
# 运行状态一键查询（进程/健康端点/数据新鲜度/近 2h 日志错误，--json 供 AI）
python status.py --json

# 查询本地 K 线（含 ema20/atr14；--env testnet 查测试网库）
python query_kline.py -s BTC_USDT -i 15m -n 20

# 辅助信息滚动缓存清理与去重测试（111 项断言）
python test_purge.py

# 健康检查（实盘端口 18080 / 测试网端口 18081，仅本机 127.0.0.1 可访问）
curl http://localhost:18080/health
curl http://localhost:18081/health
```

**CLI 验证示例：**

```bash
# 查询 Gate 官方 BTC 1小时K线（用于对比验证）
gate-cli info marketdetail get-kline --symbol BTC_USDT --timeframe 1h --limit 5 --format json

# 查询本地数据库对比
sqlite3 data/kline.db "SELECT t, o, h, l, c FROM kline WHERE symbol='BTC_USDT' AND interval='1h' ORDER BY t DESC LIMIT 5"
```

> **注意：** CLI 返回 UTC 时间（`t_utc`），数据库存储 Unix 时间戳。同一根K线的时间戳值相同，只是显示时区不同（UTC vs 本地时间）。

### 4. 查看日志

日志文件位于 `logs/kline_watcher.log`，按日期自动轮转，保留 7 天。

```bash
# 实时查看日志
Get-Content logs\kline_watcher.log -Wait

# 查看最近 50 行
Get-Content logs\kline_watcher.log -Tail 50
```

---

## 服务器部署（Linux）

完整指南见 `SERVER.md`。快速流程：

```bash
bash server_setup.sh          # 建 .venv + 装依赖 + 下载 Linux gate-cli（SHA256 校验）
export GATE_API_KEY="你的key"  # 密钥用环境变量，不落盘
export GATE_API_SECRET="你的secret"
./.venv/bin/python watchdog.py
curl http://127.0.0.1:18080/health   # 验证
```

### systemd 开机自启（推荐）

创建 `/etc/systemd/system/pa-data-source.service`：

```ini
[Unit]
Description=PA Data Source (Gate.io kline + account watcher)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/pa-data-source
Environment=GATE_API_KEY=你的key
Environment=GATE_API_SECRET=你的secret
ExecStart=/opt/pa-data-source/.venv/bin/python watchdog.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

启用并查看：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now pa-data-source
sudo systemctl status pa-data-source
journalctl -u pa-data-source -f          # 查看日志
```

> 生产建议把密钥放 systemd `EnvironmentFile=` 或环境变量服务（如 systemd-creds / docker secret），不要写死在 unit 文件里。

---

## 快速下单工具 (quick_order.py)

### 环境选择（实盘/模拟盘）

```bash
# 默认实盘（使用 GATE_API_KEY）
python quick_order.py --balance

# 模拟盘（测试网，使用 GATE_TESTNET_API_KEY，独立密钥不混用）
python quick_order.py --env testnet --balance
python quick_order.py --env testnet -s BTC_USDT --side long --type market --size 1 --confirm
```

> 所有操作前会打印环境横幅（实盘/模拟盘 + 所用密钥变量），防止误下单。模拟盘未配置测试网密钥时会明确报错，不会静默回退到实盘密钥。

### 查询命令

```bash
# 查询持仓
python quick_order.py --positions

# 查询挂单
python quick_order.py --orders -s BTC_USDT

# 查询计划委托（止盈止损）
python quick_order.py --price-orders

# 查询余额
python quick_order.py --balance

# 查询持仓模式
python quick_order.py --position-mode
```

### 开仓命令

```bash
# 做多市价开仓 1 张
python quick_order.py -s BTC_USDT --side long --type market --size 1

# 做空限价开仓 1 张
python quick_order.py -s BTC_USDT --side short --type limit --price 50000 --size 1

# 做多仅Maker开仓（PostOnly）
python quick_order.py -s BTC_USDT --side long --type post_only --price 50000 --size 1

# 做多 IOC 开仓
python quick_order.py -s BTC_USDT --side long --type ioc --price 50000 --size 1

# 做多 FOK 开仓
python quick_order.py -s BTC_USDT --side long --type fok --price 50000 --size 1

# 逐仓模式做多
python quick_order.py -s BTC_USDT --side long --type market --size 1 -m isolated

# 跳过确认直接下单
python quick_order.py -s BTC_USDT --side long --type market --size 1 --confirm
```

### 平仓命令

```bash
# 平仓（单向模式自动识别方向）
python quick_order.py -s BTC_USDT --close --size 1

# 平多仓（双向模式）
python quick_order.py -s BTC_USDT --close-long --size 1

# 平空仓（双向模式）
python quick_order.py -s BTC_USDT --close-short --size 1

# 市价全平所有仓位
python quick_order.py -s BTC_USDT --close-all

# 市价全平多仓
python quick_order.py -s BTC_USDT --close-long-all

# 市价全平空仓
python quick_order.py -s BTC_USDT --close-short-all
```

### 止盈止损（价格触发订单）

> **注意**：真实平仓触发单（`--close-trigger`）禁止 `--trigger-type market`（市价触发在极端行情下滑点不可控），
> 必须使用 `--trigger-type limit` 并显式指定 `--trigger-limit-price`。条件**开仓**单不受此限制。

```bash
# 止损：价格跌到 72000 时限价 71900 平多仓
python quick_order.py -s BTC_USDT --close-trigger --trigger-price 72000 --trigger-type limit --trigger-limit-price 71900 --side short --size 1 --trigger-rule 2

# 止盈：价格涨到 75000 时限价 74900 平多仓
python quick_order.py -s BTC_USDT --close-trigger --trigger-price 75000 --trigger-type limit --trigger-limit-price 74900 --side short --size 1 --trigger-rule 1

# 条件开仓：价格突破 72000 时市价做多（开仓不受 market 限制）
python quick_order.py -s BTC_USDT --trigger-price 72000 --trigger-type market --side long --size 1

# 使用标记价触发（而非最新价）
python quick_order.py -s BTC_USDT --close-trigger --trigger-price 72000 --trigger-type limit --trigger-limit-price 71900 --side short --size 1 --trigger-rule 2 --trigger-price-type mark

# 设置 24 小时过期
python quick_order.py -s BTC_USDT --close-trigger --trigger-price 72000 --trigger-type limit --trigger-limit-price 71900 --side short --size 1 --trigger-rule 2 --trigger-expiration 86400
```

> **测试网限制**：`--trigger-expiration`（过期时间）参数测试网（模拟盘）不支持，工具会自动忽略并提示；实盘支持该参数。

**止盈止损参数说明：**

| 参数 | 说明 |
|------|------|
| `--close-trigger` | 触发后平仓（止盈止损模式） |
| `--trigger-price` | 触发价格 |
| `--trigger-type market/limit` | 触发后执行类型 |
| `--trigger-limit-price` | 触发后限价价格（limit 时必填） |
| `--trigger-rule 1/2` | 1=价格>=触发价时执行，2=价格<=触发价时执行 |
| `--trigger-price-type` | 触发价格类型：latest(最新价)/mark(标记价)/index(指数价) |
| `--trigger-expiration` | 过期秒数（如 86400=24h） |

**触发规则速查：**

| 场景 | side | trigger-rule |
|------|------|-------------|
| 做多止损（价格下跌触发） | short | 2（默认） |
| 做多止盈（价格上涨触发） | short | 1 |
| 做空止损（价格上涨触发） | long | 1（默认） |
| 做空止盈（价格下跌触发） | long | 2 |
| 条件开多（突破买入） | long | 1（默认） |
| 条件开空（跌破卖出） | short | 2（默认） |

### 撤单命令

```bash
# 撤销指定普通挂单
python quick_order.py --cancel-order 36028829985149125

# 撤销该合约所有普通挂单
python quick_order.py --cancel-all -s BTC_USDT

# 撤销指定计划委托/止盈止损单
python quick_order.py --cancel-price-order 2062933944050909184

# 撤销该合约所有计划委托/止盈止损单
python quick_order.py --cancel-all-price-orders -s BTC_USDT

# 撤销所有合约的计划委托/止盈止损单
python quick_order.py --cancel-all-price-orders
```

### 订单类型说明

| 类型 | 参数 | 说明 |
|------|------|------|
| 限价单 | `--type limit` | 指定价格挂单，未成交前挂在订单簿 |
| 市价单 | `--type market` | 以当前市场价格立即成交 |
| 仅Maker | `--type post_only` | 只能作为 Maker 成交，不会吃单 |
| IOC | `--type ioc` | 立即成交剩余取消，部分成交 |
| FOK | `--type fok` | 全部成交或全部取消 |

### 持仓模式

| 模式 | 说明 | 平仓方式 |
|------|------|---------|
| single | 单向持仓 | `--close` 自动识别方向 |
| dual | 双向持仓 | `--close-long` / `--close-short` |
| dual_long_short | 双向分仓 | 同 dual，可指定 `pos_margin_mode` |

---

## 账户推送说明

### API Key 获取

1. 登录 [Gate.io](https://www.gate.io)
2. 进入 **API管理**（[链接](https://www.gate.io/myaccount/apikeys)）
3. 创建新 API Key，权限选择 **Futures**
4. 将 `api_key` 和 `api_secret` 填入 `watchlist.yaml`

### 推送的数据类型

| WebSocket 频道 | 数据类型 | 事件 |
|----------------|----------|------|
| `futures.positions` | 持仓信息 | `update` — 持仓变动时推送 |
| `futures.balances` | 余额信息 | `update` — 余额变动时推送 |
| `futures.orders` | 限价挂单 | `put` 新挂单 / `update` 更新 / `finish` 完成 |
| `futures.autoorders` | 计划委托/止盈止损 | `put` 新委托 / `update` 更新 / `finish` 触发完成 |
| `futures.usertrades` | 成交记录 | `update` — 每笔成交推送 |

### 定时汇总输出

开启 `account_push: true` 后，守护线程按 `account_push_interval` 间隔输出：

```
============================================================
[12:34:56] === 账户汇总 ===
  余额: 总计=468.44 USDT  可用=468.44 USDT  浮盈=0 USDT
  持仓: 无
  挂单: 无
  计划委托: 无
============================================================
```

---

## 配置参数说明

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `market_type` | `futures` | 市场类型：`futures`（合约）或 `spot`（现货） |
| `symbols[].name` | — | 交易对名称，如 `BTC_USDT` |
| `symbols[].intervals` | — | K线周期列表，支持：`1m`, `5m`, `15m`, `1h`, `4h`, `1d` 等 |
| `retention_days` | `30` | 数据保留天数，超期自动清理。设为 `0` 表示永久保留（当前配置 180） |
| `max_candles` | `500` | 每个品种/周期存储的最大K线数（当前配置 2000） |
| `output_dir` | `./data` | 数据库输出目录 |
| `api_key` | — | Gate.io API Key（需 Futures 权限） |
| `api_secret` | — | Gate.io API Secret |
| `account_push` | `false` | 启用账户状态推送 |
| `account_push_interval` | `60` | 账户汇总打印间隔（秒，最小 10） |
| `log_level` | `INFO` | 日志级别：`DEBUG`/`INFO`/`WARNING`/`ERROR` |

---

## 数据说明

### SQLite 表结构

#### K线表 (kline.db)

统一 `kline` 表存储所有品种/周期数据，使用复合主键 `(symbol, interval, t)`。

| 字段 | 类型 | 说明 |
|------|------|------|
| `symbol` | TEXT | 交易对名称（如 `BTC_USDT`），联合主键 |
| `interval` | TEXT | K线周期（如 `1m`、`5m`），联合主键 |
| `t` | INTEGER | Unix 时间戳（联合主键），即K线起始时间 |
| `o` | TEXT | 开盘价 |
| `h` | TEXT | 最高价 |
| `l` | TEXT | 最低价 |
| `c` | TEXT | 收盘价 |
| `v` | TEXT | 成交量（基础币数量） |
| `sum` | TEXT | 成交额（计价币金额） |
| `ema20` | TEXT | EMA20 指标值（可为 NULL） |
| `atr14` | TEXT | ATR14 指标值（可为 NULL） |

**索引：**
- `idx_kline_time(t)` — 按时间查询
- `idx_kline_symbol(symbol, t)` — 按品种+时间查询

#### 账户数据库 (account.db)

独立数据库存储账户相关数据，分为**当前状态表**（AI 友好）和**历史记录表**（流水）：

**当前状态表（实时覆盖，方便 AI 查询，均含 UTC 和本地时间）：**

| 表名 | 主键 | 说明 | AI 查询示例 |
|------|------|------|------------|
| `balance_current` | `id=1` | 最新余额、可用、浮盈、保证金 | `SELECT * FROM balance_current WHERE id=1` |
| `position_current` | `(contract, mode)` | 每个品种最新持仓（size=0 自动删除；双向模式同合约可有多/空两行） | `SELECT * FROM position_current WHERE contract='BTC_USDT'` |
| `order_current` | `order_id` | 当前未成交**普通挂单**（成交后自动删除；触发单见 `price_order_current`） | `SELECT * FROM order_current WHERE contract='ETH_USDT'` |
| `price_order_current` | `order_id` | 当前未触发计划委托/止盈止损单（含 `trigger_price`/`rule`/`is_stop`，触发后自动删除） | `SELECT * FROM price_order_current WHERE contract='BTC_USDT'` |

> 四个状态表均含 `updated_at_str`（UTC 时间）和 `updated_at_local_str`（本地时间）字段，便于直接显示。

**历史记录表（时间序列，保留完整流水）：**

| 表名 | 主键 | 说明 |
|------|------|------|
| `balance_history` | `id` (自增) | 余额历史快照 |
| `position_history` | `id` (自增) | 持仓变动历史 |
| `order_history` | `id` (自增) | 订单事件历史（put/update/finish；轮询仅在状态变化时记录，不刷屏） |
| `price_order_history` | `id` (自增) | 计划委托/止盈止损事件历史（put/update/finish，含触发价） |
| `trade_history` | `id` (自增) | 成交记录历史 |

**索引（支持按品种快速查询）：**
- `idx_order_contract_ts(contract, timestamp)` — 查某品种最近订单
- `idx_price_order_contract_ts(contract, timestamp)` — 查某品种最近计划委托/止盈止损
- `idx_trade_contract_ts(contract, timestamp)` — 查某品种最近成交

**balance_current 字段：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | INTEGER | 主键（固定为 1） |
| `updated_at` | INTEGER | 更新时间戳（NOT NULL） |
| `updated_at_str` | TEXT | 更新时间（UTC） |
| `updated_at_local_str` | TEXT | 更新时间（本地时间） |
| `total` | TEXT | 总余额 |
| `available` | TEXT | 可用余额 |
| `unrealised_pnl` | TEXT | 未实现盈亏 |
| `position_margin` | TEXT | 持仓保证金 |
| `order_margin` | TEXT | 挂单保证金 |

**position_history 字段：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | INTEGER | 主键（自增） |
| `timestamp` | INTEGER | 时间戳（NOT NULL） |
| `contract` | TEXT | 合约名称（NOT NULL） |
| `mode` | TEXT | 持仓模式 |
| `size` | TEXT | 持仓数量 |
| `entry_price` | TEXT | 开仓均价 |
| `mark_price` | TEXT | 标记价格 |
| `unrealised_pnl` | TEXT | 未实现盈亏 |
| `leverage` | TEXT | 杠杆倍数 |
| `liq_price` | TEXT | 强平价格 |
| `margin` | TEXT | 保证金 |

**数据保留：** 默认保留 90 天，超期自动清理。

---

## 添加新品种

### 品种命名规则

Gate.io 合约交易对格式：`{基础币}_{计价币}`，如 `BTC_USDT`、`ETH_USDT`。

常见计价币：
- `USDT` — 最常用，几乎所有品种都支持
- `USDC` — 部分品种支持

### 查看可用品种

访问 Gate.io 合约页面查看所有可交易品种：
- 合约列表：[https://www.gate.io/futures](https://www.gate.io/futures)
- 或使用 CLI 查看：`gate-cli cex futures market list`

### Step 1：编辑 watchlist.yaml

在 `symbols` 列表中添加新品种：

```yaml
symbols:
  - name: BTC_USDT
    intervals: [1m, 5m, 15m, 1h, 4h]
  - name: ETH_USDT
    intervals: [1m, 5m, 15m, 1h, 4h]
  - name: SOL_USDT
    intervals: [1m, 5m, 15m, 1h, 4h]
  - name: DOGE_USDT          # 新增 DOGE
    intervals: [1m, 5m, 15m, 1h, 4h]
  - name: XRP_USDT           # 新增 XRP
    intervals: [5m, 15m, 1h, 4h]  # 只订阅部分周期
```

### 配置说明

| 配置项 | 说明 |
|--------|------|
| `name` | 交易对名称，必须与 Gate.io 完全一致 |
| `intervals` | 要订阅的K线周期列表 |

支持的周期：

| 周期 | 说明 | 适用场景 |
|------|------|----------|
| `1m` | 1分钟 | 短线交易、剥头皮 |
| `5m` | 5分钟 | 日内交易 |
| `15m` | 15分钟 | 日内波段 |
| `1h` | 1小时 | 波段交易 |
| `4h` | 4小时 | 中长期趋势 |
| `1d` | 1天 | 长期趋势 |

**数据保留说明：**

| 周期 | 180天保留量 | 限制因素 |
|------|-------------|----------|
| `1d` | 180 根 | `retention_days` |
| `4h` | 1080 根 | `retention_days` |
| `1h` | 2000 根 | `max_candles` 截断 |
| `15m` | 2000 根 | `max_candles` 截断 |
| `5m` | 2000 根 | `max_candles` 截断 |
| `1m` | 2000 根 | `max_candles` 截断 |

### Step 2：重启采集

```bash
python kline_watcher.py
```

启动后自动执行：
1. 为新品种创建 SQLite 表记录
2. 通过 REST API 加载 2000 根历史K线
3. 重新计算所有 EMA20 和 ATR14 指标（确保与 TradingView 对齐）
4. 开始 WebSocket 实时订阅

### Step 3：验证新品种

```bash
# 查看运行状态与数据新鲜度，确认新品种已加载
python status.py --json
# 查询新品种 K 线（本地库，含 ema20/atr14）
python query_kline.py -s DOGE_USDT -i 15m -n 20
```

输出示例（query_kline.py）：
```
DOGE_USDT 15m (本地库) 最新 20 根:
  2026-08-25 10:00   o=0.1820  h=0.1825  l=0.1818  c=0.1823  ema20=0.1821  atr14=0.0004
  ...
```

### 别名映射

符号归一（`BTC` → `BTC_USDT`、`BTCUSDT` → `BTC_USDT`、`AU` → `XAU_USDT`）集中在**库里**
（`omnialpha/gate_client.py` 的 `resolve_symbol` 与它那张别名表），本目录的
`kline_watcher.py` / `quick_order.py` 都**直接 import 同一份实现**：

```python
# pa-data-source/kline_watcher.py（quick_order.py 同理）
from omnialpha.gate_client import resolve_symbol
```

> ⚠️ **不要在本目录再写一份自己的别名表或解析函数。** 这里历史上出现过三份副本，
> 结果同一个写法在不同入口指向不同合约（`BTCUSDT` 在一处被拼成 `BTCUSDT_USDT`），
> 而查询命中 0 行时**只会静默返回空**、不报错。守卫测试
> `tests/test_symbol_identity.py::TestSingleImplementation` 会扫整个目录拦下副本。

要加别名（例如 `DOGE`）就改 `omnialpha/gate_client.py` 里那张表 —— 采集侧与 bot 侧
**同时生效**，不需要两边各改一次。

然后在 `watchlist.yaml` 中可以使用短名称：

```yaml
symbols:
  - name: DOGE             # 等同于 DOGE_USDT
    intervals: [1m, 5m, 15m, 1h, 4h]
```

### 使用 Gate CLI 查询品种

```bash
# 查看合约市场列表
gate-cli info marketdetail get-kline --symbol BTC_USDT --timeframe 1h --limit 1

# 支持的 timeframe: 1m, 5m, 15m, 1h, 4h, 1d
# 支持的 market_type: spot(现货), futures(合约)
```

### 常见品种参考

**主流币种：**
```yaml
- name: BTC_USDT     # 比特币
- name: ETH_USDT     # 以太坊
- name: SOL_USDT     # Solana
- name: BNB_USDT     # 币安币
- name: XRP_USDT     # 瑞波
- name: ADA_USDT     # 卡尔达诺
- name: DOGE_USDT    # 狗狗币
- name: AVAX_USDT    # Avalanche
```

**Meme 币种：**
```yaml
- name: PEPE_USDT    # PEPE
- name: WIF_USDT     # dogwifhat
- name: BONK_USDT    # BONK
- name: FLOKI_USDT   # Floki
```

**商品（支持短别名 `AU`/`AG` 映射到 `XAU_USDT`/`XAG_USDT`）：**
```yaml
- name: XAU_USDT     # 黄金（alias: AU）
- name: XAG_USDT     # 白银（alias: AG）
```

### 注意事项

1. **品种名称必须准确** — 使用错误的名称会导致订阅失败，脚本会打印错误日志
2. **周期选择建议** — 订阅太多周期会增加 API 调用频率，建议只订阅需要的周期
3. **重启生效** — 添加新品种后必须重启脚本，无法热加载
4. **历史数据** — 新品种会自动加载 2000 根历史K线，无需手动导入
5. **存储空间** — 每个品种/周期约占 1-2MB 磁盘空间（2000 根K线）

---

## 工具脚本

| 脚本 | 用途 | 命令 |
|------|------|------|
| `status.py` | 运行状态一键查询（进程/健康/新鲜度/日志错误） | `python status.py [--json]` |
| `query_kline.py` | K线统一查询：本地库优先，非监控品种自动转交易所 | `python query_kline.py -s BTC_USDT -i 15m` |
| `test_purge.py` | 辅助信息滚动缓存清理与去重测试（111 项断言） | `python test_purge.py` |

---

## 常见问题

### Q：启动后数据不更新？

确认 Gate.io API 是否可访问，检查网络连接。脚本会自动断线重连。

### Q：如何切换到现货市场？

修改 `watchlist.yaml` 中 `market_type: spot`，重启即可。现货和合约的 WebSocket 地址会自动切换。

### Q：EMA20 / ATR14 值不准确？

EMA20 使用 TradingView 标准公式 `ta.ema(src, 20)` 计算，前 20 根K线使用 SMA 作为种子值。ATR14 使用 TradingView 标准公式 `ta.rma(ta.tr(true), 14)`（Wilder 平滑法）计算。两个指标均与 TradingView 完全对齐，误差小于 0.01。重启脚本会自动执行指标重算。

### Q：账户推送不工作？

1. 确认 `api_key` 和 `api_secret` 已正确填写
2. 确认 API Key 已开通 **Futures** 权限
3. 确认 `account_push: true` 已设置

### Q：账户数据存储在哪里？

账户数据存储在独立的 `account.db` 数据库中，与 K线数据分离：

- **当前状态**：`balance_current`、`position_current`、`order_current`、`price_order_current` — 实时覆盖，方便 AI 查询
- **历史流水**：`balance_history`、`position_history`、`order_history`、`price_order_history`、`trade_history` — 保留完整记录

默认保留 90 天历史记录。

### Q：如何查看系统运行状态？

1. 使用健康检查端点：`curl http://localhost:18080/health`
2. 查看日志文件：`logs/kline_watcher.log`
3. 使用状态查询：`python status.py --json`
4. CLI 验证数据：`gate-cli info marketdetail get-kline --symbol BTC_USDT --timeframe 1h --limit 5`

### Q：脚本崩溃后如何自动重启？

使用 watchdog.py 守护进程启动脚本，崩溃后 5 秒内自动重启，最多 5 次/小时：

```bash
python watchdog.py
```

### Q：日志文件在哪里？

日志文件位于 `logs/kline_watcher.log`，按日期自动轮转，保留最近 7 天。日志级别可在 `watchlist.yaml` 中配置。

### Q：如何查看实时持仓？

**方式一：终端输出**
脚本运行时，持仓变动会实时打印到终端。定时汇总也会输出当前持仓状态。

**方式二：数据库查询**
```sql
-- 查看所有当前持仓
SELECT contract, size, entry_price, mark_price, unrealised_pnl, updated_at_local_str
FROM position_current;

-- 查看 BTC 最近 10 次订单
SELECT * FROM order_history
WHERE contract='BTC_USDT'
ORDER BY timestamp DESC LIMIT 10;

-- 查看 BTC 最近 10 笔成交
SELECT * FROM trade_history
WHERE contract='BTC_USDT'
ORDER BY timestamp DESC LIMIT 10;

-- 查看实时余额
SELECT total, available, unrealised_pnl, updated_at_local_str
FROM balance_current WHERE id=1;
```

### Q：quick_order.py 和 gate-cli 哪个快？

gate-cli（Go 二进制）平均 ~550ms，quick_order.py（Python 脚本）平均 ~1500ms。gate-cli 快约 2.6 倍，但 quick_order.py 功能更完整（自动识别持仓模式、支持所有订单类型）。

### Q：为什么价格触发订单返回 AUTO_INVALID_REQUEST_BODY？

常见原因：
1. `strategy_type`、`price_type`、`rule` 必须是**整数**，不能是字符串
2. 平仓时 `size` 必须是**整数**，不支持小数
3. 使用 `--close-trigger` 时需要同时指定 `--side` 和 `--size`

---

## 版本历史

### pa-data-source v2.10 (2026-08-29)
- **测试网密钥明文清理（密钥不落盘）**：`GATE_TESTNET_API_KEY`/`GATE_TESTNET_API_SECRET` 迁入用户级环境变量（setx），yaml 明文密钥删除；kline_watcher.py 测试网分支补环境变量优先支持（原仅读 yaml，与 SKILL.md 声明不符的缺口）；迁移后实测账户推送正常（enabled、last_success ≤60s）、技能目录全量扫描无密钥残留
- **测试网 WS 复测仍不通（Gate 服务端问题）**：官方新地址 `wss://ws-testnet.gate.com` TCP 握手即被重置（10054），旧地址 `fx-ws-testnet.gateio.ws` 502 已废弃、`api-testnet.gateapi.io` 404，三候选全挂；watcher ~70s 自动重试，端点恢复后自动切回 WS，无需人工干预
- **实盘 WS 直连探针验证**：`wss://fx-ws.gateio.ws/v4/ws/usdt` 握手成功并收到 kline 推送，1m 数据年龄 14s
- **测试网 REST 轮询提速**：`TESTNET_POLL_INTERVAL` 60s→10s（60s 实测过慢；30 路/周期 ≈3 req/s 远低于限频），双实例健康端点 30 路全部新鲜
- **系统环境启动**：`autostart.vbs` 优先技能目录 `.venv`（系统 Python），脱离 TRAE 进程树，关闭 TRAE 采集不中断
- **重启风暴事故复盘**：fetch_aux 退码 1 连锁 + 双 watchdog 竞态触发 5 次/小时上限停摆；处置 = 清进程树+锁文件后单实例重启。进程表每组件"双进程"是 venv 启动器+真实解释器正常形态

### pa-data-source v2.9 (2026-08-27)
- **trades 复合主键（v8→v9 定向迁移）**：Gate 各合约成交 ID 独立编号，单字段主键 `trade_id` 跨合约同 ID 时 INSERT OR IGNORE 会静默丢数据（潜伏隐患，实测 v8 库五合约区间暂未相交）；迁移为 `(trade_id, contract)` 复合主键，事务包裹失败回滚，实测 805,613 行 100% 保留、time 索引重建、其余 16 表不动
- **账户健康检查（堵监控盲区）**：08-26 夜实盘账户推送静默停更 20h 但健康端点 status=running 的事故根因补丁——健康端点 account 区块新增 `health`（configured/enabled/stale/push_mismatch/last_success_age_s），停滞判定 = 距上次成功 > max(3×interval, 180s)；配置 account_push 但未生效或数据停滞 → status 503
- **日志计数真实性**：trades/xposts"新增 N 条"改用 `total_changes` 差值只计真实插入（旧口径无条件 +1 把 IGNORE 去重也计成新增，虚高）；重启实测每合约真实增量 1~50 条/轮

### pa-data-source v2.8 (2026-08-26)
- **测试网密钥已配置**：测试网密钥写入 `watchlist_testnet.yaml`，`account_push: true` 开启，模拟盘账户推送正常
- **模拟盘订单全类型实测通过**：限价/市价/PostOnly/IOC/FOK/价格触发条件单（触发后市价·限价·标记价·止盈止损）全部验证；FOK 无法全部成交返回 400 属正常拒绝；清理后无残留订单
- **测试网 expiration 兼容**：价格触发订单 `--trigger-expiration` 测试网不支持（400 AUTO_INVALID_PARAM_TRIGGER_EXPIRATION），quick_order.py 加兼容提示（testnet 忽略并提示，实盘保留）
- **稳定性全面测试通过**：双实例 K 线 30 路连续无缺口（1m 新鲜度 47s）、健康端点并发 10×200 请求成功率 100%（p95≈26ms）、watchdog 故障 5s 内整体重启（有防崩溃循环保护）、辅助数据 16 类全绿（orderbook 1s / trades 7.7s 高频新鲜，liquidations 事件驱动缺口正常）

### pa-data-source v2.7 (2026-08-26)
- **测试网（模拟盘）K线数据源**：实盘+测试网双实例同时常驻，同一份 `kline_watcher.py` 代码 `--env` 区分，一比一复刻实盘规范（K线采集/EMA20/ATR14/历史回填/断线补全/REST校准/保留清理/健康检查）
- **数据完全隔离**：测试网独立库 `kline_testnet.db`/`account_testnet.db`、独立健康端口 18081、独立锁文件、独立配置 `watchlist_testnet.yaml`，绝不与实盘共用
- **测试网 REST 轮询主采集兜底**：测试网 WS 官方支持实时推送（`wss://ws-testnet.gate.com/v4/ws/futures/usdt`）但当前端点不可达（10054，2026-08-29 复测仍不通），REST 轮询 10s 间隔保证数据持续新鲜
- **合约存在性校验**：启动时查询当前环境合约列表，不存在的品种（如测试网 XAU/XAG）自动跳过，不阻塞其他品种
- **测试网品种补位**：XAU/XAG 测试网无合约，用 DOGE_USDT/XRP_USDT 两个热门品种补位
- **下单执行独立适配**：`quick_order.py --env testnet` 用独立测试网密钥（GATE_TESTNET_API_KEY）与测试网 REST 地址，操作前打印环境横幅防误下单，实盘/模拟盘配置不混用
- `query_kline.py --env testnet` 查询测试网库；watchdog 双实例守护（kline-live/kline-testnet/fetch-aux）

### pa-data-source v2.6 (2026-08-26)
- 修复健康端点超时：`health_check.py` 由单线程 `HTTPServer` 改为 `ThreadingHTTPServer`（`daemon_threads=True`），根治高写入负载下 `/health` 间歇性卡死（单慢请求阻塞 accept 循环导致后续全部超时）
- 新增 `autostart.vbs`：用户级开机自启脚本（pythonw 无窗口拉起 watchdog，单实例锁防重复）。通用版：技能目录取脚本所在位置、pythonw 动态检测（PATH 优先，python.exe 同目录换算回退），不绑定本机路径；部署方式为 Startup 中放快捷方式指向本脚本（勿复制副本）
- 新增 `requirements.txt` + `install.bat`：Windows 一键安装（自动建 `.venv` 装 pyyaml/websocket-client），与 Linux `server_setup.sh` 对称，实现通用 skill 的轻量分发

### pa-data-source v2.5 (2026-08-25)
- 辅助信息流新增链上数据 + 事件信号（P2）：`info onchain get-token-onchain`（6h，仅 ETH）+ `news prediction search-events`/`get-event-signal`（30min，前 5 事件）
- `aux_cache.db` schema 升级 v8：新增 `onchain_ts`（(fetched_ts, token) 复合主键，TTL 30d）与 `event_signals`（(event_ref, window) 唯一，TTL 7d）
- 实施修正：BTC/SOL 链上查询实测返回与 ETH 相同的总览（token 参数对原生币不生效），故仅采集 ETH；`search-events --category crypto_price` 过滤加密预测事件（避免默认 Polymarket 政治数据）
- `aux_monitor.py` 一致性检查新增 onchain（时序表偏差校验）/event_signals（事件型仅校验有数据）；`test_purge.py` 扩至 111 项断言

### pa-data-source v2.4 (2026-08-25)
- 辅助信息流新增币种基本面 + 技术面情报（P1）：`info coin get-coin-info`（6h，BTC/ETH/SOL）+ `info markettrend get-technical-analysis`（30min，5 合约）
- `aux_cache.db` schema 升级 v7：新增 `coin_info_ts`（(fetched_ts, symbol) 复合主键，TTL 30d）与 `tech_analysis_ts`（(fetched_ts, symbol) 复合主键，TTL 7d）
- 实施修正：XAU/XAG 为贵金属，get-coin-info 实测返回无关 meme 代币故不采集；同 symbol 多条目优先 CEX 主条目
- `aux_monitor.py` 一致性检查新增 coin_info/tech_analysis；`test_purge.py` 扩至 100 项断言

### pa-data-source v2.3 (2026-08-25)
- 辅助信息流新增情报数据（P0）：`info coin get-coin-rankings`（15min，5 类榜×10）+ `news feed get-exchange-announcements`（15min，binance 公告）
- `aux_cache.db` schema 升级 v5：新增 `coin_rankings_ts`（(fetched_ts, ranking_type, symbol) 复合主键，TTL 7d）与 `exchange_announcements`（notice_id 唯一，TTL 7d）
- 实施修正：`new_listing` 榜结构不同（无 symbol）已由公告接口覆盖故不采集；`--platform gate` 实测返回空改用 binance
- `aux_monitor.py` 一致性检查新增 rankings/announcements；`test_purge.py` 扩至 84 项断言

### pa-data-source v2.2 (2026-08-25)
- 辅助信息流新增社区舆情数据（P1）：`news feed search-ugc` 多平台社交帖（gate/twitter/telegram/reddit），覆盖 5 合约品种，30min 采集
- `aux_cache.db` schema 升级 v6：新增 `social_posts_ts` 表（post_id 唯一，含逐帖情绪/质量档）；TTL 3d
- 实施修正：原方案 `search-x` 实测 items 恒空（xAI 路径只回聚合摘要无原文），改用 `search-ugc` 拿真实帖子原文 + 逐帖情绪
- 查询词映射 `XPOST_QUERIES`：`BTC Bitcoin`/`ETH Ethereum`/`SOL Solana`/`XAU gold`/`XAG silver`（XAU/XAG 用 gold/silver 提升召回）
- `aux_monitor.py` 一致性检查新增 social 项；`test_purge.py` 扩至 90 项断言（social 落库/post_id 去重/跨币去重）

### pa-data-source v2.1 (2026-08-25)
- 辅助信息流新增合约市场结构数据：盘口（orderbook 5s）/ 成交（trades 5s）/ 爆仓（liquidations 5m）/ 持仓量·多空比（stats 1h），覆盖 5 个合约品种（BTC/ETH/SOL/XAU/XAG USDT）
- `fetch_aux.py` 循环粒度降为 5s（`LOOP_INTERVAL`），维护操作按 `MAINT_INTERVAL=60s` 门控；`fetch_schedule.json` 频率门控拦截低频接口，5s 循环不空转
- `aux_cache.db` schema 升级 v4：新增 `market_stats_ts`/`liquidations`/`orderbook_snap`/`trades` 4 表；TTL 分层（orderbook 1h / trades 24h / liquidations 24h / stats 90d）
- 去重：trades 按 `trade_id`、liquidations 按 `time+contract`、orderbook/stats 按 `(fetched_ts, contract)` 复合主键
- `aux_monitor.py` 一致性检查扩展 4 项（liquidations 为事件型表，无爆仓时段表空属正常）；`test_purge.py` 扩至 73 项断言
- 修复调度 bug：`_due` 判断移到合约循环前、`sched` 更新移到循环后，避免首合约触发后其余品种被误判未到点

### pa-data-source v2.0 (2026-08-25)
- 按技能规范重构 SKILL.md：明确节点职责（节点1=数据保障）、强制原则、契约与门禁；原版见 `SKILL.md.bak-20260825`
- 新增辅助信息流旁路 `fetch_aux.py`（5 接口：新闻事件/市场总览/宏观日历/市场情绪/交易所储备，频率调度 + 滚动缓存 + 按级别清理）
- watchdog 升级为多目标守护：同时守护 `kline_watcher.py` + `fetch_aux.py --loop`，整体重启
- 新增 `aux_monitor.py` 一致性判定 + `status.py` 辅助流监控 + 健康端点 `aux_info`（与 K 线同等级可观测）
- 辅助信息流设计文档：`aux-data/DESIGN.md` + `aux-data/README.md`

### V6 (2026-06-07)
- 清空数据库后全新重建，稳定运行超 24 小时验证通过
- 全面测试：进程守护、数据库结构、符号映射、数据覆盖均正常
- 新增 `launcher.pyw` GUI 启动器，支持桌面双击启动
- 新增 `launcher.vbs` VBScript 入口（相对路径，解压到任意目录可用）
- 守护进程使用 `sys.executable` + `CREATE_NO_WINDOW`，避免 console 窗口
- 新增 launcher.pyw 单实例锁（TCP 端口 18081），防止重复启动
- 新增 `check_db.py` 数据库结构检查工具
- 打包包含 `gate-cli.exe` 备用数据源

### V5 (2026-06-05)
- 修复断线重连周期遗漏：补全 1m/1d 间隔
- SQLite 连接增加 `timeout=30` 防并发超时
- 状态表新增 `updated_at_local_str` 本地时间字段
- 验证 quick_order.py 符号映射（XAU/XAG/AU/AG）

### V4 及之前
- 初始版本：K线采集、账户监控、EMA20/ATR14 计算
- 断线重连、历史数据回填、watchdog 守护进程
- quick_order.py 快速下单工具
