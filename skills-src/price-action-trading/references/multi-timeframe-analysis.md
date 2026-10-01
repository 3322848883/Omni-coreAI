# Multi-Timeframe Analysis with account-watcher Data

## Data Loading Pattern

```python
import os, json
from pathlib import Path
from datetime import datetime

skill_dir = Path(os.environ.get("PRICE_ACTION_SKILL_ROOT", Path(__file__).resolve().parents[1]))
data_dir = Path(os.environ.get("PRICE_ACTION_DATA_DIR", skill_dir / "data"))

def load_data(symbol, timeframe):
    path = data_dir / f"{symbol}_{timeframe}.json"
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

# Data fields: t (timestamp), o, h, l, c, v, ema20, atr14
eth_5m = load_data("eth_usdt", "5m")
```

## Analysis Pipeline

1. **Load 4h/1h/15m/5m data** (20-50 bars each)
2. **Identify swing highs/lows** for trend structure
3. **Check Always In direction** (price vs EMA20 on each timeframe)
4. **Classify market state** (trend/range) per timeframe
5. **Identify setups** (pullback to EMA, breakout, reversal)
6. **Apply traders equation** for valid setups
7. **Output trade plan** with entry/stop/target

## Saving Analysis to Wiki

Save trade analysis to Obsidian wiki `queries/` folder:
- Filename: `<symbol>-analysis-YYYY-MM-DD.md`
- Include: market state, key levels, setups, trade plan
- Add wikilinks to relevant concept pages

## Key Timeframes for Crypto (UTC)

| Window | Event | Action |
|--------|-------|--------|
| 08:00 | Europe open | Best window |
| 12:00-14:00 | EU/US overlap | Best window |
| 14:30 | US data | Avoid |
| 20:00 | US close | Reduce new positions |
