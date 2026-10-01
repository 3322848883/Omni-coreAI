# Account-Watcher 数据库结构

> 外部行情/账户数据源（可选）。根路径通过 `PRICE_ACTION_DATA_DIR`（K 线 JSON）及同级项目配置指定，不写死个人目录。

## 目录结构

```
account-watcher/
├── data/
│   ├── account.db          # 账户数据 (SQLite)
│   ├── kline.db            # K线数据 (SQLite)
│   ├── eth_usdt_1m.json    # ETH 1分钟K线
│   ├── eth_usdt_5m.json    # ETH 5分钟K线
│   ├── eth_usdt_15m.json   # ETH 15分钟K线
│   ├── eth_usdt_1h.json    # ETH 1小时K线
│   ├── eth_usdt_4h.json    # ETH 4小时K线
│   ├── btc_usdt_*.json     # BTC 各周期
│   ├── sol_usdt_*.json     # SOL 各周期
│   ├── xau_usdt_*.json     # 黄金各周期
│   ├── xag_usdt_*.json     # 白银各周期
│   └── positions.json      # 当前持仓
├── kline_watcher.py        # K线监控
├── watchdog.py             # 主程序
└── watchlist.yaml          # 监控列表
```

## JSON K线数据格式

每个 JSON 文件是一个数组，每个元素是一根K线：

```json
{
  "t": 1780344000,           // Unix 时间戳 (秒)
  "o": "2003.80",            // 开盘价 (字符串)
  "h": "2008.56",            // 最高价
  "l": "1991.87",            // 最低价
  "c": "1994.41",            // 收盘价
  "v": "11807836.00",        // 成交量
  "sum": "236138431.33914",  // 成交额
  "ema20": "1994.53923728",  // 20周期EMA
  "atr14": "16.69267325"     // 14周期ATR
}
```

**注意:** 价格和成交量是字符串类型，需要 `float()` 转换。

## 数据量参考

| 周期 | 典型条数 | 覆盖时间 |
|------|---------|---------|
| 1m   | ~1440   | ~1天    |
| 5m   | ~288    | ~1天    |
| 15m  | ~96     | ~1天    |
| 1h   | ~720    | ~30天   |
| 4h   | ~180    | ~30天   |

## 时间戳转换

```python
import datetime
def ts_to_str(ts):
    return datetime.datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M')
```

## 已知品种

- ETH/USDT, BTC/USDT, SOL/USDT (加密货币)
- XAU/USDT, XAG/USDT (贵金属)

## 运行注意事项

- 使用 Python 3.11 (uv)，不是系统 python
- 健康检查端口 18080
- 启动: `cd "C:/Users/w6485/Desktop/account-watcher"; python watchdog.py`
- 多实例会导致 JSON 文件锁冲突
- 停止时用 Ctrl+C 优雅退出
