# AGENTS.md — 给人 / AI 的系统入口

读完本文件应能独立使用 **OmniAlpha**（写配置 → 投信号 / 跑 LLM 策略 → 下单）。细节再按「文档地图」下钻。

---

## 1. 这是什么

Gate.io **永续合约**信号执行 + LLM 策略层 monorepo：

```text
pa-data-source（可选，独立进程）──写──► kline.db（只读接缝）
                                            │
人 / AI 策略 ──JSON──► inbox/<bot_id>/ ──► omnialpha
                                            │  解析 → 风控 → Gate 下单
LLM strategist（可选）──Plan JSON──┘        │
                                            ▼
                                    logs/trades/*.jsonl + archive/
```

| 目录 | 职责 |
|------|------|
| `omnialpha/` | 信号执行、LLM 策略、风控、日志 |
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
.venv\Scripts\python.exe -m omnialpha once --bot mybot

# 5) 看结果
.venv\Scripts\python.exe -m omnialpha status --bot mybot
#    成功：archive/done/；失败：archive/failed/*.error.json
```

LLM 策略另需 `OPENAI_BASE_URL` / `OPENAI_API_KEY`，然后：

```powershell
.venv\Scripts\python.exe -m omnialpha plan --bot mybot     # 单轮 Plan
.venv\Scripts\python.exe -m omnialpha plan-loop --bot mybot  # 常驻策略
.venv\Scripts\python.exe -m omnialpha run --bot mybot        # 常驻执行
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
9. **币种由 `symbols` 声明**（可多个、可随时改）：它同时是**执行白名单**与工具的【品种宇宙】。多币下工具/触发器漏写 `symbol` 会被**拒绝**（不猜币），单币自动补；`strategist.symbols` 必须 ⊆ `symbols`。`symbols: []` 必须配 `symbols_unrestricted: true` —— 空白名单等于「任意币可开仓 + 零守护」。

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
.venv\Scripts\python.exe -m omnialpha status
.venv\Scripts\python.exe -m omnialpha once --bot <id>
.venv\Scripts\python.exe -m omnialpha plan --bot <id>
# 多 bot 托管 / 迁移
.venv\Scripts\python.exe -m omnialpha migrate
.venv\Scripts\python.exe -m omnialpha supervisor --bot brooks-btc
# 无窗口启动
.\scripts\start_brooks_btc_bg.bat
.venv\Scripts\python.exe scripts\prelaunch_runner.py --phase readonly --env testnet
# 回测（journal 回放，PnL/Sharpe/DSR）
.venv\Scripts\python.exe -m omnialpha backtest --bot <id> --days 30
# 进程看门狗（挂了自动拉起 + 飞书通知）
.\scripts\start_watchdog_bg.bat
.venv\Scripts\python.exe -m omnialpha watchdog --interval 15
```

---

## 5a. 告警与守护（运维必读）

| 能力 | 入口 | 说明 |
|------|------|------|
| **告警落盘** | `data/bots/<id>/state/alerts.json` | 权益偏离/重复成交/孤儿保护单，自动写入 |
| **成交推送** | 飞书彩色卡片 | 开/止盈/止损/平/减仓/改单；`config/alerts.yaml` 或 `FEISHU_*` 环境变量 |
| **进程看门狗** | `python -m omnialpha watchdog` | 守护 `enabled: true` 的 bot + **显式 opt-in** 的 persona 组，挂了补拉 |
| **单实例锁** | `state/plan.lock` / `run.lock` | OS 文件锁（msvcrt/flock），防 PID 复用/孤儿双开 |

**bot 开关 = 唯一在管判据**：

```yaml
# config/bots/xxx.yaml
enabled: true    # 看门狗守护、supervisor 拉起
enabled: false   # 一律不管，绝不凭空开
```

**组级进程的守护是另一套开关**（`config/persona_groups.yaml`）。`enabled` 只表示
「这份组配置有效」，本机 9 个组都是 true —— 按 `enabled` 守护会让本地一启动
watchdog 就拉起 9 个 persona-run（每个都在跑真实 LLM 分析）：

```yaml
  - name: eth-disc
    enabled: true
    runtime: {persona_run: true}   # ← 只有显式写这行的组才被 watchdog 守护
