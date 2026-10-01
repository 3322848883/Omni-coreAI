# 完整分析工作流（Complete Analysis Workflow）

> 本文档记录从数据加载到报告生成的完整工作流。
> 适用于：用户要求"分析行情"、"使用完整技能"、"26步分析"等场景。

---

## 工作流概览

```
1. 加载数据（execute_code）
   ↓
2. 分析市场状态（多时间框架）
   ↓
3. 读取记忆文件（memory/*.md）
   ↓
4. 按26步模板生成报告
   ↓
5. 保存报告到文件
```

## 步骤1：加载数据

使用 `execute_code` 加载 account-watcher JSON 数据：

```python
import json
import os
from pathlib import Path

skill_dir = Path(os.environ.get("PRICE_ACTION_SKILL_ROOT", Path(__file__).resolve().parents[1]))
data_dir = Path(os.environ.get("PRICE_ACTION_DATA_DIR", skill_dir / "data"))

def load_kline_data(symbol, timeframe, limit=200):
    file_path = data_dir / f"{symbol}_usdt_{timeframe}.json"
    if not file_path.exists():
        return []
    with open(file_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    return data[-limit:]

# 加载三个时间框架
btc_4h = load_kline_data("btc", "4h", 200)
btc_1h = load_kline_data("btc", "1h", 200)
btc_5m = load_kline_data("btc", "5m", 200)
```

## 步骤2：分析市场状态

对每个时间框架计算：
- 收盘>EMA 比例
- AIS/AIL 判断
- HH/LL 计数
- 日内波幅和波动率

```python
def analyze_market_state(bars, timeframe):
    latest = bars[-1]
    close = float(latest['c'])
    ema20 = float(latest['ema20']) if latest['ema20'] else None
    
    # 计算收盘>EMA比例
    valid_bars = [b for b in bars[-20:] if b['ema20']]
    above_ema = sum(1 for b in valid_bars if float(b['c']) > float(b['ema20']))
    ratio = above_ema / len(valid_bars) if valid_bars else 0
    
    # AIS/AIL判断
    if ratio > 0.6:
        ai_state = "AIL"
    elif ratio < 0.4:
        ai_state = "AIS"
    else:
        ai_state = "不确定"
    
    return {
        'close': close,
        'ema20': ema20,
        'ai_state': ai_state,
        'above_ema_count': above_ema,
        'total_count': len(valid_bars),
    }
```

## 步骤3：读取记忆文件

```python
memory_dir = skill_dir / "memory"

def load_memory_file(file_name):
    file_path = memory_dir / file_name
    if file_path.exists():
        with open(file_path, 'r', encoding='utf-8') as f:
            return f.read()
    return ""

error_patterns = load_memory_file("error_patterns.md")
pattern_effectiveness = load_memory_file("pattern_effectiveness.md")
trader_profile = load_memory_file("trader_profile.md")
market_wisdom = load_memory_file("market_wisdom.md")
strategy_hypotheses = load_memory_file("strategy_hypotheses.md")
```

## 步骤4：生成报告

参考模板 `assets/templates/price_action_analysis.md`，按26步结构生成报告。

关键部分：
1. **阶段零**：盘前准备（记忆检索、自我评估、心理准备）
2. **阶段一**：市场分析（多时间框架、Q1-Q7决策树）
3. **阶段二**：开盘专项（逐棒分析）
4. **阶段三**：信号识别（建仓形态、信号棒评估、交易者方程）
5. **阶段四**：交易执行（止损校验、入场方式、仓位计算）
6. **阶段五**：管理（追踪止损、目标管理、仓位缩放）
7. **阶段六**：失败处理（止损触发后、失败突破分类）
8. **阶段七**：再决策（平仓后评估、再决策分支）

## 步骤5：保存报告

```python
from datetime import datetime, timezone

output_dir = Path(os.environ.get("PRICE_ACTION_OUTPUT_DIR", skill_dir / "logs" / "reports"))
output_dir.mkdir(exist_ok=True)

now_utc = datetime.now(timezone.utc)
output_file = output_dir / f"{symbol}_analysis_{now_utc.strftime('%Y%m%d')}.md"

with open(output_file, 'w', encoding='utf-8') as f:
    f.write('\n'.join(report))

print(f"✓ 报告已保存到: {output_file}")
```

## 自动化脚本

完整的自动化脚本位于：
- `scripts/analyze_market_state.py` — 市场状态快速扫描
- `scripts/generate_analysis.py` — 26步分析报告生成器

使用示例：
```bash
python scripts/analyze_market_state.py btc 4h,1h,5m
python scripts/generate_analysis.py btc
```

## 注意事项

1. **数据加载用 execute_code**：不要用 read_file（可能失败）
2. **200 根 K 线**：所有时间框架统一加载 200 根
3. **手续费 0.1%**：必须在交易者方程中扣除
4. **SB/CT 逐条检查**：Step 3.2 必须逐条列出，不可一句话带过
5. **引用清单表格**：报告末尾必须输出 Step→workflow→strategy_workflow 映射表
