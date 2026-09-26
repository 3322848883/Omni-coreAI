---
feature: multi-exchange
status: designed
updated: 2026-10-20
branch: feat/multi-exchange
commits: 
---

# 六交易所行情+交易全能力（Binance / OKX / Bybit / Gate / Bitget / Hyperliquid）

## Report

## [S1] Problem

系统目前只对接 **Gate**（`gate_client.py` 写死 `FUTURES_API=/api/v4/futures/usdt`）。  
用户需要把**本地数据源 + REST + 交易**拓展到 6 个热门交易所，且：

- 行情：k 线 / 指标 / 盘口 / 资金费 / 合约元数据  
- 交易：开平仓、限价/市价、TP/SL、账户、杠杆  
- 本地库：**每所一库**（与现有 `kline.db` 同构、可独立采集）  
- bot 可按配置选择交易所，不在代码里绑死 Gate

## [S2] Design

### 2.1 交易所清单

| id | 名称 | 类型 |
|----|------|------|
| `gate` | Gate.io | 兼容现状（默认） |
| `binance` | Binance USDT-M 永续 | 新 |
| `okx` | OKX 永续 | 新 |
| `bybit` | Bybit 永续 | 新 |
| `bitget` | Bitget USDT 永续 | 新 |
| `hyperliquid` | Hyperliquid | 新 |

### 2.2 架构：交易所适配层

```text
gate_bot/
  exchanges/
    __init__.py      ExchangeClient 工厂 create_exchange(name, env, keys)
    base.py          抽象基类（市场+交易契约）
    gate.py          包装现有 GateClient
    binance.py okx.py bybit.py bitget.py hyperliquid.py
  gate_client.py     保留；Gate 实现内部复用
```

**统一契约**（`base.ExchangeClient`）：

| 方法 | 说明 | 归一字段 |
|------|------|----------|
| `get_last_price(symbol)` | 最新价 | float |
| `get_ticker(symbol)` | ticker | last/mark/funding/high/low/… |
| `get_klines(symbol, interval, limit)` | K 线 | `[{t,o,h,l,c,v,sum}]` |
| `get_orderbook_top(symbol, limit)` | 盘口 | bids/asks |
| `get_contract(symbol)` | 合约元数据 | quanto/order_round/leverage_max |
| `get_account()` | 账户 | available/total |
| `get_positions()` | 持仓 | symbol/size/entry/leverage… |
| `place_order(body)` / `cancel_*` | 交易 | 统一 order 字段 |
| `place_price_order` / `cancel_price_order` | 触发单 | TP/SL |
| `set_leverage` / `set_margin_mode` | 杠杆 | |

**符号归一**：内部 `BTC_USDT`；各所映射（Binance `BTCUSDT`、OKX `BTC-USDT-SWAP`、Bybit `BTCUSDT`、Hyperliquid `BTC`…）。  
**下单 body 归一**：`{action,size,size_usd,price,type,tp,sl,leverage}` → 所内 API。

### 2.3 配置

```yaml
# config/bots/<id>.yaml
exchange: binance          # 默认 gate，可选 6 所
env: live|testnet
api_key_env: BINANCE_API_KEY
api_secret_env: BINANCE_API_SECRET
```

```yaml
# config/exchanges.yaml（可选预设）
binance:
  base_url: https://fapi.binance.com
  testnet_url: https://testnet.binancefuture.com
  symbol_map: {BTC_USDT: BTCUSDT}
```

### 2.4 本地数据源（pa-data-source）

- **每所一库**：`kline_<exchange>.db`（live） / `kline_<exchange>_testnet.db`  
- schema 与 `kline.db` 相同（`contracts/KLINE_SCHEMA.md` v1 + `exchange` 由文件名区分）  
- `kline_watcher` 增加 `--exchange binance` 读取该所 WS/REST  
- bot 侧 `market.resolve_db_path` 按 `exchange` 选库；缺库降级 REST

### 2.5 工具层

现有 `klines/ticker/account/...` **不变**，底层改走 `ExchangeClient`；自动适配六所。

### 2.6 错误与降级

- 所级 API 差异映射到统一 `ExchangeError`  
- 单所失败不影响其它 bot  
- 不支持的能力（如 Hyperliquid 无传统 trigger）显式返回 `unsupported`

## [S3] Out of Scope

- 股票/外汇  
- 跨所套利路由  
- UI/网页管理  
- 现货（仅永续/合约）

## Tasks

- [ ] T1: `exchanges/base.py` 契约 + `create_exchange` 工厂 — acceptance: 单测假实现注册 6 所 (covers: S2.2)
- [ ] T2: `binance.py` 公共行情（ticker/klines/book/contract） — acceptance: testnet/主网抽样字段正确 (covers: S2.2)
- [ ] T3: `okx.py` 公共行情 — acceptance: 同上 (covers: S2.2)
- [ ] T4: `bybit.py` 公共行情 — acceptance: 同上 (covers: S2.2)
- [ ] T5: `bitget.py` 公共行情 — acceptance: 同上 (covers: S2.2)
- [ ] T6: `hyperliquid.py` 公共行情 — acceptance: 同上 (covers: S2.2)
- [ ] T7: 六所**下单/撤单/TP-SL/账户/杠杆**统一契约 — acceptance: mock+单测字段映射 (covers: S2.2)
- [ ] T8: bot 配置 `exchange` + 符号映射 — acceptance: binance bot 能 plan 下单路径 (covers: S2.3)
- [ ] T9: pa-data-source 多库（`kline_<ex>.db`）— acceptance: 三所 watcher 写库 schema 一致 (covers: S2.4)
- [ ] T10: 工具层/快照走 ExchangeClient — acceptance: 六所抽样 tools 返回正常 (covers: S2.5)
- [ ] T11: 回归 unittest + 三所 testnet 冒烟 — acceptance: 173+ 全绿 (covers: S2.6; depends: T2–T10)