```

`broadcast` 只在 `config/broadcast.yaml` 里**至少有一条 `enabled: true` 的路由**时才守护。

**告警类型**（`omnialpha.monitoring`）：

| type | 触发 | 落盘 |
|------|------|------|
| `equity_deviation` | 权益相对日初偏离 >10% | alerts.json |
| `equity_deviation_halt` | 权益**向下**偏离越过 `account_risk.equity_deviation_halt_pct` → 写熔断标记 | alerts.json + halt.json |
| `dup_fill` | 同一 order_id 重复成交 | alerts.json |
| `orphan_protector` | 保护单异常，两种情形共用这个 type：① **平仓后遗留**的 reduce-only SL/TP（`_cleanup_orphan_protectors`）；② **张数超额**——保护单张数多于「持仓 + 待成交入场单」，对账撤掉多余那批（`reconcile_protectors`，文案是「保护单张数超额已对齐」） | alerts.json |
| `plan_fail` | plan 周期连续失败（LLM/解析），每 `error_warn`(5) 次一条 | alerts.json + 飞书 |
| 成交卡片 | 开/平/减仓/改保护 | 飞书 |
| 衰减 | 滚动胜率/Sharpe 跌破阈值 | 飞书 |
| 进程事件 | 看门狗重启/停手 | 飞书 |

**权益熔断**（`account_risk.equity_deviation_halt`，**逐 bot 配、默认关**）：
`false`（默认）/ `"dry"`（只记日志）/ `true`（写 `halt.json`，当日有效、跨日自动复位）。
与 `daily_loss_limit_usd` 的区别是**比例 vs 绝对值** —— 小资金账户配不出有意义的
绝对额（$20 对 $84 权益是 24%），比例口径才能表达「亏一成停手」。只在向下偏离时触发。

**峰值回撤保护**（`account_risk.peak_trail`，**逐 bot 配、默认关**）—— 防坐电梯：

```yaml
account_risk:
  peak_trail: true        # false（默认）/ "dry"（只记「会挂哪」）/ true
  peak_trail_atr: 1.5     # 距**价格峰值**回撤 k×ATR 时把 SL 上移（必须 > 0）
  peak_trail_atr_period: 14   # 可选，ATR 回看根数（1h 周期），默认 14
```

为什么需要它：**止损挂上去就不动了** —— 它锚定的是挂单那一刻给定的价，价格涨上去
再跌回来它管不了，那正是「坐电梯」。交易所侧的移动止盈（`trail`）在本项目搁置
（需资金密码），所以这是防坐电梯**唯一**的程序化手段。

口径是**逐仓 + 价格峰值**（只上不下），与入场价解耦 —— 所以浮盈仓和浮亏仓一视同仁，
也不受出入金 / 其他 bot 已实现盈亏干扰。真动手时**先挂新、再撤旧**（挂失败时旧 SL
还在，不会裸仓）。三条红线只报不挂：目标不比现有 SL 更保守（`sl_not_better`）、
目标落在 mark 非法侧即回撤已发生（`trail_breached`）、没有本 bot 的 SL（`no_owned_sl`）。
峰值状态落 `data/accounts/<name>/state/peak_trail.json`（无 `account:` 时落 bot 目录），
**持仓消失 / 方向反转 / 入场价变化 / 观测间隔 > 900s** 四种情况重置。
挂在 `run` 的 300s 扫描循环里（不是 LLM 轮次上 —— 浮盈回吐不等人）。

`health.json` 的 `error_streak` **跨实例落盘**（`record_error()` 每次续算，不是实例内计数），
所以每轮新建 `HealthMonitor(root, bot_id)` 也能累计；`llm_latency` 取 LLM 调用真实耗时。

读告警：

```python
from omnialpha.monitoring import read_alerts
read_alerts(root, "brooks-btc")              # 全部
read_alerts(root, "brooks-btc", "dup_fill")  # 按类型
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

**36 个工具**（原生 function calling）：

