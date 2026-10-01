# Account-Watcher Data Source for Price Action Analysis

## Data Location

Configure via environment variable (do not hardcode personal paths):

```
PRICE_ACTION_DATA_DIR=<path-to-kline-json-dir>
```

Default: `<skill-root>/data`

## Available Files

### K-Line Data (JSON format)
| File | Timeframe | Records |
|------|-----------|---------|
| `eth_usdt_1m.json` | 1 minute | ~1000 |
| `eth_usdt_5m.json` | 5 minutes | ~500 |
| `eth_usdt_15m.json` | 15 minutes | ~500 |
| `eth_usdt_1h.json` | 1 hour | ~720 |
| `eth_usdt_4h.json` | 4 hours | ~180 |
| `btc_usdt_*.json` | All timeframes | Same structure |
| `sol_usdt_*.json` | All timeframes | Same structure |
| `xau_usdt_*.json` | Gold | Same structure |
| `xag_usdt_*.json` | Silver | Same structure |

### Other Files
- `account.db` - SQLite account database
- `kline.db` - SQLite K-line database
- `positions.json` - Current positions

## JSON Structure
```json
{
  "t": 1780344000,        // Unix timestamp
  "v": "11807836.00",     // Volume (string)
  "c": "1994.41",         // Close price (string)
  "h": "2008.56",         // High price (string)
  "l": "1991.87",         // Low price (string)
  "o": "2003.80",         // Open price (string)
  "sum": "236138431.33",  // Quote volume
  "ema20": "1994.539",    // EMA 20 (pre-calculated)
  "atr14": "16.6926"      // ATR 14 (pre-calculated)
}
```

## Quick Analysis Code Pattern
```python
import os
import json
import datetime
from pathlib import Path

data_dir = Path(os.environ.get("PRICE_ACTION_DATA_DIR", Path("data")))
with open(data_dir / "eth_usdt_4h.json", "r", encoding="utf-8") as f:
    eth_4h = json.load(f)

# Access latest bar
last = eth_4h[-1]
current_price = float(last['c'])
ema20 = float(last['ema20'])
atr14 = float(last['atr14'])

# Convert timestamp
time_str = datetime.datetime.fromtimestamp(last['t']).strftime('%Y-%m-%d %H:%M')
```

## Key Notes
- Prices are strings, must convert to float
- EMA20 and ATR14 are pre-calculated (saves computation)
- Data updates in real-time via kline_watcher.py
- Gate.io API is the data source
- Multi-symbol support (ETH, BTC, SOL, XAU, XAG)

## Usage with price-action-trading Skill
1. Load 4h data for trend/structure analysis
2. Load 1h data for entry timing
3. Use pre-calculated EMA20 for Always In direction
4. Use pre-calculated ATR14 for stop/target sizing
