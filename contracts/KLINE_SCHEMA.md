# kline.db 契约（节点1 → 节点2/3 唯一接缝）

**schema_version: 1**  
约定方：`pa-data-source`（写） / `gate-signal-bot.strategist.market`（只读）

## 原则

- bot **只读**，不写、不迁移、不清理 pa 的库
- 文件路径：**每所一库**，schema 全部相同：

| 交易所 | live | testnet |
|--------|------|---------|
| gate | `pa-data-source/data/kline.db` | `kline_testnet.db` |
| binance | `kline_binance.db` | `kline_binance_testnet.db` |
| okx | `kline_okx.db` | `kline_okx_testnet.db` |
| bybit | `kline_bybit.db` | `kline_bybit_testnet.db` |
| bitget | `kline_bitget.db` | `kline_bitget_testnet.db` |
| hyperliquid | `kline_hyperliquid.db` | `kline_hyperliquid_testnet.db` |

  采集方：Gate → `kline_watcher.py`；其余所 → `kline_watcher_multi.py`（同一 schema / 同一 ema20·atr14 算法）

- **品种**（每所 5 个）：Gate = BTC/ETH/SOL/XAU/XAG；其余五所 = BTC/ETH/SOL/DOGE/XRP（XAU/XAG 为 Gate 独有金银合约，用 DOGE/XRP 补位）
- **周期**：六所统一 `1m / 5m / 15m / 1h / 4h / 1d`
- **任意币**：库仅存监控品种；任意已上线合约可随时经 REST/工具取（不限库），库空时 bot 自动降级 REST
- 列缺失或库不可读 → bot 自动降级 REST，**不硬读**
- 升级 schema 必须提升本文件的 `schema_version`，并同步 `validate_kline_schema()`

## 表 `kline`

| 列 | 类型 | 说明 |
|----|------|------|
| `t` | INTEGER | bar 开盘时间，**Unix 秒**（非 ms） |
| `symbol` | TEXT | 如 `BTC_USDT` |
| `interval` | TEXT | `1m/5m/15m/30m/1h/4h/1d` |
| `o,h,l,c` | TEXT | 开高低收（数值字符串） |
| `v` | TEXT | 成交量 |
| `sum` | TEXT | 成交额 |
| `ema20` | TEXT/NULL | EMA(k=2/(20+1))，与 pa `compute_ema` 一致 |
| `atr14` | TEXT/NULL | Wilder ATR(14) |

查询约定：

```sql
SELECT t,o,h,l,c,v,sum,ema20,atr14 FROM kline
WHERE symbol=? AND interval=? ORDER BY t DESC LIMIT ?;
```

## 新鲜度门禁

- bot：末根 `t` 距今 ≤ `stale_factor × interval`（默认 factor=2.0）
- 超龄 → hybrid 回退 REST；`local_only` 仍交付并标 `stale`

## 可选健康端点

`GET http://127.0.0.1:18080/health`（live） / `:18081`（testnet）  
非 200 时 bot 将本地库视为不可信（仅当配置了 `health_url`）

## 兼容性

| 变更 | 版本 |
|------|------|
| v1 初始：上述列 | 1 |
| 新增可空列 | 1.x（bot 忽略未知列） |
| 改名/删列/改 `t` 单位 | 2（必须同步 bot） |
