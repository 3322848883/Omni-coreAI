# Efficient BTC Analysis Pattern (Single execute_code Call)

> **Purpose**: Reference for how to run a full 26-step price action analysis in ONE execute_code call.
> **Standard**: Match `assets/examples/01-btc-20260602.md` format exactly.
> **Target time**: <1 second total.

## Architecture

```
execute_code (single call)
├── 1. Load K-line data (200 bars × 3 timeframes)
├── 2. Load knowledge files (strategy_workflow key chapters)
├── 3. Load memory files (5 files)
├── 4. Compute everything (Q1-Q7, SB/CT, stop loss, trader's equation)
└── 5. Print complete 26-step analysis
```

## Key Imports and Helpers

```python
import json, re
from pathlib import Path
from datetime import datetime, timezone, timedelta

import os
from pathlib import Path
skill_dir = Path(os.environ.get("PRICE_ACTION_SKILL_ROOT", Path(__file__).resolve().parents[1]))
data_dir = Path(os.environ.get("PRICE_ACTION_DATA_DIR", skill_dir / "data"))

def load_json(path):
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)

def load_text(path):
    with open(path, 'r', encoding='utf-8') as f:
        return f.read()

def fv(bar, key):
    v = bar.get(key)
    return float(v) if v is not None else None

def extract_chapter(text, chapter_num):
    pattern = rf"## 第{chapter_num}章.*?(?=## 第\d+章|## 附录|\Z)"
    match = re.search(pattern, text, re.DOTALL)
    return match.group(0) if match else ""
```

## Data Loading (200 bars per timeframe)

```python
btc_4h = load_json(data_dir / "btc_usdt_4h.json")[-200:]
btc_1h = load_json(data_dir / "btc_usdt_1h.json")[-200:]
btc_5m = load_json(data_dir / "btc_usdt_5m.json")[-200:]

strategy_wf = load_text(skill_dir / "knowledge" / "strategy_workflow.md")
workflow = load_text(skill_dir / "knowledge" / "workflow.md")

memory_dir = skill_dir / "memory"
# Load all 5 memory files
error_patterns = load_text(memory_dir / "error_patterns.md")
pattern_effectiveness = load_text(memory_dir / "pattern_effectiveness.md")
trader_profile = load_text(memory_dir / "trader_profile.md")
market_wisdom = load_text(memory_dir / "market_wisdom.md")
strategy_hypotheses = load_text(memory_dir / "strategy_hypotheses.md")

# Extract key chapters from strategy_workflow
ch2 = extract_chapter(strategy_wf, 2)   # Decision tree
ch6 = extract_chapter(strategy_wf, 6)   # Entry signals
ch7 = extract_chapter(strategy_wf, 7)   # Stop loss
ch11 = extract_chapter(strategy_wf, 11) # Trader's equation
ch12 = extract_chapter(strategy_wf, 12) # Prohibited list
```

## Timeframe Analysis Function

```python
def analyze_tf(data):
    valid = [b for b in data if fv(b, 'ema20') is not None]
    latest = valid[-1]
    close, ema20, atr = fv(latest,'c'), fv(latest,'ema20'), fv(latest,'atr14')
    above_ema = sum(1 for b in valid if fv(b,'c') > fv(b,'ema20'))
    ema_vals = [fv(b,'ema20') for b in valid[-5:] if fv(b,'ema20')]
    ema_change = ema_vals[-1] - ema_vals[0] if len(ema_vals)>=2 else 0
    highs = [fv(b,'h') for b in valid[-20:]]
    lows = [fv(b,'l') for b in valid[-20:]]
    hh = sum(1 for i in range(1,len(highs)) if highs[i]>highs[i-1])
    ll = sum(1 for i in range(1,len(lows)) if lows[i]<lows[i-1])
    lh = sum(1 for i in range(1,len(highs)) if highs[i]<highs[i-1])
    hl = sum(1 for i in range(1,len(lows)) if lows[i]>lows[i-1])
    day_range = max(highs) - min(lows)
    return {
        'close':close,'ema20':ema20,'atr':atr,
        'above_ema':above_ema,'total':len(valid),
        'pct_above':above_ema/len(valid)*100,
        'ema_change':ema_change,'hh':hh,'ll':ll,'lh':lh,'hl':hl,
        'day_range':day_range
    }
```

## Output Format (match assets/examples/ exactly)

The output must match `assets/examples/01-btc-20260602.md` format:
- Compact, no verbose explanations
- Decision tree: `Q1: → Q2: → 第X章` inline format
- SB/CT: 6 rows each, one-line format
- Step 4.1/4.2/5/6: filled even when "不交易"
- Core conclusion: one sentence
- Reference table: merged format (multiple Steps per row)

## Pitfalls

1. **Never print intermediate results** — compute everything, then print once
2. **Never use multiple execute_code calls** for one analysis
3. **Always read actual knowledge files** — don't rely on memory
4. **Always reference assets/examples/ format** before generating output
