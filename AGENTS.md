# AGENTS.md — 给人 / AI 的系统入口

读完本文件应能独立使用 **gate-signal-bot**（写配置 → 投信号 / 跑 LLM 策略 → 下单）。细节再按「文档地图」下钻。

---

## 1. 这是什么

Gate.io **永续合约**信号执行 + LLM 策略层 monorepo：

```text
pa-data-source（可选，独立进程）──写──► kline.db（只读接缝）
                                            │
人 / AI 策略 ──JSON──► inbox/<bot_id>/ ──► gate_bot
                                            │  解析 → 风控 → Gate 下单
LLM strategist（可选）──Plan JSON──┘        │
                                            ▼
                                    logs/trades/*.jsonl + archive/
```

| 目录 | 职责 |
|------|------|
| `gate_bot/` | 信号执行、LLM 策略、风控、日志 |
| `pa-data-source/` | 行情采集（可选；缺库自动 REST） |
| `contracts/` | `kline.db` schema（两组件唯一接缝） |
| `config/bots/` | 每个机器人一份 yaml |
| `inbox/<bot_id>/` | 信号入口（丢 JSON 即执行） |
| `prompts/` | 可替换的策略人格 |
| `templates/` | 信号字段模板 |
| `examples/` | 可复制的信号 / 配置案例 |
| `scripts/` | 测试与上线前矩阵 |

**核心心智模型**：合法 JSON 进 inbox → **无 dry-run，直接下单**。约束靠 yaml 风控 + 程序校验，不靠模型自觉。

---

## 2. 五分钟上手

```powershell
# 1) 环境
.venv\Scripts\python.exe -m pip install -r requirements.txt   # 一般已备好
$env:GATE_TESTNET_API_KEY = "..."
$env:GATE_TESTNET_API_SECRET = "..."

# 2) 复制机器人配置
Copy-Item config\bots\_example.yaml config\bots\mybot.yaml
#    改 bot_id / env: testnet / symbols / max_notional_usd / label_prefix

# 3) 写一个信号（或抄 examples/signals/01-open-long-tpsl.json）
#    inbox\mybot\20260101-120000-demo.json

# 4) 单次执行
.venv\Scripts\python.exe -m gate_bot once --bot mybot

# 5) 看结果
.venv\Scripts\python.exe -m gate_bot status --bot mybot
#    成功：archive/done/；失败：archive/failed/*.error.json
```

LLM 策略另需 `OPENAI_BASE_URL` / `OPENAI_API_KEY`，然后：

```powershell
.venv\Scripts\python.exe -m gate_bot plan --bot mybot     # 单轮 Plan
.venv\Scripts\python.exe -m gate_bot plan-loop --bot mybot  # 常驻策略
.venv\Scripts\python.exe -m gate_bot run --bot mybot        # 常驻执行
```

**建议路径**：`testnet` 小信号 → `prelaunch_runner` 全矩阵 → `live` 且 `max_notional_usd ≤ 10`。

---

## 3. 关键规则（写代码 / 写信号前必读）

1. **仓位优先 `size_usd`（名义 USDT）**；`size` 是合约张数，各币 1 张名义不同，用前查快照 `contract.min_notional_usd`。
2. **突破进场用 `stop_entry_*`，止损保护用 `sl`**，禁止互换。
3. **开仓必须带 `sl`**（除非 `require_sl: false`）。
4. **密钥只进环境变量**，不写 yaml、不进 git。
5. **多 bot 隔离靠 `label_prefix`**（订单 text `t-<label>`），是命名空间不是鉴权。
6. **`type=limit` 必须给 `price`**；市价遇偏离自动回退「公允价 IOC」。
7. **trail 追踪单搁置**（需资金密码）。
8. **Plan JSON 仓位字段只有 `size_usd` / `size`**；`size_pct`/`margin_pct` 是外部信号用的。

---

## 4. 文档地图（按问题找文档）

| 你想知道 | 读这个 |
|----------|--------|
| **10 分钟图文教程（推荐先做）** | `docs/TUTORIAL-10min.md` |
| **如何添加新机器人（照抄清单）** | `docs/HOWTO-add-bot.md` |
| **持续运行 / 7×24 运维** | `docs/OPERATIONS.md` |
| LLM 供应商配置 | `config/providers.yaml` + `docs/compose/spec/llm-provider-registry.md` |
| 总览 / 配置 / 运行 | **`README.md`** |
| 信号字段全集、示例 | `templates/README.md` |
| 止损 vs 突破 | `templates/STOP-ENTRY-vs-STOP-LOSS.md` |
| 怎么写策略人格 | `prompts/README.md` + `prompts/vergex_default.md` |
| 可抄信号案例 | `examples/README.md`（12 个 signals + bots） |
| LLM 策略层设计 | `docs/compose/spec/llm-strategist.md` |
| 行情 hybrid / 指标 | `docs/compose/spec/market-data-hybrid.md`、`docs/reference/indicator-support-matrix.md` |
| AI 自设触发 | `docs/compose/spec/ai-event-triggers.md` |
| TP/SL 归属与挂法 | `docs/compose/spec/tpsl-ownership-revamp.md` |
| 上线前怎么测 | `docs/compose/spec/prelaunch-test.md` + `scripts/prelaunch_runner.py` |
| kline.db 格式 | `contracts/KLINE_SCHEMA.md` |
| pa 数据管道 | `pa-data-source/README.md` |
| 金额 / 张数换算 | README「张数换算」 |

