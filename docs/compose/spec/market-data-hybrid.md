---
feature: market-data-hybrid
status: delivered
updated: 2026-08-31
branch: feat/market-hybrid
commits: bd2cf1f..9fc2422
---

# Market Data Hybrid — 行情混合接入

## Report

**What was built** — strategist 快照按数据特性分源：`hybrid|rest_only|local_only` 三态读 pa-data-source `kline.db`（只读），指标 ema20/50、atr14、rsi14 在缺口处从末次库值 warm-start（不重算已知库值）；`last`/持仓/余额强制实时 API，account 不完整则本轮 abort 不写 inbox；testnet 自动切 `kline_testnet.db`；stale（末根年龄 > `stale_factor×周期`）自动 REST 降级并写入 `meta.degraded`。默认 `rest_only` 保持零依赖旧行为。

**Verification** — `python -m unittest discover -s tests`：**86 PASS**（新增 indicators warm-start/空值、market 三 mode/降级/ms 时间戳、snapshot meta、account abort 且 inbox 为空）。独立复审第一轮 FAIL（warm-start 与 account 边界），修复后复审 **PASS**，无新增 critical。

**Journey log** —
1. 复审误报「EMA 应用 Wilder 1/n」——对照 pa `kline_watcher.compute_ema` 的 `k=2/(n+1)` 判定公式正确，仅收紧 docstring。
2. 递归指标不能在窗口内独立重算填缺口，必须从末次库值续算（warm-start）。
3. `get_positions` 失败不应抹掉已取到的 `available`，但仍应 abort 规划。
4. 门禁用 epoch 秒判断时要兼容 ms 时间戳，否则永远不 stale。

## [S1] Problem

strategist 快照目前每轮只做 REST 现拉：`last` + 60 根 K + 账户。没有 EMA/ATR/RSI，历史浅，LLM 缺结构信息。pa-data-source 已在同机维护 `kline.db`（WS/REST 管道、2000 根、EMA20/ATR14 同源、实测实时），但 bot 完全用不上。需要按数据特性分源：**深历史+指标读本地库，决策价与账户强制实时**，并在库缺失/过期时自动降级，不改 pa-data-source 一行代码。

## [S2] Design

### 分源契约

| 字段 | 主源 | 门禁 | 降级 |
|------|------|------|------|
| `candles` | `kline.db`（hybrid/local_only） | 末根 `t` 距今 ≤ `stale_factor × interval` | hybrid→REST candles；local_only→仍用本地并标 `stale` |
| `ema20` / `atr14` | 库列（local 且非空） | 随 candle | 缺口从末次库值 warm-start；**不重算已有库值** |
| `ema50` / `rsi14` | 本地算（库无此列） | 输入长度足够 | 不足则该指标为 `null` |
| `last` | **实时 REST** ticker | — | 跳过该字段并记 `degraded` |
| `positions` / `available` / `position_mode` | **实时私有 API** | — | **abort 本轮**，不调 LLM、不写 inbox |
| 下单报价 | executor `get_last_price` | — | 不变；禁止用库价 |

`mode`：

- `rest_only`（默认）— 与现状一致，零外部依赖；仍附加本地算指标（若有足够 K）。
- `hybrid` — 本地优先，stale/缺失/读失败 → REST，指标按上表。
- `local_only` — 只读库，不 REST K 线；stale 仍交付并标记。

### 配置

```yaml
strategist:
  candles: 120
  market:
    mode: hybrid                 # hybrid | rest_only | local_only
    pa_data_root: ../pa-data-source-v2.11/data   # 或 env GATE_BOT_PA_DATA
    db: kline.db                 # testnet 自动改 kline_testnet.db
    stale_factor: 2.0
    health_url: null             # 可选 http://127.0.0.1:18080/health；非 200 视本地不可信
    indicators: [ema20, ema50, atr14, rsi14]
```

- 路径解析顺序：`GATE_BOT_PA_DATA` → `market.pa_data_root` → `../pa-data-source-v2.11/data`（相对 bot root）。
- bot `env: testnet` ⇒ `kline_testnet.db`；`live` ⇒ `kline.db`。**绝不跨库。**
- bot **只读** pa 库，不写、不迁移。