- 行情：`klines` `indicators` `ticker` `orderbook` `contract` `stats` `account` `smc_map` `smc_events` `sqzmom` `taker_delta`
- aux：`trades_flow` `liquidations` `market_stats` `tech_analysis` `coin_info` `onchain` `social` `overview` `sentiment` `macro`
- 订单流（pa-data-source 实采）：`orderflow_tape` `orderflow_footprint` `orderbook_state` `orderbook_walls`
- skill：`skill` `skill_ref`（SkillKit）
- 记忆：`journal_lookup`（按 `cycle_id` 取回某轮决策的 decision/reasoning/执行结果；prompt 里的 `[近期决策索引]` 靠它兑现——索引只给一行编号，细节按需取，所以近况段体积不随历史增长）
- **TV 指标**：`tv_linreg_trendlines` `tv_rsi_yata` `tv_lr_ha_candles` `tv_delta_flow_profile` `tv_oi_visible_range` `tv_vol_oi_footprint` `tv_cdv` `tv_wyckoff`

**SkillKit（可安装 skill）** —— 三级渐进披露：

| 层 | 机制 | 说明 |
|----|------|------|
| **L1** | `<skill_catalog>` 注入 system prompt | 只含 name + description（含使用提示），body 永不预载 |
| **L2** | `skill(name)` 工具 | 载入 SKILL.md 正文，本回合生效 |
| **L3** | `skill_ref(name, path)` 工具 | 按需读 `references/`/`scripts/`/`assets/`（realpath 遏制） |

```powershell
python -m omnialpha skill validate <dir>      # 校验（E01-E10 拒装）
python -m omnialpha skill install <dir>       # 安装（不自动启用）
python -m omnialpha skill list [--bot ID]     # 列出 + 启用状态
python -m omnialpha skill show <id>           # 元数据
python -m omnialpha skill remove <id> --yes   # 卸载
python -m omnialpha skill run <id>            # 用户专属 skill 的 CLI 入口
```

启用：bot yaml `skills: [id]`（`[]` = 无 skill；缺省 = 全部可见）。
skill 可声明 `allowed-tools`（激活后**收窄**工具面）、`model-invocation: false`（模型不可自主触发）。

**红线**：skill 只影响 Plan 的观点与理由；`allowed-tools` 只能收窄；执行闸门永远在 executor + yaml 风控。详见 `docs/HOWTO-add-skill.md` 与 `docs/compose/spec/skillkit.md`。

**两套 SMC 并存（按作用命名）**：
- `smc_map`（市场地图 → 在哪/往哪）：swing/internal 双周期趋势、Premium/Discount 估值区、EQH/EQL 关键位、OB/FVG 区域
- `smc_events`（结构事件 → 发生了什么/何时动手）：枢轴 BOS/CHoCH 事件流、流动性扫荡(x)、OB+Breaker+活动、FVG+突袭
- `sqzmom`（Squeeze Momentum）：BB/KC 挤压状态 + linreg 动量

**指标 26 族**（`indicators` 工具，周期任意）：EMA/SMA/MA/RMA/WMA/VWMA/HMA/KAMA/ALMA/T3/LSMA、Linreg/Linreg Channel、ATR（Pine 平滑 rma/sma/ema/wma）、RSI（含平滑+BB）、MACD（EMA/SMA）、BOLL（SMA/EMA/RMA/WMA/VWMA 基线）、Stoch、CCI、Williams %R、MFI、ADX、VWAP、OBV、SuperTrend、SQZMOM、pine_ema 套件。

**TV 指标 8 套**（独立工具，Pine 原版移植并已对齐验证）：`tv_linreg_trendlines`（Linreg & Trendlines：3 层回归通道 + 枢轴趋势线）、`tv_rsi_yata`（RSI Yata：平滑 RSI + MA + BB + RSI 蜡烛 + OB/OS + 直方图 + RSI-MACD + HH/HL/LH/LL）、`tv_lr_ha_candles`（LR HA Candles：线性回归 Heikin-Ashi + T3 + ATR 波动带）、`tv_delta_flow_profile`（Delta Flow Profile：逐价位资金流/Delta + POC 迁移）、`tv_oi_visible_range`（OI Visible Range：持仓量四象限 + 价位分布）、`tv_vol_oi_footprint`（Volume/OI Footprint：逐价位买卖足迹）、`tv_cdv`（Cumulative Delta Volume：K 线几何估算累积 Delta）、`tv_wyckoff`（Wyckoff [theUltimator5]：A→E 五阶段状态机 + 15 个 Wyckoff 事件 + 结构置信度/验证双评分 + `next`「还差什么条件」+ `checks` 门槛明细）。

