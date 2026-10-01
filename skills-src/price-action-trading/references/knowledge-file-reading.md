# Knowledge File Reading Patterns

> 如何正确读取 price-action-trading 技能的知识文件。避免空壳分析。

## 文件路径

技能根目录：`PRICE_ACTION_SKILL_ROOT`，默认为 references/SOUL.md / SKILL.md 所在目录（禁止使用已失效的个人绝对路径）。

### 核心文件

| 文件 | 路径 | 大小 | 用途 |
|------|------|------|------|
| strategy_workflow.md | `references/knowledge/strategy_workflow.md` | ~108KB | 23章决策树+133条规则 |
| workflow.md | `references/knowledge/workflow.md` | ~278KB | 8阶段49步骤流程 |
| references/SOUL.md | `references/SOUL.md` | ~11KB | AI角色+流程图+映射表 |
| crypto-stop-loss-framework.md | `references/crypto-stop-loss-framework.md` | ~2KB | 加密货币止损速查 |

### 记忆文件

| 文件 | 路径 |
|------|------|
| error_patterns.md | `memory/error_patterns.md` |
| pattern_effectiveness.md | `memory/pattern_effectiveness.md` |
| trader_profile.md | `memory/trader_profile.md` |
| market_wisdom.md | `memory/market_wisdom.md` |
| strategy_hypotheses.md | `memory/strategy_hypotheses.md` |

## 推荐读取方式

**使用 execute_code + Python open()**（最可靠）：

```python
import os

import os
from pathlib import Path
skill_dir = os.environ.get("PRICE_ACTION_SKILL_ROOT") or str(Path(__file__).resolve().parents[1])

# 读取 strategy_workflow.md 特定章节
sw_path = os.path.join(skill_dir, "knowledge", "strategy_workflow.md")
with open(sw_path, 'r', encoding='utf-8') as f:
    content = f.read()

# 按章节标题定位
ch2_start = content.find("## 第2章：市场状态判断决策树")
ch2_end = content.find("## 第3章", ch2_start)
chapter2 = content[ch2_start:ch2_end]
print(chapter2)
```

### 按需读取的章节

| 分析步骤 | 需要读取的章节 | 关键内容 |
|----------|---------------|----------|
| Step 1 市场状态 | 第2章（决策树 Q1-Q7） | 趋势/区间/反转判断 |
| Step 3.1 建仓形态 | 第6章（入场信号优先级表） | P1-P21 优先级排序 |
| Step 3.2 信号棒 | 第6.5章（SB系列）+ 第6.6章（CT系列） | SB-01~20 + CT-01~13 |
| Step 4.0 止损 | 第7章（止损规则）+ crypto §5 | SL-01~10 + SA-01~08 |
| Step 4.1 入场 | 第17章（限价单规则） | 止损/限价/市价选择 |
| Step 5 方程 | 第11章（交易者方程式） | TE-01~05 + 概率数据 |
| Step 6 计划 | 第18章（10条核心规则） | 最终合规检查 |
| Step 3.9 禁止 | 第12章（禁止清单）+ 第16章（概率） | BAN-01~15 + P-01~76 |

## 常见错误

1. **用 read_file 读取 Windows 长路径**：可能返回 "File not found"。改用 execute_code + open()。
2. **一次性读取整个文件**：strategy_workflow.md 有 108KB，应按章节定位读取。
3. **只读不引用**：读取后必须在分析中引用具体规则 ID（T-001、SB-01 等）。
4. **记忆文件跳过**：Step 0 必须读取 memory/ 下 5 个文件，即使内容为空也要读取确认。

## 验证规则

分析完成后自查：
- [ ] strategy_workflow.md 至少读取了 3 个章节
- [ ] memory/ 至少读取了 5 个文件
- [ ] 全文中出现 ≥10 个具体规则 ID
- [ ] Q1-Q7 每条写了 ✓/✗ + 依据
- [ ] SB 系列至少检查 SB-01/02/06/08/10
- [ ] CT 系列至少检查 CT-03/05/07/09/13
