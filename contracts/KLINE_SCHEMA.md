# kline.db 契约（节点1 → 节点2/3 唯一接缝）

**schema_version: 1**  
约定方：`pa-data-source`（写） / `gate-signal-bot.strategist.market`（只读）

## 原则

- bot **只读**，不写、不迁移、不清理 pa 的库
- 文件路径：`pa-data-source/data/kline.db`（live） / `kline_testnet.db`（testnet）
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