`tv_wyckoff` 的输入是**纯 OHLCV**，内部固定喂 ≥500 根（`limit` 低于 500 会被抬到 500），不移植原版的 `request.security` 多周期扫描——要跨周期就让模型自己换 `tf` 多次调用。`phase: null` 表示当前没有活跃战役（原版指标此时显示 "Searching for SC / BC"），是正常状态。

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

- 命令：python -m omnialpha paper-run --bot <id>（独立进程 + 撮合 tick 线程）
- 配置（bot yaml paper: 段）：
eed_exchange（绑定行情所）、initial_capital（默认 10000）、leverage（默认 20）、
ee_rate、
unding_enabled、position_mode、margin_mode、price_band_pct
- 成交价：**盘口价**（买→ask 卖→bid）；订单全类型（limit/market/stop_entry/TP-SL 触发/GTC/IOC/FOK/PO）
- 强平：按初始/维持保证金反推强平价，触及即强制平仓
- 资金费率：每 8h 取真实费率对持仓结算
- 精度校验：tick/lot/最小名义/价格带/杠杆上限，拒绝原因对齐交易所

**多人格共管**：N 人格（brooks/smc/scalper…）共管订单。各人格独立分析 → 融合（`weighted_vote`/`master_arbiter`/`consensus`）→ 按拓扑执行（`single_account` 去重 / `mirror_accounts` 同步）= **2 拓扑 × 3 融合 = 6 种组合**。

- **讨论模式**（3 阶段，可选）：`discussion.enabled: true` + `rounds: 3` → ①相互讨论与反驳 ②深化讨论 ③最终决策；`early_exit_on_agreement` 控制一致即退；改口落盘 `data/shared/discussion_log.jsonl`
- **稳定性（不掉票）**：LLM 5xx/网络错误**指数退避重试 3 次**；重试仍失败或解析失败 → **降级 hold**（保留投票权）；`analyze_once` 异常也降级；**账户缺失继续行情分析**；无工具调用**强制重试**
- **工具审计**：每轮 `tool_usage` + `tool_usage_summary` 随 `thinking.json` 落盘（防偷懒）
- **事件留痕**：`data/shared/persona_log.jsonl`（`_log`，每行带 `ts`/`group`）—— 含 `decision`、
  `hold`、`symbol_rejected`（**越界 chip 被拒**：`meta.reason` 是 `symbol_not_in_universe` /
  `symbol_missing`，另有 `corrected_from` 与 `universe`）、`analyze_failed`。
  它**不是** `alerts.json` 的告警类型（被拒不等于要告警），查"这轮为什么没下单"看这里。
- 共同记忆 `data/shared/orders/<order_id>.json`。`python -m omnialpha persona-run --group <name>`

**记忆系统**（agent-memory）：四层记忆（Order/Journal/Profile/Working）。订单上下文含 reason/lifecycle/recent_events(top-5)/memory_refs/invalidation，**匹配按路径区分**：单 bot（`plan-loop`）只看 `target_account == 自己`，人格（`persona-run`）看全组 `members`（组内共管同一张单）；**方向反转时另起一张单**（旧单关闭），否则注入的持仓方向会与账户相反。**单 bot 也写**订单库（`PlanRunner._sync_order_memory`，按 symbol 建/复用/关闭），所以 `[订单上下文]` 对 `plan-loop` 同样成立；订单记忆写失败只告警、绝不影响下单。决策日志 append-only 事件溯源（`state/memory_journal.jsonl`，含 `snapshot_digest`/`llm_model`/`prompt_cache_hit_tokens`，**hold 轮也记**）。策略画像（`state/memory_profile.json`）的**核心统计由 paper 账本投影**（`fills.realised_pnl`）—— 覆盖**全部**平仓（含 SL/TP 触发：交易所侧成交、没有信号）与**全部** bot；`by_action`/`best_act` 由平仓事件尽力而为，无账本（live）时退回文件累加值。上下文由 `build_context` 按缓存顺序组装（稳定前缀 → 变化值殿后）。遗忘按 24h 间隔跑（TTL 归档 + 超龄已平仓清理，状态落 `data/shared/memory_gc.json`）。