---

## 5. 常用命令

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests
.venv\Scripts\python.exe -m gate_bot status
.venv\Scripts\python.exe -m gate_bot once --bot <id>
.venv\Scripts\python.exe -m gate_bot plan --bot <id>
# 多 bot 托管 / 迁移
.venv\Scripts\python.exe -m gate_bot migrate
.venv\Scripts\python.exe -m gate_bot supervisor --bot brooks-btc
# 无窗口启动
.\scripts\start_brooks_btc_bg.bat
.venv\Scripts\python.exe scripts\prelaunch_runner.py --phase readonly --env testnet
```

---

## 5b. LLM 与工具

**供应商**：`config/providers.yaml` 配一次；bot 只写 `llm.provider`。

```yaml
llm:
  provider: gw-flash          # 或 deepseek-official
  reasoning_effort: high      # 可选覆盖
```

| 项 | 默认 |
|----|------|
| 模型 | `deepseek-flash` / 网关 `global:deepseek-v4.1-flash` |
| 思考 | 可开；`reasoning_effort: high/max` |
| 落盘 | 每轮 **`state/*.thinking.json`**（思维链 reasoning_content） |
| 账户 | **REST**：余额+持仓+open_orders+TP/SL（全 bot） |

**20 个工具**（原生 function calling）：

- 行情：`klines` `indicators` `ticker` `orderbook` `contract` `stats` `account` `smc_map` `smc_events` `sqzmom`
- aux：`trades_flow` `liquidations` `market_stats` `tech_analysis` `coin_info` `onchain` `social` `overview` `sentiment` `macro`

**两套 SMC 并存（按作用命名）**：
- `smc_map`（市场地图 → 在哪/往哪）：swing/internal 双周期趋势、Premium/Discount 估值区、EQH/EQL 关键位、OB/FVG 区域
- `smc_events`（结构事件 → 发生了什么/何时动手）：枢轴 BOS/CHoCH 事件流、流动性扫荡(x)、OB+Breaker+活动、FVG+突袭
- `sqzmom`（Squeeze Momentum）：BB/KC 挤压状态 + linreg 动量

**指标 23 族**（`indicators` 工具，周期任意）：EMA/SMA/RMA/WMA/VWMA、ATR（Pine 平滑 rma/sma/ema/wma）、RSI（含平滑+BB）、MACD（EMA/SMA）、BOLL（SMA/EMA/RMA/WMA/VWMA 基线）、Stoch、CCI、Williams %R、MFI、ADX、VWAP、OBV、SuperTrend、SQZMOM、pine_ema 套件。

取数与 `klines` 同源：hybrid 时优先本所 `kline_<ex>.db`，否则走**该所** REST（`exchange:` 决定数据源）。

**多所采集**：`pa-data-source/kline_watcher_multi.py` 写 `kline_<ex>.db`（schema 与 Gate 一致）；WS 实时（`ws_venues.py`）+ REST 兜底/补全。本机网络下 **Gate + Hyperliquid** WS 可用，binance/bybit 被墙、okx/bitget TLS 重置 → 自动降级 REST 轮询（日志标明 `ws`/`rest`）。

**品种（对标 Gate，每所 5 个）**：
| | Gate | 其余五所 |
|---|---|---|
| 主流 | BTC / ETH / SOL | BTC / ETH / SOL |
| 补位 | XAU / XAG（金银，Gate 独有） | **DOGE / XRP**（六所都有） |

**周期**：六所统一 `1m / 5m / 15m / 1h / 4h / 1d`（与 Gate `watchlist.yaml` 一致）。

**任意币 REST**：六所均可取任意已上线合约（不限 watchlist）——AI 工具 `klines/indicators/smc_*` 直接传 symbol 即可。个别币缺是**上币差异**（如 binance 无 PEPE 合约、bitget 无 TON），非 REST 限制。

**本地加币（随时，对标 Gate）**：
- 五所：`kline_watcher_multi.py --symbols "新币_USDT" --intervals "1m,5m,15m,1h,4h,1d" --once` 即时入库；长期监控改 watchdog args
- Gate：改 `watchlist.yaml` → 重启 `kline_watcher`（30–60s 回填）
- 加库后 hybrid 立即 `source=local`；不加则自动 REST（`degraded=['local_db']`）

**注意**：命令行周期参数必须引号包裹（`--intervals "1m,...,1d"`），否则 PowerShell 逗号解析会把 `1d` 截成 `1`；采集器已加周期白名单校验兜底。


**本地模拟盘（paper）**：复刻交易所语义的本地模拟交易。env: paper 即启用，行情/费率/合约元数据委托绑定的真实所，订单/持仓/资金/强平/费率结算全在本地 data/bots/<id>/paper/account.db。

- 命令：python -m gate_bot paper-run --bot <id>（独立进程 + 撮合 tick 线程）
- 配置（bot yaml paper: 段）：eed_exchange（绑定行情所）、initial_capital（默认 10000）、leverage（默认 20）、ee_rate、unding_enabled、position_mode、margin_mode、price_band_pct
- 成交价：**盘口价**（买→ask 卖→bid）；订单全类型（limit/market/stop_entry/TP-SL 触发/GTC/IOC/FOK/PO）
- 强平：按初始/维持保证金反推强平价，触及即强制平仓
- 资金费率：每 8h 取真实费率对持仓结算
- 精度校验：tick/lot/最小名义/价格带/杠杆上限，拒绝原因对齐交易所

**多人格共管**：N 人格（brooks/smc/scalper…）共管订单。各人格独立分析 → 融合（`weighted_vote`/`master_arbiter`/`consensus`）→ 按拓扑执行（`single_account` 去重 / `mirror_accounts` 同步）。共同记忆 `data/shared/orders/<order_id>.json`。`python -m gate_bot persona-run --group <name>`。

**信号广播**：一信号 → 多所，目标在 `config/broadcast.yaml`（AI 碰不到）。`python -m gate_bot broadcast` 常驻分发；可选 1/2/N 个目标；逐个校验写入、失败报错；目标 bot 各自独立执行。信号内 `targets` 字段忽略（防 AI 注入）。

**账户信息一律 REST**（全 bot 通用）；`account.db` 仅历史。K 线可 hybrid。

---

## 5c. 无窗口运行（Windows）

| 做法 | 说明 |
|------|------|
| **默认终端** | 设置为「Windows 控制台主机」（防 Windows Terminal 套壳弹窗） |
| **启动** | `scripts\start_brooks_bg.cmd` / `run_brooks_bg.vbs` |
| 子进程 | `pythonw` + `CREATE_NO_WINDOW` |
| Linux | systemd，无窗口问题 |

---

## 6. 验证习惯

- 改代码：`unittest`（约 173 项）。
- 改下单 / 风控：`prelaunch_runner --phase orders --env testnet`。
- 改 LLM/工具：`scripts/_verify_effect.py`；看 thinking：`data/bots/<id>/state/*.thinking.json`。
- 上线：`prelaunch --phase live` 小额闭环后再加大额度。

---

## 7. 边界与风险

- 无 dry-run：合法信号会真实下单。
- LLM 输出只是 Plan，**最终约束在 yaml 风控 + 执行器**。
- 不要把不可信 JSON 直接丢进生产 `inbox/`。
- 实盘先小额；密钥不要给提币权限。

---

## 8. 换机器 / 装到服务器（可移植性）

**代码不绑本机路径**，换目录/换 OS 只需处理环境与启动方式。

| 项 | Windows | Linux / 服务器 |
|----|---------|----------------|
| Python | `.venv\Scripts\python.exe` | `python3` 或 `.venv/bin/python` |
| 装依赖 | `pip install -r requirements.txt` | 同左（建议 venv） |
| 项目根 | 当前目录或 `--root` | `--root /opt/gate-signal-bot` 或 **`GATE_BOT_ROOT`** |
| 行情库 | 默认 `<root>/pa-data-source/data` | 可用 **`GATE_BOT_PA_DATA`** 改到数据盘 |
| 密钥 | 终端 `$env:...` / 系统环境变量 | systemd `Environment=` 或 `.env` 由外部注入 |

```bash
# Linux 服务器示例
export GATE_BOT_ROOT=/opt/gate-signal-bot
export GATE_API_KEY=...
export GATE_API_SECRET=...
export GATE_BOT_PA_DATA=/data/gate-kline   # 可选

cd /opt/gate-signal-bot
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
pip install -e .          # 任意目录都能 python -m gate_bot
python -m gate_bot status --root "$GATE_BOT_ROOT"
python -m gate_bot run --root "$GATE_BOT_ROOT"
```

systemd 建议：`WorkingDirectory=/opt/gate-signal-bot`，并设 `GATE_BOT_ROOT` 与密钥环境变量；或 `ExecStart=/opt/gate-signal-bot/.venv/bin/python -m gate_bot run --root /opt/gate-signal-bot`。

**相对路径规则**（代码已按此实现）：
- 配置：`<root>/config/bots/`
- 信号：`<root>/inbox/<bot_id>/`
- 策略人格：`<root>/prompts/`（`prompt_file` 不依赖 cwd）
- 行情：`GATE_BOT_PA_DATA` 或 yaml `pa_data_root` 或 `<root>/pa-data-source/data`

自检：**在任意 cwd 下**执行 `python -m gate_bot --root /path/to/repo status` 应正常。