### 模块

```text
strategist/market.py
  MarketConfig
  resolve_db_path(env, market_cfg) -> Path
  load_local_candles(db_path, symbol, interval, limit) -> list[dict] | None
  fetch_rest_candles(client, symbol, interval, limit) -> list[dict]
  is_stale(rows, interval, stale_factor, now=None) -> bool
  resolve_candles(client, symbol, interval, limit, market_cfg, env) -> CandleResult

strategist/indicators.py
  ema(closes, period) -> list[float|None]
  rsi(closes, period=14) -> list[float|None]
  atr(highs, lows, closes, period=14) -> list[float|None]
  attach_indicators(rows, wanted) -> list[dict]   # 原地/返回带指标行

strategist/snapshot.py
  collect_snapshot(client, symbols, candles, interval, market_cfg=None, env="live")
  # 行结构: t,o,h,l,c,v[,sum][,ema20][,ema50][,atr14][,rsi14]
  # 顶层 meta: {market_mode, candle_source[sym], degraded[], stale[]}
  # account 失败 → {"account": {"error": ...}, ...}；由 PlanRunner 判定 abort
```

`CandleResult`：`{rows, source: "local"|"exchange", stale: bool, degraded: list[str]}`。

### PlanRunner 行为

1. `collect_snapshot(...)`  
2. 若 `snapshot.account` 含 `error` 且无 `available` → `return {ok: False, error: "account_unavailable", cycle_id}`（**不调 LLM**）  
3. 其余照旧：prompt → LLM → risk → inbox  

`StrategistConfig` 增加 `market: MarketConfig`；`__main__._build_plan_runner` 从 `strategist.market` 装配。

### 行级指标写入

对每根 candle dict 追加所选指标的**当根值**（对齐 pa 的逐根 EMA/ATR 口径）；另在 `market[sym].indicators` 给出**最新一根**摘要，便于 prompt 短读。

### 错误与降级标记

| 情况 | 行为 |
|------|------|
| 库文件不存在 / 读异常 | `degraded+= "local_db"`，hybrid→REST |
| 末根过期 | `stale=True`，hybrid→REST，`degraded+= "candles_stale"` |
| REST K 失败且无本地 | 该 symbol `candles_error` |
| `health_url` 非 200（仅 hybrid/local_only 且配置了） | 视本地不可信，hybrid→REST |
| last 失败 | `degraded+= "<sym>:last"`，继续 |
| account 失败 | abort 本轮 |

### 测试边界

- `indicators`：纯函数，已知序列对照手算/固定夹具。  
- `market`：临时 SQLite 建表插行，覆盖 fresh/local、stale→REST、缺库→REST、local_only 不 REST。  
- `snapshot`：mock `GateClient`，断言 meta/degraded 与 account abort 信号。  
- `rest_only` 回归：不触库也能出 candles+指标（本地算）。  

## [S3] Out of Scope

- funding_rate / orderbook / contract_stats / trades 等 refresh 块（P1）  
- aux 舆情旁路（P2）  
- 写入/迁移 pa 的 `kline.db`、`account.db`  
- 多周期同时进快照  
- 用 `account.db` 替代实时账户  
- trail 追踪单（继续搁置）  
- pa-data-source 侧任何改动  

## Tasks

- [x] T1: `strategist/indicators.py` — ema/rsi/atr + attach — acceptance: 已知序列单测通过 (covers: S2)
- [x] T2: `strategist/market.py` — MarketConfig/读库/REST/stale/resolve — acceptance: mock SQLite+client 覆盖三 mode 与降级 (covers: S2; depends: T1)
- [x] T3: `strategist/snapshot.py` hybrid 接入 — acceptance: meta.degraded/stale 与 indicator 字段存在 (covers: S2; depends: T2)
- [x] T4: `StrategistConfig.market` + `__main__` 装配 + account abort — acceptance: account 失败不写 inbox；配置可加载 (covers: S2; depends: T3)
- [x] T5: README「数据来源」+ 示例 yaml + 全量单测 — acceptance: `python -m unittest discover -s tests` 全绿 (covers: S2; depends: T1, T2, T3, T4)
