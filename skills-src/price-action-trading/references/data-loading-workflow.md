# 数据加载工作流（Data Loading Workflow）

> 本文档记录从外部行情 JSON 文件加载 K 线数据的标准流程。
> 数据来源：环境变量 `PRICE_ACTION_DATA_DIR`（默认 `<skill>/data`）。

---

## JSON 文件格式

每个文件包含一个 JSON 数组，每个元素是一根 K 线：

```json
{
  "t": 1777852800,        // Unix 时间戳（秒）
  "v": 251971142.00,      // 成交量
  "c": 80258.70,          // 收盘价（字符串格式，需转 float）
  "h": 80435.50,          // 最高价
  "l": 78216.30,          // 最低价
  "o": 78536.20,          // 开盘价
  "sum": 2005569068.09,   // 成交额
  "ema20": null,          // EMA20（早期棒可能为 null）
  "atr14": null           // ATR14（早期棒可能为 null）
}
```

## 可用文件

| 文件 | 时间框架 | 典型数据量 |
|------|----------|-----------|
| `btc_usdt_4h.json` | 4小时 | ~180条 |
| `btc_usdt_1h.json` | 1小时 | ~200条 |
| `btc_usdt_5m.json` | 5分钟 | ~2000条 |
| `btc_usdt_15m.json` | 15分钟 | ~2000条 |
| `btc_usdt_1m.json` | 1分钟 | ~2000条 |
| `eth_usdt_*.json` | 同上 | 同上 |
| `sol_usdt_*.json` | 同上 | 同上 |

## 标准加载代码

```python
import json
import os
from pathlib import Path
from datetime import datetime, timezone

skill_dir = Path(os.environ.get("PRICE_ACTION_SKILL_ROOT", Path(__file__).resolve().parents[1]))
data_dir = Path(os.environ.get("PRICE_ACTION_DATA_DIR", skill_dir / "data"))

def load_kline_data(symbol, timeframe, limit=200):
    """加载指定品种和时间框架的K线数据"""
    file_path = data_dir / f"{symbol}_usdt_{timeframe}.json"
    if not file_path.exists():
        return []
    
    with open(file_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    # 只取最后 limit 条数据
    if len(data) > limit:
        data = data[-limit:]
    
    return data

def parse_bar(bar):
    """解析单根K线，返回数值类型"""
    return {
        'timestamp': bar['t'],
        'datetime': datetime.fromtimestamp(bar['t'], tz=timezone.utc),
        'open': float(bar['o']),
        'high': float(bar['h']),
        'low': float(bar['l']),
        'close': float(bar['c']),
        'volume': float(bar['v']),
        'ema20': float(bar['ema20']) if bar['ema20'] else None,
        'atr14': float(bar['atr14']) if bar['atr14'] else None,
    }

# 使用示例
btc_5m = load_kline_data("btc", "5m", 200)
latest = parse_bar(btc_5m[-1])
```

## 注意事项

1. **EMA20 为 None**：早期 K 线的 EMA20 可能为 null，需要跳过或检查
2. **价格是字符串**：JSON 中的价格字段是字符串格式，需要 `float()` 转换
3. **时间戳是秒**：不是毫秒，直接用 `datetime.fromtimestamp()`
4. **200 根标准**：分析统一使用 200 根 K 线（4H 覆盖 33 天，1H 覆盖 8.3 天，5M 覆盖 16.7 小时）

## 市场状态快速判断

```python
def quick_market_state(bars):
    """快速判断市场状态"""
    latest = bars[-1]
    close = float(latest['c'])
    ema20 = float(latest['ema20']) if latest['ema20'] else None
    
    if not ema20:
        return "未知", "EMA20 数据不足"
    
    # 计算最近20根收盘>EMA的比例
    valid_bars = [b for b in bars[-20:] if b['ema20']]
    above_ema = sum(1 for b in valid_bars if float(b['c']) > float(b['ema20']))
    ratio = above_ema / len(valid_bars) if valid_bars else 0
    
    if ratio > 0.6:
        return "AIL", f"{above_ema}/{len(valid_bars)} 在 EMA 上方"
    elif ratio < 0.4:
        return "AIS", f"{above_ema}/{len(valid_bars)} 在 EMA 上方"
    else:
        return "不确定", f"{above_ema}/{len(valid_bars)} 在 EMA 上方"
```