**看缓存成本**：`data/bots/<id>/state/cache_stats.jsonl` 每轮一行（`hit`/`total`/`hit_rate`/`model`）；前缀稳定性摘要落 `state/cache_prefix.sha256`，变了会打 `memory cache prefix changed` 警告。**注意 `total` 是本轮累计**（工具循环里每次 LLM 调用都计入）—— 实测 **70–331k token/轮**、命中率约 **0.04–0.75**，远超设计时的 ~4k 预算，大头是工具返回的行情数据；做成本估算别按 4k 算。**没有这个文件说明没在记**。

**信号广播**：一信号 → 多所，目标在 `config/broadcast.yaml`（AI 碰不到）。`python -m omnialpha broadcast` 常驻分发；可选 1/2/N 个目标；逐个校验写入、失败报错；目标 bot 各自独立执行。信号内 `targets` 字段忽略（防 AI 注入）。

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

### 8.1 环境区分（本地 vs 服务器）

**同一份代码 + overlay 声明差异**。完整流程见 `docs/DEPLOY.md`。

```
config/bots/        ① 基线（git 跟踪）— 全部 enabled: false
config/bots.local/  ② 环境覆盖（gitignore，每机一份）— 本机启用哪些 bot
config/alerts.yaml  ③ 密钥（gitignore）
```

合并：先读基线 → `bots.local/<同名>` **深度合并**覆盖（dict 递归 / list 替换 / `null` 删除键）。

**关键**：`bots.local/` 被 gitignore → `git pull` **永不冲突**，本机启用集天然保留。

```bash
# 部署（七步：检查→pull→依赖→skill→编码自检→测试→重启验证）
./scripts/deploy.sh [--no-restart|--dry-run]
python -m omnialpha deploy-check      # 部署后验证
```

### 8.2 平台差异

| 项 | Windows | Linux / 服务器 |
|----|---------|----------------|
| Python | `.venv\Scripts\python.exe` | `python3` 或 `.venv/bin/python` |
| 装依赖 | `pip install -r requirements.txt` | 同左（建议 venv） |
| 项目根 | 当前目录或 `--root` | `--root /opt/omnialpha` 或 **`OMNIALPHA_ROOT`** |
| 行情库 | 默认 `<root>/pa-data-source/data` | 可用 **`OMNIALPHA_PA_DATA`** 改到数据盘 |
| 密钥 | 终端 `$env:...` / 系统环境变量 | systemd `Environment=` 或 `.env` 由外部注入 |
| skill 打包 | `scripts/pack_skill.py`（UTF-8 安全） | 解压用 Python / `skill doctor --fix` 修乱码 |

> ⚠️ **编码陷阱**：Windows 打的 zip 在 Linux 用 `unzip` 解，UTF-8 文件名会被按 CP866
> 解读 → 乱码。用 `scripts/pack_skill.py` 打包 + `python -m omnialpha skill doctor --fix` 自检。

```bash
# Linux 服务器示例
export OMNIALPHA_ROOT=/opt/omnialpha
export GATE_API_KEY=...
export GATE_API_SECRET=...
export OMNIALPHA_PA_DATA=/data/gate-kline   # 可选

cd /opt/omnialpha
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
pip install -e .          # 任意目录都能 python -m omnialpha
python -m omnialpha status --root "$OMNIALPHA_ROOT"
python -m omnialpha run --root "$OMNIALPHA_ROOT"
```

systemd 建议：`WorkingDirectory=/opt/omnialpha`，并设 `OMNIALPHA_ROOT` 与密钥环境变量；或 `ExecStart=/opt/omnialpha/.venv/bin/python -m omnialpha run --root /opt/omnialpha`。

**相对路径规则**（代码已按此实现）：
- 配置：`<root>/config/bots/`
- 信号：`<root>/inbox/<bot_id>/`
- 策略人格：`<root>/prompts/`（`prompt_file` 不依赖 cwd）
- 行情：`OMNIALPHA_PA_DATA` 或 yaml `pa_data_root` 或 `<root>/pa-data-source/data`

自检：**在任意 cwd 下**执行 `python -m omnialpha --root /path/to/repo status` 应正常。
